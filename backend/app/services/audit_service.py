"""Run, store and update the fact-check audit of a course.

The report is saved with the course (`data/courses/{id}/audit.json`) so the
review screen can open it without redoing the work, and so a reviewer's
decisions on individual findings (accepted, dismissed) persist. Re-running the
audit keeps those decisions for every finding that is still raised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agents.auditor import AuditorAgent
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.fact_check import (
    apply_verdicts,
    audit_blocks,
    collect_audit_blocks,
    verification_candidates,
)
from app.schemas.audit import AuditFinding, AuditReport, ClaimVerdict, FindingStatus
from app.schemas.document import CourseDocument
from app.services.knowledge_service import get_knowledge_service
from app.services.source_service import SourceLibrary, SourceService, get_source_service
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

# Chapters are verified a few at a time: each is one model call.
_AI_CONCURRENCY = 3


class AuditService:
    def __init__(
        self,
        storage: StorageService | None = None,
        sources: SourceService | None = None,
        auditor: AuditorAgent | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()
        self.sources = sources or get_source_service()
        self._auditor = auditor

    @property
    def auditor(self) -> AuditorAgent:
        if self._auditor is None:
            self._auditor = AuditorAgent(settings=self.settings)
        return self._auditor

    # --- storage --------------------------------------------------------------
    def _path(self, course_id: str) -> Path:
        return self.storage.course_dir(course_id) / "audit.json"

    def load(self, course_id: str) -> AuditReport | None:
        path = self._path(course_id)
        if not path.exists():
            return None
        try:
            return AuditReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("Unreadable audit report for %s", course_id)
            return None

    def _save(self, report: AuditReport) -> None:
        path = self._path(report.course_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=1), encoding="utf-8")

    # --- running --------------------------------------------------------------
    async def run(
        self,
        *,
        document: CourseDocument,
        owner_key: str,
        source_ids: list[str],
        use_ai: bool | None = None,
    ) -> AuditReport:
        """Audit the document against its sources and save the report."""
        if not source_ids:
            raise ConflictError("This course has no source documents to check it against.")
        library = self.sources.load_library(owner_key, source_ids)
        if not library.passages:
            raise ConflictError("None of the course's source documents is available and ready.")

        warnings: list[str] = []
        if len(library.passages) and len({r.source_id for r, _ in library.passages.values()}) < len(set(source_ids)):
            warnings.append("Some source documents were unavailable and could not be checked against.")

        # A claim the global library supports is supported, and a [G2.5] citation
        # is real if that passage exists.
        if self.settings.enable_global_knowledge:
            shared = get_knowledge_service().library()
            if shared.passages:
                library = SourceLibrary(
                    {**library.passages, **shared.passages}, [*library.candidates, *shared.candidates]
                )
        result = audit_blocks(collect_audit_blocks(document), library)
        findings = result.findings

        ai_used = False
        if (self.settings.fact_check_ai if use_ai is None else use_ai) and findings:
            findings, ai_used = await self._verify(findings, warnings)

        # Keep what reviewers already decided about findings that are still raised.
        previous = self.load(document.course_id)
        if previous is not None:
            decided: dict[str, FindingStatus] = {f.finding_id: f.status for f in previous.findings}
            for finding in findings:
                finding.status = decided.get(finding.finding_id, "open")

        report = AuditReport(
            course_id=document.course_id,
            document_id=document.document_id,
            generated_at=utc_now_iso(),
            source_count=len(set(source_ids)),
            ai_used=ai_used,
            sentences_checked=result.sentences_checked,
            cited_sentences=result.cited_sentences,
            supported_sentences=result.supported_sentences,
            findings=findings,
            warnings=warnings,
        )
        self._save(report)
        log.info("Audited %s: %s findings (%s sentences)", report.course_id, len(findings), report.sentences_checked)
        return report

    async def _verify(
        self, findings: list[AuditFinding], warnings: list[str]
    ) -> tuple[list[AuditFinding], bool]:
        chapters: dict[str, list[AuditFinding]] = {}
        for finding in findings:
            chapters.setdefault(finding.chapter, []).append(finding)
        limit = self.settings.fact_check_max_claims
        gate = asyncio.Semaphore(_AI_CONCURRENCY)

        async def one(chapter: str, items: list[AuditFinding]) -> dict[str, ClaimVerdict]:
            candidates = verification_candidates(items, limit)
            async with gate:
                return await self.auditor.verify(chapter, candidates)

        results = await asyncio.gather(*(one(c, items) for c, items in chapters.items()))
        verdicts: dict[str, ClaimVerdict] = {}
        for part in results:
            verdicts.update(part)
        if not verdicts:
            warnings.append("The AI could not verify the doubtful sentences; the findings are from rules alone.")
            return findings, False
        return apply_verdicts(findings, verdicts), True

    # --- reviewer decisions ---------------------------------------------------
    def set_status(self, course_id: str, finding_id: str, status: FindingStatus) -> AuditFinding:
        report = self.load(course_id)
        if report is None:
            raise NotFoundError("No fact check has been run for this course")
        for finding in report.findings:
            if finding.finding_id == finding_id:
                finding.status = status
                self._save(report)
                return finding
        raise NotFoundError("Finding not found")


_service: AuditService | None = None


def get_audit_service() -> AuditService:
    global _service
    if _service is None:
        _service = AuditService()
    return _service


def reset_audit_service() -> None:
    global _service
    _service = None
