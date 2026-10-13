"""Writer output.

The model returns a *flat* draft block: one model with every optional field for
every block type. This keeps the JSON schema simple and portable (no unions),
and `app.course.blocks.normalizer` converts a draft block into a validated
Course Document block.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.blocks import BlockType

_AI = ConfigDict(extra="ignore")


class DraftQuizQuestion(BaseModel):
    model_config = _AI

    question: str = ""
    kind: str = "multiple_choice"
    options: list[str] = Field(default_factory=list)
    answer: str = ""
    explanation: str = ""


class DraftTableRow(BaseModel):
    model_config = _AI

    cells: list[str] = Field(default_factory=list)


class DraftBlock(BaseModel):
    """Superset of all block contents. Only fill the fields your type needs."""

    model_config = _AI

    type: BlockType
    section_key: str = ""  # which template section this block belongs to

    # text-ish
    title: str = ""
    text: str = ""
    items: list[str] = Field(default_factory=list)
    intro: str = ""
    takeaway: str = ""
    attribution: str = ""
    variant: str = ""
    level: int = 0

    # code
    language: str = ""
    code: str = ""
    caption: str = ""

    # table
    columns: list[str] = Field(default_factory=list)
    rows: list[DraftTableRow] = Field(default_factory=list)

    # quiz
    questions: list[DraftQuizQuestion] = Field(default_factory=list)

    # exercise / challenge
    instructions: str = ""
    steps: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    difficulty: str = ""

    # case study
    context: str = ""
    challenge: str = ""
    actions: list[str] = Field(default_factory=list)
    outcome: str = ""
    lessons: list[str] = Field(default_factory=list)

    # summary
    key_takeaways: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    # image
    image_purpose: str = ""
    image_prompt: str = ""
    alt_text: str = ""
    image_kind: str = ""  # "illustration" (default) | "diagram" | "concept_experience"
    # Which diagram shape is needed, when image_kind == "diagram" - one of
    # app.schemas.diagram.DIAGRAM_KINDS (e.g. "concept_map", "flow_chart").
    # Optional: leave blank to let DiagramService decide.
    diagram_kind: str = ""
    # "" (default) | "textbook" | "section_intro" - see
    # ImageContent.illustration_style. Only meaningful when
    # image_kind == "illustration". "section_intro" is the small (~4:3)
    # illustration that opens a section and floats left of its first
    # paragraph in the PDF export - the writer sets this directly, same as
    # it already does for "textbook".
    illustration_style: str = ""
    # See ImageContent.expected_labels. Only meaningful when
    # illustration_style == "textbook".
    image_expected_labels: list[str] = Field(default_factory=list)


class BlockReplacement(BaseModel):
    """One rewritten block, addressed by its index in the chapter."""

    model_config = _AI

    index: int = -1
    block: DraftBlock


class BlockRevision(BaseModel):
    """Surgical revision: only the blocks the reviewer objected to.

    Far cheaper than re-running the writer over the whole chapter, which is what
    a naive revision pass does.
    """

    model_config = _AI

    replacements: list[BlockReplacement] = Field(default_factory=list)
    chapter_summary: str = ""


class ChapterDraft(BaseModel):
    model_config = _AI

    chapter_title: str = ""
    blocks: list[DraftBlock] = Field(default_factory=list)
    chapter_summary: str = ""
    words_estimate: int = 0


class StructuralFinding(BaseModel):
    """One deterministic claim established by a structural check at
    generation time (app.agents.reviewer._apply_structural_checks) - never
    an AI-facing schema (unlike `ChapterReview.missing_required_blocks`,
    which the reviewer model can also populate itself), so a finding built
    from this list is the one place app.services.quality_framework can
    safely mark something `independently_verified` without the model being
    able to claim that status for itself.

    `check` is a stable code naming exactly which deterministic check
    produced this (e.g. "missing_block_type", "visual_balance",
    "content_level_3", "section_completeness") - narrowly scoped to what
    that check actually proves (see each check function's own docstring/
    comment), never a broader claim about chapter quality.

    `reason`/`severity`/`verification_status`/`verifier`/`verification_error`
    are additive overrides (code-execution-verification follow-up): every
    PRE-EXISTING check leaves them `None`, and
    app.services.quality_framework._chapter_findings falls back to its
    original hardcoded values (`reason="missing_required_block"`,
    `severity="blocker"`, `verification_status="independently_verified"`)
    exactly as before this change - so this is 100% backward compatible.
    They exist because a NEW check (sandboxed code-cell execution) can
    itself fail to run (the executor down/busy) or be deliberately skipped
    (a per-chapter budget cap) - neither of those is a confirmed,
    independently-verified content defect the way every prior structural
    check's output always was, so they must be able to self-report their
    own real `verification_status` instead of inheriting one that would be
    dishonest for them specifically."""

    model_config = ConfigDict(extra="allow")

    check: str
    description: str
    dimension: str = "completeness"
    reason: str | None = None
    severity: str | None = None
    verification_status: str | None = None
    verifier: str | None = None
    verification_error: str | None = None


class GeneratedChapter(BaseModel):
    """Persisted chapter artifact: data/courses/{id}/chapters/chapter_XX.json"""

    model_config = ConfigDict(extra="allow")

    chapter_id: str
    chapter_number: int = 0
    title: str = ""
    summary: str = ""
    blocks: list[dict] = Field(default_factory=list)  # normalised Block payloads
    review: dict | None = None
    # Additive (MOM quality-governance follow-up) - the deterministic subset
    # of `review["missing_required_blocks"]`, returned by
    # `ReviewerAgent._apply_structural_checks` and persisted separately so
    # app.services.quality_framework can tell a real structural check apart
    # from the reviewer model's own self-reported text. `[]` for every
    # chapter generated before this field existed - never backfilled or
    # inferred after the fact.
    structural_findings: list[StructuralFinding] = Field(default_factory=list)
    revisions: int = 0
    model: str = ""
    generated_at: str = ""


class ChapterInventoryIssue(BaseModel):
    """One blueprint-expected chapter that is not present and loadable on
    disk (see StorageService.verify_chapter_inventory) - the fail-closed
    chapter-inventory check. `detail` carries the real parse-failure
    message when `status == "corrupt"`; always "" for "missing" (nothing
    to parse, so nothing is invented)."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    status: Literal["missing", "corrupt"]
    detail: str = ""


class ChapterResearchIssue(BaseModel):
    """One chapter whose persisted research artifact (see
    StorageService.has_research/load_research) either records no citation
    at all, or exists but could not be read (the citation-presence
    counterpart to `ChapterInventoryIssue`, MOM quality-governance
    follow-up, Phase 3.8C).

    A non-blank `references[].url` is only evidence that a reference was
    recorded - never proof that a claim is true or reliably sourced; see
    app.services.quality_framework.research_findings for the exact finding
    this becomes. `detail` carries the real read/parse/validation error for
    `status == "unreadable"`; always "" for "uncited" (the artifact loaded
    fine, it simply has no citation)."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    status: Literal["uncited", "unreadable"]
    detail: str = ""
