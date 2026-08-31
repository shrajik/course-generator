"""User-facing course input and course record."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TemplateKind = Literal["technical", "non_technical"]

TEMPLATE_IDS: dict[str, str] = {
    "technical": "technical_v1",
    "non_technical": "non_technical_v1",
}


class TocItem(BaseModel):
    """One chapter of the table of contents."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    sections: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("sections", mode="before")
    @classmethod
    def _coerce_sections(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class CourseInput(BaseModel):
    """The six MVP inputs."""

    model_config = ConfigDict(extra="forbid")

    course_title: str = Field(min_length=1, max_length=300)
    toc: list[TocItem] = Field(min_length=1)
    target_audience: str = Field(min_length=1, max_length=2000)
    dos: list[str] = Field(default_factory=list)
    donts: list[str] = Field(default_factory=list)
    template: TemplateKind
    language: str = "en"
    tone: str = ""

    @field_validator("toc", mode="before")
    @classmethod
    def _coerce_toc(cls, value: object) -> object:
        """Accept a plain list of chapter titles for convenience."""
        if isinstance(value, list):
            return [{"title": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def template_id(self) -> str:
        return TEMPLATE_IDS[self.template]

    def chapter_titles(self) -> list[str]:
        return [item.title for item in self.toc]


CourseStatus = Literal[
    "created",
    "planned",
    "researching",
    "writing",
    "reviewing",
    "assembling",
    "illustrating",
    "ready",
    "failed",
]


class ChapterProgress(BaseModel):
    model_config = ConfigDict(extra="allow")

    chapter_id: str
    title: str
    researched: bool = False
    written: bool = False
    reviewed: bool = False
    review_score: float | None = None
    error: str | None = None


class RunInfo(BaseModel):
    """Live state of the current or last generation run.

    Persisted alongside the course so the progress screen can show a real ETA and
    an interrupted run can be resumed.
    """

    model_config = ConfigDict(extra="allow")

    job_id: str = ""
    state: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    mode: Literal["background", "sync"] = "background"
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    chapters_total: int = 0
    chapters_done: int = 0
    writing_mode: str = "parallel"
    eta_seconds: float | None = None
    error: str | None = None
    timings: dict[str, Any] = Field(default_factory=dict)


class CourseRecord(BaseModel):
    """Everything we persist about a course besides blueprint/research/chapters."""

    model_config = ConfigDict(extra="allow")

    course_id: str
    document_id: str
    run: RunInfo | None = None
    status: CourseStatus = "created"
    input: CourseInput
    template_id: str
    created_at: str
    updated_at: str
    chapters: list[ChapterProgress] = Field(default_factory=list)
    has_blueprint: bool = False
    has_document: bool = False
    pdf_path: str | None = None
    last_error: str | None = None
    warnings: list[str] = Field(default_factory=list)


# --- API payloads -----------------------------------------------------------


class CreateCourseRequest(CourseInput):
    """Course creation runs the planner unless `run_planner` is false."""

    run_planner: bool = True


class ImproveTocRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_title: str = Field(min_length=1)
    toc: list[TocItem] = Field(default_factory=list)
    audience: str = ""
    template: TemplateKind = "technical"
    dos: list[str] = Field(default_factory=list)
    donts: list[str] = Field(default_factory=list)

    @field_validator("toc", mode="before")
    @classmethod
    def _coerce_toc(cls, value: object) -> object:
        if isinstance(value, list):
            return [{"title": item} if isinstance(item, str) else item for item in value]
        return value


class TocChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str  # add | remove | rename | reorder | split | merge
    target: str = ""
    proposed: str = ""
    reason: str = ""


class ImproveTocResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    suggested_toc: list[TocItem] = Field(default_factory=list)
    changes: list[TocChange] = Field(default_factory=list)
    reasoning: str = ""
    missing_concepts: list[str] = Field(default_factory=list)
    duplicate_topics: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    """Drive a full or partial generation run."""

    model_config = ConfigDict(extra="forbid")

    chapter_ids: list[str] | None = None
    force: bool = False
    skip_research: bool = False
    generate_images: bool | None = None
    build_document: bool = True
    # background returns immediately with a job id and the caller polls
    # GET /api/courses/{id}; sync blocks until the run finishes.
    mode: Literal["background", "sync"] = "background"
    # Skip chapters that already have artifacts on disk (used when resuming an
    # interrupted run).
    resume: bool = False
    # Deep Research costs minutes per chapter, so it is opt-in per chapter rather
    # than a whole-course mode.
    deep_research_chapter_ids: list[str] | None = None


class GenerateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    course_id: str
    document_id: str
    status: CourseStatus
    chapters_generated: list[str] = Field(default_factory=list)
    chapters_failed: list[str] = Field(default_factory=list)
    images_generated: int = 0
    pages: int = 0
    warnings: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    mode: Literal["background", "sync"] = "sync"
    job_id: str = ""
    accepted: bool = False  # true when the run was started in the background
    timings: dict[str, Any] = Field(default_factory=dict)
