"""Per-chapter research artifacts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")


class Reference(BaseModel):
    model_config = _AI

    title: str = ""
    url: str = ""
    note: str = ""


class Definition(BaseModel):
    model_config = _AI

    term: str = ""
    definition: str = ""


class ExampleItem(BaseModel):
    model_config = _AI

    title: str = ""
    description: str = ""
    why_it_matters: str = ""


class CaseStudyNote(BaseModel):
    model_config = _AI

    title: str = ""
    context: str = ""
    challenge: str = ""
    approach: str = ""
    outcome: str = ""
    lessons: list[str] = Field(default_factory=list)


class FactItem(BaseModel):
    model_config = _AI

    statement: str = ""
    source: str = ""
    confidence: str = "medium"  # low | medium | high


class VisualOpportunity(BaseModel):
    model_config = _AI

    purpose: str = ""
    description: str = ""
    suggested_prompt: str = ""


class QuizIdea(BaseModel):
    model_config = _AI

    question: str = ""
    answer: str = ""
    difficulty: str = "medium"


class ExerciseIdea(BaseModel):
    model_config = _AI

    title: str = ""
    description: str = ""
    difficulty: str = "medium"


class FaqItem(BaseModel):
    model_config = _AI

    question: str = ""
    answer: str = ""


class ResearchPayload(BaseModel):
    """The structured part the model fills in."""

    model_config = _AI

    overview: str = ""
    definitions: list[Definition] = Field(default_factory=list)
    core_concepts: list[str] = Field(default_factory=list)
    examples: list[ExampleItem] = Field(default_factory=list)
    real_world_examples: list[ExampleItem] = Field(default_factory=list)
    case_studies: list[CaseStudyNote] = Field(default_factory=list)
    important_facts: list[FactItem] = Field(default_factory=list)
    statistics: list[FactItem] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    best_practices: list[str] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    faq: list[FaqItem] = Field(default_factory=list)
    visual_opportunities: list[VisualOpportunity] = Field(default_factory=list)
    exercise_ideas: list[ExerciseIdea] = Field(default_factory=list)
    quiz_ideas: list[QuizIdea] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ChapterResearch(BaseModel):
    model_config = ConfigDict(extra="allow")

    chapter_id: str
    chapter_title: str = ""
    mode: str = "fast"  # fast | deep | mock
    model: str = ""
    generated_at: str = ""
    raw_notes: str = ""
    payload: ResearchPayload = Field(default_factory=ResearchPayload)

    def compact_context(self, *, max_chars: int = 12000) -> str:
        """A trimmed textual view handed to the writer."""
        data = self.payload.model_dump(mode="json", exclude_defaults=False)
        import json

        text = json.dumps(data, ensure_ascii=False, indent=1)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n... [research truncated]"
