"""Writer output.

The model returns a *flat* draft block: one model with every optional field for
every block type. This keeps the JSON schema simple and portable (no unions),
and `app.course.blocks.normalizer` converts a draft block into a validated
Course Document block.
"""

from __future__ import annotations

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


class GeneratedChapter(BaseModel):
    """Persisted chapter artifact: data/courses/{id}/chapters/chapter_XX.json"""

    model_config = ConfigDict(extra="allow")

    chapter_id: str
    chapter_number: int = 0
    title: str = ""
    summary: str = ""
    blocks: list[dict] = Field(default_factory=list)  # normalised Block payloads
    review: dict | None = None
    revisions: int = 0
    model: str = ""
    generated_at: str = ""
