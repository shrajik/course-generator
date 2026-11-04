"""Shadow comparison, the query-embedding cache, and the audit-compatible library shape."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.course.fact_check import PassageIndex
from app.schemas.source import SourceExcerpt
from app.services import knowledge_shadow as shadow
from app.services.embedding_service import EmbeddingService
from app.services.knowledge_pg import PostgresKnowledgeBackend, _library_of, _Passage
from app.services.knowledge_service import KnowledgeService
from app.services.source_service import get_source_service
from tests import golden_knowledge as golden


def _hit(ref: str) -> SourceExcerpt:
    return SourceExcerpt(ref=ref, source_id="src_aaaaaaaaaaaa", filename="a.txt", text="t")


@pytest.fixture(autouse=True)
def _fresh_stats():
    shadow.reset_shadow_stats()
    yield
    shadow.reset_shadow_stats()


def test_how_two_answers_relate():
    a, b, c = _hit("G1.1"), _hit("G1.2"), _hit("G2.1")
    assert shadow.classify([a, b], [a, b]) == "match"
    assert shadow.classify([], []) == "match"
    assert shadow.classify([a, b], [b, a]) == "reordered"
    assert shadow.classify([a, b], [a, c]) == "partial"
    assert shadow.classify([a], [c]) == "different"
    assert shadow.classify([a], []) == "different"


class _Fake:
    def __init__(self, answer=None, fail=False, library=None):
        self.answer, self.fail, self.library = answer or [], fail, library or set()
        self.calls = []

    async def asearch(self, query, *, limit=None, char_budget=None, trace=None):
        self.calls.append((query, limit, char_budget, trace))
        if self.fail:
            raise RuntimeError("database down")
        return self.answer

    async def alibrary(self, extra_refs=None):
        if self.fail:
            raise RuntimeError("database down")

        class Lib:
            passages = {ref: None for ref in self.library}

        return Lib()


async def test_comparisons_are_counted_and_differences_are_kept():
    files = [_hit("G1.1"), _hit("G1.2")]
    assert await shadow.compare_search("q1", files, limit=4, char_budget=None, backend=_Fake(files)) == "match"
    assert await shadow.compare_search("q2", files, limit=4, char_budget=None, backend=_Fake([_hit("G9.9")])) == "different"
    assert await shadow.compare_search("q3", files, limit=4, char_budget=None, backend=_Fake(fail=True)) == "error"
    stats = shadow.get_shadow_stats()
    assert (stats.counts["match"], stats.counts["different"], stats.counts["error"], stats.searches) == (1, 1, 1, 3)
    assert [d["query"] for d in stats.recent_differences] == ["q2"]


async def test_the_library_comparison_counts_agreement_and_disagreement():
    assert await shadow.compare_library({"G1.1", "G1.2"}, backend=_Fake(library={"G1.1", "G1.2"})) is True
    assert await shadow.compare_library({"G1.1"}, backend=_Fake(library={"G1.1", "G2.1"})) is False
    assert await shadow.compare_library({"G1.1"}, backend=_Fake(fail=True)) is False
    stats = shadow.get_shadow_stats()
    assert (stats.library_matches, stats.library_differences) == (1, 1)


async def _library_with_corpus(**settings):
    service = KnowledgeService(settings=get_settings().model_copy(update=settings))
    for name, data in golden.CORPUS:
        record = service.upload(name, data)
        await get_source_service().wait(record.source_id)
    return service


async def test_shadow_mode_serves_the_file_answer_and_compares_in_the_background(monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(shadow, "_get_backend", lambda: fake)
    service = await _library_with_corpus(kb_shadow_compare=True, use_database=True)
    query = "boil pasta and drain it"
    served = await service.asearch(query, trace=("course_1", "ch_1"))
    assert [e.ref for e in served] == [e.ref for e in await service.search(query)] == ["G2.1"]
    await shadow.drain()
    assert fake.calls and fake.calls[0][0] == query
    assert shadow.get_shadow_stats().counts["different"] == 1  # the fake answered nothing
    # The comparison is only scheduled when switched on, and only with a database to compare against.
    for off in ({"kb_shadow_compare": False, "use_database": True}, {"kb_shadow_compare": True, "use_database": False}):
        fake.calls.clear()
        quiet = await _library_with_corpus(**off)
        await quiet.asearch(query)
        await shadow.drain()
        assert fake.calls == []


async def test_a_failing_shadow_never_fails_the_real_search(monkeypatch):
    monkeypatch.setattr(shadow, "_get_backend", lambda: _Fake(fail=True))
    service = await _library_with_corpus(kb_shadow_compare=True, use_database=True)
    assert [e.ref for e in await service.asearch("boil pasta and drain it")] == ["G2.1"]
    await shadow.drain()
    assert shadow.get_shadow_stats().counts["error"] == 1


# --- the query embedding cache --------------------------------------------------------


class _CountingAI:
    def __init__(self):
        self.calls = 0
        self.fail_next = False

    async def embed(self, texts, model):
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider down")
        return [[1.0, 2.0, 3.0] for _ in texts]


async def test_a_repeated_query_is_embedded_once_and_failures_are_not_remembered():
    ai = _CountingAI()
    service = EmbeddingService(ai=ai, settings=get_settings().model_copy(update={"enable_embeddings": True, "embedding_query_cache_size": 2}))
    first = await service.embed_one("refund policy")
    assert await service.embed_one("refund policy") == first and ai.calls == 1
    first.append(9.0)  # a caller changing its copy must not change the cached one
    assert await service.embed_one("refund policy") == [1.0, 2.0, 3.0]
    ai.fail_next = True
    assert await service.embed_one("other") is None
    assert await service.embed_one("other") == [1.0, 2.0, 3.0] and ai.calls == 3  # retried, not cached as failed
    await service.embed_one("third")  # capacity 2: the least recently used ("refund policy") is evicted
    await service.embed_one("refund policy")
    assert ai.calls == 5


async def test_the_cache_can_be_switched_off():
    ai = _CountingAI()
    service = EmbeddingService(ai=ai, settings=get_settings().model_copy(update={"enable_embeddings": True, "embedding_query_cache_size": 0}))
    await service.embed_one("x")
    await service.embed_one("x")
    assert ai.calls == 2


# --- the library shape the audit reads -------------------------------------------------


def _passage(number: int, ordinal: int, text: str, heading: str = "") -> _Passage:
    return _Passage(
        chunk_id=None, ref=f"G{number}.{ordinal}", number=number, ordinal=ordinal, public_id=f"src_{number:012x}",
        filename="policy.pdf", heading=heading, heading_path=[heading] if heading else [], page=3, text=text, words=len(text.split()),
        version_id=None, kind="pdf", size_bytes=10, sha256="ab",
    )


def test_a_database_library_has_the_shape_the_audit_expects():
    library = _library_of([
        _passage(1, 1, "Refunds are issued within fourteen days.", "Refunds"),
        _passage(1, 2, "Disputes go to the billing team."),
        _passage(2, 1, "Passwords rotate every ninety days."),
    ])
    assert set(library.passages) == {"G1.1", "G1.2", "G2.1"} and library.candidates == []
    record, chunk = library.passages["G1.1"]
    assert (record.global_number, record.filename, record.owner_key, chunk.index, chunk.page) == (1, "policy.pdf", "global", 0, 3)
    assert library.passages["G1.1"][0] is library.passages["G1.2"][0]  # one record per item
    assert library.excerpt("G1.1").text.startswith("Refunds are issued") and library.excerpt("G9.9") is None
    # The audit's own index runs on it unchanged.
    index = PassageIndex(library)
    assert index.best({"refund", "fourteen"})[0][0] == "G1.1"


def test_the_backend_can_be_built_without_a_database_connection():
    backend = PostgresKnowledgeBackend(session_factory=lambda: (_ for _ in ()).throw(AssertionError("no connection yet")))
    assert backend.log_retrievals is True


async def test_a_failure_to_record_a_retrieval_is_swallowed():
    def no_connection():
        raise RuntimeError("log table gone")

    backend = PostgresKnowledgeBackend(session_factory=no_connection)
    await backend._log_retrieval("q", [_passage(1, 1, "text")], ("course_1", "ch_1"))  # noqa: SLF001 - must not raise
