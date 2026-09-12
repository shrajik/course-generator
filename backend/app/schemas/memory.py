"""AI memory layer: DB-backed templates, visual knowledge, course samples,
generation history, and the bounded MemoryContext assembled from them.

Reuses `CourseTemplate` (schemas/template.py) as the payload shape for a
template version instead of reinventing template fields here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.template import CourseTemplate

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "template"


# --- Templates ---------------------------------------------------------------


class TemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["technical", "non_technical"]
    description: str = Field(default="", max_length=2000)
    # A fresh copy of the config schema - callers typically start from an
    # existing template's `config` (e.g. GET /api/courses/templates) and
    # edit it, rather than authoring one from scratch.
    config: CourseTemplate


class TemplateUpdateRequest(BaseModel):
    """Editing a template always creates a new version - see TemplateService.
    Every field is optional; unset fields carry over from the current version."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: CourseTemplate | None = None


class TemplateRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    slug: str
    version: int
    template_id: str
    name: str
    kind: str
    description: str
    is_current: bool
    is_archived: bool
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    config: CourseTemplate


class TemplateListResponse(BaseModel):
    templates: list[TemplateRecord]
    total: int
    limit: int
    offset: int


# --- Visual knowledge ---------------------------------------------------------


class VisualKnowledgeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str | None = Field(default=None, description="Course the asset was generated for.")
    asset_path: str = Field(min_length=1, max_length=300, description="Course-relative path, e.g. 'assets/image_003.svg'.")
    kind: Literal["illustration", "diagram"] = "illustration"
    diagram_kind: str | None = None
    topic: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class VisualKnowledgeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)
    approved: bool | None = None


class VisualKnowledgeEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    course_id: str | None = None
    asset_path: str
    asset_url: str | None = None
    kind: str
    diagram_kind: str | None = None
    topic: str
    description: str
    tags: list[str]
    approved: bool
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class VisualKnowledgeListResponse(BaseModel):
    items: list[VisualKnowledgeEntry]
    total: int
    limit: int
    offset: int


# --- Course samples ------------------------------------------------------------


class CourseSampleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(min_length=1, description="An existing, already-generated course.")
    title: str | None = Field(default=None, max_length=300, description="Defaults to the course title.")
    topic: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class CourseSampleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    topic: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)
    approved: bool | None = None


class CourseSampleEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    course_id: str
    title: str
    topic: str
    description: str
    tags: list[str]
    approved: bool
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class CourseSampleListResponse(BaseModel):
    items: list[CourseSampleEntry]
    total: int
    limit: int
    offset: int


# --- Generation history --------------------------------------------------------


class GenerationRunEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    job_id: str
    state: str
    chapters_generated: list[str] = Field(default_factory=list)
    chapters_failed: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime


class GenerationRunListResponse(BaseModel):
    course_id: str
    runs: list[GenerationRunEntry]


# --- Memory context (retrieval-time, not persisted) ----------------------------

MAX_SAMPLE_NOTES = 2
MAX_VISUAL_NOTES = 3
MAX_HISTORY_NOTES = 2
MAX_NOTE_CHARS = 320
MAX_CONTEXT_CHARS = 1800


class MemoryContext(BaseModel):
    """A bounded, already-rendered slice of memory for one generation stage.
    Every list here is capped at retrieval time (see MemoryService) - this
    model does not re-enforce the caps, it just carries the result."""

    model_config = ConfigDict(extra="allow")

    template_notes: str = ""
    sample_notes: list[str] = Field(default_factory=list)
    visual_notes: list[str] = Field(default_factory=list)
    history_notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # human-readable provenance, for /admin/memory/preview

    def is_empty(self) -> bool:
        return not (self.template_notes or self.sample_notes or self.visual_notes or self.history_notes)

    def render(self) -> str:
        """A single prompt-ready block, or "" when there is nothing useful.
        Capped so memory can never dominate a prompt's token budget."""
        parts: list[str] = []
        if self.template_notes:
            parts.append(f"Template guidance: {self.template_notes}")
        for note in self.sample_notes[:MAX_SAMPLE_NOTES]:
            parts.append(f"Reference course example: {note}")
        for note in self.visual_notes[:MAX_VISUAL_NOTES]:
            parts.append(f"Existing relevant visual: {note}")
        for note in self.history_notes[:MAX_HISTORY_NOTES]:
            parts.append(f"Past generation note: {note}")
        text = "\n".join(parts)
        return text[:MAX_CONTEXT_CHARS]


class MemoryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_title: str = Field(min_length=1, max_length=300)
    target_audience: str = Field(default="", max_length=2000)
    template: Literal["technical", "non_technical"] = "technical"
    stage: Literal["planner", "writer", "reviewer", "visual"] = "planner"
    chapter_title: str = Field(default="", max_length=300)


class MemoryPreviewResponse(BaseModel):
    context: MemoryContext
    rendered: str


# --- Embedding status / backfill (admin) ---------------------------------------


class EmbeddingSourceStatus(BaseModel):
    source_type: str
    total_rows: int
    embedded_rows: int


class EmbeddingStatusResponse(BaseModel):
    enabled: bool
    model: str
    dimensions: int
    similarity_threshold: float
    keyword_weight: float
    semantic_weight: float
    sources: list[EmbeddingSourceStatus]


class EmbeddingBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_types: list[Literal["template", "visual_knowledge", "course_sample", "generation_run"]] = Field(
        default_factory=lambda: ["template", "visual_knowledge", "course_sample", "generation_run"]
    )
    dry_run: bool = False
    concurrency: int = Field(default=4, ge=1, le=16)


class EmbeddingBackfillSourceResult(BaseModel):
    source_type: str
    scanned: int
    embedded: int
    skipped_valid: int
    skipped_blank: int
    failed: int


class EmbeddingBackfillResponse(BaseModel):
    dry_run: bool
    results: list[EmbeddingBackfillSourceResult]
