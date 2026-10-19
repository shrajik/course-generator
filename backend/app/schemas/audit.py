"""The fact-check audit: a course compared with the creator's own sources.

Client requirement (Blueprint section 4, Agent 4 "The Automated Auditor"):
compare the generated content line by line against the uploaded source files,
highlight technical assumptions, and flag hallucinated details, so the human
SME at Gate 2 has an obvious path to verify factual correctness.

A finding is one sentence of the course that needs a person's eye, with the
passages of the sources that bear on it and the reason it was raised.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FindingKind = Literal[
    "invalid_citation",  # cites a reference that does not exist - a made-up source
    "contradicted",  # the sources say something different
    "weak_citation",  # cites a passage that does not appear to support the sentence
    "unsupported",  # a specific claim (figure, name, absolute) with no support in the sources
    "assumption",  # general content the sources do not cover: for the SME to confirm
]
Severity = Literal["high", "medium", "low"]
FindingStatus = Literal["open", "accepted", "dismissed"]

# How serious each kind is, for ordering and for the summary.
KIND_SEVERITY: dict[str, str] = {
    "invalid_citation": "high",
    "contradicted": "high",
    "weak_citation": "medium",
    "unsupported": "medium",
    "assumption": "low",
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    filename: str
    page: int | None = None
    heading: str = ""
    excerpt: str


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    block_id: str
    page_number: int | None = None
    chapter: str = ""
    sentence: str
    # Where the sentence sits in the block's text, so a reviewer's screen can
    # highlight it in place. None for a report made before offsets existed.
    start: int | None = None
    end: int | None = None
    kind: FindingKind
    severity: Severity
    refs: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    explanation: str = ""
    # "rules" = decided by the deterministic checks alone; "ai" = a model also
    # read the sentence against its evidence.
    verified_by: Literal["rules", "ai"] = "rules"
    # What a reviewer decided. Carried across re-runs of the audit.
    status: FindingStatus = "open"


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    document_id: str
    generated_at: str
    source_count: int
    ai_used: bool = False
    sentences_checked: int = 0
    cited_sentences: int = 0
    supported_sentences: int = 0
    findings: list[AuditFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        by_severity = {"high": 0, "medium": 0, "low": 0}
        open_count = 0
        for finding in self.findings:
            by_kind[finding.kind] = by_kind.get(finding.kind, 0) + 1
            by_severity[finding.severity] += 1
            open_count += finding.status == "open"
        checked = max(self.sentences_checked, 1)
        return {
            "findings": len(self.findings),
            "open": open_count,
            "by_kind": by_kind,
            "by_severity": by_severity,
            "sentences_checked": self.sentences_checked,
            "supported_percent": round(100 * self.supported_sentences / checked),
            "cited_percent": round(100 * self.cited_sentences / checked),
        }


# --- what the model returns when asked to verify claims -----------------------

_LENIENT = ConfigDict(extra="ignore")


class ClaimVerdict(BaseModel):
    model_config = _LENIENT

    claim_id: str = ""
    verdict: Literal["supported", "partial", "unsupported", "contradicted", "not_a_claim"] = "unsupported"
    reason: str = Field(default="", max_length=400)


class ClaimVerdicts(BaseModel):
    model_config = _LENIENT

    verdicts: list[ClaimVerdict] = Field(default_factory=list)
