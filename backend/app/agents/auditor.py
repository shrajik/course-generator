"""The AI half of the auditor: read doubtful sentences against their evidence.

The rules in app/course/fact_check.py find sentences worth a second look; this
agent reads each of them next to the source passages that bear on it and says
whether the passages support, partly support, fail to support, or contradict
it. It sees only what it is handed - the sentence and its evidence - and is
told to use nothing else, because its whole value is that a claim is judged
against the creator's documents, not against what a model happens to believe.

It is best-effort: any failure leaves the rule-based findings as they were.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.audit import AuditFinding, ClaimVerdict, ClaimVerdicts
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

AUDITOR_SYSTEM = """\
You are a meticulous fact-checker. You are given sentences from a course and, for
each one, the passages of the client's own source documents that bear on it.

Judge every sentence using ONLY the passages given for it. Do not use your own
knowledge, and do not assume a passage says more than it does.

Give one verdict per claim:
- supported: the passages state what the sentence says.
- partial: some of it is stated, but a part (a figure, condition or detail) is not.
- unsupported: the passages do not state it. Absence of support is not contradiction.
- contradicted: the passages state something different (another figure, the opposite rule).
- not_a_claim: it is an instruction, a definition of the course's own terms, a
  transition or an opinion, not a statement of fact that a source could confirm.

Give a short reason (one sentence) that points at the passage wording. Reuse each
claim_id exactly as given and return one verdict per claim."""


def _user(chapter: str, findings: list[AuditFinding]) -> str:
    lines = [f"CHAPTER: {chapter}", ""]
    for finding in findings:
        lines.append(f"CLAIM {finding.finding_id}:")
        lines.append(f"Sentence: {finding.sentence}")
        if finding.evidence:
            for item in finding.evidence:
                where = f"{item.filename}" + (f", page {item.page}" if item.page else "")
                lines.append(f"Passage [{item.ref}] ({where}): {item.excerpt}")
        else:
            lines.append("Passages: (none of the sources is close to this sentence)")
        lines.append("")
    return "\n".join(lines)


class AuditorAgent:
    def __init__(self, ai: AIClient | None = None, settings: Settings | None = None) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()

    async def verify(self, chapter: str, findings: list[AuditFinding]) -> dict[str, ClaimVerdict]:
        """A verdict per finding id. Empty on any failure."""
        if not findings:
            return {}
        try:
            result = await self.ai.structured(
                schema=ClaimVerdicts,
                system=AUDITOR_SYSTEM,
                user=_user(chapter, findings),
                model=self.settings.reviewer_model,
                purpose=f"audit:{chapter[:40]}",
                phase="reviewer",
            )
        except Exception as exc:  # noqa: BLE001 - the audit must not depend on the model
            log.warning("Fact-check verification failed for %r: %s", chapter, exc)
            return {}
        shown = {finding.finding_id for finding in findings}
        # Only ids the model was actually shown count; a made-up one is ignored.
        return {v.claim_id: v for v in result.verdicts if v.claim_id in shown}
