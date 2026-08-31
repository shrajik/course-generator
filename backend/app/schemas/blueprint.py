"""Course Blueprint - the planner's output, input to every later phase."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# NOTE: models in this module are produced by the LLM via JSON-schema constrained
# decoding, so they use `extra="ignore"` (never `allow`) and always provide
# defaults so a partially populated response still validates.
_AI = ConfigDict(extra="ignore")


class ChapterSection(BaseModel):
    model_config = _AI

    title: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)


class BlueprintChapter(BaseModel):
    model_config = _AI

    id: str = ""
    title: str = ""
    order: int = 0
    objective: str = ""
    summary: str = ""
    sections: list[ChapterSection] = Field(default_factory=list)
    required_blocks: list[str] = Field(default_factory=list)
    optional_blocks: list[str] = Field(default_factory=list)
    key_concepts: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    research_questions: list[str] = Field(default_factory=list)
    estimated_words: int = 0
    difficulty: str = "intermediate"


class BlueprintCritique(BaseModel):
    """What the planner noticed - reported, never silently applied."""

    model_config = _AI

    missing_concepts: list[str] = Field(default_factory=list)
    duplicate_topics: list[str] = Field(default_factory=list)
    ordering_issues: list[str] = Field(default_factory=list)
    learning_progression_notes: str = ""
    suggested_toc: list[str] = Field(default_factory=list)


class CourseBlueprint(BaseModel):
    model_config = ConfigDict(extra="allow")

    course_title: str = ""
    audience: str = ""
    template_id: str = ""
    course_summary: str = ""
    learning_objectives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    tone: str = ""
    dos: list[str] = Field(default_factory=list)
    donts: list[str] = Field(default_factory=list)
    chapters: list[BlueprintChapter] = Field(default_factory=list)
    critique: BlueprintCritique = Field(default_factory=BlueprintCritique)
    generated_at: str = ""

    def chapter_by_id(self, chapter_id: str) -> BlueprintChapter | None:
        return next((c for c in self.chapters if c.id == chapter_id), None)

    def chapter_ids(self) -> list[str]:
        return [c.id for c in self.chapters]


class ChapterSummaryPlan(BaseModel):
    model_config = _AI

    chapter_id: str = ""
    summary: str = ""


class PlannedSummaries(BaseModel):
    """Filled in before writing so chapters can be written in parallel.

    The planner already produces a `summary` per chapter; this schema only backs
    the one cheap call that fills gaps when it didn't.
    """

    model_config = _AI

    summaries: list[ChapterSummaryPlan] = Field(default_factory=list)


class PlannerOutput(BaseModel):
    """Exactly what we ask the planner model for (no ids / timestamps)."""

    model_config = _AI

    course_summary: str = ""
    learning_objectives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    tone: str = ""
    chapters: list[BlueprintChapter] = Field(default_factory=list)
    critique: BlueprintCritique = Field(default_factory=BlueprintCritique)
