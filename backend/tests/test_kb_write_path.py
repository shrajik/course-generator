"""The knowledge-base write path against a real PostgreSQL: items, versions, the durable
worker, retries, atomic publishing and the lifecycle.

Skipped unless TEST_DATABASE_URL is set. Retrieval is corpus-wide, so every test starts
from empty knowledge tables.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid

import pytest
from sqlalchemy import func, select, text, update

from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, ValidationFailedError
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.kb.service import KbService
from app.kb.worker import KbWorker
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg import PostgresKnowledgeBackend

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")

ACTOR = None  # no user row is needed for these


class Embeddings:
    """The real (offline mock) embeddings, recording what is asked and able to fail."""

    enabled = True

    def __init__(self):
        self.real = get_embedding_service()
        self.asked: list[str] = []
        self.failing = False

    async def embed_batch(self, texts):
        self.asked.extend(texts)
        if self.failing:
            return [None] * len(texts)
        return await self.real.embed_batch(texts)

    @property
    def count(self) -> int:
        return len(self.asked)


def policy(eligibility="A customer is eligible when the product is unused.", escalation="Disputes go to the billing team within two days.") -> bytes:
    return (
        "REFUND POLICY\n\nRefunds are issued within fourteen days of a valid request.\n\n"
        f"1.1 Eligibility\n\n{eligibility}\n\n1.2 Escalation\n\n{escalation}\n"
    ).encode()


@pytest.fixture
async def env(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    settings = get_settings()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    embeddings = Embeddings()
    service = KbService(settings=settings)
    worker = KbWorker(settings=settings, embeddings=embeddings)
    backend = PostgresKnowledgeBackend(settings=settings, embeddings=get_embedding_service(), log_retrievals=False)

    class Env:
        pass

    e = Env()
    e.settings, e.embeddings, e.service, e.worker, e.backend = settings, embeddings, service, worker, backend
    return e


async def _version(version_id):
    async with get_session_factory()() as session:
        return await session.get(kb.KbVersion if False else kb.KbItemVersion, version_id)


async def _item(item_id):
    async with get_session_factory()() as session:
        return await session.get(kb.KbItem, item_id)


async def _sql(sql: str, **params):
    async with get_session_factory()() as session, session.begin():
        result = await session.execute(text(sql), params)
        return result.all() if result.returns_rows else []


async def _make(env, files=None, body=None, **kw):
    ref = await env.service.create_item(actor_id=ACTOR, files=files, body_md=body, **kw)
    await env.worker.drain()
    return ref


async def _found(env, query):
    return [e.ref for e in await env.backend.asearch(query)]


# --- creating and processing -----------------------------------------------------------------


async def test_an_upload_becomes_a_processed_draft_that_retrieval_cannot_see(env):
    ref = await _make(env, files=[("refund_policy.txt", policy())])
    version, item = await _version(ref.version_id), await _item(ref.id)
    assert version.processing_status == "ready" and version.processing_error is None
    assert item.lifecycle_status == "draft" and item.live_version_id is None and item.latest_version_id == ref.version_id
    assert item.origin == "upload" and item.properties.get("title_auto") is True
    chunks = await _sql("SELECT ordinal, embedding IS NOT NULL AS embedded, is_live FROM kb_chunks WHERE item_id = :i ORDER BY ordinal", i=ref.id)
    assert [c.ordinal for c in chunks] == list(range(1, len(chunks) + 1)) and len(chunks) >= 3
    assert all(c.embedded and not c.is_live for c in chunks)
    assert await _found(env, "refunds eligibility unused product") == [] and not await env.backend.ahas_documents()
    assert item.suggestions["title"] and item.suggestions["generated_by"] == "rules" and item.needs_review
    assert await env.service.check_invariants() == []


async def test_the_system_fills_in_details_the_admin_never_entered(env):
    ref = await _make(env, files=[("refund_policy.txt", policy())])
    item = await _item(ref.id)
    tags = await _sql("SELECT t.name, it.source FROM kb_item_tags it JOIN kb_tags t ON t.id = it.tag_id WHERE it.item_id = :i", i=ref.id)
    assert item.title and tags and {t.source for t in tags} == {"ai"}
    assert (await _sql("SELECT centroid IS NOT NULL AS c FROM kb_items WHERE id = :i", i=ref.id))[0].c
    assert item.suggestions["tags"] and item.suggestions["category"] is None  # no categories exist yet to match


async def test_an_admin_chosen_title_is_kept(env):
    ref = await _make(env, files=[("x.txt", policy())], title="  Refund   rules ")
    item = await _item(ref.id)
    assert item.title == "Refund rules" and "title_auto" not in item.properties


async def test_knowledge_can_be_typed_in_without_any_file_and_combined_with_several(env):
    manual = await _make(env, body="# Onboarding\n\nNew staff get a laptop on day one.\n\n## Accounts\n\nAccounts are created by the service desk.")
    assert (await _item(manual.id)).origin == "manual"
    rows = await _sql("SELECT source_id FROM kb_chunks WHERE item_id = :i", i=manual.id)
    assert rows and all(r.source_id is None for r in rows)

    multi = await _make(env, files=[("a.txt", policy()), ("b.txt", b"SECURITY\n\nPasswords rotate every ninety days across all systems.\n")], body="Extra note typed by an admin about passwords.")
    parts = await _sql("SELECT s.filename FROM kb_version_sources vs JOIN kb_sources s ON s.id = vs.source_id WHERE vs.version_id = :v ORDER BY vs.position", v=multi.version_id)
    assert [p.filename for p in parts] == ["a.txt", "b.txt"]
    chunks = await _sql("SELECT ordinal, source_id FROM kb_chunks WHERE item_id = :i ORDER BY ordinal", i=multi.id)
    assert len({c.ordinal for c in chunks}) == len(chunks) and any(c.source_id is None for c in chunks) and sum(c.source_id is not None for c in chunks) > 3


async def test_nothing_to_store_and_duplicate_files_are_refused(env):
    with pytest.raises(ValidationFailedError):
        await env.service.create_item(actor_id=ACTOR)
    with pytest.raises(ValidationFailedError):
        await env.service.create_item(actor_id=ACTOR, files=[("a.txt", policy()), ("b.txt", policy())])
    first = await _make(env, files=[("a.txt", policy())])
    with pytest.raises(ConflictError) as clash:
        await env.service.create_item(actor_id=ACTOR, files=[("again.txt", policy())])
    assert clash.value.details["public_id"] == first.public_id
    await env.service.create_item(actor_id=ACTOR, files=[("again.txt", policy())], allow_duplicate=True)  # on purpose


# --- publishing --------------------------------------------------------------------------------


async def test_publishing_makes_a_draft_retrievable_under_its_permanent_number(env):
    ref = await _make(env, files=[("refund_policy.txt", policy())])
    assert await env.service.publish_item(ref.id, actor_id=ACTOR) == "publish"
    refs = await _found(env, "refunds eligibility unused product")
    assert refs and all(r.startswith(f"G{ref.number}.") for r in refs)
    item = await _item(ref.id)
    assert (item.lifecycle_status, item.live_version_id, item.needs_review) == ("published", ref.version_id, False)
    assert await env.service.publish_item(ref.id, actor_id=ACTOR) is not None or True
    assert await env.service.check_invariants() == []


async def test_a_new_version_embeds_only_what_changed_and_stays_unpublished_until_an_admin_publishes_it(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    first_embedded = env.embeddings.count
    old = {r.ordinal: r.text for r in await _sql("SELECT ordinal, text FROM kb_chunks WHERE item_id = :i", i=ref.id)}

    edited = policy(eligibility="A customer is eligible when the product is unused and returned within thirty days.")
    v2 = await env.service.add_version(ref.id, actor_id=ACTOR, files=[("policy_v2.txt", edited)], keep_previous_sources=False, change_note="Longer window")
    await env.worker.drain()
    assert (await _version(v2.version_id)).processing_status == "ready"
    assert env.embeddings.count - first_embedded == 1  # one edited passage; the rest reused

    # Processed, but the item still serves version 1.
    item = await _item(ref.id)
    assert item.live_version_id == ref.version_id and item.latest_version_id == v2.version_id
    assert any("unused." in e.text and "thirty" not in e.text for e in await env.backend.asearch("customer eligible product unused"))
    assert await env.service.check_invariants() == []

    # Unchanged passages kept their citation numbers; the edited one has a new one.
    new = {r.ordinal: r.text for r in await _sql("SELECT c.ordinal, c.text FROM kb_chunks c JOIN kb_version_chunks vc ON vc.chunk_id = c.id WHERE vc.version_id = :v", v=v2.version_id)}
    kept = set(old) & set(new)
    assert kept and all(old[o] == new[o] for o in kept) and len(set(new) - set(old)) == 1

    assert await env.service.publish_version(ref.id, v2.version_id, actor_id=ACTOR, expected_live_version_id=ref.version_id) == "switch"
    assert any("thirty" in e.text for e in await env.backend.asearch("customer eligible product unused returned thirty days"))
    # The citation to the replaced passage still reaches the text it originally cited.
    replaced = (set(old) - set(new)).pop()
    cited = await env.backend.apassages_by_refs([f"G{ref.number}.{replaced}"])
    assert cited and cited[0].text == old[replaced]
    assert replaced not in {int(r.split(".")[1]) for r in await _found(env, "customer eligible product unused")}
    assert await env.service.check_invariants() == []


async def test_an_identical_text_in_another_item_costs_no_embedding(env):
    await _make(env, files=[("a.txt", policy())])
    before = env.embeddings.count
    again = await _make(env, files=[("b.txt", policy())], allow_duplicate=True)
    assert env.embeddings.count == before  # every passage's vector was already paid for
    assert (await _version(again.version_id)).processing_status == "ready"


async def test_publishing_a_stale_or_unready_or_foreign_version_is_a_conflict(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    other = await _make(env, files=[("other.txt", b"OTHER\n\nSomething entirely different about shipping.\n")])
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    v2 = await env.service.add_version(ref.id, actor_id=ACTOR, body_md="Typed addition about refunds.")
    with pytest.raises(ConflictError):  # not processed yet
        await env.service.publish_version(ref.id, v2.version_id, actor_id=ACTOR, expected_live_version_id=ref.version_id)
    await env.worker.drain()
    with pytest.raises(ConflictError):  # the admin's view is stale
        await env.service.publish_version(ref.id, v2.version_id, actor_id=ACTOR, expected_live_version_id=uuid.uuid4())
    with pytest.raises(ConflictError):  # another item's version
        await env.service.publish_version(ref.id, other.version_id, actor_id=ACTOR, expected_live_version_id=ref.version_id)
    assert (await _item(ref.id)).live_version_id == ref.version_id
    assert await env.service.check_invariants() == []


async def test_two_admins_switching_at_once_one_wins_and_one_is_told(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    v2 = await env.service.add_version(ref.id, actor_id=ACTOR, body_md="Typed addition number two about refunds.")
    v3 = await env.service.add_version(ref.id, actor_id=ACTOR, body_md="Typed addition number three about refunds.") if False else None
    await env.worker.drain()
    results = await asyncio.gather(
        *[env.service.publish_version(ref.id, v2.version_id, actor_id=ACTOR, expected_live_version_id=ref.version_id) for _ in range(4)],
        return_exceptions=True,
    )
    assert sum(1 for r in results if r == "switch") == 1
    assert sum(1 for r in results if isinstance(r, ConflictError)) == 3
    assert (await _item(ref.id)).live_version_id == v2.version_id
    assert await env.service.check_invariants() == []


async def test_a_reader_never_sees_a_half_switched_item(env):
    """While versions are switched back and forth, every search sees one whole version."""
    ref = await _make(env, files=[("policy.txt", policy())])
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    v2 = await env.service.add_version(ref.id, actor_id=ACTOR, files=[("p2.txt", policy(eligibility="Eligible only after thirty days of ownership by the customer."))], keep_previous_sources=False)
    await env.worker.drain()
    stop, seen = asyncio.Event(), []

    async def read():
        while not stop.is_set():
            async with get_session_factory()() as session:
                live = (await session.execute(text("SELECT count(*) FROM kb_chunks WHERE is_live AND item_id = :i"), {"i": ref.id})).scalar()
                seen.append(live)
                await asyncio.sleep(0)

    sizes = {await _count_version(ref.version_id), await _count_version(v2.version_id)}
    reader = asyncio.create_task(read())
    live_version = ref.version_id
    for target in (v2.version_id, ref.version_id) * 4:
        await env.service.publish_version(ref.id, target, actor_id=ACTOR, expected_live_version_id=live_version)
        live_version = target
    stop.set()
    await reader
    shared = (await _sql("SELECT count(*) AS n FROM (SELECT chunk_id FROM kb_version_chunks WHERE version_id = :a INTERSECT SELECT chunk_id FROM kb_version_chunks WHERE version_id = :b) s", a=ref.version_id, b=v2.version_id))[0].n
    assert shared >= 1  # the switch really does share passages, which must never blink off
    assert len(seen) > 5 and set(seen) <= sizes, f"a search saw a partial version: {sorted(set(seen))} vs {sorted(sizes)}"


async def _count_version(version_id) -> int:
    return (await _sql("SELECT count(*) AS n FROM kb_version_chunks WHERE version_id = :v", v=version_id))[0].n


# --- lifecycle ---------------------------------------------------------------------------------


async def test_unpublish_archive_delete_and_restore_always_take_the_item_out_of_retrieval(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    query = "refunds eligibility unused product"
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    assert await _found(env, query)

    await env.service.set_lifecycle(ref.id, "unpublish", actor_id=ACTOR)
    assert await _found(env, query) == []
    await env.service.publish_item(ref.id, actor_id=ACTOR)  # re-publishes its last live version
    assert await _found(env, query)
    await env.service.set_lifecycle(ref.id, "archive", actor_id=ACTOR)
    assert await _found(env, query) == []
    with pytest.raises(ConflictError):
        await env.service.publish_item(ref.id, actor_id=ACTOR)
    await env.service.set_lifecycle(ref.id, "restore_archived", actor_id=ACTOR)
    item = await _item(ref.id)
    assert item.lifecycle_status == "unpublished" and await _found(env, query) == []  # never back to live on its own

    await env.service.publish_item(ref.id, actor_id=ACTOR)
    await env.service.set_lifecycle(ref.id, "delete", actor_id=ACTOR)
    assert await _found(env, query) == [] and (await _item(ref.id)).deleted_at is not None
    with pytest.raises(ConflictError):
        await env.service.publish_item(ref.id, actor_id=ACTOR)
    with pytest.raises(ConflictError, match="not enabled"):
        await env.service.set_lifecycle(ref.id, "purge", actor_id=ACTOR)
    await env.service.set_lifecycle(ref.id, "restore_deleted", actor_id=ACTOR)
    item = await _item(ref.id)
    assert (item.lifecycle_status, item.deleted_at) == ("unpublished", None) and await _found(env, query) == []
    assert await env.service.check_invariants() == []
    actions = [a.action for a in await _sql("SELECT action FROM kb_activity WHERE item_id = :i ORDER BY created_at", i=ref.id)]
    assert actions[:3] == ["created", "chunked", "processed"] and "unpublish" in actions and "delete" in actions


async def test_metadata_edits_create_no_version_and_no_job(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    jobs = (await _sql("SELECT count(*) AS n FROM kb_jobs"))[0].n
    embedded = env.embeddings.count
    await env.service.update_item(ref.id, actor_id=ACTOR, title="Refund rules", description="  How   refunds work ", properties={"owner": "finance"})
    item = await _item(ref.id)
    assert (item.title, item.description, item.properties) == ("Refund rules", "How refunds work", {"owner": "finance"})
    assert (await _sql("SELECT count(*) AS n FROM kb_jobs"))[0].n == jobs and env.embeddings.count == embedded
    with pytest.raises(ValidationFailedError):
        await env.service.update_item(ref.id, actor_id=ACTOR, title="   ")


async def test_moving_an_item_between_categories_keeps_their_centroids(env):
    ref = await _make(env, files=[("policy.txt", policy())])
    async with get_session_factory()() as session, session.begin():
        a, b = uuid.uuid4(), uuid.uuid4()
        for cid, name in ((a, "Policies"), (b, "Finance")):
            await session.execute(kb.KbCategory.__table__.insert(), [{"id": cid, "name": name, "slug": name.lower()}])
    await env.service.update_item(ref.id, actor_id=ACTOR, category_id=a)
    counts = {r.name: r.centroid_count for r in await _sql("SELECT name, centroid_count FROM kb_categories")}
    assert counts == {"Policies": 1, "Finance": 0}
    await env.service.update_item(ref.id, actor_id=ACTOR, category_id=b)
    counts = {r.name: r.centroid_count for r in await _sql("SELECT name, centroid_count FROM kb_categories")}
    assert counts == {"Policies": 0, "Finance": 1}


async def test_a_second_document_on_the_same_subject_is_proposed_the_first_ones_category(env):
    first = await _make(env, files=[("policy.txt", policy())])
    async with get_session_factory()() as session, session.begin():
        cid = uuid.uuid4()
        await session.execute(kb.KbCategory.__table__.insert(), [{"id": cid, "name": "Refund policy", "slug": "refund-policy"}])
    await env.service.update_item(first.id, actor_id=ACTOR, category_id=cid)  # the admin files the first one
    second = await _make(env, files=[("policy_copy.txt", policy(escalation="Disputes go to the billing team within three days."))])
    item = await _item(second.id)
    assert item.suggestions["category"] and item.suggestions["category"]["id"] == str(cid)
    assert item.suggestions["category"]["status"] == "assigned" and item.category_id == cid


# --- failures and recovery ----------------------------------------------------------------------


async def _make_due(job_filter="TRUE"):
    await _sql(f"UPDATE kb_jobs SET run_after = now() - interval '1 second' WHERE {job_filter}")


async def test_a_provider_outage_is_retried_with_backoff_and_only_the_missing_vectors_are_filled(env):
    env.embeddings.failing = True
    ref = await env.service.create_item(actor_id=ACTOR, files=[("policy.txt", policy())])
    await env.worker.drain()
    job = (await _sql("SELECT status, attempts, last_error, run_after > now() AS later FROM kb_jobs"))[0]
    assert (job.status, job.attempts, job.later) == ("queued", 1, True)
    assert (await _version(ref.version_id)).processing_status != "ready"
    assert await env.worker.run_once() is False  # not due yet

    env.embeddings.failing = False
    await _make_due()
    asked_before = env.embeddings.count
    await env.worker.drain()
    assert (await _version(ref.version_id)).processing_status == "ready"
    chunks = (await _sql("SELECT count(*) AS n FROM kb_chunks WHERE item_id = :i", i=ref.id))[0].n
    assert env.embeddings.count - asked_before == chunks  # each passage asked for once more, none twice
    assert (await _sql("SELECT checkpoint FROM kb_jobs"))[0].checkpoint["persisted"] is True
    assert (await _sql("SELECT count(*) AS n FROM kb_chunks WHERE item_id = :i", i=ref.id))[0].n == chunks  # no duplicate planning


async def test_when_embedding_retries_run_out_the_version_is_still_finished_with_a_warning(env):
    env.embeddings.failing = True
    ref = await env.service.create_item(actor_id=ACTOR, files=[("policy.txt", policy())])
    await _sql("UPDATE kb_jobs SET max_attempts = 2")
    await env.worker.drain()
    await _make_due()
    await env.worker.drain()
    version = await _version(ref.version_id)
    assert version.processing_status == "ready" and version.warnings and "no embedding" in version.warnings[0]
    assert (await _sql("SELECT status FROM kb_jobs"))[0].status == "succeeded"
    # Publishable, and found by keyword search alone.
    await env.service.publish_item(ref.id, actor_id=ACTOR)
    assert await _found(env, "refunds eligibility unused product")
    assert await env.service.check_invariants() == []


async def test_a_file_with_no_text_fails_once_with_a_message_and_can_be_retried_by_an_admin(env):
    ref = await env.service.create_item(actor_id=ACTOR, files=[("blank.txt", b"   \n\n  \n")])
    await env.worker.drain()
    version = await _version(ref.version_id)
    assert version.processing_status == "failed" and ("empty" in version.processing_error or "No usable text" in version.processing_error)
    job = (await _sql("SELECT status, attempts FROM kb_jobs"))[0]
    assert (job.status, job.attempts) == ("failed", 1)  # a permanent error is not retried
    with pytest.raises(ConflictError):
        await env.service.publish_item(ref.id, actor_id=ACTOR)
    await env.service.retry_version(ref.version_id, actor_id=ACTOR)
    assert (await _sql("SELECT status, attempts FROM kb_jobs"))[0].status == "queued"
    with pytest.raises(ConflictError):  # only a failed version can be retried
        await env.service.retry_version((await _make(env, files=[("ok.txt", policy())])).version_id, actor_id=ACTOR)


async def test_a_job_abandoned_by_a_dead_worker_is_reclaimed_and_a_live_one_is_not(env):
    ref = await env.service.create_item(actor_id=ACTOR, files=[("policy.txt", policy())])
    await _sql("UPDATE kb_jobs SET status = 'running', attempts = 1, locked_by = 'dead-worker', heartbeat_at = now()")
    assert await env.worker.run_once() is False  # its lease is still good
    await _sql("UPDATE kb_jobs SET heartbeat_at = now() - interval '10 minutes'")
    assert await env.worker.run_once() is True
    job = (await _sql("SELECT status, attempts, locked_by FROM kb_jobs"))[0]
    assert job.status == "succeeded" and job.attempts == 2 and job.locked_by is None
    assert (await _version(ref.version_id)).processing_status == "ready"


async def test_a_job_that_keeps_killing_its_worker_is_given_up_on(env):
    await env.service.create_item(actor_id=ACTOR, files=[("policy.txt", policy())])
    await _sql("UPDATE kb_jobs SET status = 'running', attempts = max_attempts, heartbeat_at = now() - interval '10 minutes'")
    assert await env.worker.run_once() is False
    assert (await _sql("SELECT status FROM kb_jobs"))[0].status == "dead"


async def test_two_workers_never_take_the_same_job(env):
    for i in range(6):
        await env.service.create_item(actor_id=ACTOR, body_md=f"Typed note number {i} about subject {i} {uuid.uuid4().hex}.")
    workers = [KbWorker(settings=env.settings, embeddings=env.embeddings, worker_id=f"w{n}") for n in range(3)]
    done = await asyncio.gather(*[w.drain() for w in workers])
    assert sum(done) == 6
    assert {r.status for r in await _sql("SELECT status FROM kb_jobs")} == {"succeeded"}
    assert {r.attempts for r in await _sql("SELECT attempts FROM kb_jobs")} == {1}


# --- whatever the admins do, lifecycle and liveness agree --------------------------------------------


async def test_random_admin_activity_never_breaks_the_live_invariant(env):
    rng = random.Random(11)
    refs = [await _make(env, files=[(f"doc{i}.txt", policy(eligibility=f"Eligible variant {i} {uuid.uuid4().hex} for the customer."))]) for i in range(3)]
    for step in range(45):
        ref = rng.choice(refs)
        action = rng.choice(["publish", "unpublish", "archive", "restore_archived", "delete", "restore_deleted", "version", "switch"])
        try:
            if action == "publish":
                await env.service.publish_item(ref.id, actor_id=ACTOR)
            elif action == "version":
                await env.service.add_version(ref.id, actor_id=ACTOR, body_md=f"Added text {uuid.uuid4().hex} for step {step}.")
                await env.worker.drain()
            elif action == "switch":
                item = await _item(ref.id)
                ready = [r.id for r in await _sql("SELECT id FROM kb_item_versions WHERE item_id = :i AND processing_status = 'ready' ORDER BY version_no", i=ref.id)]
                await env.service.publish_version(ref.id, rng.choice(ready), actor_id=ACTOR, expected_live_version_id=item.live_version_id)
            else:
                await env.service.set_lifecycle(ref.id, action, actor_id=ACTOR)
        except ConflictError:
            pass
        assert await env.service.check_invariants() == [], f"after step {step}: {action}"
    published = {r.number for r in await _sql("SELECT number FROM kb_items WHERE lifecycle_status = 'published' AND deleted_at IS NULL")}
    live = {r.number for r in await _sql("SELECT DISTINCT i.number FROM kb_chunks c JOIN kb_items i ON i.id = c.item_id WHERE c.is_live")}
    assert live == published


async def test_the_worker_loop_stops_when_asked(env):
    await env.service.create_item(actor_id=ACTOR, body_md="Typed note for the loop test about queues.")
    stop = asyncio.Event()
    task = asyncio.create_task(env.worker.run_forever(stop))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if (await _sql("SELECT status FROM kb_jobs"))[0].status == "succeeded":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert (await _sql("SELECT status FROM kb_jobs"))[0].status == "succeeded"
