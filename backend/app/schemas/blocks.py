"""Block type registry.

A block is `type + content + style + layout`. `content` is stored as a plain dict
inside the Course Document so the future editor can patch arbitrary keys, but it
is *validated* against a registered Pydantic model for its type.

Adding a new block type = add an enum member + a content model + one registry
entry + one renderer branch. Nothing else in the pipeline needs to change.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlockType(str, Enum):
    # --- required by the POC spec ------------------------------------------
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    IMAGE = "image"
    QUOTE = "quote"
    CALLOUT = "callout"
    CODE = "code"
    TABLE = "table"
    QUIZ = "quiz"
    EXERCISE = "exercise"
    CASE_STUDY = "case_study"
    STORY = "story"
    TIP = "tip"
    WARNING = "warning"
    SUMMARY = "summary"
    DIVIDER = "divider"
    # --- extensions used by the two templates ------------------------------
    LEARNING_OBJECTIVES = "learning_objectives"
    CHALLENGE = "challenge"
    REFLECTION = "reflection"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


TEXTUAL_BLOCK_TYPES = {
    BlockType.PARAGRAPH,
    BlockType.QUOTE,
    BlockType.TIP,
    BlockType.WARNING,
    BlockType.STORY,
}

# A block that carries real visual weight on the page (as opposed to text
# that merely mentions or introduces a visual) - shared between the reviewer
# (chapter-level pacing, see app.agents.reviewer) and the layout engine
# (page-level pagination, see app.course.document.layout) so both mean
# exactly the same thing by "visual".
VISUAL_BLOCK_TYPES = {BlockType.IMAGE, BlockType.TABLE, BlockType.CODE}


class _Content(BaseModel):
    """Content models tolerate unknown keys so the editor can add metadata."""

    model_config = ConfigDict(extra="allow")


class HeadingContent(_Content):
    text: str
    level: int = Field(default=2, ge=1, le=4)


class ParagraphContent(_Content):
    text: str


class ImageContent(_Content):
    purpose: str = ""
    prompt: str = ""
    caption: str = ""
    alt: str = ""
    path: str | None = None  # relative to the course directory
    asset_id: str | None = None
    generated: bool = False
    error: str | None = None
    # Intrinsic pixel size of the generated asset, used by the layout engine.
    width: int | None = None
    height: int | None = None
    # "illustration" (default) renders `prompt` through the image model as a
    # raster picture. "diagram" renders it as a structured, on-theme SVG -
    # see app.services.diagram_service - for flow charts, SmartArt-style
    # lists, hierarchies and other content where exact labels matter.
    kind: str = "illustration"
    # Optional hint for which DiagramSpec shape this should be (e.g.
    # "concept_map", "flow_chart") - see app.schemas.diagram.DIAGRAM_KINDS.
    # Only meaningful when kind == "diagram"; ignored otherwise. Empty means
    # DiagramService free-decides, same as before this field existed.
    diagram_kind: str = ""
    # Only meaningful when kind == "illustration". "" (default) keeps the
    # template's own image_guidance (flat/decorative style, unchanged
    # behaviour). "textbook" is for a companion picture placed ALONGSIDE a
    # diagram/concept_experience visual for the same concept - an actual
    # scientifically/technically accurate illustration (anatomy, an
    # experiment apparatus, a molecular structure) rather than a decorative
    # or flat-vector graphic - see ImageService.build_prompt. "section_intro"
    # is the small (~4:3, 400x300) illustration that opens a section - the
    # writer places it as that section's first block, immediately before its
    # first paragraph, and app.course.document.layout pairs the two so the
    # PDF export floats the image left of that paragraph's own text (see
    # layout.flow_blocks and render_document_html); the in-app editor has no
    # float/wrap concept, so it simply renders it as an ordinary small block,
    # stacked on top of the paragraph via a higher z_index rather than beside
    # it.
    illustration_style: str = ""
    # Only meaningful when illustration_style == "textbook". The exact short
    # labels (if any) the image is expected to render as text - the ground
    # truth ImageService's post-generation text check compares the image's
    # actual rendered text against (image models garble baked-in text often
    # enough that this is checked automatically, not assumed correct - see
    # ImageService._verify_generated_text). Leave empty for an illustration
    # that isn't meant to have any text in it at all.
    expected_labels: list[str] = Field(default_factory=list)
    # The honest, final word on whether the delivered image's text passed
    # `_verify_generated_text` - `None` means nothing was promised to check
    # (no expected_labels, or a non-illustration kind), `True` means it
    # passed a real check, `False` means every bounded attempt (including
    # the guaranteed-effort text-free ones) still came back with incorrect
    # text - see ImageService._generate_with_text_check. Never silently
    # absent just because generation "succeeded" - a caller (the editor, a
    # future course-quality audit) can tell "verified" from "shipped
    # anyway" instead of assuming every generated image is correct.
    text_verified: bool | None = None


class QuoteContent(_Content):
    text: str
    attribution: str = ""


class CalloutContent(_Content):
    title: str = ""
    text: str
    variant: str = "info"  # info | note | did_you_know | motivation | humour


class CodeContent(_Content):
    language: str = "text"
    code: str
    caption: str = ""


class TableRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    cells: list[str] = Field(default_factory=list)


class TableContent(_Content):
    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="allow")
    question: str
    kind: str = "multiple_choice"  # multiple_choice | true_false | short_answer
    options: list[str] = Field(default_factory=list)
    answer: str = ""
    explanation: str = ""


class QuizContent(_Content):
    title: str = "Knowledge Check"
    questions: list[QuizQuestion] = Field(default_factory=list)


class ExerciseContent(_Content):
    title: str = "Exercise"
    instructions: str
    steps: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    difficulty: str = "medium"  # easy | medium | hard


class CaseStudyContent(_Content):
    title: str = "Case Study"
    context: str = ""
    challenge: str = ""
    actions: list[str] = Field(default_factory=list)
    outcome: str = ""
    lessons: list[str] = Field(default_factory=list)


class StoryContent(_Content):
    title: str = ""
    text: str
    takeaway: str = ""


class TipContent(_Content):
    text: str
    title: str = "Pro Tip"


class WarningContent(_Content):
    text: str
    title: str = "Common Mistake"


class SummaryContent(_Content):
    title: str = "Summary"
    key_takeaways: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class DividerContent(_Content):
    pass


class ListContent(_Content):
    """Shared shape for objectives / reflection / any bulleted block."""

    title: str = ""
    items: list[str] = Field(default_factory=list)
    intro: str = ""


CONTENT_MODELS: dict[BlockType, type[_Content]] = {
    BlockType.HEADING: HeadingContent,
    BlockType.PARAGRAPH: ParagraphContent,
    BlockType.IMAGE: ImageContent,
    BlockType.QUOTE: QuoteContent,
    BlockType.CALLOUT: CalloutContent,
    BlockType.CODE: CodeContent,
    BlockType.TABLE: TableContent,
    BlockType.QUIZ: QuizContent,
    BlockType.EXERCISE: ExerciseContent,
    BlockType.CASE_STUDY: CaseStudyContent,
    BlockType.STORY: StoryContent,
    BlockType.TIP: TipContent,
    BlockType.WARNING: WarningContent,
    BlockType.SUMMARY: SummaryContent,
    BlockType.DIVIDER: DividerContent,
    BlockType.LEARNING_OBJECTIVES: ListContent,
    BlockType.CHALLENGE: ExerciseContent,
    BlockType.REFLECTION: ListContent,
}


def content_model_for(block_type: BlockType) -> type[_Content]:
    try:
        return CONTENT_MODELS[block_type]
    except KeyError as exc:  # pragma: no cover - guarded by the enum
        raise KeyError(f"No content model registered for block type {block_type}") from exc


def validate_content(block_type: BlockType, content: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a content dict for the given block type."""
    model = content_model_for(block_type)
    return model.model_validate(content or {}).model_dump(mode="json")


def merge_content(
    block_type: BlockType, current: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """Shallow merge then re-validate; used when applying editor patches."""
    merged = {**(current or {}), **(patch or {})}
    return validate_content(block_type, merged)


def register_block_type(name: str, model: type[_Content]) -> BlockType:  # pragma: no cover
    """Escape hatch for runtime extension (plugins, experiments)."""
    member = BlockType(name)
    CONTENT_MODELS[member] = model
    return member
