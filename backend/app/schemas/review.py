"""One common reviewer for both templates."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")


class ReviewScores(BaseModel):
    model_config = _AI

    accuracy: float = 0.0
    clarity: float = 0.0
    structure: float = 0.0
    audience_fit: float = 0.0
    non_repetition: float = 0.0
    logical_flow: float = 0.0
    template_compliance: float = 0.0
    technical_correctness: float | None = None  # technical courses only
    practical_relevance: float | None = None  # non-technical courses only

    def overall(self) -> float:
        values = [
            v
            for v in self.model_dump().values()
            if isinstance(v, (int, float)) and v is not None
        ]
        return round(sum(values) / len(values), 2) if values else 0.0


class ReviewIssue(BaseModel):
    model_config = _AI

    severity: str = "minor"  # blocker | major | minor
    category: str = ""  # accuracy | clarity | structure | dos | donts | template | code
    block_index: int = -1
    description: str = ""
    suggestion: str = ""


class ChapterReview(BaseModel):
    model_config = ConfigDict(extra="allow")

    approved: bool = True
    scores: ReviewScores = Field(default_factory=ReviewScores)
    issues: list[ReviewIssue] = Field(default_factory=list)
    missing_required_blocks: list[str] = Field(default_factory=list)
    dos_violations: list[str] = Field(default_factory=list)
    donts_violations: list[str] = Field(default_factory=list)
    repetition_notes: list[str] = Field(default_factory=list)
    summary: str = ""
    reviewed_at: str = ""

    def offending_indices(self) -> list[int]:
        """Block indices a surgical revision should rewrite."""
        indices = {
            issue.block_index
            for issue in self.issues
            if issue.severity in {"blocker", "major"} and issue.block_index >= 0
        }
        return sorted(indices)

    def blockers(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity in {"blocker", "major"}]

    def needs_revision(self) -> bool:
        return (
            not self.approved
            or bool(self.blockers())
            or bool(self.missing_required_blocks)
            or bool(self.donts_violations)
        )


class ContinuityIssue(BaseModel):
    model_config = _AI

    kind: str = "repetition"  # repetition | gap | transition | ordering
    chapter_ids: list[str] = Field(default_factory=list)
    description: str = ""
    suggestion: str = ""


class ContinuityReport(BaseModel):
    """Output of the single cross-chapter pass that follows a parallel run.

    Parallel writing trades a real previous-chapter summary for the planned one,
    so this pass exists to catch the failure mode that trade can introduce:
    repetition between chapters and broken transitions.
    """

    model_config = ConfigDict(extra="allow")

    approved: bool = True
    issues: list[ContinuityIssue] = Field(default_factory=list)
    summary: str = ""
    reviewed_at: str = ""

    def warnings(self) -> list[str]:
        return [
            f"continuity/{issue.kind}"
            + (f" ({', '.join(issue.chapter_ids)})" if issue.chapter_ids else "")
            + f": {issue.description}"
            for issue in self.issues
        ]
