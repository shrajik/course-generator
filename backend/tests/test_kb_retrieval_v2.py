"""Retrieval v2 and the relationship rules in the PostgreSQL search, against a real database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings, reset_settings_cache
from app.db.session import get_session_factory
from app.kb.service import KbService
from app.kb.taxonomy import Taxonomy
from app.kb.worker import KbWorker
from app.services.embedding_service import get_embedding_service
from app.services.knowledge_pg import PostgresKnowledgeBackend, SearchScope

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured")

QUERY = "refund eligibility for returned products"


def doc(topic: str, body: str) -> bytes:
    return f"{topic.upper()}\n\n{body}\n".encode()


REFUNDS = "Refunds are issued within fourteen days when the product is returned unused. Eligibility requires the original packaging."
REFUNDS_NEW = "Refunds are issued within thirty days when the product is returned unused. Eligibility requires the original packaging."
PASTA = "Boil the pasta for ten minutes in plenty of salted water, then drain it well before serving."


class Env:
    def __init__(self, settings):
        self.settings = settings
        self.kb = KbService(settings=settings)
        self.tax = Taxonomy()
        self.worker = KbWorker(settings=settings)
        self.backend = PostgresKnowledgeBackend(settings, embeddings=get_embedding_service(), log_retrievals=False)

    def search_with(self, **overrides):
        return PostgresKnowledgeBackend(self.settings.model_copy(update=overrides), embeddings=get_embedding_service(), log_retrievals=False)

    async def publish(self, name: str, body: str, **kw):
        ref = await self.kb.create_item(actor_id=None, files=[(name, doc(name, body))], allow_duplicate=True, **kw)
        await self.worker.drain()
        await self.kb.publish_item(ref.id, actor_id=None)
        return ref


@pytest.fixture
async def env(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    reset_settings_cache()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("TRUNCATE kb_items, kb_sources, kb_categories, kb_tags, kb_retrieval_log CASCADE"))
    return Env(get_settings())


def numbers(excerpts) -> list[int]:
    return [int(e.ref[1:].split(".")[0]) for e in excerpts]


# --- relationships shape what is offered ----------------------------------------------------------


async def test_a_superseded_item_is_not_offered_while_its_replacement_is_published(env):
    old = await env.publish("old.txt", REFUNDS)
    new = await env.publish("new.txt", REFUNDS_NEW)
    assert set(numbers(await env.backend.asearch(QUERY))) == {old.number, new.number}
    await env.tax.add_relation(new.id, old.id, "supersedes")
    assert numbers(await env.backend.asearch(QUERY)) == [new.number]

    await env.kb.set_lifecycle(new.id, "unpublish", actor_id=None)  # the replacement is withdrawn: the original returns
    assert numbers(await env.backend.asearch(QUERY)) == [old.number]


async def test_of_two_duplicates_only_the_original_is_offered(env):
    original = await env.publish("original.txt", REFUNDS)
    copy = await env.publish("copy.txt", REFUNDS)
    assert set(numbers(await env.backend.asearch(QUERY))) == {original.number, copy.number}
    await env.tax.add_relation(copy.id, original.id, "duplicate_of")
    assert numbers(await env.backend.asearch(QUERY)) == [original.number]
    off = env.search_with(knowledge_graph_rules=False)
    assert set(numbers(await off.asearch(QUERY))) == {original.number, copy.number}  # the rules can be switched off


async def test_related_items_are_added_only_when_relevant_and_only_if_switched_on(env):
    main = await env.publish("refunds.txt", REFUNDS)
    related = await env.publish("returns.txt", "Returned products must be inspected before a refund is paid, and eligibility is decided at inspection.")
    unrelated = await env.publish("pasta.txt", PASTA)
    await env.tax.add_relation(main.id, related.id, "related_to")
    await env.tax.add_relation(main.id, unrelated.id, "related_to")
    base = await env.backend.asearch("refund eligibility", limit=1)
    assert len(base) == 1
    expanded = await env.search_with(knowledge_related_expansion=2).asearch("refund eligibility", limit=1)
    assert numbers(expanded)[0] == numbers(base)[0]
    assert related.number in numbers(expanded) and unrelated.number not in numbers(expanded)  # relevance still decides
    assert len(expanded) <= 3


# --- filters ---------------------------------------------------------------------------------------


async def test_a_search_can_be_narrowed_by_category_tags_and_topic(env):
    finance = await env.tax.create_category("Finance")
    refunds_cat = await env.tax.create_category("Refunds", parent_id=finance)
    kitchen = await env.tax.create_category("Kitchen")
    a = await env.publish("refunds.txt", REFUNDS)
    b = await env.publish("pasta.txt", PASTA)
    await env.kb.update_item(a.id, actor_id=None, category_id=refunds_cat)
    await env.kb.update_item(b.id, actor_id=None, category_id=kitchen)
    await env.tax.set_tags(a.id, ["policy", "Finance"])
    await env.tax.set_tags(b.id, ["policy"])
    both = "returned products pasta"

    assert set(numbers(await env.backend.asearch(f"{QUERY} {both}"))) >= {a.number}
    only_finance = await env.backend.asearch(QUERY, scope=SearchScope(category_id=finance))  # includes sub-categories
    assert numbers(only_finance) == [a.number]
    assert await env.backend.asearch("boil pasta and drain it", scope=SearchScope(category_id=finance)) == []
    assert numbers(await env.backend.asearch("boil pasta and drain it", scope=SearchScope(category_id=kitchen))) == [b.number]
    assert numbers(await env.backend.asearch(QUERY, scope=SearchScope(tags=("POLICY", "finance")))) == [a.number]  # all tags, any case
    assert await env.backend.asearch(QUERY, scope=SearchScope(tags=("policy", "nonexistent"))) == []
    assert await env.backend.asearch(QUERY, scope=SearchScope(category_id=uuid.uuid4())) == []


async def test_a_scope_never_lets_in_unpublished_content(env):
    cat = await env.tax.create_category("Finance")
    a = await env.publish("refunds.txt", REFUNDS)
    await env.kb.update_item(a.id, actor_id=None, category_id=cat)
    await env.kb.set_lifecycle(a.id, "unpublish", actor_id=None)
    assert await env.backend.asearch(QUERY, scope=SearchScope(category_id=cat)) == []


async def test_section_topics_filter_inside_a_document(env):
    ref = await env.kb.create_item(actor_id=None, body_md="# Refunds\n\nRefunds are issued within fourteen days of a valid request.\n\n# Shipping\n\nOrders ship from the warehouse by courier within two days.")
    await env.worker.drain()
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("UPDATE kb_chunks SET topics = ARRAY['Refunds'] WHERE heading = 'Refunds' AND item_id = :i"), {"i": ref.id})
        await session.execute(text("UPDATE kb_chunks SET topics = ARRAY['Shipping'] WHERE heading = 'Shipping' AND item_id = :i"), {"i": ref.id})
    await env.kb.publish_item(ref.id, actor_id=None)
    query = "refunds shipping courier warehouse within days"
    assert len(await env.backend.asearch(query)) == 2
    only = await env.backend.asearch(query, scope=SearchScope(topics=("Shipping",)))
    assert len(only) == 1 and "courier" in only[0].text


# --- shaping what is returned --------------------------------------------------------------------------


async def test_dedupe_drops_a_passage_that_repeats_a_better_one_and_keeps_distinct_ones(env):
    await env.publish("a.txt", REFUNDS)
    await env.publish("b.txt", REFUNDS + " ")  # the same words again
    await env.publish("c.txt", "Refund eligibility also depends on the product being returned in its original packaging unused.")
    plain = await env.backend.asearch(QUERY)
    deduped = await env.search_with(knowledge_dedupe=True).asearch(QUERY)
    assert len(plain) == 3 and 2 <= len(deduped) < 3
    assert len({e.text for e in deduped}) == len(deduped)


async def test_compression_fits_a_long_passage_into_the_budget_it_would_otherwise_miss(env):
    sentences = " ".join(f"Filler sentence number {i} says nothing useful." for i in range(30))
    await env.publish("first.txt", REFUNDS)
    await env.publish("long.txt", f"{sentences} Refund eligibility depends on the original packaging. {sentences}")
    budget = len(REFUNDS) + 400
    plain = await env.backend.asearch("refund eligibility original packaging", char_budget=budget)
    compressed = await env.search_with(knowledge_compress=True).asearch("refund eligibility original packaging", char_budget=budget)
    assert len(plain) == 1  # the long passage did not fit and was left out
    assert len(compressed) == 2 and "original packaging" in compressed[1].text
    assert sum(len(e.text) for e in compressed) <= budget


# --- the result cache ---------------------------------------------------------------------------------------


class Counting:
    enabled = True

    def __init__(self):
        self.real = get_embedding_service()
        self.calls = 0

    async def embed_one(self, text_):
        self.calls += 1
        return await self.real.embed_one(text_)


async def test_a_repeated_question_is_answered_from_memory_until_anything_retrievable_changes(env):
    counting = Counting()
    backend = PostgresKnowledgeBackend(env.settings, embeddings=counting, log_retrievals=False)
    a = await env.publish("refunds.txt", REFUNDS)
    first = await backend.asearch(QUERY)
    assert await backend.asearch(QUERY) == first and await backend.asearch("  " + QUERY.upper() + " ") == first
    assert counting.calls == 1  # asked once, however it was spelled

    await env.kb.set_lifecycle(a.id, "unpublish", actor_id=None)
    assert await backend.asearch(QUERY) == [] and counting.calls == 2  # the epoch moved: no stale answer
    await env.publish("again.txt", REFUNDS_NEW)
    assert numbers(await backend.asearch(QUERY)) != [] and counting.calls == 3
    # An edit to an item's category (which filtered searches depend on) invalidates too.
    cat = await env.tax.create_category("Finance")
    scoped = SearchScope(category_id=cat)
    assert await backend.asearch(QUERY, scope=scoped) == []
    item = (await backend.asearch(QUERY))[0]
    holder = (await _item_by_number(numbers([item])[0]))
    await env.kb.update_item(holder, actor_id=None, category_id=cat)
    assert numbers(await backend.asearch(QUERY, scope=scoped)) == numbers([item])


async def _item_by_number(number: int):
    async with get_session_factory()() as session:
        return (await session.execute(text("SELECT id FROM kb_items WHERE number = :n"), {"n": number})).scalar()


async def test_the_cache_returns_copies_and_can_be_switched_off(env):
    await env.publish("refunds.txt", REFUNDS)
    first = await env.backend.asearch(QUERY)
    first[0].text = "tampered by a caller"
    assert (await env.backend.asearch(QUERY))[0].text != "tampered by a caller"
    counting = Counting()
    off = PostgresKnowledgeBackend(env.settings.model_copy(update={"knowledge_cache_size": 0}), embeddings=counting, log_retrievals=False)
    await off.asearch(QUERY)
    await off.asearch(QUERY)
    assert counting.calls == 2


async def test_a_cached_answer_is_still_logged_for_each_course_that_receives_it(env):
    await env.publish("refunds.txt", REFUNDS)
    logging_backend = PostgresKnowledgeBackend(env.settings, embeddings=get_embedding_service())
    await logging_backend.asearch(QUERY, trace=("course_a", "ch_1"))
    await logging_backend.asearch(QUERY, trace=("course_b", "ch_1"))  # served from memory
    async with get_session_factory()() as session:
        courses = {r.course_id for r in (await session.execute(text("SELECT course_id FROM kb_retrieval_log"))).all()}
    assert courses == {"course_a", "course_b"}
