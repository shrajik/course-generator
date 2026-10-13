"""MOM quality-governance follow-up - citation-presence verification
(Phase 3.8C).

Deterministic, chapter-level check: does a chapter's persisted research
artifact record at least one non-blank reference URL? A non-blank URL is
only evidence a reference was recorded - never proof a claim is true or
reliably sourced (see ChapterResearchIssue and
app.services.quality_framework.research_findings's own docstrings).

These tests exercise `CourseService._verify_research_citations` directly
against hand-built research artifacts on disk (mirroring
test_chapter_inventory.py's approach for the sibling chapter-inventory
check), plus the two wiring points (`_evaluate_quality_for_approval` via
`get_quality_report`, and the generation-time `_finalise` isolation
guarantee). All offline/mock - no live AI calls (MOCK_OPENAI=true,
USE_DATABASE=false, see conftest.py).
"""

from __future__ import annotations

from app.schemas.course import GenerateRequest
from app.schemas.draft import GeneratedChapter
from app.schemas.research import ChapterResearch, Reference, ResearchPayload
import app.services.quality_framework as qf_module


def _chapter(chapter_id: str, chapter_number: int = 1) -> GeneratedChapter:
    return GeneratedChapter(chapter_id=chapter_id, chapter_number=chapter_number, title="T")


def _research(
    chapter_id: str, *, mode: str = "fast", urls: list[str] | None = None
) -> ChapterResearch:
    references = [Reference(url=url) for url in (urls or [])]
    return ChapterResearch(chapter_id=chapter_id, mode=mode, payload=ResearchPayload(references=references))


# --- _verify_research_citations: per-chapter classification -----------------


def test_a_genuinely_missing_research_artifact_is_skipped_not_flagged(service):
    chapters = [_chapter("chapter_1")]
    issues = service._verify_research_citations("crs_test", chapters)
    assert issues == []


def test_a_cited_fast_mode_artifact_has_no_issue(service):
    service.storage.save_research("crs_test", _research("chapter_1", urls=["https://example.com/a"]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert issues == []


def test_an_uncited_fast_mode_artifact_is_flagged(service):
    service.storage.save_research("crs_test", _research("chapter_1", urls=[]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert len(issues) == 1
    assert issues[0].chapter_id == "chapter_1"
    assert issues[0].status == "uncited"
    assert issues[0].detail == ""


def test_a_whitespace_only_url_counts_as_blank(service):
    service.storage.save_research("crs_test", _research("chapter_1", urls=["   "]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert len(issues) == 1
    assert issues[0].status == "uncited"


def test_at_least_one_real_url_among_several_blank_ones_is_sufficient(service):
    service.storage.save_research("crs_test", _research("chapter_1", urls=["", "https://example.com/b"]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert issues == []


def test_a_successfully_loaded_mock_mode_artifact_is_exempt_even_when_uncited(service):
    service.storage.save_research("crs_test", _research("chapter_1", mode="mock", urls=[]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert issues == []


def test_a_mock_mode_artifact_with_a_citation_is_also_exempt(service):
    """The mock exemption is keyed on `mode`, never on citation presence -
    this guards against an implementation that accidentally only exempts
    mock research when it ALSO happens to be uncited."""
    service.storage.save_research("crs_test", _research("chapter_1", mode="mock", urls=["https://x"]), 1)
    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert issues == []


def test_an_existing_but_corrupt_research_artifact_fails_closed_as_unreadable(service):
    path = service.storage.research_path("crs_test", "chapter_1", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert len(issues) == 1
    assert issues[0].chapter_id == "chapter_1"
    assert issues[0].status == "unreadable"
    assert issues[0].detail  # the real parse-failure message, never fabricated


def test_an_artifact_failing_schema_validation_also_fails_closed_as_unreadable(service):
    """pydantic.ValidationError is a ValueError subclass - covered by the
    same narrow except clause as a JSON parse failure, never a separate,
    broader `except Exception`."""
    path = service.storage.research_path("crs_test", "chapter_1", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON, but `chapter_id` (required, no default) is absent.
    path.write_text('{"mode": "fast"}', encoding="utf-8")

    issues = service._verify_research_citations("crs_test", [_chapter("chapter_1")])
    assert len(issues) == 1
    assert issues[0].status == "unreadable"
    assert issues[0].detail


def test_multiple_chapters_are_classified_independently(service):
    service.storage.save_research("crs_test", _research("chapter_1", urls=["https://x"]), 1)
    service.storage.save_research("crs_test", _research("chapter_2", urls=[]), 2)
    # chapter_3: genuinely missing - skipped.
    corrupt_path = service.storage.research_path("crs_test", "chapter_4", 4)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{bad", encoding="utf-8")

    chapters = [_chapter("chapter_1", 1), _chapter("chapter_2", 2), _chapter("chapter_3", 3), _chapter("chapter_4", 4)]
    issues = service._verify_research_citations("crs_test", chapters)

    by_id = {i.chapter_id: i.status for i in issues}
    assert by_id == {"chapter_2": "uncited", "chapter_4": "unreadable"}


# --- wiring: get_quality_report (on-demand only) -----------------------------


async def test_get_quality_report_surfaces_an_uncited_chapter(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    generated = service.storage.load_all_chapters(record.course_id)
    target_chapter = generated[0]
    target = target_chapter.chapter_id
    chapter_number = target_chapter.chapter_number
    # The mock pipeline's own research is mode="mock" (exempt) - overwrite it
    # with a real-mode, uncited artifact to exercise the actual check this
    # phase adds, the same hand-edit-after-generation technique
    # test_chapter_inventory.py already uses for its own fixtures.
    service.storage.save_research(record.course_id, _research(target, mode="fast", urls=[]), chapter_number)

    report = await service.get_quality_report(record.course_id)
    uncited = [f for f in report.findings if f.reason == "chapter_research_uncited"]
    assert len(uncited) == 1
    assert uncited[0].artifact == f"chapter:{target}"
    assert uncited[0].severity == "major"
    assert uncited[0].verification_status == "independently_verified"


async def test_get_quality_report_is_unaffected_when_research_is_cited_or_mock(
    service, technical_input
):
    """Regression guard: the mock pipeline's own research (mode="mock",
    always carrying a reference - see app.services.mock_ai) must never be
    flagged, so a freshly generated course's report has zero research
    findings."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    report = await service.get_quality_report(record.course_id)
    assert not any(f.source == "research" for f in report.findings)


# --- isolation: citation findings never reach generation-time warnings ------


async def test_finalise_never_calls_research_findings(service, technical_input, monkeypatch):
    """Locked requirement: citation findings are visible ONLY in the
    on-demand quality report - `_finalise` must never compute or surface
    them. Proven behaviourally (not just by reading the call site): if
    `_finalise`'s own `build_course_quality_report` call ever started
    passing `research_issues`, `research_findings` would be invoked during
    plain generation - it must not be."""
    calls: list[object] = []
    original = qf_module.research_findings

    def _tracking(issues):
        calls.append(issues)
        return original(issues)

    monkeypatch.setattr(qf_module, "research_findings", _tracking)

    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert result.status == "ready"
    assert calls == []


async def test_generation_time_warnings_never_mention_research_citations(service, technical_input):
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    stored = await service.get_course_record(record.course_id)
    assert not any("research" in (w or "").lower() for w in (stored.warnings or []))
    assert result.status == "ready"
