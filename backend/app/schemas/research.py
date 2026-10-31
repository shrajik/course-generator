"""Per-chapter research artifacts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")


class Reference(BaseModel):
    model_config = _AI

    title: str = ""
    url: str = ""
    note: str = ""
    # Additive (course-sources follow-up) - all three default "" so a
    # reference built before these fields existed (or merged from a real
    # web-search citation via ResearchAgent._merge_citations, which only
    # ever sets title/url) stays perfectly valid. Populated by the research-
    # structuring call ONLY when evident in the actual research notes - see
    # RESEARCH_STRUCTURE_SYSTEM's own "never add a new fact" rule, which
    # applies to these exactly as it already does to every other field here.
    author: str = ""  # the resource's named author/organization, if stated
    source_type: str = ""  # e.g. "official documentation", "textbook", "research paper"
    relevance: str = ""  # one short sentence: what a learner gets from this resource


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


def render_source_excerpts(excerpts: list) -> str:
    """The uploaded-source passages as the writer reads them, each with the
    reference it must cite. "" when there are none."""
    if not excerpts:
        return ""
    lines = [
        "UPLOADED SOURCE EXCERPTS (the client's own documents - the ground truth for this "
        "chapter; prefer them over web research, and cite each fact you take from one with "
        "its reference in square brackets, for example [S1.3]):"
    ]
    for item in excerpts:
        where = f"{item.filename}" + (f", page {item.page}" if item.page else "")
        heading = f" - {item.heading}" if item.heading else ""
        lines.append(f"[{item.ref}] ({where}{heading})\n{item.text}")
    return "\n\n".join(lines)


def render_knowledge_excerpts(excerpts: list) -> str:
    """Passages of the global knowledge library, each with its `G` reference."""
    if not excerpts:
        return ""
    lines = [
        "KNOWLEDGE LIBRARY EXCERPTS (the organisation's approved reference documents: use them "
        "where they bear on this chapter, and cite each fact you take from one with its reference "
        "in square brackets, for example [G2.5]. Where they disagree with the course's own source "
        "documents, follow the course's documents):"
    ]
    for item in excerpts:
        where = f"{item.filename}" + (f", page {item.page}" if item.page else "")
        heading = f" - {item.heading}" if item.heading else ""
        lines.append(f"[{item.ref}] ({where}{heading})\n{item.text}")
    return "\n\n".join(lines)


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
        """A trimmed textual view handed to the writer.

        Passages from the creator's uploaded sources (attached for a run, never
        stored with the research) come first and are never trimmed away by the
        web research: they are the ground truth the course is checked against.
        """
        data = self.payload.model_dump(mode="json", exclude_defaults=False)
        import json

        excerpts = "\n\n".join(
            part
            for part in (
                render_source_excerpts(getattr(self, "source_excerpts", None) or []),
                render_knowledge_excerpts(getattr(self, "knowledge_excerpts", None) or []),
            )
            if part
        )
        text = json.dumps(data, ensure_ascii=False, indent=1)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [research truncated]"
        return f"{excerpts}\n\nWEB RESEARCH:\n{text}" if excerpts else text
