"""Document-level operations: AI patch editing and PDF export."""

from __future__ import annotations

from pathlib import Path

from app.agents.editor import EditorAgent
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.ids import course_id_for_document, utc_now_iso
from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.course.document.patcher import apply_patch
from app.course.document.visual_repair import audit_and_repair_visuals
from app.course.templates.registry import load_template
from app.db.service import get_database_service
from app.render.concept_experience_renderer import estimate_pixel_size, render_concept_experience_html
from app.schemas.blocks import BlockType, merge_content
from app.schemas.course import CourseRecord
from app.schemas.diagram import DiagramSpec
from app.schemas.document import (
    CourseDocument,
    RegenerateVisualsResponse,
    RepairVisualsResponse,
    UpdateConceptVisualSpecResponse,
)
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

    # --- manual editor saves ------------------------------------------------
    async def save(self, document_id: str, incoming: CourseDocument) -> CourseDocument:
        """Persist the editor's full document state as the new source of truth.

        Unlike `ai_edit`, this never reflows pagination: the editor already
        computed every block's absolute layout client-side (the same A4
        coordinate system the PDF renderer uses), so re-flowing here would
        discard the very edit being saved. `document_id`/`course_id` are
        always taken from the server, never the request body, so a client
        can't redirect a save onto a different document/course by editing
        the JSON it sends.
        """
        if not incoming.pages:
            raise ValidationFailedError("A document must have at least one page")

        # Re-validates the template exists; a document can only ever have
        # been created against a real template, so a bad id here means a
        # corrupt or tampered payload.
        try:
            load_template(incoming.template_id)
        except NotFoundError as exc:
            raise ValidationFailedError(f"Unknown template '{incoming.template_id}'") from exc

        try:
            existing = await self.load(document_id)
        except NotFoundError:
            existing = None

        document = incoming.model_copy(deep=True)
        document.document_id = document_id
        if existing is not None:
            document.course_id = existing.course_id
            document.created_at = existing.created_at
            document.version = existing.version + 1
        else:
            document.course_id = self.resolve_course_id(document_id)
            document.version = 1
        document.updated_at = utc_now_iso()

        await self._save_document(document)
        log.info("Saved manual edits to %s (v%s)", document_id, document.version)
        return document

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

    # --- bulk visual regeneration -------------------------------------------
    async def regenerate_all_visuals(
        self, document_id: str, kinds: list[str] | None = None
    ) -> RegenerateVisualsResponse:
        """Re-render every image block in an EXISTING document with today's
        renderer/prompt code, regardless of whether it already has a `path` -
        the missing mechanism this document's whole visual-consistency
        problem traces back to: a rendered diagram is a static SVG/PNG file
        saved once at generation time, so a later renderer change (new
        colours, new shapes, a fixed layout bug) never touches a course
        that was already generated - only a fresh generation, or an explicit
        regenerate like this one, does. Reuses `ImageService.generate_missing`
        with `force=True` (the exact mechanism `ai_edit` already uses per-block
        after a patch), just scoped to every matching block in the whole
        document instead of a specific patch's own block ids - same
        concurrency limiting, same per-block failure isolation (one bad
        regeneration never aborts the rest)."""
        document = await self.load(document_id)
        template = load_template(document.template_id)

        wanted_kinds = {k.strip() for k in (kinds or []) if k.strip()}
        target_ids = [
            block.id
            for _, block in document.iter_blocks()
            if block.type is BlockType.IMAGE
            and (not wanted_kinds or block.content.get("kind", "illustration") in wanted_kinds)
        ]

        regenerated = await self.images.generate_missing(
            document=document, template=template, only_block_ids=target_ids, force=True
        )
        failed = [
            block.id
            for _, block in document.iter_blocks()
            if block.id in target_ids and block.content.get("error")
        ]
        if regenerated or failed:
            reflow_document(document, template)
            await self._save_document(document)

        return RegenerateVisualsResponse(
            document_id=document.document_id,
            version=document.version,
            regenerated=regenerated,
            failed=failed,
        )

    # --- post-pagination visual audit and repair ----------------------------
    async def repair_visual_coverage(self, document_id: str) -> RepairVisualsResponse:
        """Runs the same post-pagination visual audit/repair every newly
        generated course already gets (see
        app.course.document.visual_repair.audit_and_repair_visuals) against
        an EXISTING document - the explicit "repair a course I already
        generated" mechanism: loads it, audits every page, generates and
        inserts a relevant visual for each one that's still deficient,
        re-paginates, and saves - never touching a page that already meets
        the target, never silently claiming success on one that couldn't be
        repaired within the bounded number of passes."""
        document = await self.load(document_id)
        template = load_template(document.template_id)

        report = await audit_and_repair_visuals(document, template, images=self.images, ai=self.ai)
        if report.repaired:
            await self._save_document(document)
            log.info(
                "Visual repair for %s: %s page(s) repaired, %s still deficient",
                document_id, len(report.repaired), len(report.still_deficient),
            )

        return RepairVisualsResponse(
            document_id=document.document_id,
            version=document.version,
            passes_run=report.passes_run,
            repaired=report.repaired,
            skipped=report.skipped,
            failed=report.failed,
            still_deficient=report.still_deficient,
            validation_findings=report.validation_findings,
        )

    # --- editable diagram text (no AI call) --------------------------------
    async def update_concept_visual_spec(
        self, document_id: str, block_id: str, spec_update: dict
    ) -> UpdateConceptVisualSpecResponse:
        """Re-render a `concept_experience` block from an edited copy of its
        own spec - a typo/wording fix, not a new concept, so this never
        calls the AI planner. Same renderer + estimator
        `ConceptVisualService.generate_for_block` uses, so the result is
        exactly as reliable (no overflow, no overlap - see
        `render_concept_experience_html`'s own cap-and-scale contract);
        `reflow_document` repositions any later block if this one's height
        changed, the same way a regenerated image already does today.
        """
        document = await self.load(document_id)
        template = load_template(document.template_id)

        found = document.find_block(block_id)
        if found is None:
            raise NotFoundError(f"Block '{block_id}' not found in document '{document_id}'")
        _, block = found
        if block.type is not BlockType.IMAGE or block.content.get("kind") != "concept_experience":
            raise ValidationFailedError(
                f"Block '{block_id}' is not an editable concept_experience visual"
            )

        try:
            spec = DiagramSpec.model_validate(spec_update)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 422, not a 500
            raise ValidationFailedError(f"Invalid visual spec: {exc}") from exc

        html_bytes = render_concept_experience_html(spec, template.theme)
        relative = self.storage.save_asset(document.course_id, html_bytes, extension="html")
        width, height = estimate_pixel_size(spec)
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                "width": width,
                "height": height,
                "spec": spec.model_dump(mode="json"),
            },
        )
        reflow_document(document, template)
        document.touch()
        await self._save_document(document)
        log.info("Updated concept_visual spec for block %s in %s", block_id, document_id)

        return UpdateConceptVisualSpecResponse(
            document_id=document.document_id, version=document.version, block=block
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
