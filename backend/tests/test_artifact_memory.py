"""Artifact memory (Memory 2): the lock, the lifecycle and the links, against a real PostgreSQL."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.artifacts.service import ArtifactService
from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.session import get_session_factory
from tests.artifact_helpers import wipe_artifact_memory
from tests.test_artifact_extract import block, document
from app.schemas.blocks import BlockType

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")

REVIEWER = uuid.uuid4()


class Embeddings:
    """Deterministic vectors, recording the texts, and able to fail."""

    enabled = True

    def __init__(self):
        self.failing = False
        self.texts: list[str] = []

    async def embed_batch(self, texts):
        self.texts.extend(texts)
        if self.failing:
            return [None] * len(texts)
        return [[(hash(t) % 97) / 97.0 + 0.01] * 1536 for t in texts]


@pytest.fixture
async def env(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    await wipe_artifact_memory()
    async with get_session_factory()() as session, session.begin():
        # Approved rows are locked, so the test cleanup has to go through the guard like the downgrade does.
        # The reviewer is a real user: approvals are recorded against one.
        await session.execute(
            text("INSERT INTO users (id, email, password_hash, role, is_active, is_verified, created_at, updated_at) VALUES (:i, :e, 'x', 'editor_reviewer', true, true, now(), now()) ON CONFLICT (id) DO NOTHING"),
            {"i": REVIEWER, "e": f"sme-{REVIEWER}@example.com"},
        )

    class Env:
        pass

    e = Env()
    e.embeddings = Embeddings()
    e.service = ArtifactService(settings=get_settings(), embeddings=e.embeddings)
    yield e
    await wipe_artifact_memory()  # approved artifacts are reused by every course: leave none behind


def course(paragraph="Refunds are issued within fourteen days [G2.5]."):
    """The same course each time: a document's block ids persist across saves, and an artifact's identity rests on them."""
    def fixed(id_, *args, **kwargs):
        return block(*args, **kwargs).model_copy(update={"id": id_})

    return document(
        fixed("blk_heading", BlockType.HEADING, {"text": "How refunds work", "level": 1}, chapter_number=1),
        fixed("blk_text", BlockType.PARAGRAPH, {"text": paragraph}),
        fixed("blk_quiz", BlockType.QUIZ, {"title": "Knowledge Check", "questions": [{"question": "How many days?", "answer": "14"}]}),
        fixed("blk_image", BlockType.IMAGE, {"caption": "The refund flow", "generated": True, "path": "assets/flow.png", "asset_id": "img_1"}),
    )


async def _sql(sql, **params):
    async with get_session_factory()() as session, session.begin():
        result = await session.execute(text(sql), params)
        return result.all() if result.returns_rows else []


async def _submit(env, doc=None, course_id="course_1", **kw):
    return await env.service.sync_course(course_id=course_id, course_title="Refund Handling", document=doc or course(), generation_run_id="job_1", model_info={"generated_with": "test"}, **kw)


async def _approved(env, doc=None, course_id="course_1"):
    await _submit(env, doc, course_id)
    await env.service.approve_course(course_id, REVIEWER)


async def _versions(artifact_title):
    return await _sql(
        "SELECT v.version_no, v.review_status, v.locked, v.is_active FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id WHERE a.title = :t ORDER BY v.version_no",
        t=artifact_title,
    )


# --- the lock is the database's, not just the application's -----------------------------------------------------


async def test_an_approved_version_cannot_be_changed_or_deleted_by_anyone(env):
    await _approved(env)
    # The chapter, explicitly: it is the one with provenance, so every column below really would change.
    row = (await _sql("SELECT v.id FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id WHERE v.locked AND a.artifact_type = 'chapter'"))[0]
    for column, value in (("content", '{"hacked": true}'), ("content_hash", "x"), ("search_text", "x"), ("source_refs", "[]"), ("review_comment", "x"), ("approved_by", None), ("generation_run_id", "x")):
        sql = f"UPDATE am_artifact_versions SET {column} = " + (f"CAST('{value}' AS jsonb)" if column in ("content", "source_refs") else ":v") + " WHERE id = :i"
        with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
            await _sql(sql, i=row.id, v=value)
    for statement in ("UPDATE am_artifact_versions SET review_status = 'pending_review', locked = false, approved_at = NULL WHERE id = :i", "UPDATE am_artifact_versions SET version_no = 99 WHERE id = :i"):
        with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
            await _sql(statement, i=row.id)
    with pytest.raises((IntegrityError, DBAPIError), match="cannot be deleted"):
        await _sql("DELETE FROM am_artifact_versions WHERE id = :i", i=row.id)
    # Deleting the artifact would delete its versions too: that is refused as well.
    with pytest.raises((IntegrityError, DBAPIError), match="cannot be deleted"):
        await _sql("DELETE FROM am_artifacts WHERE id = (SELECT artifact_id FROM am_artifact_versions WHERE id = :i)", i=row.id)


async def test_what_may_still_change_on_an_approved_version_is_only_which_one_is_active_and_a_missing_vector(env):
    env.embeddings.failing = True
    await _approved(env)
    row = (await _sql("SELECT id, embedding IS NULL AS no_vector FROM am_artifact_versions WHERE locked LIMIT 1"))[0]
    assert row.no_vector
    await _sql("UPDATE am_artifact_versions SET is_active = false WHERE id = :i", i=row.id)  # allowed
    await _sql("UPDATE am_artifact_versions SET is_active = true WHERE id = :i", i=row.id)
    env.embeddings.failing = False
    assert await env.service.index_missing() == 3  # the vector may be filled in once ...
    vec = "[" + ",".join(["0.5"] * 1536) + "]"
    with pytest.raises((IntegrityError, DBAPIError), match="immutable"):  # ... and then never changed
        await _sql("UPDATE am_artifact_versions SET embedding = CAST(:v AS vector) WHERE id = :i", i=row.id, v=vec)
    assert await env.service.index_missing() == 0


async def test_the_table_rules_keep_approval_activity_and_locking_consistent(env):
    await _submit(env)
    row = (await _sql("SELECT id FROM am_artifact_versions LIMIT 1"))[0]
    for bad in (
        "UPDATE am_artifact_versions SET is_active = true WHERE id = :i",  # active but not approved
        "UPDATE am_artifact_versions SET locked = true WHERE id = :i",  # locked but not approved
        "UPDATE am_artifact_versions SET review_status = 'approved', locked = true WHERE id = :i",  # approved with no approval time
        "UPDATE am_artifact_versions SET review_status = 'nonsense' WHERE id = :i",
    ):
        with pytest.raises(IntegrityError):
            await _sql(bad, i=row.id)


# --- submitting and approving ------------------------------------------------------------------------------------------


async def test_a_submitted_course_waits_for_review_and_is_not_retrievable(env):
    result = await _submit(env)
    assert (result.created, result.unchanged) == (3, 0)
    rows = await _sql("SELECT a.artifact_type, v.review_status, v.locked, v.is_active, v.embedding IS NULL AS no_vector FROM am_artifacts a JOIN am_artifact_versions v ON v.id = a.latest_version_id ORDER BY 1")
    assert sorted(r.artifact_type for r in rows) == ["chapter", "questions", "visual"]
    assert all((r.review_status, r.locked, r.is_active, r.no_vector) == ("pending_review", False, False, True) for r in rows)
    assert env.embeddings.texts == []  # nothing is embedded for work nobody has approved


async def test_approving_locks_indexes_and_activates_every_pending_version_together(env):
    await _submit(env)
    assert await env.service.approve_course("course_1", REVIEWER, "Looks right") == 3
    rows = await _sql("SELECT v.review_status, v.locked, v.is_active, v.approved_by, v.approved_at IS NOT NULL AS stamped, v.embedding IS NOT NULL AS indexed, v.review_comment FROM am_artifact_versions v")
    assert len(rows) == 3 and all(r.review_status == "approved" and r.locked and r.is_active and r.approved_by == REVIEWER and r.stamped and r.indexed and r.review_comment == "Looks right" for r in rows)
    assert await env.service.check_invariants() == []
    assert await env.service.approve_course("course_1", REVIEWER) == 0  # nothing left to approve
    assert len(await _sql("SELECT 1 FROM am_activity WHERE action = 'approved'")) == 3


async def test_a_course_sent_back_returns_its_pending_work_with_the_reason_and_can_be_resubmitted(env):
    await _submit(env)
    assert await env.service.request_changes_course("course_1", REVIEWER, "The numbers are wrong") == 3
    rows = await _sql("SELECT review_status, review_comment, locked FROM am_artifact_versions")
    assert {(r.review_status, r.review_comment, r.locked) for r in rows} == {("changes_requested", "The numbers are wrong", False)}
    result = await _submit(env, course("Refunds are issued within thirty days [G2.5]."))
    assert (result.updated, result.resubmitted) == (1, 2)  # the edited chapter updated; the others back in the queue as they were
    assert {r.review_status for r in await _sql("SELECT review_status FROM am_artifact_versions")} == {"pending_review"}
    assert len(await _sql("SELECT 1 FROM am_artifact_versions")) == 3  # in place: no new versions for work nobody approved


async def test_resubmitting_an_approved_course_with_a_change_makes_a_new_version_that_needs_approval(env):
    await _approved(env)
    result = await _submit(env, course("Refunds are issued within thirty days [G2.5]."))
    assert (result.new_versions, result.unchanged) == (1, 2)
    chapter = await _versions("How refunds work")
    assert [(v.version_no, v.review_status, v.locked, v.is_active) for v in chapter] == [(1, "approved", True, True), (2, "pending_review", False, False)]
    # The approved version is untouched and still what retrieval would offer.
    assert (await _sql("SELECT content::text LIKE '%fourteen%' AS old FROM am_artifact_versions WHERE version_no = 1 AND is_active AND artifact_id IN (SELECT id FROM am_artifacts WHERE artifact_type = 'chapter')"))[0].old
    await env.service.approve_course("course_1", REVIEWER)
    chapter = await _versions("How refunds work")
    assert [(v.version_no, v.review_status, v.is_active) for v in chapter] == [(1, "approved", False), (2, "approved", True)]  # the old one is kept, not offered
    assert await env.service.check_invariants() == []


async def test_only_a_version_waiting_for_review_and_the_newest_can_be_approved(env):
    await _approved(env)
    version = (await _sql("SELECT id FROM am_artifact_versions LIMIT 1"))[0].id
    with pytest.raises(ConflictError, match="waiting for review"):
        await env.service.approve_version(version, REVIEWER)
    with pytest.raises(NotFoundError):
        await env.service.approve_version(uuid.uuid4(), REVIEWER)
    with pytest.raises(ValidationFailedError):
        await env.service.request_changes_version(version, REVIEWER, "  ")
    with pytest.raises(ConflictError):
        await env.service.request_changes_version(version, REVIEWER, "too late: already approved")


async def test_one_artifact_can_be_reviewed_on_its_own(env):
    await _submit(env)
    quiz = (await _sql("SELECT v.id FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id WHERE a.artifact_type = 'questions'"))[0].id
    visual = (await _sql("SELECT v.id FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id WHERE a.artifact_type = 'visual'"))[0].id
    await env.service.approve_version(quiz, REVIEWER, "Good questions")
    await env.service.request_changes_version(visual, REVIEWER, "Wrong diagram")
    states = {r.artifact_type: (r.review_status, r.is_active) for r in await _sql("SELECT a.artifact_type, v.review_status, v.is_active FROM am_artifacts a JOIN am_artifact_versions v ON v.id = a.latest_version_id")}
    assert states == {"questions": ("approved", True), "visual": ("changes_requested", False), "chapter": ("pending_review", False)}


async def test_an_approval_with_no_embedding_available_still_approves_and_is_indexed_later(env):
    env.embeddings.failing = True
    await _submit(env)
    assert await env.service.approve_course("course_1", REVIEWER) == 3
    assert {r.embedding_missing for r in await _sql("SELECT embedding IS NULL AS embedding_missing FROM am_artifact_versions")} == {True}
    env.embeddings.failing = False
    assert await env.service.index_missing() == 3
    assert {r.embedding_missing for r in await _sql("SELECT embedding IS NULL AS embedding_missing FROM am_artifact_versions")} == {False}


# --- editing, archiving ------------------------------------------------------------------------------------------------


async def test_editing_approved_work_is_a_new_version_and_editing_unapproved_work_is_in_place(env):
    await _approved(env)
    artifact = (await _sql("SELECT id FROM am_artifacts WHERE artifact_type = 'questions'"))[0].id
    edited = {"kind": "quiz", "block": {"type": "quiz", "content": {"title": "Knowledge Check", "questions": [{"question": "How many days exactly?", "answer": "14"}]}}}
    version = await env.service.new_version(artifact, edited, actor_id=REVIEWER, change_note="Sharper question")
    rows = await _versions("Knowledge Check")
    assert [(r.version_no, r.review_status, r.is_active) for r in rows] == [(1, "approved", True), (2, "pending_review", False)]
    again = {"kind": "quiz", "block": {"type": "quiz", "content": {"title": "Knowledge Check", "questions": [{"question": "And how many hours?", "answer": "336"}]}}}
    assert await env.service.new_version(artifact, again, actor_id=REVIEWER) == version  # still unapproved: changed where it stands
    assert len(await _versions("Knowledge Check")) == 2
    with pytest.raises(ConflictError, match="same"):
        await env.service.new_version(artifact, again, actor_id=REVIEWER)
    with pytest.raises(ValidationFailedError):
        await env.service.new_version(artifact, {}, actor_id=REVIEWER)
    with pytest.raises(NotFoundError):
        await env.service.new_version(uuid.uuid4(), again, actor_id=REVIEWER)


async def test_archiving_takes_an_artifact_out_of_retrieval_and_restoring_brings_it_back(env):
    await _approved(env)
    artifact = (await _sql("SELECT id FROM am_artifacts WHERE artifact_type = 'visual'"))[0].id
    await env.service.set_archived(artifact, True, actor_id=REVIEWER)
    assert not (await _sql("SELECT is_active FROM am_artifact_versions WHERE artifact_id = :a", a=artifact))[0].is_active
    assert await env.service.check_invariants() == []
    with pytest.raises(ConflictError):
        await env.service.set_archived(artifact, True, actor_id=REVIEWER)
    await env.service.set_archived(artifact, False, actor_id=REVIEWER)
    assert (await _sql("SELECT is_active FROM am_artifact_versions WHERE artifact_id = :a", a=artifact))[0].is_active
    assert await env.service.check_invariants() == []


# --- links ----------------------------------------------------------------------------------------------------------------


async def test_a_submitted_course_is_connected_to_its_course_its_parts_and_the_knowledge_it_cites(env):
    await _submit(env)
    links = await _sql(
        "SELECT a.artifact_type, l.link_type, l.target_course_id, l.target_kb_number, l.target_kb_ordinal, t.artifact_type AS target_type "
        "FROM am_links l JOIN am_artifacts a ON a.id = l.from_artifact_id LEFT JOIN am_artifacts t ON t.id = l.target_artifact_id"
    )
    seen = {(r.artifact_type, r.link_type, r.target_course_id, r.target_kb_number, r.target_kb_ordinal, r.target_type) for r in links}
    assert ("chapter", "belongs_to_course", "course_1", None, None, None) in seen and ("visual", "belongs_to_course", "course_1", None, None, None) in seen
    assert ("chapter", "references_knowledge", None, 2, 5, None) in seen  # a citation number: no foreign key into Memory 1
    assert ("chapter", "contains_visual", None, None, None, "visual") in seen and ("chapter", "contains_questions", None, None, None, "questions") in seen
    await _submit(env)  # resubmitting adds no duplicates
    assert len(await _sql("SELECT 1 FROM am_links")) == len(links)


async def test_a_link_must_have_exactly_one_target_that_fits_its_type(env):
    await _submit(env)
    a, b = [r.id for r in (await _sql("SELECT id FROM am_artifacts WHERE artifact_type IN ('chapter', 'visual') ORDER BY artifact_type"))]
    await env.service.add_link(a, "related_to", to_artifact=b, note="similar")
    await env.service.add_link(a, "references_knowledge", kb_number=7, kb_ordinal=3)
    await env.service.add_link(a, "references_knowledge", kb_number=7, kb_ordinal=3)  # a repeat is harmless
    for kwargs in (
        dict(link_type="related_to", to_artifact=a),  # itself
        dict(link_type="related_to", course_id="course_1"),  # a course is not an artifact
        dict(link_type="belongs_to_course", to_artifact=b),  # a course link must name a course
        dict(link_type="references_knowledge", to_artifact=b),
        dict(link_type="related_to", to_artifact=b, kb_number=1),  # two targets
        dict(link_type="friends", to_artifact=b),
    ):
        with pytest.raises(ValidationFailedError):
            await env.service.add_link(a, **kwargs)
    with pytest.raises(NotFoundError):
        await env.service.add_link(a, "related_to", to_artifact=uuid.uuid4())
    link = (await _sql("SELECT id FROM am_links WHERE link_type = 'related_to' AND note = 'similar'"))[0].id
    await env.service.remove_link(link)
    with pytest.raises(NotFoundError):
        await env.service.remove_link(link)


async def test_supersedes_cannot_loop(env):
    await _submit(env)
    ids = [r.id for r in await _sql("SELECT id FROM am_artifacts")]
    a, b, c = ids[:3]
    await env.service.add_link(a, "supersedes", to_artifact=b)
    await env.service.add_link(b, "supersedes", to_artifact=c)
    with pytest.raises(ConflictError, match="itself"):
        await env.service.add_link(c, "supersedes", to_artifact=a)
    await env.service.add_link(c, "related_to", to_artifact=a)  # other kinds may


async def test_identical_content_from_elsewhere_is_noted_and_never_merged(env):
    await _approved(env, course(), "course_1")
    result = await _submit(env, course(), "course_2")
    assert result.created == 3 and len(result.duplicates) == 3
    assert len(await _sql("SELECT 1 FROM am_artifacts")) == 6
    assert len(await _sql("SELECT 1 FROM am_activity WHERE action = 'duplicate_noted'")) == 3
