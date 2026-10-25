"""Results of checking a course's actual images and diagrams against its text.

See app.services.visual_review. Every AI judgement is tri-state - True,
False or None (not judged) - and a check only passes when the judgements it
needs are explicitly True. A missing answer is reported as unchecked, never
as a pass.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")

CheckStatus = Literal["passed", "failed", "unchecked", "skipped"]


class ImageAssessment(BaseModel):
    """What a vision model reports after looking at one generated picture."""

    model_config = _AI

    depicts: str = ""  # what the picture actually shows, in plain words
    relevant_to_topic: bool | None = None
    matches_text: bool | None = None  # shows what the neighbouring text explains
    misleading: bool | None = None  # True = could teach something wrong
    necessary: bool | None = None  # adds teaching value beyond the text
    text_in_image_correct: bool | None = None  # None when the picture has no text
    labels_and_arrows_correct: bool | None = None  # for a picture meant as a diagram
    problems: list[str] = Field(default_factory=list)
    better_prompt: str = ""  # a corrected brief for regenerating it


class DiagramAssessment(BaseModel):
    """What a model reports after comparing a rendered diagram's content with its text."""

    model_config = _AI

    represents_text: bool | None = None  # shows the same concept/process the text explains
    labels_correct: bool | None = None  # names, terms and spellings match the text
    arrows_correct: bool | None = None  # directions and relationships are right
    missing: list[str] = Field(default_factory=list)  # parts/steps the text has but the diagram lacks
    wrong: list[str] = Field(default_factory=list)  # wrong labels, arrows or relationships
    problems: list[str] = Field(default_factory=list)
    correction: str = ""  # what the diagram must show instead


class VisualCheck(BaseModel):
    """The verdict on one image block, and what was done about it."""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    chapter_id: str = ""
    page_number: int = 0
    section: str = ""
    kind: str = "illustration"
    status: CheckStatus = "unchecked"
    problems: list[str] = Field(default_factory=list)
    depicts: str = ""
    duplicate_of: str = ""  # block id of the picture this one repeats
    action: Literal["none", "regenerated", "flagged"] = "none"
    # After a fix: whether the regenerated visual passed its re-check.
    fixed: bool | None = None
    note: str = ""


class VisualReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    course_id: str
    checked_at: str
    checks: list[VisualCheck] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"passed": 0, "failed": 0, "unchecked": 0, "skipped": 0, "regenerated": 0, "fixed": 0}
        for check in self.checks:
            counts[check.status] += 1
            if check.action == "regenerated":
                counts["regenerated"] += 1
            if check.fixed:
                counts["fixed"] += 1
        return counts

    def warnings(self) -> list[str]:
        out: list[str] = []
        for check in self.checks:
            if check.status == "failed":
                where = f"page {check.page_number}" + (f', "{check.section}"' if check.section else "")
                done = " (regenerated, still needs review)" if check.action == "regenerated" else ""
                out.append(f"Visual check ({where}): {'; '.join(check.problems[:2]) or 'problem found'}{done}")
        unchecked = sum(1 for check in self.checks if check.status == "unchecked")
        if unchecked:
            out.append(f"Visual check: {unchecked} visual(s) could not be checked and need a look by hand.")
        return out
