"""MOM quality-governance follow-up - the verification-status policy
(Option B) and the admin-approval gate's own predicate.

All offline/mock - pure unit tests against the quality framework's own
functions, plus a few integration tests through the real (mock) pipeline
where a hand-built fixture would risk testing something that doesn't
actually happen in practice (see test_finalization_gate.py's own existing
level-3 fixture for the same reasoning).
"""

from __future__ import annotations

from app.schemas.blocks import BlockType
from app.schemas.document import CONTENT_HEIGHT, Block, CourseDocument, Page
from app.schemas.draft import GeneratedChapter, StructuralFinding
import app.services.quality_framework as qf_module
from app.services.quality_framework import (
    QualityFinding,
    QualityReport,
    build_course_quality_report,
    document_findings,
    inventory_findings,
    research_findings,
    visual_findings,
)
from app.schemas.draft import ChapterInventoryIssue, ChapterResearchIssue


def _image_block(**content) -> Block:
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


# --- QualityFinding default and the approval predicate ---------------------


def test_a_finding_defaults_to_unverified():
    finding = QualityFinding(
        artifact="x", dimension="pedagogy", severity="blocker", reason="r", description="d"
    )
    assert finding.verification_status == "unverified"
    assert finding.verifier is None
    assert finding.verification_error is None


def test_approval_blocking_findings_excludes_unverified_blockers():
    report = QualityReport(
        course_id="c1",
        findings=[
            QualityFinding(
                artifact="a", dimension="pedagogy", severity="blocker", reason="r",
                description="d", verification_status="unverified",
            )
        ],
    )
    assert report.approval_blocking_findings == []
    # hard_failures/passed keep their EXISTING, unchanged meaning - unaffected
    # by verification_status (must not regress the generation-time gate).
    assert report.hard_failures != []
    assert report.passed is False


def test_approval_blocking_findings_includes_independently_verified_blockers():
    finding = QualityFinding(
        artifact="a", dimension="completeness", severity="blocker", reason="r",
        description="d", verification_status="independently_verified",
    )
    report = QualityReport(course_id="c1", findings=[finding])
    assert report.approval_blocking_findings == [finding]


def test_approval_blocking_findings_includes_verification_failed_blockers_fail_closed():
    finding = QualityFinding(
        artifact="a", dimension="document", severity="blocker", reason="r",
        description="d", verification_status="verification_failed", verification_error="boom",
    )
    report = QualityReport(course_id="c1", findings=[finding])
    assert report.approval_blocking_findings == [finding]


def test_approval_blocking_findings_never_includes_major_or_minor_regardless_of_status():
    findings = [
        QualityFinding(
            artifact="a", dimension="visual", severity="major", reason="r", description="d",
            verification_status="independently_verified",
        ),
        QualityFinding(
            artifact="b", dimension="visual", severity="minor", reason="r", description="d",
            verification_status="verification_failed",
        ),
    ]
    report = QualityReport(course_id="c1", findings=findings)
    assert report.approval_blocking_findings == []


# --- chapter_findings: structural (verified) vs reviewer-model (unverified) --


def test_structural_findings_are_independently_verified():
    chapter = GeneratedChapter(
        chapter_id="chapter_1",
        chapter_number=1,
        title="T",
        review={"missing_required_blocks": ["section 'intro'"]},
        structural_findings=[
            StructuralFinding(check="missing_section", description="section 'intro'", dimension="completeness")
        ],
    )
    report = build_course_quality_report(course_id="c1", chapters=[chapter], document=None)
    assert len(report.findings) == 1  # never double-counted against missing_required_blocks
    finding = report.findings[0]
    assert finding.verification_status == "independently_verified"
    assert finding.verifier == "structural_check:missing_section"
    assert finding.severity == "blocker"


def test_ai_only_missing_required_block_text_stays_unverified():
    """Text the reviewer MODEL added itself (not present in
    structural_findings) must never be marked verified."""
    chapter = GeneratedChapter(
        chapter_id="chapter_1",
        chapter_number=1,
        title="T",
        review={"missing_required_blocks": ["the model's own free-text complaint"]},
        structural_findings=[],
    )
    report = build_course_quality_report(course_id="c1", chapters=[chapter], document=None)
    assert len(report.findings) == 1
    assert report.findings[0].verification_status == "unverified"


def test_issues_donts_dos_and_not_approved_are_all_unverified():
    review = {
        "issues": [{"severity": "major", "category": "clarity", "description": "x"}],
        "donts_violations": ["used marketing language"],
        "dos_violations": ["skipped a worked example"],
    }
    chapter = GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="T", review=review)
    report = build_course_quality_report(course_id="c1", chapters=[chapter], document=None)
    assert len(report.findings) == 3
    assert all(f.verification_status == "unverified" for f in report.findings)

    not_approved_chapter = GeneratedChapter(
        chapter_id="chapter_2", chapter_number=2, title="T", review={"approved": False}
    )
    report2 = build_course_quality_report(course_id="c1", chapters=[not_approved_chapter], document=None)
    assert report2.findings[0].reason == "not_approved"
    assert report2.findings[0].verification_status == "unverified"


def test_chapter_with_no_structural_findings_field_still_works_pre_existing_contract():
    """Backward compatibility: a chapter persisted before `structural_findings`
    existed has it default to `[]`, not raise."""
    chapter = GeneratedChapter(
        chapter_id="chapter_1", chapter_number=1, title="T",
        review={"missing_required_blocks": ["section 'intro'"]},
    )
    assert chapter.structural_findings == []
    report = build_course_quality_report(course_id="c1", chapters=[chapter], document=None)
    assert report.findings[0].verification_status == "unverified"


def test_chapter_findings_derivation_failure_is_verification_failed_not_a_crash(monkeypatch):
    """A broken re-derivation must produce ONE verification_failed finding,
    never raise and sink the whole report (QualityEvaluationFailedError is a
    distinct, separate category - see test_chapter_inventory.py)."""

    def _boom(chapter):
        raise RuntimeError("corrupt review payload")

    monkeypatch.setattr(qf_module, "_chapter_findings", _boom)
    chapter = GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="T", review={})
    report = build_course_quality_report(course_id="c1", chapters=[chapter], document=None)
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verification_status == "verification_failed"
    assert finding.verification_error is not None
    assert finding.severity == "blocker"


# --- document_findings: a live verifier that can itself fail transiently ---


def test_document_findings_success_is_independently_verified():
    document = _document([_image_block(path="a.png", generated=True, error=None)])
    findings = document_findings(document)
    assert findings == []  # nothing wrong here - but exercise the success path below


def test_document_findings_failure_is_verification_failed_not_an_exception(monkeypatch):
    def _boom(document):
        raise RuntimeError("document mid-rebuild")

    # document_findings() now calls validate_document_structured() directly
    # (Phase 3.3/3.4: structured layout findings) - patch that real call
    # site, not the legacy validate_document() wrapper it no longer invokes.
    monkeypatch.setattr(qf_module, "validate_document_structured", _boom)
    document = _document([_image_block(path="a.png", generated=True, error=None)])
    findings = document_findings(document)  # must not raise
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "blocker"
    assert finding.verification_status == "verification_failed"
    assert finding.verifier == "layout_validator"
    assert "document mid-rebuild" in finding.verification_error

    # Folded into a whole report the same way - never escalates to a crashed
    # report computation (QualityEvaluationFailedError is a distinct category).
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    assert report.approval_blocking_findings  # blocks approval, fail-closed


# --- document_findings: structured reason codes, severity policy unchanged -
# (Phase 3.3/3.4 - structured layout findings). No severity-policy decision
# was approved as part of this change: every reason code below must still
# map to "major", exactly as before validate_document_structured existed.


def test_all_layout_reason_codes_currently_map_to_major_severity():
    """Direct, exhaustive guard on the severity table itself - flipping any
    of these to "blocker" is an unapproved policy change (see
    QUALITY_STANDARDS_STATUS's own "proposed" status) this pass does not
    make. Every reason code document_findings() can ever produce must be
    present here, held at "major"."""
    assert qf_module._LAYOUT_REASON_SEVERITY == {
        "overlap": "major",
        "page_overflow": "major",
        "orphaned_fragment": "major",
        "stranded_visual_density": "major",
        "content_reordering": "major",
    }


def test_document_findings_classifies_a_genuine_overlap_as_major_with_a_stable_reason_code():
    """End-to-end through document_findings() itself (not just the
    severity table in isolation) - a real, structurally-detected overlap
    must still be "major", classified via LayoutFinding.reason, with the
    PUBLIC QualityFinding.reason staying the pre-existing "layout_defect"
    (never split into per-category values, per this pass's explicit
    backward-compatibility requirement)."""
    a = Block(type=BlockType.PARAGRAPH, content={"text": "x"})
    b = Block(type=BlockType.PARAGRAPH, content={"text": "y"})
    a.layout.y = 100.0
    a.layout.height = 60.0
    b.layout.y = 100.0  # genuinely overlaps a
    document = _document([a, b])

    findings = document_findings(document)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "major"
    assert finding.reason == "layout_defect"
    assert finding.verification_status == "independently_verified"
    assert finding.verifier == "layout_validator"
    assert f"'{a.id}' and '{b.id}'" in finding.description

    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    # "major", independently_verified severity never reaches the approval
    # gate - approval_blocking_findings requires severity == "blocker".
    assert report.approval_blocking_findings == []
    assert report.hard_failures == []


# --- visual_findings: deterministic vs vision-model judgment ----------------


def test_image_generation_failed_is_independently_verified():
    document = _document([_image_block(kind="illustration", generated=False, error="boom")])
    findings = visual_findings(document)
    assert findings[0].reason == "image_generation_failed"
    assert findings[0].verification_status == "independently_verified"


def test_image_text_unverified_stays_unverified_even_when_it_blocks():
    """A vision-model judgment call must never be marked verified, even
    though essential-content unverified text is severity "blocker"."""
    document = _document(
        [_image_block(kind="diagram", text_verified=False, expected_labels=["x"], path="a.svg")]
    )
    findings = visual_findings(document)
    assert findings[0].reason == "image_text_unverified"
    assert findings[0].severity == "blocker"
    assert findings[0].verification_status == "unverified"
    report = build_course_quality_report(course_id="c1", chapters=[], document=document)
    # A hard failure (unchanged existing behaviour) that never blocks APPROVAL
    # on its own (new policy) - it's a real, visible, but unproven finding.
    assert report.hard_failures
    assert report.approval_blocking_findings == []


def test_visual_qa_findings_are_independently_verified_deterministic_checks():
    """Quality-governance follow-up (Phase 3.7): `resolution` alone never
    decides this - the explicit `deterministic` key does (see
    DiagramQAIssue.deterministic / visual_qa_finding_entries). This fixture
    is a genuinely deterministic concept_qa Layer-1 (structural) issue that
    reached `rendered_with_unresolved_issues` (concept_visual_service.py has
    no fallback - it ships the best attempt either way, regardless of which
    layer produced that attempt's issues)."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "generic_visual",
                        "detail": "x",
                        "resolution": "rendered_with_unresolved_issues",
                        "deterministic": True,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].verification_status == "independently_verified"
    assert findings[0].verifier == "concept_qa:generic_visual"


def test_visual_qa_findings_without_a_deterministic_flag_default_to_unverified():
    """The direct counterpart to the test above, and THE regression this
    whole fix exists for: an entry persisted before `deterministic` existed
    (old-format data) at the genuinely ambiguous `rendered_with_unresolved_
    issues` resolution (concept_qa's Layer 2 IS a real LLM call) must never
    default to "independently_verified" - that was the actual mislabeling
    bug this fix corrects. The honest default is "unverified"."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {"type": "generic_visual", "detail": "x", "resolution": "rendered_with_unresolved_issues"}
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].verification_status == "unverified"
    assert findings[0].verifier is None


def test_a_flow_chart_connectivity_finding_is_attributed_to_diagram_service_not_concept_qa():
    """Real, confirmed mislabeling this fixes: `source` used to be guessed
    purely from `resolution`, which assumed "rendered_with_unresolved_issues"
    could only ever come from concept_visual_service's concept_qa layer -
    until app.services.diagram_service's own flow_chart-connectivity-
    exhausted-retry path started persisting a finding at that exact same
    resolution too. Its `type` ("flow_chart_connectivity") must route to
    its real producer, never the wrong subsystem."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "flow_chart_connectivity",
                        "detail": "2 different nodes have no outgoing edge",
                        "resolution": "rendered_with_unresolved_issues",
                        "deterministic": True,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].source == "diagram_service"
    assert findings[0].verifier == "diagram_service:flow_chart_connectivity"
    assert findings[0].verification_status == "independently_verified"
    assert findings[0].severity == "major"  # "rendered_with_unresolved_issues" always ships the flaw


def test_a_diagram_content_qa_finding_is_attributed_to_diagram_content_qa_not_diagram_render_qa():
    """The mirror case on the "fell_back_to_illustration" side: that
    resolution used to mean, unambiguously, a diagram_render_qa geometry
    finding - now app.services.diagram_content_qa's semantic/content-
    relevance check ALSO falls back to illustration and persists a finding
    at that same resolution. Its `type` ("off_topic"/"generic_content"/
    "missing_key_concept") must route to its real producer, and must stay
    `unverified` (an LLM judgment call, never independently verified)."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "off_topic",
                        "detail": "labels describe an unrelated topic",
                        "resolution": "fell_back_to_illustration",
                        "deterministic": False,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].source == "diagram_content_qa"
    assert findings[0].verification_status == "unverified"
    assert findings[0].verifier is None
    assert findings[0].severity == "minor"  # a safe replacement already shipped


def test_a_garbled_diagram_text_finding_is_attributed_to_diagram_content_qa_not_concept_qa():
    """Release-readiness audit follow-up: diagram_content_qa's own two new
    reason codes ("structurally_garbled_text" via the deterministic
    _looks_garbled reuse, "misspelled_or_garbled_text" via the LLM critique)
    must route to diagram_content_qa - NOT to concept_qa, which already uses
    the similarly-named "garbled_text" string (deliberately different, see
    diagram_content_qa.py's own module docstring on why the string had to
    be distinct rather than reused)."""
    document = _document(
        [
            _image_block(
                kind="diagram",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "structurally_garbled_text",
                        "detail": "'Тriggered probe' mixes Latin and Cyrillic letters",
                        "resolution": "fell_back_to_illustration",
                        "deterministic": True,
                    },
                    {
                        "type": "misspelled_or_garbled_text",
                        "detail": "'Triggerted probe' and 'Ambigeous' are misspelled",
                        "resolution": "fell_back_to_illustration",
                        "deterministic": False,
                    },
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert len(findings) == 2
    assert all(f.source == "diagram_content_qa" for f in findings)
    assert findings[0].verification_status == "independently_verified"
    assert findings[0].verifier == "diagram_content_qa:structurally_garbled_text"
    assert findings[1].verification_status == "unverified"
    assert findings[1].verifier is None
    assert all(f.severity == "minor" for f in findings)  # a safe replacement already shipped


def test_a_formula_verification_failure_is_attributed_to_formula_qa_and_is_a_blocker():
    """app.services.formula_qa's own findings route through the same
    `visual_qa_findings` mechanism - `source` must name its real producer.
    Release-readiness audit fix: a confirmed-wrong/malformed EQUATION is a
    factual-correctness defect (not a layout one, unlike "rendered anyway"),
    and formula_qa is 100% deterministic - this must be severity="blocker",
    not "major", or it can never actually block course approval (see
    QualityReport.hard_failures/approval_blocking_findings, both severity
    == "blocker" only) - a real, confirmed gap this fixes (see the matching
    end-to-end test in test_quality_overrides.py proving it actually gates
    approve_course, not just this report-building function in isolation)."""
    document = _document(
        [
            _image_block(
                kind="illustration",
                generated=True,
                error=None,
                formula_expression="F = m / a",
                visual_qa_findings=[
                    {
                        "type": "formula_mismatch",
                        "detail": "uses the same symbols as Newton's second law but divides instead of multiplying",
                        "resolution": "formula_verification_failed",
                        "deterministic": True,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].source == "formula_qa"
    assert findings[0].severity == "blocker"
    assert findings[0].verification_status == "independently_verified"
    assert findings[0].verifier == "formula_qa:formula_mismatch"


def test_a_malformed_formula_finding_is_major_not_a_blocker():
    """Release-readiness audit correction: `malformed_expression` must NOT
    get the same "blocker" treatment as a confirmed `formula_mismatch` -
    formula_qa cannot tell a genuinely broken formula apart from a
    legitimate equation in a notation outside its narrow algebraic grammar
    (confirmed via direct testing: a textbook-correct chemical equation,
    "2 H2 + O2 -> 2 H2O", reports `malformed_expression` exactly like plain
    prose does). Asserting "blocker" here would falsely tell a reviewer a
    possibly-correct chemistry/calculus formula is a confirmed defect,
    purely because this tool doesn't understand that notation - still
    real and visible ("major", never silently "minor"), never an
    automatic approval block based on a classification formula_qa itself
    admits it cannot make reliably."""
    document = _document(
        [
            _image_block(
                kind="illustration",
                generated=True,
                error=None,
                formula_expression="2 H2 + O2 -> 2 H2O",
                visual_qa_findings=[
                    {
                        "type": "malformed_expression",
                        "detail": "must be exactly one equation (one '=' with a non-empty expression on each side)",
                        "resolution": "formula_verification_failed",
                        "deterministic": True,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].source == "formula_qa"
    assert findings[0].severity == "major"  # NOT "blocker" - see this test's own docstring
    assert findings[0].verification_status == "independently_verified"


def test_an_image_relevance_failure_is_attributed_to_image_relevance_qa_and_is_major():
    """app.services.image_service's own relevance-check findings route
    through the same `visual_qa_findings` mechanism - `source` must name
    its real producer, distinct from every other subsystem that already
    writes to this list."""
    document = _document(
        [
            _image_block(
                kind="illustration",
                generated=True,
                error=None,
                visual_qa_findings=[
                    {
                        "type": "off_topic_image",
                        "detail": "depicts an unrelated generic office scene",
                        "resolution": "image_relevance_unverified",
                        "deterministic": False,
                    }
                ],
            )
        ]
    )
    findings = visual_findings(document)
    assert findings[0].source == "image_relevance_qa"
    assert findings[0].severity == "major"
    assert findings[0].verification_status == "unverified"  # an LLM judgment, never independently verified
    assert findings[0].verifier is None


def test_visual_finding_derivation_failure_is_isolated_to_that_block(monkeypatch):
    def _boom(artifact, content):
        raise RuntimeError("malformed image content")

    monkeypatch.setattr(qf_module, "_visual_findings_for_block", _boom)
    document = _document([_image_block(path="a.png", generated=True, error=None)])
    findings = visual_findings(document)
    assert len(findings) == 1
    assert findings[0].verification_status == "verification_failed"
    assert findings[0].severity == "blocker"


# --- inventory_findings: wording must never claim a confirmed defect --------


def test_inventory_finding_wording_for_missing_never_claims_a_confirmed_defect():
    issues = [ChapterInventoryIssue(chapter_id="chapter_3", status="missing")]
    findings = inventory_findings(issues)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.verification_status == "independently_verified"
    assert finding.severity == "blocker"
    assert "could not be evaluated" in finding.description
    assert "defect" not in finding.description.lower()


def test_inventory_finding_wording_for_corrupt_carries_the_real_parse_detail():
    issues = [ChapterInventoryIssue(chapter_id="chapter_4", status="corrupt", detail="Expecting value: line 1")]
    findings = inventory_findings(issues)
    assert "Expecting value: line 1" in findings[0].description
    assert "defect" not in findings[0].description.lower()


def test_build_course_quality_report_with_no_inventory_issues_is_unaffected():
    """Additive/backward-compatible: existing callers that never pass
    inventory_issues get exactly the same findings as before this existed."""
    report = build_course_quality_report(course_id="c1", chapters=[], document=None)
    assert report.findings == []
    report_explicit_empty = build_course_quality_report(
        course_id="c1", chapters=[], document=None, inventory_issues=[]
    )
    assert report_explicit_empty.findings == []


# --- research_findings: citation-presence (Phase 3.8C) ----------------------
# Per-chapter service behaviour (has_research/mode/missing-reference logic)
# lives in test_chapter_research_citations.py - these cover only the pure
# ChapterResearchIssue -> QualityFinding conversion, mirroring the
# inventory_findings tests above.


def test_research_uncited_finding_is_major_accuracy_independently_verified():
    issues = [ChapterResearchIssue(chapter_id="chapter_5", status="uncited")]
    findings = research_findings(issues)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.dimension == "accuracy"
    assert finding.severity == "major"
    assert finding.reason == "chapter_research_uncited"
    assert finding.verification_status == "independently_verified"
    assert finding.verifier == "research_citation_check"


def test_research_unreadable_finding_is_blocker_fail_closed_with_the_real_error():
    issues = [
        ChapterResearchIssue(chapter_id="chapter_6", status="unreadable", detail="Expecting value")
    ]
    findings = research_findings(issues)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.dimension == "accuracy"
    assert finding.severity == "blocker"
    assert finding.reason == "chapter_research_unreadable"
    assert finding.verification_status == "verification_failed"
    assert finding.verification_error == "Expecting value"
    assert "Expecting value" in finding.description


def test_build_course_quality_report_with_no_research_issues_is_unaffected():
    """Additive/backward-compatible, mirroring the inventory_issues guard
    above: existing callers that never pass research_issues get exactly the
    same findings as before this parameter existed."""
    report = build_course_quality_report(course_id="c1", chapters=[], document=None)
    assert report.findings == []
    report_explicit_empty = build_course_quality_report(
        course_id="c1", chapters=[], document=None, research_issues=[]
    )
    assert report_explicit_empty.findings == []


def test_build_course_quality_report_folds_in_research_issues():
    uncited = ChapterResearchIssue(chapter_id="chapter_7", status="uncited")
    unreadable = ChapterResearchIssue(chapter_id="chapter_8", status="unreadable", detail="boom")
    report = build_course_quality_report(
        course_id="c1", chapters=[], document=None, research_issues=[uncited, unreadable]
    )
    assert len(report.findings) == 2
    # "major" never blocks approval or counts as a hard failure.
    uncited_finding = next(f for f in report.findings if f.reason == "chapter_research_uncited")
    assert uncited_finding not in report.hard_failures
    assert uncited_finding not in report.approval_blocking_findings
    # "blocker" + verification_failed is fail-closed: blocks both.
    unreadable_finding = next(f for f in report.findings if f.reason == "chapter_research_unreadable")
    assert unreadable_finding in report.hard_failures
    assert unreadable_finding in report.approval_blocking_findings
