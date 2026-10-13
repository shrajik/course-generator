"""Unified, versioned quality framework.

MOM Phase 8 - a single place that normalises findings from every quality
check already in the pipeline (ReviewerAgent's AI+deterministic chapter
review - including the Phase 4/7 content-level-3 and section-completeness
structural checks it already runs - the document layout validator, and the
per-image generation/text-verification signals already persisted on each
image block) into one common shape, `QualityFinding`, instead of a caller
needing to know three or four different result shapes (`ReviewIssue`, plain
layout-validator strings, `ImageContent` fields) to answer "is this course
good, and specifically what is wrong with it?"

Explicitly NOT a new quality-checking engine: every finding here is produced
by an EXISTING, already-tested check (app.agents.reviewer,
app.course.document.layout_validator) re-read from already-PERSISTED data
(`GeneratedChapter.review`, the built `CourseDocument`) - this module adds
aggregation and a shared vocabulary on top, never a competing pipeline.

Known, explicit gap (documented rather than silently glossed over): the
diagram/concept-visual QA systems (app.services.diagram_qa,
app.services.concept_qa) run DURING generation as a retry gate, but their
per-attempt issue lists are never persisted anywhere on the document - only
each image's final `generated`/`error`/`text_verified` state survives. This
module reports on that persisted state (a failed-to-generate or
text-verification-failed image IS a real, available finding) but cannot
reconstruct the full QA issue history a diagram went through during
generation. Making those systems persist their own results is out of this
pass's scope.

Never treat `accuracy`-dimension findings as proof of fact: they come from
the reviewer MODEL's judgement (an LLM call), not from any external
verification - see REVIEWER_SYSTEM's own scoring instructions in
app.agents.prompts. A clean accuracy dimension means the model found nothing
wrong, not that the content has been fact-checked.

Standards versioning: `QUALITY_STANDARDS_VERSION` names the specific set of
thresholds and dimension mappings this module encodes. Every threshold it
reads from (e.g. reviewer._MAX_TEXT_ONLY_RUN_WORDS, layout_validator's
MIN_CONTENT_DENSITY) was already a real, shipped default before this module
existed - nothing here invents a new number, only aggregates and labels
what already runs. `QUALITY_STANDARDS_STATUS` is deliberately "proposed", not
"approved" - the MOM explicitly requires keeping a proposed baseline
distinct from a formally-approved standard, and no stakeholder has signed
off on these specific thresholds as a hard policy. Flip it to "approved"
only when that sign-off actually happens; nothing in this module should be
read as having done that on its own authority.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.document.layout_validator import validate_document
from app.schemas.blocks import BlockType
from app.schemas.course import QualitySnapshot
from app.schemas.document import CourseDocument
from app.schemas.draft import ChapterInventoryIssue, GeneratedChapter

log = get_logger(__name__)

# The three-state verification contract (MOM quality-governance follow-up):
# a finding is never treated as "proven true" merely because a model wrote
# it. Only code that ACTUALLY performed an independent, deterministic check
# may set "independently_verified"; a verifier that itself errored sets
# "verification_failed" (fail-closed: still blocks a "blocker"-severity
# finding, but never implies the underlying claim is true); everything
# else - in particular every reviewer-LLM finding, with NO exception for
# category/dimension/text content - defaults to, and stays, "unverified".
# Never inferred from `source`, `dimension`, severity, or finding text.
VerificationStatus = Literal["unverified", "independently_verified", "verification_failed"]

QUALITY_STANDARDS_VERSION = "2026.1"
# "proposed" | "approved" - see module docstring. Keep this honest: nothing
# in this codebase currently records a stakeholder sign-off, so it starts
# (and should stay) "proposed" until that genuinely happens elsewhere.
QUALITY_STANDARDS_STATUS = "proposed"

# The five dimensions the MOM's quality framework requirement names.
QUALITY_DIMENSIONS = ("accuracy", "completeness", "pedagogy", "visual", "document")

_CATEGORY_TO_DIMENSION: dict[str, str] = {
    "accuracy": "accuracy",
    "technical_correctness": "accuracy",
    "code": "accuracy",
    "completeness": "completeness",
    "template": "completeness",
    "pedagogy": "pedagogy",
    "chunk_size": "pedagogy",
    "style": "pedagogy",
    "clarity": "pedagogy",
    "structure": "pedagogy",
    "repetition": "pedagogy",
    "dos": "pedagogy",
    "donts": "pedagogy",
    "audience_fit": "pedagogy",
    "logical_flow": "pedagogy",
    "visual": "visual",
}


def _dimension_for_category(category: str) -> str:
    return _CATEGORY_TO_DIMENSION.get((category or "").strip().lower(), "pedagogy")


def _dimension_for_missing_block(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("visual balance"):
        return "visual"
    if lowered.startswith("content level 3"):
        return "pedagogy"
    if lowered.startswith("section completeness"):
        return "completeness"
    return "completeness"  # a missing required template block/section is a completeness gap


# TASK 3 (quality audit follow-up) - a shared, minimal hook for persisting
# the REAL structured QA issues app.services.diagram_service and
# app.services.concept_visual_service already compute during generation but
# previously only logged. Both call this at their own fallback/acceptance
# decision point, writing the result onto block.content["visual_qa_findings"]
# (see ImageContent in app.schemas.blocks) - never reconstructed from log
# text, always the real (type, detail) pairs from RenderQAIssue/DiagramQAIssue.
#
# Deliberately NOT exhaustive: only the ONE dominant fallback/accept point in
# each service is wired (the rendered-geometry QA failure in diagram_service,
# the post-retry concept QA result in concept_visual_service). Schematic's
# own dedicated QA (evaluate_blueprint/evaluate_schematic) and the diagram
# "unusable spec"/connectivity/consolidation/kind-correction retry paths are
# NOT captured by this pass - a documented limitation, not an oversight; see
# the module docstring's existing note on diagram/concept QA persistence.
def visual_qa_finding_entries(
    issues: list[tuple[str, str]], *, resolution: str, limit: int = 5
) -> list[dict]:
    """Bounded, deduplicated-by-type summary of real structured QA issues.

    `issues` is `[(type, detail), ...]`, taken directly from the real QA
    result object's own fields - a diagram can report the same issue type
    dozens of times across many edges (e.g. twenty `edge_crosses_node`
    entries), so one representative detail per distinct type is the useful
    signal, not every repetition; capped at `limit` distinct types so this
    never grows unbounded."""
    seen: dict[str, str] = {}
    for issue_type, detail in issues:
        if issue_type not in seen and len(seen) < limit:
            seen[issue_type] = detail
    return [
        {"type": issue_type, "detail": detail[:200], "resolution": resolution}
        for issue_type, detail in seen.items()
    ]


class QualityFinding(BaseModel):
    """One normalised finding, regardless of which subsystem produced it.

    `artifact` identifies what the finding is about in a human-readable,
    stable way (e.g. "chapter:chapter_2#block_5", "document:page_3",
    "image:blk_abc123") - always specific enough to locate, never just
    "the course"."""

    model_config = ConfigDict(extra="forbid")

    artifact: str
    dimension: str  # one of QUALITY_DIMENSIONS
    severity: str  # "blocker" | "major" | "minor"
    reason: str  # a short, stable category/code - not free prose
    description: str
    remediation: str = ""
    source: str = ""  # which subsystem produced this: "reviewer" | "layout_validator" | "image"
    # Additive (MOM quality-governance follow-up) - see VerificationStatus
    # above for the contract. Every finding constructor in this module sets
    # this EXPLICITLY (never left to infer from the other fields).
    verification_status: VerificationStatus = "unverified"
    verifier: str | None = None  # which deterministic check/mechanism set verification_status
    verified_at: str | None = None  # when verification_status was last determined
    # Populated ONLY for "verification_failed" - the real error from the
    # verifier itself, never a claim about the underlying finding.
    verification_error: str | None = None


def chapter_findings(chapter: GeneratedChapter) -> list[QualityFinding]:
    """Findings re-derived from a persisted chapter's own stored review.

    `GeneratedChapter.review` is a plain dict (see app.schemas.draft) rather
    than a typed `ChapterReview`, because it is written once by the pipeline
    and read generically thereafter - this re-validates it defensively
    (`.get` everywhere) so a chapter from before a given review field existed
    still produces a (shorter, honest) finding list instead of raising.

    Verification policy (MOM quality-governance follow-up, Option B applied
    UNIFORMLY): every finding sourced from the reviewer MODEL's own output -
    `issues`, `donts_violations`, `dos_violations`, the `not_approved`
    fallback, and whatever part of `missing_required_blocks` the model
    contributed itself - is "unverified": a real, visible finding, but
    never independently proven. The ONLY findings this function marks
    "independently_verified" are the ones in `chapter.structural_findings`
    (app.agents.reviewer._apply_structural_checks's deterministic output,
    never an AI-facing schema target - see StructuralFinding's own
    docstring) - each scoped to exactly the narrow claim its own check
    establishes, nothing broader. A chapter from before `structural_findings`
    existed simply has an empty list here, same backward-compatibility
    contract as every other additive field."""
    try:
        return _chapter_findings(chapter)
    except Exception as exc:  # noqa: BLE001 - a broken re-derivation must not crash the whole report
        log.warning("Chapter finding derivation failed for %s: %s", chapter.chapter_id, exc)
        return [
            QualityFinding(
                artifact=f"chapter:{chapter.chapter_id}",
                dimension="completeness",
                severity="blocker",
                reason="chapter_findings_verification_failed",
                description=(
                    f"This chapter's stored review/structural data could not be read to "
                    f"determine its quality findings: {exc}"
                ),
                remediation="Investigate the persisted chapter artifact; it may be malformed.",
                source="reviewer",
                verification_status="verification_failed",
                verifier="chapter_findings",
                verification_error=str(exc)[:500],
            )
        ]


def _chapter_findings(chapter: GeneratedChapter) -> list[QualityFinding]:
    review = chapter.review or {}
    findings: list[QualityFinding] = []
    label = f"chapter:{chapter.chapter_id}"

    for issue in review.get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "minor")
        category = str(issue.get("category") or "")
        block_index = issue.get("block_index", -1)
        artifact = f"{label}#block_{block_index}" if block_index is not None and block_index >= 0 else label
        findings.append(
            QualityFinding(
                artifact=artifact,
                dimension=_dimension_for_category(category),
                severity=severity if severity in ("blocker", "major", "minor") else "minor",
                reason=category or "review_issue",
                description=str(issue.get("description") or ""),
                remediation=str(issue.get("suggestion") or ""),
                source="reviewer",
                verification_status="unverified",
            )
        )

    # The deterministic subset, re-surfaced from its own typed, non-AI-facing
    # field (see StructuralFinding's docstring for exactly why this is safe
    # to mark verified while the model's own self-reported text, below,
    # never is).
    structural_texts = {f.description for f in chapter.structural_findings}
    for finding in chapter.structural_findings:
        # Every PRE-EXISTING check leaves these overrides unset, so this is
        # byte-identical to before for them - see StructuralFinding's own
        # docstring for why a NEW check (sandboxed code-cell execution) needs
        # to be able to self-report a different verification_status/severity/
        # reason than "a confirmed content defect" (it can itself fail to
        # run, or be deliberately skipped by a budget cap - neither is that).
        findings.append(
            QualityFinding(
                artifact=label,
                dimension=finding.dimension,
                severity=finding.severity or "blocker",
                reason=finding.reason or "missing_required_block",
                description=finding.description,
                remediation="Add the missing block/section, or (for content level 3 / section "
                "completeness) the specific content named above, then re-review.",
                source="reviewer",
                verification_status=finding.verification_status or "independently_verified",
                verifier=finding.verifier or f"structural_check:{finding.check}",
                verified_at=chapter.generated_at or None,
                verification_error=finding.verification_error,
            )
        )

    for missing in review.get("missing_required_blocks", []) or []:
        text = str(missing)
        if text in structural_texts:
            continue  # already emitted above as independently_verified - never double-counted
        findings.append(
            QualityFinding(
                artifact=label,
                dimension=_dimension_for_missing_block(text),
                severity="blocker",
                reason="missing_required_block",
                description=text,
                remediation="Add the missing block/section, or (for content level 3 / section "
                "completeness) the specific content named above, then re-review.",
                source="reviewer",
                verification_status="unverified",
            )
        )

    for violation in review.get("donts_violations", []) or []:
        findings.append(
            QualityFinding(
                artifact=label,
                dimension="pedagogy",
                severity="blocker",
                reason="donts_violation",
                description=str(violation),
                remediation="Revise the chapter so it no longer violates this Don't.",
                source="reviewer",
                verification_status="unverified",
            )
        )

    for violation in review.get("dos_violations", []) or []:
        # Not itself a `needs_revision()` trigger (see ChapterReview.needs_revision
        # in app.schemas.review) - reported as "major", not "blocker", so this
        # framework's severities stay honest about what actually blocks approval.
        findings.append(
            QualityFinding(
                artifact=label,
                dimension="pedagogy",
                severity="major",
                reason="dos_violation",
                description=str(violation),
                remediation="Revise the chapter to honour this Do.",
                source="reviewer",
                verification_status="unverified",
            )
        )

    if review.get("approved") is False and not findings:
        # The model disapproved without tripping any of the structured
        # channels above (e.g. only via its own free-text `summary`) - still
        # a real finding, just without a more specific reason to report.
        findings.append(
            QualityFinding(
                artifact=label,
                dimension="pedagogy",
                severity="blocker",
                reason="not_approved",
                description=str(review.get("summary") or "Reviewer did not approve this chapter."),
                source="reviewer",
                verification_status="unverified",
            )
        )

    return findings


# A block whose text/structure genuinely needs to be correct for the
# learner to understand the right thing - as opposed to a decorative
# illustration, where a garbled label is a cosmetic flaw, not a correctness
# problem. Both signals already exist on ImageContent for other reasons
# (`kind` picks the diagram-vs-illustration renderer; `illustration_style`
# distinguishes a real technical/scientific illustration from a flat
# decorative one - see its own field docstring) - reused here, not invented.
def _is_educationally_essential(content: dict) -> bool:
    return content.get("kind") == "diagram" or content.get("illustration_style") == "textbook"


def visual_findings(document: CourseDocument) -> list[QualityFinding]:
    """Findings from each image block's own persisted, already-checked
    state - not a re-run of any QA, just surfacing what ImageService and
    the text-verification check (see app.schemas.blocks.ImageContent's own
    `text_verified` docstring) already recorded at generation time.

    Severity policy (TASK 1 of the quality-gate audit follow-up):
    - `image_generation_failed` is always "blocker" - every repair/fallback
      this pipeline has (diagram -> raster illustration, bounded text-check
      retries - see ImageService) has already run by the time this is read
      in _finalise, so an `error` still set here means every one of them was
      exhausted: no asset exists for a block the course asked for, which
      must never be indistinguishable from a clean course.
    - `image_text_unverified` is "blocker" only when the image was carrying
      real structured educational content (a diagram, or a "textbook"
      scientific/technical illustration - see `_is_educationally_essential`)
      - for a purely decorative illustration, the same unverified state
      stays "minor": a harmless stylistic imperfection, not something that
      should block a course over a false-positive-prone text check."""
    findings: list[QualityFinding] = []
    for _page, block in document.iter_blocks():
        if block.type is not BlockType.IMAGE:
            continue
        artifact = f"image:{block.id}"
        try:
            findings.extend(_visual_findings_for_block(artifact, block.content))
        except Exception as exc:  # noqa: BLE001 - one block's malformed data must not sink the report
            log.warning("Visual finding derivation failed for %s: %s", artifact, exc)
            findings.append(
                QualityFinding(
                    artifact=artifact,
                    dimension="visual",
                    severity="blocker",
                    reason="visual_verification_failed",
                    description=f"This image's stored QA/verification data could not be read: {exc}",
                    remediation="Investigate the persisted image block; its content may be malformed.",
                    source="image",
                    verification_status="verification_failed",
                    verifier="visual_findings",
                    verification_error=str(exc)[:500],
                )
            )
    return findings


def _visual_findings_for_block(artifact: str, content: dict) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    error = content.get("error")
    if error:
        # Deterministic, operational fact: every repair/fallback this
        # pipeline has already ran by the time this is read (see
        # ImageService) - an `error` still set here means no asset exists,
        # which is directly checkable, not a judgment call.
        findings.append(
            QualityFinding(
                artifact=artifact,
                dimension="visual",
                severity="blocker",
                reason="image_generation_failed",
                description=f"Image generation failed with no usable fallback: {error}",
                remediation="Regenerate this image, or replace it with a different visual.",
                source="image",
                verification_status="independently_verified",
                verifier="image_service:generation_failed",
            )
        )
    if content.get("text_verified") is False:
        essential = _is_educationally_essential(content)
        findings.append(
            QualityFinding(
                artifact=artifact,
                dimension="visual",
                severity="blocker" if essential else "minor",
                reason="image_text_unverified",
                description=(
                    ("This image carries required educational content whose baked-in text "
                     if essential else "This image's baked-in text ")
                    + "could not be verified as matching its expected labels "
                    f"({', '.join(content.get('expected_labels', []))})."
                ),
                remediation="Regenerate with a text-free illustration style, or verify the "
                "rendered text manually.",
                source="image",
                # A vision-model judgment call, not a deterministic check -
                # must never be marked verified, regardless of severity.
                verification_status="unverified",
            )
        )
    for entry in content.get("visual_qa_findings") or []:
        if not isinstance(entry, dict):
            continue
        resolution = str(entry.get("resolution") or "")
        # "Fell back to illustration" means a SAFE replacement already
        # shipped (see diagram_service.py) - informational, not a
        # current defect. "Rendered with unresolved issues" means the
        # flawed structure shipped anyway (concept_visual_service.py has
        # no fallback) - a real, visible quality problem.
        severity = "major" if resolution == "rendered_with_unresolved_issues" else "minor"
        source = "diagram_qa" if resolution == "fell_back_to_illustration" else "concept_qa"
        findings.append(
            QualityFinding(
                artifact=artifact,
                dimension="visual",
                severity=severity,
                reason=f"visual_qa_{entry.get('type', 'unknown')}",
                description=str(entry.get("detail") or ""),
                remediation="Review this visual's structure and content for the issue noted.",
                source=source,
                # The underlying diagram/concept-visual QA checks are real
                # structural/geometric Python logic (edge crossings,
                # property-key overlap, etc.), never an LLM judgment call -
                # see app.services.diagram_render_qa / concept_qa.
                verification_status="independently_verified",
                verifier=f"{source}:{entry.get('type', 'unknown')}",
            )
        )
    return findings


def document_findings(document: CourseDocument) -> list[QualityFinding]:
    """Wraps `app.course.document.layout_validator.validate_document` -
    plain-string findings keyed by page number, normalised into the same
    shape as every other dimension's findings. All layout findings are
    "major": they indicate a rendering defect (clipped/overlapping content,
    an orphaned page) a learner would actually see, but never block the
    course from existing the way a missing required block does.

    `validate_document` is a live call (recomputed on every report - see
    `get_quality_report`'s own docstring) - a genuine verifier that can
    itself fail transiently (e.g. a document mid-rebuild). A crash here is
    reported as ONE "verification_failed" finding (fail-closed: severity
    "blocker", so it still blocks approval until a fresh recomputation
    succeeds or an admin override) rather than propagating and aborting the
    whole report - a transient layout-check failure is a different, more
    localized category of problem than the entire report computation
    crashing (see QualityEvaluationFailedError)."""
    try:
        problems = validate_document(document)
    except Exception as exc:  # noqa: BLE001 - localize to the document dimension
        log.warning("Document layout verification failed for %s: %s", document.document_id, exc)
        return [
            QualityFinding(
                artifact="document",
                dimension="document",
                severity="blocker",
                reason="layout_verification_failed",
                description=f"Document layout could not be verified: {exc}",
                remediation="Re-run pagination/layout, or retry once the document finishes "
                "rebuilding, then re-evaluate.",
                source="layout_validator",
                verification_status="verification_failed",
                verifier="layout_validator",
                verification_error=str(exc)[:500],
            )
        ]

    from app.course.document.layout_validator import DOCUMENT_LEVEL

    findings: list[QualityFinding] = []
    for page_number, texts in problems.items():
        artifact = "document" if page_number == DOCUMENT_LEVEL else f"document:page_{page_number}"
        for text in texts:
            findings.append(
                QualityFinding(
                    artifact=artifact,
                    dimension="document",
                    severity="major",
                    reason="layout_defect",
                    description=text,
                    remediation="Re-run pagination/layout for this document, or adjust the "
                    "offending block's content so it fits its page.",
                    source="layout_validator",
                    # Pure geometry/structure checks (re.py + schema types
                    # only, no AI dependency) - see layout_validator's own
                    # module docstring.
                    verification_status="independently_verified",
                    verifier="layout_validator",
                )
            )
    return findings


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    document_id: str = ""
    standards_version: str = QUALITY_STANDARDS_VERSION
    standards_status: str = QUALITY_STANDARDS_STATUS
    generated_at: str = Field(default_factory=utc_now_iso)
    findings: list[QualityFinding] = Field(default_factory=list)

    @property
    def hard_failures(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def warnings(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.severity != "blocker"]

    @property
    def passed(self) -> bool:
        """No hard failures. NOT a claim of factual correctness or of
        publication-readiness on its own - see `build_course_quality_report`'s
        caller (the finalisation gate, MOM Phase 9) for the actual gating
        decision, which also checks things this report cannot see (e.g.
        whether generation itself completed for every chapter)."""
        return not self.hard_failures

    @property
    def approval_blocking_findings(self) -> list[QualityFinding]:
        """The admin-approval gate's own predicate (MOM quality-governance
        follow-up) - deliberately NARROWER than `hard_failures`/`passed`
        above, which keep their existing, unchanged meaning (the
        generation-time advisory warning and the persisted
        `quality_snapshot`, both already-tested behaviour this must not
        regress): `severity == "blocker" AND verification_status !=
        "unverified"`. A merely-unverified reviewer-model opinion - however
        severe it claims to be - never blocks approval on its own; a
        confirmed (`independently_verified`) or uncertain
        (`verification_failed`, fail-closed) blocker always does."""
        return [
            f
            for f in self.findings
            if f.severity == "blocker" and f.verification_status != "unverified"
        ]

    def by_dimension(self) -> dict[str, list[QualityFinding]]:
        grouped: dict[str, list[QualityFinding]] = {d: [] for d in QUALITY_DIMENSIONS}
        for finding in self.findings:
            grouped.setdefault(finding.dimension, []).append(finding)
        return grouped


def quality_snapshot_from_report(report: QualityReport) -> QualitySnapshot:
    """Builds the audit-trail record persisted on `CourseRecord.quality_snapshot`
    at the moment `_finalise` evaluates a course (TASK 2, quality audit
    follow-up) - distinct from the LIVE `QualityReport` `get_quality_report()`
    recomputes on every call (which always reflects the course's CURRENT
    state under the CURRENT code's standards). This snapshot answers "what
    standards version evaluated this course, and what did it conclude, when
    it finished generating" - a question the live report alone can't answer
    once `QUALITY_STANDARDS_VERSION` moves on or the course is hand-edited.

    `outcome` describes whether THIS COURSE passed the (proposed) standards
    at evaluation time - never a claim that the standards themselves are
    approved; `standards_status` is carried through unmodified for that
    reason, never upgraded to "approved" here. `QualitySnapshot` itself lives
    in app.schemas.course (not here) so that schema module - which nothing
    in app/services depends on - can reference it without an app/schemas ->
    app/services import, which no other schema module does either."""
    return QualitySnapshot(
        standards_version=report.standards_version,
        standards_status=report.standards_status,
        evaluated_at=report.generated_at,
        outcome="passed" if report.passed else "failed",
        hard_failure_count=len(report.hard_failures),
        warning_count=len(report.warnings),
    )


def quality_gate_warning(report: QualityReport) -> str | None:
    """The user-visible warning string for a report's hard failures, or
    `None` when there are none. Extracted out of CourseService._finalise so
    the exact wording is directly unit-testable without running generation -
    `_finalise` just calls this and appends the result when it isn't None."""
    if not report.hard_failures:
        return None
    by_artifact: dict[str, int] = {}
    for finding in report.hard_failures:
        by_artifact[finding.artifact] = by_artifact.get(finding.artifact, 0) + 1
    detail = ", ".join(f"{artifact} ({count})" for artifact, count in by_artifact.items())
    return f"quality gate: {len(report.hard_failures)} unresolved hard failure(s) - {detail}"


def quality_gate_evaluation_failed_warning(error: BaseException) -> str:
    """The user-visible warning for when quality evaluation itself raised -
    TASK 1 of the quality-gate audit follow-up: an evaluation that couldn't
    run must never be silently indistinguishable from "evaluated and clean"
    (the bug this replaces logged the exception server-side only, so the
    course still shipped with zero quality-related warnings)."""
    return f"quality gate: evaluation failed and could not be completed - {error}"


def quality_gate_incomplete_inventory_warning(issues: list[ChapterInventoryIssue]) -> str | None:
    """The generation-time advisory counterpart to `IncompleteChapterInventoryError`
    (which blocks submission/approval outright - see CourseService). `_finalise`
    must never raise over this (generation itself stays fail-soft), so this is
    a third, distinct warning shape alongside `quality_gate_warning` and
    `quality_gate_evaluation_failed_warning` - a missing/corrupt chapter must
    stay visibly distinguishable from an ordinary hard failure or an
    evaluation crash, per the quality-governance failure taxonomy."""
    if not issues:
        return None
    detail = ", ".join(f"{i.chapter_id} ({i.status})" for i in issues)
    return f"quality gate: chapter inventory incomplete - {len(issues)} chapter(s) - {detail}"


def inventory_findings(issues: list[ChapterInventoryIssue]) -> list[QualityFinding]:
    """One blocker finding per chapter-inventory issue (see
    StorageService.verify_chapter_inventory) - deterministic (file
    existence/parse, not a judgment call), so always
    "independently_verified". Wording deliberately says "could not be
    evaluated"/"unreadable", never "confirmed defect" - a missing or
    corrupt chapter is unevaluated content, not a proven content problem."""
    findings: list[QualityFinding] = []
    for issue in issues:
        if issue.status == "missing":
            description = (
                f"Expected chapter '{issue.chapter_id}' is missing from storage and could not "
                "be evaluated."
            )
        else:
            description = (
                f"Expected chapter '{issue.chapter_id}' exists but could not be read and could "
                f"not be evaluated: {issue.detail}"
            )
        findings.append(
            QualityFinding(
                artifact=f"chapter:{issue.chapter_id}",
                dimension="completeness",
                severity="blocker",
                reason="incomplete_chapter_inventory",
                description=description,
                remediation="Regenerate this chapter, or verify it was written and saved "
                "correctly, then re-evaluate.",
                source="inventory",
                verification_status="independently_verified",
                verifier="chapter_inventory_check",
                verified_at=utc_now_iso(),
            )
        )
    return findings


def build_course_quality_report(
    *,
    course_id: str,
    document_id: str = "",
    chapters: list[GeneratedChapter],
    document: CourseDocument | None = None,
    inventory_issues: list[ChapterInventoryIssue] | None = None,
) -> QualityReport:
    """The one place that assembles a whole course's quality findings from
    already-persisted artifacts - no AI calls, no re-generation, safe to run
    as often as a caller (an admin page, the finalisation gate) wants.

    `inventory_issues` (MOM quality-governance follow-up) is optional and
    additive: existing callers that never pass it get exactly the same
    findings as before this parameter existed."""
    findings: list[QualityFinding] = []
    for chapter in chapters:
        findings.extend(chapter_findings(chapter))
    if document is not None:
        findings.extend(visual_findings(document))
        findings.extend(document_findings(document))
    if inventory_issues:
        findings.extend(inventory_findings(inventory_issues))
    return QualityReport(course_id=course_id, document_id=document_id, findings=findings)
