"""MOM Phase 8 - unified, versioned quality framework.

`app.services.quality_framework` normalises findings from the EXISTING
reviewer, layout validator and image-generation state into one shared
`QualityFinding` shape - these tests cover the normalisation logic with
hand-built fixtures (fast, no AI) plus one end-to-end test against a real
(mock) generated course, proving the aggregator reads genuinely persisted
data rather than a fixture shaped to pass.
"""

from __future__ import annotations

from app.schemas.course import GenerateRequest
from app.schemas.draft import GeneratedChapter
from app.services.quality_framework import (
    QUALITY_STANDARDS_STATUS,
    QUALITY_STANDARDS_VERSION,
    QualityReport,
    build_course_quality_report,
    chapter_findings,
)


def _chapter(review: dict) -> GeneratedChapter:
    return GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="Test", review=review)


# --- standards versioning -------------------------------------------------


def test_standards_are_explicitly_proposed_not_approved():
    """A real, explicit MOM requirement: a proposed baseline must stay
    distinguishable from a formally-approved standard."""
    assert QUALITY_STANDARDS_STATUS == "proposed"
    assert QUALITY_STANDARDS_VERSION


# --- chapter_findings: normalising ReviewIssue / missing_required_blocks ------


def test_no_review_produces_no_findings():
    assert chapter_findings(_chapter(review=None)) == []
    assert chapter_findings(_chapter(review={})) == []


def test_an_issue_is_normalised_with_its_own_severity_and_block():
    review = {
        "approved": True,
        "issues": [
            {
                "severity": "major",
                "category": "clarity",
                "block_index": 3,
                "description": "Too wordy.",
                "suggestion": "Tighten it.",
            }
        ],
    }
    findings = chapter_findings(_chapter(review))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.artifact == "chapter:chapter_1#block_3"
    assert finding.severity == "major"
    assert finding.dimension == "pedagogy"  # clarity maps to pedagogy
    assert finding.description == "Too wordy."
    assert finding.remediation == "Tighten it."
    assert finding.source == "reviewer"


def test_missing_required_blocks_are_always_blockers():
    review = {"missing_required_blocks": ["block type 'quiz'"]}
    findings = chapter_findings(_chapter(review))
    assert findings[0].severity == "blocker"
    assert findings[0].dimension == "completeness"


def test_visual_balance_missing_block_maps_to_visual_dimension():
    review = {"missing_required_blocks": ["visual balance: a stretch of 600 words..."]}
    findings = chapter_findings(_chapter(review))
    assert findings[0].dimension == "visual"


def test_content_level_3_missing_block_maps_to_pedagogy_dimension():
    review = {"missing_required_blocks": ["content level 3 (experiential): needs..."]}
    findings = chapter_findings(_chapter(review))
    assert findings[0].dimension == "pedagogy"


def test_section_completeness_missing_block_maps_to_completeness_dimension():
    review = {"missing_required_blocks": ["section completeness: block 2 is a heading..."]}
    findings = chapter_findings(_chapter(review))
    assert findings[0].dimension == "completeness"


def test_donts_violation_is_a_blocker_dos_violation_is_not():
    """Severity must reflect ChapterReview.needs_revision()'s real semantics:
    a Don't violation blocks approval, a Do violation does not (see
    app.schemas.review.ChapterReview.needs_revision)."""
    review = {"donts_violations": ["used marketing language"], "dos_violations": ["skipped an example"]}
    findings = chapter_findings(_chapter(review))
    by_reason = {f.reason: f for f in findings}
    assert by_reason["donts_violation"].severity == "blocker"
    assert by_reason["dos_violation"].severity == "major"


def test_unapproved_with_no_other_signal_still_produces_a_finding():
    review = {"approved": False, "summary": "Just not good enough."}
    findings = chapter_findings(_chapter(review))
    assert len(findings) == 1
    assert findings[0].severity == "blocker"
    assert findings[0].reason == "not_approved"


def test_a_clean_approved_review_produces_no_findings():
    review = {"approved": True, "issues": [], "missing_required_blocks": []}
    assert chapter_findings(_chapter(review)) == []


# --- QualityReport aggregation -------------------------------------------


def test_report_passed_is_false_when_any_blocker_exists():
    report = QualityReport(
        course_id="c1",
        findings=list(
            chapter_findings(_chapter({"missing_required_blocks": ["block type 'quiz'"]}))
        ),
    )
    assert report.passed is False
    assert len(report.hard_failures) == 1
    assert len(report.warnings) == 0


def test_report_passed_is_true_with_only_warnings():
    review = {
        "issues": [
            {"severity": "minor", "category": "style", "block_index": 0, "description": "nit"}
        ]
    }
    report = QualityReport(course_id="c1", findings=chapter_findings(_chapter(review)))
    assert report.passed is True
    assert len(report.warnings) == 1


def test_by_dimension_groups_across_all_five_dimensions():
    report = QualityReport(course_id="c1")
    grouped = report.by_dimension()
    assert set(grouped.keys()) == {"accuracy", "completeness", "pedagogy", "visual", "document"}


# --- end-to-end: real (mock) generated course, not a hand-built fixture -------


async def test_build_report_from_a_real_generated_course(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    chapters = service.storage.load_all_chapters(record.course_id)
    document = await service.load_document(record.course_id)
    report = build_course_quality_report(
        course_id=record.course_id,
        document_id=document.document_id,
        chapters=chapters,
        document=document,
    )

    assert report.course_id == record.course_id
    assert report.standards_status == "proposed"
    # Every finding must carry a real, locatable artifact and a reason - the
    # minimum MOM Phase 8 requires ("affected artifact + severity + reason +
    # remediation").
    for finding in report.findings:
        assert finding.artifact
        assert finding.dimension in {"accuracy", "completeness", "pedagogy", "visual", "document"}
        assert finding.reason
