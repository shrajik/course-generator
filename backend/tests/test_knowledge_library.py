"""The global knowledge library: the index, the service, generation, audit and the admin API."""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.api.dependencies import get_current_admin
from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError, NotFoundError
from app.course.fact_check import AuditBlock, audit_blocks
from app.ingestion.index import IndexedPassage, SearchIndex
from app.schemas.research import ChapterResearch
from app.services import course_service
from app.services.knowledge_service import GLOBAL_OWNER, get_knowledge_service
from app.services.source_service import get_source_service
from app.services.storage_service import get_storage
from tests.sample_files import POLICY_TEXT, make_pdf

COOKING = (
    b"COOKING PASTA\n\nBoil the pasta for ten minutes in plenty of salted water, then drain it well.\n\n"
    b"1.1 Sauces\n\nSimmer the tomato sauce slowly with garlic and fresh basil until it thickens.\n"
)
SECURITY = (
    b"PASSWORD STANDARD\n\nPasswords must contain at least fourteen characters and must be rotated "
    b"every ninety days across all corporate systems.\n"
)


# --- the search index ----------------------------------------------------------

PASSAGES = [
    IndexedPassage("G1.1", "Refunds are issued within fourteen days when the product is returned unused."),
    IndexedPassage("G1.2", "Orders ship from the warehouse within two business days by courier."),
    IndexedPassage("G2.1", "Boil the pasta for ten minutes in salted water and drain it well."),
]


def _index(passages=PASSAGES, dims=4) -> SearchIndex:
    return SearchIndex(passages, dims)


def test_keyword_search_finds_the_passage_about_the_query():
    assert _index().search("how long do refunds take after the product is returned")[0][0] == "G1.1"


def test_a_passage_sharing_only_a_common_word_is_not_offered():
    # "business" appears in the shipping passage; the query is about something else entirely.
    assert _index().search("quarterly business strategy for enterprise cloud migration planning") == []


def test_the_coverage_floor_can_be_relaxed():
    query = "business strategy for enterprise cloud migration planning"
    assert _index().search(query, min_coverage=0.0, min_terms=1)[0][0] == "G1.2"


def test_a_query_with_no_matching_words_returns_nothing():
    assert _index().search("quantum chromodynamics") == []
    assert _index().search("") == []
    assert SearchIndex([], 4).search("refunds") == []


def test_semantic_search_matches_by_meaning_when_no_words_overlap():
    passages = [
        IndexedPassage("A", "alpha", vector=[1, 0, 0, 0]),
        IndexedPassage("B", "beta", vector=[0, 1, 0, 0]),
    ]
    assert _index(passages).search("zzz", query_vector=[0.95, 0.1, 0, 0])[0][0] == "A"


def test_a_vector_that_is_not_close_enough_contributes_nothing():
    passages = [IndexedPassage("A", "alpha", vector=[1, 0, 0, 0])]
    assert _index(passages).search("zzz", query_vector=[0.1, 1, 0, 0], min_semantic=0.5) == []


def test_passages_without_vectors_still_match_by_keyword_and_bad_vectors_are_ignored():
    passages = [IndexedPassage("A", "refund policy for returned goods", vector=None),
                IndexedPassage("B", "refund policy wrong size", vector=[1, 2])]
    index = _index(passages)
    assert {ref for ref, _ in index.search("refund policy", query_vector=[1, 0, 0, 0])} == {"A", "B"}
    assert index.search("zzz", query_vector=[1, 0]) == []  # a query vector of the wrong size is ignored


def test_agreement_between_keywords_and_meaning_ranks_first_and_the_limit_holds():
    passages = [
        IndexedPassage("both", "refund policy details", vector=[1, 0, 0, 0]),
        IndexedPassage("word", "refund unrelated phrasing", vector=[0, 1, 0, 0]),
        IndexedPassage("sense", "something else entirely", vector=[0.9, 0.1, 0, 0]),
    ]
    ranked = _index(passages).search("refund policy", query_vector=[1, 0, 0, 0], limit=2)
    assert ranked[0][0] == "both" and len(ranked) == 2


def test_the_index_scales_to_many_passages_quickly():
    rng = np.random.default_rng(1)
    passages = [IndexedPassage(f"G1.{i}", f"topic{i % 50} details about subject {i % 200}", rng.random(64).tolist()) for i in range(6000)]
    index = SearchIndex(passages, 64)
    started = time.perf_counter()
    for _ in range(20):
        index.search("topic7 details subject 107", query_vector=rng.random(64).tolist())
    assert (time.perf_counter() - started) / 20 < 0.25


# --- the service ----------------------------------------------------------------


@pytest.fixture
def library():
    return get_knowledge_service()


async def _add(library, name, data, description=""):
    record = library.upload(name, data, description=description)
    await get_source_service().wait(record.source_id)
    return library.get(record.source_id)


async def test_documents_get_permanent_numbers_in_order_and_start_enabled(library):
    first = await _add(library, "policy.txt", POLICY_TEXT, "  Refund   policy  ")
    second = await _add(library, "cooking.txt", COOKING)
    assert (first.global_number, second.global_number) == (1, 2)
    assert first.status == "ready" and first.enabled and first.description == "Refund policy"
    assert [d.global_number for d in library.list_documents()] == [1, 2]
    assert library.stats() == {"documents": 2, "searchable": 2, "passages": first.chunk_count + second.chunk_count, "processing": 0}


async def test_the_same_file_is_not_added_twice_and_keeps_its_number(library):
    first = await _add(library, "policy.txt", POLICY_TEXT)
    again = library.upload("renamed.txt", POLICY_TEXT)
    assert again.source_id == first.source_id and again.global_number == 1
    assert (await _add(library, "cooking.txt", COOKING)).global_number == 2  # no number was wasted


async def test_a_deleted_documents_number_is_never_given_to_another(library):
    await _add(library, "policy.txt", POLICY_TEXT)
    second = await _add(library, "cooking.txt", COOKING)
    library.delete(second.source_id)
    assert (await _add(library, "security.txt", SECURITY)).global_number == 3


async def test_library_documents_are_separate_from_any_users_sources(library):
    await _add(library, "policy.txt", POLICY_TEXT)
    assert get_source_service().list_sources("user-1") == []
    assert len(get_source_service().list_sources(GLOBAL_OWNER)) == 1


async def test_a_search_returns_only_what_is_relevant_with_global_references(library):
    await _add(library, "policy.txt", POLICY_TEXT)
    await _add(library, "cooking.txt", COOKING)
    found = await library.search("how are refunds handled for disputed customer requests")
    assert found and all(e.ref.startswith("G1.") for e in found) and found[0].filename == "policy.txt"
    assert "billing team" in " ".join(e.text for e in found)
    cooking = await library.search("boil the pasta in salted water")
    assert cooking and cooking[0].ref.startswith("G2.")
    assert await library.search("quarterly enterprise cloud migration strategy") == []
    assert await library.search("   ") == []


async def test_switching_a_document_off_removes_it_from_search_and_back_on_restores_it(library):
    policy = await _add(library, "policy.txt", POLICY_TEXT)
    assert await library.search("refund eligibility for returned products")
    library.update(policy.source_id, enabled=False)
    assert await library.search("refund eligibility for returned products") == []
    assert not library.has_documents() and library.stats()["searchable"] == 0
    library.update(policy.source_id, enabled=True)
    assert await library.search("refund eligibility for returned products")


async def test_adding_and_removing_documents_changes_search_at_once(library):
    assert not library.has_documents()
    policy = await _add(library, "policy.txt", POLICY_TEXT)
    assert library.has_documents()
    library.delete(policy.source_id)
    assert not library.has_documents()


async def test_the_index_is_rebuilt_only_when_the_documents_change(library, monkeypatch):
    monkeypatch.setattr("app.services.knowledge_service._FINGERPRINT_TTL_SECONDS", 0.0)
    await _add(library, "policy.txt", POLICY_TEXT)
    first = library._current()
    assert library._current() is first  # unchanged documents: the same index
    await _add(library, "cooking.txt", COOKING)
    assert library._current() is not first


async def test_a_document_cannot_be_edited_while_it_is_still_processing(library):
    record = library.upload("policy.txt", POLICY_TEXT)
    with pytest.raises(ConflictError, match="still being processed"):
        library.update(record.source_id, enabled=False)
    await get_source_service().wait(record.source_id)
    assert library.update(record.source_id, enabled=False).enabled is False


async def test_a_failed_document_is_listed_with_its_reason_and_never_searched(library):
    bad = await _add(library, "scan.pdf", make_pdf([[], []]))
    assert bad.status == "failed" and "scanned" in bad.error and bad.global_number is not None
    assert not library.has_documents()


async def test_passages_can_be_read_back_by_reference_and_the_chunks_listed(library):
    policy = await _add(library, "policy.txt", POLICY_TEXT)
    found = library.passages_by_refs(["G1.1", "G1.1", "G9.9", "S1.1"])
    assert [e.ref for e in found] == ["G1.1"] and "fourteen days" in found[0].text
    assert len(library.chunks(policy.source_id, 0, 2)) == 2
    with pytest.raises(NotFoundError):
        library.chunks("src_aaaaaaaaaaaa")


async def test_the_whole_library_can_be_switched_off(library, monkeypatch):
    await _add(library, "policy.txt", POLICY_TEXT)
    monkeypatch.setenv("ENABLE_GLOBAL_KNOWLEDGE", "false")
    reset_settings_cache()
    off = type(library)()
    assert off.has_documents() is False and await off.search("refund eligibility for returned products") == []


# --- courses draw on the library ---------------------------------------------------


def _course(client, title="Refund handling", toc=("Refund policy",), source_ids=()):
    return client.post("/api/courses", json={
        "course_title": title, "toc": [{"title": t} for t in toc], "target_audience": "Support agents",
        "template": "non_technical", "run_planner": False, "source_ids": list(source_ids),
    }).json()


def _wait_source(client, source_id):
    for _ in range(200):
        if client.get(f"/api/sources/{source_id}").json()["status"] in {"ready", "failed"}:
            return
        time.sleep(0.05)
    raise AssertionError("source not ready")


def _spy_on_research():
    seen: dict[str, str] = {}
    writer = course_service.get_course_service().writer
    original = writer.write_chapter

    async def spy(**kwargs):
        research = kwargs.get("research")
        seen[kwargs["chapter"].id] = research.compact_context() if research else ""
        return await original(**kwargs)

    writer.write_chapter = spy
    return seen


async def test_a_course_with_no_sources_of_its_own_still_gets_relevant_library_passages(client, library):
    await _add(library, "policy.txt", POLICY_TEXT)
    seen = _spy_on_research()
    course = _course(client)
    assert client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"}).status_code == 200
    text = next(iter(seen.values()))
    assert "KNOWLEDGE LIBRARY EXCERPTS" in text and "[G1." in text and "UPLOADED SOURCE EXCERPTS" not in text
    assert "fourteen days" in text or "billing team" in text


async def test_a_chapter_unrelated_to_the_library_is_given_nothing_from_it(client, library):
    await _add(library, "policy.txt", POLICY_TEXT)
    seen = _spy_on_research()
    course = _course(client, title="Quantum field theory", toc=("Lagrangian mechanics",))
    result = client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert result.status_code == 200
    assert "KNOWLEDGE LIBRARY EXCERPTS" not in next(iter(seen.values()))
    assert not any("matched" in w for w in result.json()["warnings"])  # a chapter needing nothing is not a problem


async def test_a_disabled_library_document_is_not_used_by_courses(client, library):
    policy = await _add(library, "policy.txt", POLICY_TEXT)
    library.update(policy.source_id, enabled=False)
    seen = _spy_on_research()
    course = _course(client)
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert "KNOWLEDGE LIBRARY EXCERPTS" not in next(iter(seen.values()))


async def test_both_the_courses_own_sources_and_the_library_are_offered_course_sources_first(client, library):
    await _add(library, "policy.txt", POLICY_TEXT)
    own = client.post("/api/sources", files={"file": ("manual.txt", b"REFUND MANUAL\n\nAgents approve refunds for returned products using the refund form.\n", "text/plain")}).json()
    _wait_source(client, own["source_id"])
    seen = _spy_on_research()
    course = _course(client, source_ids=[own["source_id"]])
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    text = next(iter(seen.values()))
    assert text.index("UPLOADED SOURCE EXCERPTS") < text.index("KNOWLEDGE LIBRARY EXCERPTS") < text.index("WEB RESEARCH")
    assert "follow the course's documents" in text


async def test_without_any_library_documents_the_research_is_exactly_as_before(client):
    seen = _spy_on_research()
    course = _course(client)
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert "KNOWLEDGE LIBRARY" not in next(iter(seen.values()))


def test_knowledge_excerpts_are_attached_for_a_run_and_never_stored_with_the_research():
    from app.schemas.source import SourceExcerpt

    research = ChapterResearch(chapter_id="chapter_1")
    research.knowledge_excerpts = [SourceExcerpt(ref="G2.5", source_id="src_a", filename="standard.pdf", heading="Passwords", page=3, text="Passwords rotate every ninety days.")]
    text = research.compact_context(max_chars=50)
    assert text.index("KNOWLEDGE LIBRARY EXCERPTS") < text.index("WEB RESEARCH") and "[G2.5] (standard.pdf, page 3 - Passwords)" in text


# --- the fact check knows the library ------------------------------------------------


async def test_a_claim_the_library_supports_is_not_flagged_and_its_citation_is_valid(client, library):
    await _add(library, "security.txt", SECURITY)
    from app.services.audit_service import AuditService
    from app.agents.auditor import AuditorAgent
    from app.schemas.blocks import BlockType
    from app.schemas.document import Block, CourseDocument, Page

    own = client.post("/api/sources", files={"file": ("manual.txt", b"REFUND MANUAL\n\nAgents approve refunds using the refund form.\n", "text/plain")}).json()
    _wait_source(client, own["source_id"])
    sentences = [
        "Passwords must contain at least fourteen characters and must be rotated every ninety days [G1.1].",
        "Passwords must contain at least fourteen characters and must be rotated every ninety days.",
        "Passwords must contain at least fourteen characters and must be rotated every ninety days [G9.9].",
    ]
    blocks = [Block(id=f"block_{i:012d}", type=BlockType.PARAGRAPH, content={"text": s}) for i, s in enumerate(sentences)]
    document = CourseDocument(document_id="doc_aaaaaaaaaaaa", course_id="crs_aaaaaaaaaaaa", course_title="T", template_id="technical_v1",
                              pages=[Page(id="p1", page_number=1, kind="content", blocks=blocks)])
    class NoAI:
        async def structured(self, **kwargs):
            raise RuntimeError("off")
    service = AuditService(storage=get_storage(), sources=get_source_service(), auditor=AuditorAgent(NoAI()))
    report = await service.run(document=document, owner_key="local", source_ids=[own["source_id"]], use_ai=False)
    assert [(f.kind, f.refs) for f in report.findings] == [("invalid_citation", ["G9.9"])]
    assert report.supported_sentences == 2


def test_a_g_reference_is_read_as_a_citation():
    from app.course.fact_check import extract_refs, split_sentences, strip_refs

    assert extract_refs("Rotate passwords [G2.5, S1.3] often [G10.1].") == ["G2.5", "S1.3", "G10.1"]
    assert strip_refs("Rotate passwords [G2.5].") == "Rotate passwords ."
    assert split_sentences("Rotate passwords every ninety days. [G2.5] Another sentence follows here.")[0].endswith("[G2.5]")


# --- the admin API ---------------------------------------------------------------------


class _Admin:
    id = "admin-1"
    role = "admin"


@pytest.fixture
def admin(client):
    client.app.dependency_overrides[get_current_admin] = lambda: _Admin()
    yield client
    client.app.dependency_overrides.pop(get_current_admin, None)


def _wait(client, source_id):
    for _ in range(200):
        record = client.get(f"/api/knowledge/{source_id}").json()
        if record["status"] in {"ready", "failed"}:
            return record
        time.sleep(0.05)
    raise AssertionError("not ready")


def test_only_an_admin_can_use_the_library(client):
    for method, path in (("get", "/api/knowledge"), ("post", "/api/knowledge/search"), ("get", "/api/knowledge/src_aaaaaaaaaaaa"),
                         ("delete", "/api/knowledge/src_aaaaaaaaaaaa")):
        response = getattr(client, method)(path, **({"json": {"query": "x"}} if method == "post" else {}))
        assert response.status_code in {401, 403}, (method, path, response.status_code)
    assert client.post("/api/knowledge", files={"file": ("a.txt", b"hello", "text/plain")}).status_code in {401, 403}


def test_an_admin_uploads_lists_searches_toggles_and_deletes(admin):
    created = admin.post("/api/knowledge", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")}, data={"description": "Refund rules"})
    assert created.status_code == 201, created.text
    source_id = created.json()["source_id"]
    assert created.json()["global_number"] == 1 and created.json()["description"] == "Refund rules"

    ready = _wait(admin, source_id)
    assert ready["status"] == "ready" and ready["enabled"] is True
    listing = admin.get("/api/knowledge").json()
    assert [d["source_id"] for d in listing["documents"]] == [source_id]
    assert listing["stats"]["searchable"] == 1 and listing["stats"]["passages"] == ready["chunk_count"]

    hits = admin.post("/api/knowledge/search", json={"query": "who handles disputed refund cases", "limit": 3}).json()
    assert hits and hits[0]["ref"].startswith("G1.") and "billing team" in hits[0]["text"]
    assert admin.post("/api/knowledge/search", json={"query": "quantum chromodynamics lagrangian"}).json() == []

    off = admin.patch(f"/api/knowledge/{source_id}", json={"enabled": False, "description": "Withdrawn"})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["description"] == "Withdrawn"
    assert admin.post("/api/knowledge/search", json={"query": "who handles disputed refund cases"}).json() == []

    assert len(admin.get(f"/api/knowledge/{source_id}/chunks?limit=2").json()) == 2
    assert admin.delete(f"/api/knowledge/{source_id}").status_code == 204
    assert admin.get(f"/api/knowledge/{source_id}").status_code == 404
    assert admin.get("/api/knowledge").json()["documents"] == []


def test_the_admin_api_refuses_bad_input(admin):
    assert admin.post("/api/knowledge", files={"file": ("virus.exe", b"MZ", "application/octet-stream")}).status_code == 422
    assert admin.post("/api/knowledge/search", json={"query": ""}).status_code == 422
    assert admin.patch("/api/knowledge/src_aaaaaaaaaaaa", json={"enabled": True}).status_code == 404
    assert admin.patch("/api/knowledge/src_aaaaaaaaaaaa", json={"bogus": 1}).status_code == 422


def test_a_reviewer_can_read_a_cited_library_passage_through_the_course(admin):
    created = admin.post("/api/knowledge", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")}).json()
    _wait(admin, created["source_id"])
    course = _course(admin)
    found = admin.get(f"/api/courses/{course['course_id']}/passages?refs=G1.3,G1.3,G9.9,S1.1").json()
    assert [p["ref"] for p in found] == ["G1.3"] and "billing team" in found[0]["text"]


# --- the progress screen shows where each chapter's content comes from -------------


async def test_chapter_progress_names_the_library_document_and_the_upload_it_drew_on(client, library):
    await _add(library, "policy.txt", POLICY_TEXT)
    own = client.post("/api/sources", files={"file": ("manual.txt", b"REFUND MANUAL\n\nAgents approve refunds for returned products using the refund form.\n", "text/plain")}).json()
    _wait_source(client, own["source_id"])
    course = _course(client, source_ids=[own["source_id"]])
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    chapter = client.get(f"/api/courses/{course['course_id']}").json()["course"]["chapters"][0]
    assert chapter["sources_checked"] is True
    used = {use["origin"]: use for use in chapter["sources_offered"]}
    assert used["knowledge"]["filename"] == "policy.txt" and used["knowledge"]["passages"] >= 1
    assert all(ref.startswith("G1.") for ref in used["knowledge"]["refs"])
    assert used["upload"]["filename"] == "manual.txt" and used["upload"]["refs"][0].startswith("S1.")


async def test_a_chapter_nothing_matched_is_marked_web_research_only(client):
    course = _course(client, title="Quantum field theory", toc=("Lagrangian mechanics",))
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    chapter = client.get(f"/api/courses/{course['course_id']}").json()["course"]["chapters"][0]
    assert chapter["sources_checked"] is True and chapter["sources_offered"] == []
