"""Quality-gate audit follow-up (Phase 2): correct, explicit severity policy
(TASK 1), the persisted audit-trail snapshot (TASK 2), and visual QA finding
persistence (TASK 3).

All offline/mock - no live AI calls, no real course generation beyond the
existing MOCK_OPENAI-backed `service`/`technical_input` fixtures already used
throughout this test suite.
"""

from __future__ import annotations

from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest, QualitySnapshot
from app.schemas.document import CONTENT_HEIGHT, Block, CourseDocument, Page
from app.schemas.draft import GeneratedChapter
from app.services.quality_framework import (
    QualityReport,
    build_course_quality_report,
    quality_gate_evaluation_failed_warning,
    quality_gate_warning,
    quality_snapshot_from_report,
    visual_qa_finding_entries,
)


def _image_block(**content) -> Block:
    # A full-content-height layout so this single block never trips the
    # layout validator's "visual stranded on a mostly-empty page" check
    # (app.course.document.layout_validator.MIN_CONTENT_DENSITY) - these
    # tests are about visual/completeness findings, not layout geometry,
    # which has its own dedicated test coverage elsewhere.
    block = Block(type=BlockType.IMAGE, content=content)
    block.layout.height = CONTENT_HEIGHT
    return block


def _document(blocks: list) -> CourseDocument:
    return CourseDocument(
        document_id="doc_test",
        course_id="crs_test",
        course_title="Test",
        template_id="technical_v1",
        pages=[Page(blocks=blocks)],
    )


def _chapter(review: dict | None = None) -> GeneratedChapter:
    return GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="Test", review=review)


# --- Case 1: a clean course ----------------------------------------------


def test_case_1_a_clean_course_has_no_findings_and_no_warning():
    document = _document([_image_block(path="a.png", generated=True, error=None)])
    report = build_course_quality_report(
        course_id="c1", chapters=[_chapter({"approved": True})], document=document
    )
    assert report.findings == []
    assert report.passed is True
    assert quality_gate_warning(report) is None


# --- Case 2: a minor visual warning (decorative, unverified text) ---------


def test_case_2_a_decorative_unverified_image_is_a_minor_warning_not_a_blocker():
    document = _document(
        [
            _image_block(
                kind="illustration",
                illustration_style="",  # decorative, not "textbook"
                text_verified=False,
                expected_labels=["Some Label"],
                path="a.png",
            )
        ]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.reason == "image_text_unverified"
    assert finding.severity == "minor"
    assert report.passed is True  # a minor warning must never block
    assert quality_gate_warning(report) is None


# --- Case 3: an unverified CRITICAL educational image after bounded retries --


def test_case_3_an_unverified_diagram_is_a_blocker():
    document = _document(
        [
            _image_block(
                kind="diagram",
                text_verified=False,
                expected_labels=["Start", "End"],
                path="a.svg",
            )
        ]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.reason == "image_text_unverified"
    assert finding.severity == "blocker"
    assert report.passed is False
    warning = quality_gate_warning(report)
    assert warning is not None
    assert "quality gate" in warning


def test_case_3b_an_unverified_textbook_illustration_is_also_a_blocker():
    """Educational essentiality isn't only diagrams - a "textbook" style
    scientific/technical illustration is the same real-content promise."""
    document = _document(
        [
            _image_block(
                kind="illustration",
                illustration_style="textbook",
                text_verified=False,
                expected_labels=["Mitochondria"],
                path="a.png",
            )
        ]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert report.findings[0].severity == "blocker"
    assert report.passed is False


# --- Case 4: image-generation failure WITH a valid fallback ----------------


def test_case_4_a_recovered_fallback_produces_no_finding_at_all():
    """Once a fallback succeeds, the final block has error=None/generated=True
    - indistinguishable from an image that never needed a fallback, which is
    the correct, honest final state (the course really did ship a usable
    visual)."""
    document = _document(
        [_image_block(kind="illustration", generated=True, error=None, path="fallback.jpg")]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert report.findings == []
    assert report.passed is True


# --- Case 5: image-generation failure WITHOUT a valid fallback -------------


def test_case_5_a_total_image_failure_is_a_blocker():
    document = _document(
        [_image_block(kind="illustration", generated=False, error="content_safety_violation")]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.reason == "image_generation_failed"
    assert finding.severity == "blocker"
    assert report.passed is False
    assert "quality gate" in quality_gate_warning(report)


# --- Case 6: a quality-evaluation exception --------------------------------


async def test_case_6_a_quality_evaluation_exception_produces_a_visible_warning(
    service, technical_input, monkeypatch
):
    """The real bug this fixes: the old except-block only logged server-side,
    so a course whose quality evaluation itself crashed shipped with ZERO
    quality-related warnings - silently indistinguishable from "evaluated and
    clean". Exercised through the real (mock) generation pipeline, not just
    the pure warning-text helper, to prove the fix is actually wired into
    _finalise."""
    import app.services.course_service as course_service_module

    def _boom(**kwargs):
        raise RuntimeError("synthetic quality-evaluation failure")

    monkeypatch.setattr(course_service_module, "build_course_quality_report", _boom)

    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert any("quality gate" in w and "evaluation failed" in w for w in result.warnings)
    stored = await service.get_course_record(record.course_id)
    assert any("evaluation failed" in w for w in stored.warnings)
    # Advisory only - a quality-evaluation crash must never fail generation
    # itself or invent a new status.
    assert stored.status == "ready"
    # No fabricated outcome when evaluation itself never produced one.
    assert stored.quality_snapshot is None


def test_quality_gate_evaluation_failed_warning_text_is_direct_and_honest():
    warning = quality_gate_evaluation_failed_warning(RuntimeError("boom"))
    assert "quality gate" in warning
    assert "evaluation failed" in warning
    assert "boom" in warning


# --- Case 7: existing behaviour for ordinary reviewer warnings -------------


def test_case_7_an_ordinary_minor_reviewer_issue_still_behaves_as_before():
    review = {
        "issues": [
            {"severity": "minor", "category": "style", "block_index": 0, "description": "nit"}
        ]
    }
    report = build_course_quality_report(course_id="c1", chapters=[_chapter(review)], document=None)
    assert len(report.findings) == 1
    assert report.findings[0].severity == "minor"
    assert report.passed is True
    assert quality_gate_warning(report) is None


def test_case_7b_a_donts_violation_is_still_a_blocker_as_before():
    review = {"donts_violations": ["used marketing language"]}
    report = build_course_quality_report(course_id="c1", chapters=[_chapter(review)], document=None)
    assert report.findings[0].severity == "blocker"
    assert report.passed is False


# --- Case 8: correct final status/warnings, never over-blocking -----------


async def test_case_8_a_critical_visual_failure_sets_a_visible_warning_but_keeps_status_ready(
    service, technical_input
):
    """Status must stay within the EXISTING state machine - "ready" means
    "the pipeline ran and a document exists", not "nothing is wrong". A
    critical finding must be visible (a specific warning), never silently
    absorbed into a clean-looking "ready"."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    # The real mock pipeline's images all succeed cleanly, so inject a real,
    # terminal image failure the same way the live system would persist one,
    # then recompute the report the same way _finalise does - proving the
    # severity policy change (case 5) reaches the real document shape.
    document = await service.load_document(record.course_id)
    image_block = next(b for _p, b in document.iter_blocks() if b.type.value == "image")
    image_block.content["generated"] = False
    image_block.content["error"] = "content_safety_violation"
    chapters = service.storage.load_all_chapters(record.course_id)
    report = build_course_quality_report(
        course_id=record.course_id, document_id=document.document_id, chapters=chapters, document=document
    )
    assert report.passed is False
    warning = quality_gate_warning(report)
    assert warning is not None

    stored = await service.get_course_record(record.course_id)
    assert stored.status == "ready"  # existing state machine, not invented


def test_case_8b_harmless_stylistic_imperfections_do_not_over_block():
    """A clutch of minor findings (style nits, a decorative unverified
    image) must never add up to a blocker on their own."""
    document = _document(
        [
            _image_block(
                kind="illustration", illustration_style="", text_verified=False,
                expected_labels=["x"], path="a.png",
            )
        ]
    )
    review = {
        "issues": [
            {"severity": "minor", "category": "style", "block_index": 0, "description": "nit one"},
            {"severity": "minor", "category": "style", "block_index": 1, "description": "nit two"},
        ]
    }
    report = build_course_quality_report(
        course_id="c1", chapters=[_chapter(review)], document=document
    )
    assert len(report.findings) == 3
    assert all(f.severity == "minor" for f in report.findings)
    assert report.passed is True
    assert quality_gate_warning(report) is None


# --- TASK 2: audit-trail persistence --------------------------------------


def test_quality_snapshot_mirrors_the_report_it_was_built_from():
    report = QualityReport(course_id="c1", document_id="d1")
    snapshot = quality_snapshot_from_report(report)
    assert snapshot.standards_version == report.standards_version
    assert snapshot.standards_status == "proposed"
    assert snapshot.outcome == "passed"
    assert snapshot.hard_failure_count == 0


def test_quality_snapshot_records_a_failed_outcome_without_claiming_approval():
    document = _document([_image_block(kind="diagram", text_verified=False, expected_labels=["x"])])
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    snapshot = quality_snapshot_from_report(report)
    assert snapshot.outcome == "failed"
    assert snapshot.hard_failure_count == 1
    assert snapshot.standards_status == "proposed"  # never upgraded to "approved"


async def test_quality_snapshot_is_persisted_and_survives_a_reload(service, technical_input):
    """TASK 2's actual persistence requirement, exercised end to end: saved
    at finalisation, readable back from a freshly reloaded CourseRecord -
    not just held in the in-memory object _finalise returns."""
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    reloaded = await service.get_course_record(record.course_id)
    assert reloaded.quality_snapshot is not None
    assert isinstance(reloaded.quality_snapshot, QualitySnapshot)
    assert reloaded.quality_snapshot.standards_version
    assert reloaded.quality_snapshot.outcome in ("passed", "failed")


def test_a_course_record_without_a_quality_snapshot_still_validates():
    """Historical documents without this field must remain readable - the
    same backward-compatibility contract every other optional CourseRecord
    field already has."""
    from app.schemas.course import CourseInput, CourseRecord

    record = CourseRecord(
        course_id="crs_old",
        document_id="doc_old",
        input=CourseInput(
            course_title="Old", toc=[{"title": "A"}], target_audience="x", template="technical"
        ),
        template_id="technical_v1",
        created_at="2020-01-01T00:00:00Z",
        updated_at="2020-01-01T00:00:00Z",
    )
    assert record.quality_snapshot is None


def test_the_quality_endpoint_distinguishes_historical_from_live(client):
    """TASK 2's explicit requirement: the endpoint must not conflate a saved
    historical evaluation with a freshly recomputed one."""
    response = client.post(
        "/api/courses",
        json={
            "course_title": "Snapshot Endpoint Test",
            "toc": [{"title": "Chapter One"}],
            "target_audience": "engineers",
            "template": "technical",
        },
    )
    course_id = response.json()["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})

    body = client.get(f"/api/courses/{course_id}/quality").json()
    assert "report" in body and "historical_snapshot" in body
    assert body["historical_snapshot"] is not None
    assert body["historical_snapshot"]["standards_status"] == "proposed"


# --- TASK 3: visual QA finding persistence ---------------------------------


def test_visual_qa_finding_entries_deduplicates_by_type_and_caps_the_count():
    issues = [("edge_crosses_node", f"edge {i}") for i in range(20)] + [
        ("font_below_minimum", "too small")
    ]
    entries = visual_qa_finding_entries(issues, resolution="fell_back_to_illustration", limit=5)
    types = [e["type"] for e in entries]
    assert types == sorted(set(types), key=types.index)  # no duplicate types
    assert len(entries) <= 5
    assert all(e["resolution"] == "fell_back_to_illustration" for e in entries)


def test_visual_qa_finding_entries_is_empty_for_no_issues():
    assert visual_qa_finding_entries([], resolution="fell_back_to_illustration") == []


def test_a_diagram_that_fell_back_produces_an_informational_minor_finding():
    """A diagram that hit rendered-geometry QA issues and fell back to a
    SAFE illustration already shipped something fine - this is transparency
    into what happened, not a current defect, so it stays "minor"."""
    document = _document(
        [
            _image_block(
                kind="illustration",  # the FINAL shipped kind, after fallback
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "edge_crosses_node",
                        "detail": "an edge crossed a node",
                        "resolution": "fell_back_to_illustration",
                    }
                ],
            )
        ]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    finding = next(f for f in report.findings if f.reason.startswith("visual_qa_"))
    assert finding.severity == "minor"
    assert finding.source == "diagram_qa"
    assert report.passed is True


def test_a_concept_visual_that_shipped_with_unresolved_issues_is_a_major_finding():
    """Unlike the diagram path, concept_visual_service has no fallback - an
    unresolved issue here means the FLAWED visual actually shipped, a real
    visible problem, not just transparency - "major", not "minor"."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "generic_visual",
                        "detail": "relationship labels were all identical",
                        "resolution": "rendered_with_unresolved_issues",
                    }
                ],
            )
        ]
    )
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    finding = next(f for f in report.findings if f.reason.startswith("visual_qa_"))
    assert finding.severity == "major"
    assert finding.source == "concept_qa"
    # A major finding alone (no blocker) must not fail the whole report.
    assert report.passed is True


def test_visual_qa_findings_never_invents_a_type_or_detail():
    """TASK 3's own constraint: never reconstruct from arbitrary strings -
    a malformed/non-dict entry is skipped rather than guessed at. Pydantic's
    own `list[dict]` typing already rejects this through normal construction
    (see ImageContent.visual_qa_findings), so this exercises the defensive
    check against content mutated directly after construction instead -
    exactly how test_case_8 above legitimately does it too."""
    document = _document([_image_block(kind="diagram", generated=True, error=None)])
    # Mutate AFTER the document tree is already built and validated, so this
    # exercises the same "content was changed in place, not through Block's
    # own validator" path test_case_8 above legitimately uses.
    document.pages[0].blocks[0].content["visual_qa_findings"] = ["not a dict"]
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert report.findings == []
