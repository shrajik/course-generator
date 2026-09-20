"""Upload/list/detail for admin-uploaded course template documents.

Markdown is the internal source of truth (see CourseTemplateDocument in
app/db/models.py). A `.docx` upload is converted to Markdown once, here, at
upload time; a `.md` upload is validated and stored as-is. Nothing else in
the app ever parses DOCX, and the generation pipeline does not read this
table at all yet - this service only backs upload/listing/selection.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import mammoth
from fastapi import UploadFile
from markdown_it import MarkdownIt
from markdownify import markdownify

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models import CourseTemplateDocument
from app.db.repositories.course_template_documents import CourseTemplateDocumentRepository
from app.db.session import get_session_factory
from app.schemas.course_template_document import (
    CourseTemplateDocumentDetail,
    CourseTemplateDocumentSummary,
    CourseTemplateDocumentUploadForm,
    TemplateTypeClassification,
)
from app.services.openai_service import get_ai_client

_CLASSIFY_SYSTEM = (
    "You classify a course as either \"technical\" or \"non_technical\" from its "
    "title and intended audience. \"technical\" means the course teaches "
    "software engineering, programming, data, APIs, infrastructure or other "
    "hands-on technical skills. \"non_technical\" means it teaches business, "
    "management, sales, compliance, soft skills or other non-engineering "
    "subject matter. Always pick exactly one - if genuinely unsure, prefer "
    "whichever is the closer fit rather than refusing to decide."
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB - a reference document, not a media file.
_SUPPORTED_EXTENSIONS = {"docx": "docx", "md": "md", "markdown": "md"}
_md_parser = MarkdownIt()


def _extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        raise ValidationFailedError("Upload must be a .docx or .md file")
    return filename.rsplit(".", 1)[-1].lower()


def _docx_to_markdown(data: bytes) -> str:
    """DOCX -> HTML (mammoth, preserves headings/lists/tables/bold/italic/
    links) -> Markdown (markdownify). Two well-understood conversions
    composed, rather than one custom DOCX->Markdown parser."""
    import io

    result = mammoth.convert_to_html(io.BytesIO(data))
    html = result.value
    return markdownify(html, heading_style="ATX").strip()


def _validate_markdown(content: str) -> str:
    text = content.strip()
    if not text:
        raise ValidationFailedError("Template content is empty")
    try:
        _md_parser.parse(text)
    except Exception as exc:  # pragma: no cover - markdown-it is permissive
        raise ValidationFailedError(f"Invalid Markdown: {exc}") from exc
    return text


class CourseTemplateDocumentService:
    async def upload(
        self,
        form: CourseTemplateDocumentUploadForm,
        file: UploadFile,
        *,
        created_by: uuid.UUID | None,
        created_by_email: str | None,
    ) -> CourseTemplateDocumentDetail:
        extension = _extension(file.filename)
        if extension not in _SUPPORTED_EXTENSIONS:
            raise ValidationFailedError("Only .docx and .md files are supported")
        source_format = _SUPPORTED_EXTENSIONS[extension]

        data = await file.read()
        if not data:
            raise ValidationFailedError("Uploaded file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValidationFailedError("Uploaded file is too large (max 10 MB)")

        if source_format == "md":
            try:
                markdown_content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationFailedError("Markdown file must be UTF-8 encoded") from exc
        else:
            try:
                markdown_content = _docx_to_markdown(data)
            except Exception as exc:
                raise ValidationFailedError(f"Could not read the .docx file: {exc}") from exc
        markdown_content = _validate_markdown(markdown_content)

        source_path = self._store_source_file(data, extension) if source_format == "docx" else None

        now = datetime.now(timezone.utc)
        row = CourseTemplateDocument(
            name=form.name,
            template_type=form.template_type,
            description=form.description,
            source_format=source_format,
            content_format="markdown",
            markdown_content=markdown_content,
            source_path=source_path,
            is_active=True,
            created_by=created_by,
            created_by_email=created_by_email,
            created_at=now,
            updated_at=now,
        )
        async with get_session_factory()() as session:
            async with session.begin():
                await CourseTemplateDocumentRepository(session).create(row)
            return self._to_detail(row)

    async def list_active(self) -> list[CourseTemplateDocumentSummary]:
        async with get_session_factory()() as session:
            rows = await CourseTemplateDocumentRepository(session).list_active()
            return [self._to_summary(row) for row in rows]

    async def get(self, template_id: uuid.UUID) -> CourseTemplateDocumentDetail:
        async with get_session_factory()() as session:
            row = await CourseTemplateDocumentRepository(session).get_by_id(template_id)
            if row is None or not row.is_active:
                raise NotFoundError("Template not found")
            return self._to_detail(row)

    async def classify_type(
        self, course_title: str, target_audience: str
    ) -> TemplateTypeClassification:
        """Backs the hardcoded "Default Template" - decides technical vs
        non_technical from what's actually been entered so far. A tiny,
        standalone AI call; not part of the planner/blueprint/research/writer/
        reviewer pipeline, which stays untouched."""
        user = f"Course title: {course_title}\nTarget audience: {target_audience or '(not specified)'}"
        return await get_ai_client().structured(
            schema=TemplateTypeClassification,
            system=_CLASSIFY_SYSTEM,
            user=user,
            purpose="classify_template_type",
            phase="planner",
        )

    @staticmethod
    def _store_source_file(data: bytes, extension: str) -> str:
        """Best-effort retention of the original DOCX for reference/re-download
        only - never read back by conversion or the generation pipeline."""
        settings = get_settings()
        directory = settings.data_dir / "templates"
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{extension}"
        (directory / name).write_bytes(data)
        return f"templates/{name}"

    @staticmethod
    def _to_summary(row: CourseTemplateDocument) -> CourseTemplateDocumentSummary:
        return CourseTemplateDocumentSummary(
            id=row.id,
            name=row.name,
            template_type=row.template_type,
            description=row.description,
            source_format=row.source_format,
            content_format=row.content_format,
            created_by=row.created_by_email or (str(row.created_by) if row.created_by else None),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    def _to_detail(cls, row: CourseTemplateDocument) -> CourseTemplateDocumentDetail:
        return CourseTemplateDocumentDetail(
            **cls._to_summary(row).model_dump(),
            markdown_content=row.markdown_content,
        )


_service: CourseTemplateDocumentService | None = None


def get_course_template_document_service() -> CourseTemplateDocumentService:
    global _service
    if _service is None:
        _service = CourseTemplateDocumentService()
    return _service
