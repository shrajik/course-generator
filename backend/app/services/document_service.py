"""Document-level operations: AI patch editing and PDF export."""

from __future__ import annotations

from pathlib import Path

from app.agents.editor import EditorAgent
from app.core.config import Settings, get_settings
from app.core.ids import course_id_for_document
from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.course.document.patcher import apply_patch
from app.course.templates.registry import load_template
from app.db.service import get_database_service
from app.schemas.course import CourseRecord
from app.schemas.document import CourseDocument
from app.schemas.patch import AiEditRequest, AiEditResponse
from app.services.image_service import ImageService
from app.services.openai_service import AIClient, get_ai_client
from app.services.pdf_service import PdfService
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


class DocumentService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
        use_db: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()
        self.ai = ai or get_ai_client()
        # PostgreSQL is the primary store for document/course records; tests
        # without a live database flip this off (see conftest.py).
        self.use_db = self.settings.use_database if use_db is None else use_db
        self.editor = EditorAgent(self.ai, self.settings)
        self.images = ImageService(self.ai, self.storage, self.settings)
        self.pdf = PdfService(self.storage, self.settings)

    # --- persistence: same short-lived-session pattern as CourseService.
    # Each call opens its own session rather than holding one on the instance,
    # since this service is a long-lived singleton shared across requests. ---
    def resolve_course_id(self, document_id: str) -> str:
        if self.use_db:
            # Document/course ids are a deterministic pair (see app.core.ids),
            # so no filesystem lookup is needed once Postgres is primary.
            return course_id_for_document(document_id)
        return self.storage.resolve_course_id_for_document(document_id)

    async def _save_document(self, document: CourseDocument) -> None:
        if self.use_db:
            async with get_database_service() as db:
                await db.save_document(document)
            return
        self.storage.save_document(document)

    async def _load_course(self, course_id: str) -> CourseRecord:
        if self.use_db:
            async with get_database_service() as db:
                return await db.load_course_record(course_id)
        return self.storage.load_course(course_id)

    async def _save_course(self, record: CourseRecord) -> None:
        if self.use_db:
            async with get_database_service() as db:
                await db.save_course_record(record)
            return
        self.storage.save_course(record)

    # --- loading ----------------------------------------------------------
    async def load(self, document_id: str) -> CourseDocument:
        if self.use_db:
            async with get_database_service() as db:
                return await db.load_document(document_id)
        course_id = self.storage.resolve_course_id_for_document(document_id)
        return self.storage.load_document(course_id)

    # --- AI editing -------------------------------------------------------
    async def ai_edit(self, document_id: str, request: AiEditRequest) -> AiEditResponse:
        document = await self.load(document_id)
        template = load_template(document.template_id)

        patch = await self.editor.edit(
            document=document,
            selected_block_ids=request.selected_block_ids,
            instruction=request.instruction,
        )

        if not request.apply:
            return AiEditResponse(
                document_id=document.document_id,
                version=document.version,
                applied=False,
                patch=patch,
                pages=len(document.pages),
            )

        result = apply_patch(document, patch, template)

        if result.images_to_generate and request.regenerate_images:
            generated = await self.images.generate_missing(
                document=document,
                template=template,
                only_block_ids=result.images_to_generate,
                force=True,
            )
            if generated:
                reflow_document(document, template)

        if result.changed:
            await self._save_document(document)
            log.info(
                "Applied %s operation(s) to %s (v%s)",
                len(result.applied),
                document.document_id,
                document.version,
            )

        return AiEditResponse(
            document_id=document.document_id,
            version=document.version,
            applied=result.changed,
            patch=patch,
            applied_operations=result.applied,
            rejected_operations=result.rejected,
            pages=len(document.pages),
        )

    # --- export -----------------------------------------------------------
    async def export_pdf(self, document_id: str) -> tuple[Path, CourseDocument]:
        document = await self.load(document_id)
        path = await self.pdf.export_pdf(document)
        record = await self._load_course(document.course_id)
        record.pdf_path = path.relative_to(self.storage.course_dir(document.course_id)).as_posix()
        await self._save_course(record)
        return path, document


_service: DocumentService | None = None


def get_document_service() -> DocumentService:
    global _service
    if _service is None:
        _service = DocumentService()
    return _service


def reset_document_service() -> None:
    global _service
    _service = None
