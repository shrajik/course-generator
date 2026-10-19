"""Uploading, ingesting and retrieving source documents; the API; generation."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.config import get_settings
from app.services.embedding_service import EmbeddingService
from app.services.source_service import SourceService, get_source_service
from tests.sample_files import POLICY_TEXT, make_docx, make_pdf

OWNER = "user-1"
OTHER = "user-2"


@pytest.fixture
def service() -> SourceService:
    return get_source_service()


async def _ready(service: SourceService, owner: str, name: str, data: bytes):
    record = service.upload(owner, name, data)
    await service.wait(record.source_id)
    return service.get(owner, record.source_id)


# --- the ingestion lifecycle --------------------------------------------------


async def test_a_text_upload_becomes_ready_with_counts_an_outline_and_files(service):
    record = service.upload(OWNER, "policy.txt", POLICY_TEXT)
    assert record.status == "uploaded" and record.filename == "policy.txt"
    await service.wait(record.source_id)
    ready = service.get(OWNER, record.source_id)
    assert ready.status == "ready" and ready.error is None
    assert ready.chunk_count >= 3 and ready.word_count > 30
    assert ready.embedded_chunks == ready.chunk_count  # the mock embeds everything
    assert "REFUND POLICY" in ready.outline[0]
    folder = service.root / OWNER / record.source_id
    assert {p.name for p in folder.iterdir()} == {"original.txt", "meta.json", "chunks.json", "vectors.bin"}


async def test_pdf_and_docx_uploads_are_ingested_too(service):
    pdf = await _ready(service, OWNER, "manual.pdf", make_pdf([["REFUND POLICY", "Refunds are issued within fourteen days."]]))
    assert pdf.status == "ready" and pdf.page_count == 1
    docx = await _ready(service, OWNER, "policy.docx", make_docx())
    assert docx.status == "ready" and "Refund Policy" in docx.outline[0]


async def test_a_scanned_pdf_fails_with_a_message_the_person_can_act_on(service):
    record = await _ready(service, OWNER, "scan.pdf", make_pdf([[], []]))
    assert record.status == "failed" and "scanned" in (record.error or "")


async def test_embedding_being_unavailable_still_leaves_the_source_ready(service):
    offline = SourceService(get_settings(), EmbeddingService(settings=get_settings().model_copy(update={"enable_embeddings": False})))
    record = offline.upload(OWNER, "policy.txt", POLICY_TEXT)
    await offline.wait(record.source_id)
    ready = offline.get(OWNER, record.source_id)
    assert ready.status == "ready" and ready.embedded_chunks == 0
    excerpts = await offline.excerpts_for(OWNER, [record.source_id], "refund eligibility")
    assert excerpts and "eligible" in excerpts[0].text  # keyword-only retrieval


async def test_an_ingestion_that_was_cut_off_is_reported_as_failed_not_spinning(service):
    record = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    stale = service.get(OWNER, record.source_id)
    stale.status = "parsing"
    stale.updated_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path = service.root / OWNER / record.source_id / "meta.json"
    path.write_text(stale.model_dump_json(), encoding="utf-8")
    again = service.get(OWNER, record.source_id)
    assert again.status == "failed" and "interrupted" in again.error


# --- upload validation --------------------------------------------------------


@pytest.mark.parametrize("name,data,message", [
    ("empty.txt", b"", "empty"),
    ("notes.exe", b"MZ....", "Unsupported"),
    ("noextension", b"hello", "Unsupported"),
    ("fake.pdf", b"this is not a pdf", "not a PDF"),
    ("fake.docx", b"plain text pretending", "not a Word"),
    ("binary.txt", b"abc\x00\x01\x02def", "binary"),
])
async def test_bad_uploads_are_refused_with_a_clear_reason(service, name, data, message):
    with pytest.raises(ValidationFailedError, match=message):
        service.upload(OWNER, name, data)


async def test_an_oversized_upload_is_refused(service, monkeypatch):
    monkeypatch.setattr(service.settings, "max_source_bytes", 1024)
    with pytest.raises(ValidationFailedError, match="larger"):
        service.upload(OWNER, "big.txt", b"x" * 2048)


async def test_a_hostile_filename_never_reaches_the_filesystem_path(service):
    record = service.upload(OWNER, "../../etc/passwd.txt", POLICY_TEXT)
    assert record.filename == "passwd.txt"
    await service.wait(record.source_id)
    assert (service.root / OWNER / record.source_id / "original.txt").exists()


async def test_the_same_file_uploaded_twice_is_processed_once(service):
    first = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    second = service.upload(OWNER, "renamed.txt", POLICY_TEXT)
    assert second.source_id == first.source_id
    assert len(service.list_sources(OWNER)) == 1


async def test_a_failed_upload_can_be_retried_with_the_same_file(service):
    scan = make_pdf([[]])
    first = await _ready(service, OWNER, "scan.pdf", scan)
    assert first.status == "failed"
    assert service.upload(OWNER, "scan.pdf", scan).source_id != first.source_id


# --- ownership ----------------------------------------------------------------


async def test_a_source_is_invisible_to_other_users(service):
    record = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    with pytest.raises(NotFoundError):
        service.get(OTHER, record.source_id)
    assert service.list_sources(OTHER) == [] and len(service.list_sources(OWNER)) == 1
    with pytest.raises(NotFoundError):
        service.delete(OTHER, record.source_id)


@pytest.mark.parametrize("bad", ["../x", "src_../../etc", "src_ABCDEF123456", "", "src_123"])
async def test_a_malformed_source_id_is_a_plain_not_found(service, bad):
    with pytest.raises(NotFoundError):
        service.get(OWNER, bad)


async def test_a_malformed_owner_key_cannot_escape_the_sources_folder(service):
    with pytest.raises(NotFoundError):
        service.get("../courses", "src_aaaaaaaaaaaa")
    assert service.list_sources("../courses") == []


async def test_deleting_removes_everything(service):
    record = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    service.delete(OWNER, record.source_id)
    assert not (service.root / OWNER / record.source_id).exists()
    with pytest.raises(NotFoundError):
        service.get(OWNER, record.source_id)


# --- using sources in a course -------------------------------------------------


async def test_validate_for_course_accepts_only_ready_sources_the_caller_owns(service):
    ready = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    assert [r.source_id for r in service.validate_for_course(OWNER, [ready.source_id, ready.source_id])] == [ready.source_id]
    with pytest.raises(NotFoundError):
        service.validate_for_course(OTHER, [ready.source_id])
    failed = await _ready(service, OWNER, "scan.pdf", make_pdf([[]]))
    with pytest.raises(ConflictError, match="could not be processed"):
        service.validate_for_course(OWNER, [failed.source_id])


async def test_a_source_still_processing_is_refused_until_it_is_ready(service):
    record = service.upload(OWNER, "policy.txt", POLICY_TEXT)
    with pytest.raises(ConflictError, match="still being processed"):
        service.validate_for_course(OWNER, [record.source_id])
    await service.wait(record.source_id)


async def test_a_course_may_not_use_too_many_sources(service, monkeypatch):
    monkeypatch.setattr(service.settings, "max_sources_per_course", 1)
    with pytest.raises(ValidationFailedError, match="at most 1"):
        service.validate_for_course(OWNER, ["src_aaaaaaaaaaaa", "src_bbbbbbbbbbbb"])


# --- retrieval ----------------------------------------------------------------


async def test_excerpts_answer_the_question_with_stable_references(service):
    policy = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    other = await _ready(service, OWNER, "shipping.txt", b"SHIPPING\n\nOrders ship from the warehouse by courier within two days.\n")
    excerpts = await service.excerpts_for(OWNER, [policy.source_id, other.source_id], "who handles disputed refunds")
    assert excerpts and excerpts[0].ref.startswith("S1.")
    assert excerpts[0].filename == "policy.txt" and "billing team" in excerpts[0].text
    shipping = await service.excerpts_for(OWNER, [policy.source_id, other.source_id], "courier warehouse shipping")
    assert shipping[0].ref.startswith("S2.") and shipping[0].filename == "shipping.txt"


async def test_nothing_is_returned_when_nothing_matches_or_the_source_is_gone(service):
    record = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    assert await service.excerpts_for(OWNER, [record.source_id], "quantum chromodynamics lagrangian") == []
    service.delete(OWNER, record.source_id)
    assert await service.excerpts_for(OWNER, [record.source_id], "refund") == []


async def test_the_character_budget_limits_what_the_writer_sees(service):
    record = await _ready(service, OWNER, "long.txt", ("REFUND NOTES\n\n" + "\n\n".join(f"Refund detail number {i}. " + "refund " * 120 for i in range(12))).encode())
    small = await service.excerpts_for(OWNER, [record.source_id], "refund", limit=10, char_budget=1500)
    large = await service.excerpts_for(OWNER, [record.source_id], "refund", limit=10, char_budget=20000)
    assert 1 <= len(small) < len(large)


async def test_a_vector_file_for_a_different_embedding_size_falls_back_to_keywords(service, monkeypatch):
    record = await _ready(service, OWNER, "policy.txt", POLICY_TEXT)
    monkeypatch.setattr(service.settings, "embedding_dimensions", 8)
    excerpts = await service.excerpts_for(OWNER, [record.source_id], "refund eligibility")
    assert excerpts


# --- the API ------------------------------------------------------------------


def _wait_ready(client, source_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/sources/{source_id}").json()
        if body["status"] in {"ready", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError("ingestion did not finish")


def test_upload_poll_inspect_and_delete_over_http(client):
    response = client.post("/api/sources", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")})
    assert response.status_code == 201, response.text
    source_id = response.json()["source_id"]
    assert response.json()["status"] in {"uploaded", "parsing", "chunking", "embedding", "ready"}

    ready = _wait_ready(client, source_id)
    assert ready["status"] == "ready" and ready["chunk_count"] >= 3

    assert [s["source_id"] for s in client.get("/api/sources").json()] == [source_id]
    chunks = client.get(f"/api/sources/{source_id}/chunks?limit=2").json()
    assert len(chunks) == 2 and chunks[0]["heading_path"] == ["REFUND POLICY"]
    assert client.get(f"/api/sources/{source_id}/chunks?offset=100").json() == []

    assert client.delete(f"/api/sources/{source_id}").status_code == 204
    assert client.get(f"/api/sources/{source_id}").status_code == 404


def test_http_rejects_bad_files_with_a_422_and_unknown_ids_with_a_404(client):
    bad = client.post("/api/sources", files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 422 and "Unsupported" in bad.json()["error"]["message"]
    assert client.get("/api/sources/src_aaaaaaaaaaaa").status_code == 404
    assert client.delete("/api/sources/src_aaaaaaaaaaaa").status_code == 404
    assert client.get("/api/sources/..%2F..%2Fetc").status_code in {404, 422}


def _create_course(client, source_ids):
    return client.post("/api/courses", json={
        "course_title": "Refund handling", "toc": [{"title": "Refund policy"}, {"title": "Escalation"}],
        "target_audience": "Support agents", "template": "non_technical", "run_planner": False,
        "source_ids": source_ids,
    })


def test_a_course_can_only_use_ready_sources_that_exist(client):
    assert _create_course(client, ["src_aaaaaaaaaaaa"]).status_code == 404
    uploaded = client.post("/api/sources", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")}).json()
    _wait_ready(client, uploaded["source_id"])
    created = _create_course(client, [uploaded["source_id"]])
    assert created.status_code == 201 and created.json()["input"]["source_ids"] == [uploaded["source_id"]]


# --- generation uses the sources ----------------------------------------------


def test_chapter_research_shows_the_writer_the_uploaded_passages_first():
    from app.schemas.research import ChapterResearch
    from app.schemas.source import SourceExcerpt

    research = ChapterResearch(chapter_id="chapter_1")
    assert "UPLOADED SOURCE EXCERPTS" not in research.compact_context()
    research.source_excerpts = [
        SourceExcerpt(ref="S1.2", source_id="src_a", filename="policy.pdf", heading="Refunds", page=4, text="Refunds take fourteen days.")
    ]
    text = research.compact_context(max_chars=50)
    assert text.index("UPLOADED SOURCE EXCERPTS") < text.index("WEB RESEARCH")
    assert "[S1.2] (policy.pdf, page 4 - Refunds)" in text and "fourteen days" in text


def test_prompts_are_unchanged_for_a_course_without_sources_and_cite_rules_for_one_with():
    from app.agents import prompts
    from app.course.templates.registry import load_template
    from app.schemas.blueprint import CourseBlueprint
    from app.schemas.course import CourseInput

    def prefix(ids):
        course = CourseInput(course_title="T", toc=[{"title": "One"}], target_audience="A", template="technical", source_ids=ids)
        return prompts.writer_shared_prefix(CourseBlueprint(course_title="T"), course, load_template("technical_v1"))

    assert "UPLOADED SOURCE MATERIAL" not in prefix([])
    assert prefix(["src_aaaaaaaaaaaa"]).count("UPLOADED SOURCE MATERIAL") == 1
    assert "Never cite a reference that was not given" in prefix(["src_aaaaaaaaaaaa"])


def test_generation_hands_each_chapter_its_best_passages(client):
    uploaded = client.post("/api/sources", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")}).json()
    _wait_ready(client, uploaded["source_id"])
    course = _create_course(client, [uploaded["source_id"]]).json()

    from app.services import course_service

    seen: dict[str, str] = {}
    writer = course_service.get_course_service().writer
    original = writer.write_chapter

    async def spy(**kwargs):
        research = kwargs.get("research")
        seen[kwargs["chapter"].id] = research.compact_context() if research else ""
        return await original(**kwargs)

    writer.write_chapter = spy
    result = client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert result.status_code == 200, result.text
    assert seen and all("UPLOADED SOURCE EXCERPTS" in text for text in seen.values())
    assert "[S1." in next(iter(seen.values()))
    # The excerpts were attached for this run only; the cached research is untouched.
    stored = course_service.get_course_service().storage.load_research(course["course_id"], "chapter_1", 1)
    assert not getattr(stored, "source_excerpts", None)


def test_a_chapter_with_no_matching_passage_is_warned_about_not_failed(client):
    uploaded = client.post("/api/sources", files={"file": ("cooking.txt", b"RECIPES\n\nBoil the pasta for ten minutes in salted water.\n", "text/plain")}).json()
    _wait_ready(client, uploaded["source_id"])
    course = client.post("/api/courses", json={
        "course_title": "Quantum field theory", "toc": [{"title": "Lagrangian mechanics"}],
        "target_audience": "Physicists", "template": "technical", "run_planner": False,
        "source_ids": [uploaded["source_id"]],
    }).json()
    result = client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert result.status_code == 200
    assert any("no passage of the uploaded sources matched" in w for w in result.json()["warnings"])
