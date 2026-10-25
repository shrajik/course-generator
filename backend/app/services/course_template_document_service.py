"""Upload, list, preview, share and delete user-uploaded course templates.

An upload (DOCX or Markdown) is processed exactly once, here, by
app.course.templates.blueprint into a `CourseTemplate` carrying a blueprint:
sections, writing rules, visual rules, worked examples and reusable pictures.
That stored result is what generation and rendering consume, addressed as
`uploaded:{id}` through the template registry - the file is never re-read
per course.

Guarantees:
* A file that cannot be processed is rejected with a message saying why.
  Nothing is stored, so there is never a template that quietly falls back to
  a built-in one.
* Files are written before the database row and removed again if the insert
  fails, so a failed upload leaves no orphaned files.
* New uploads are private (the uploader and admins) unless shared. Deleting
  hides a template from everyone but keeps it resolvable, because courses
  already made from it still need it.
"""

from __future__ import annotations

import io
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.core.roles import Role
from app.course.templates.blueprint import PARSER_VERSION, BuildResult, process_template_file
from app.course.templates.registry import (
    UPLOADED_PREFIX,
    register_uploaded_template,
    set_uploaded_access,
)
from app.db.models import CourseTemplateDocument, User
from app.db.repositories.course_template_documents import CourseTemplateDocumentRepository
from app.db.session import get_session_factory
from app.schemas.course_template_document import (
    CourseTemplateDocumentDetail,
    CourseTemplateDocumentSummary,
    CourseTemplateDocumentUploadForm,
    TemplateTypeClassification,
    Visibility,
)
from app.schemas.template import CourseTemplate
from app.services.openai_service import get_ai_client

log = get_logger(__name__)

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
_ASSET_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def _extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        raise ValidationFailedError("Upload must be a .docx or .md file")
    return filename.rsplit(".", 1)[-1].lower()


async def _read_limited(file: UploadFile, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read at most `limit` bytes; one byte more means the file is too big,
    without reading the rest of an arbitrarily large upload into memory."""
    data = await file.read(limit + 1)
    if not data:
        raise ValidationFailedError("Uploaded file is empty")
    if len(data) > limit:
        raise ValidationFailedError(f"Uploaded file is too large (max {limit // (1024 * 1024)} MB)")
    return data


def _docx_to_markdown(data: bytes) -> str:
    """DOCX -> HTML (mammoth) -> Markdown (markdownify), for the human
    preview only."""
    import mammoth
    from markdownify import markdownify

    result = mammoth.convert_to_html(io.BytesIO(data))
    return markdownify(result.value, heading_style="ATX").strip()


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == Role.ADMIN.value


def _can_manage(row: CourseTemplateDocument, user: User | None) -> bool:
    return user is not None and (_is_admin(user) or row.created_by == user.id)


def _can_see(row: CourseTemplateDocument, user: User | None) -> bool:
    return row.is_active and (row.visibility == "shared" or _can_manage(row, user))


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.width, image.height
    except Exception:  # noqa: BLE001 - size is informational
        return None, None


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
        data = await _read_limited(file)

        row_id = uuid.uuid4()
        template_id = f"{UPLOADED_PREFIX}{row_id}"
        # Raises ValidationFailedError for anything unusable - before any
        # file is written or row created.
        built = process_template_file(
            data,
            source_format=source_format,
            template_id=template_id,
            name=form.name,
            kind=form.template_type,
            description=form.description,
            apply_design=form.apply_design,
        )
        markdown_content = self._preview_markdown(data, source_format, built)

        directory = get_settings().data_dir / "templates" / str(row_id)
        try:
            source_path = self._store_files(directory, row_id, data, extension, built)
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            log.error("Could not store uploaded template %s: %s", template_id, exc)
            raise

        now = datetime.now(timezone.utc)
        row = CourseTemplateDocument(
            id=row_id,
            name=form.name,
            template_type=form.template_type,
            description=form.description,
            source_format=source_format,
            content_format="markdown",
            markdown_content=markdown_content,
            source_path=source_path,
            template_json=built.template.model_dump(mode="json"),
            parse_report_json=built.report(PARSER_VERSION),
            parser_version=PARSER_VERSION,
            is_active=True,
            visibility=form.visibility,
            created_by=created_by,
            created_by_email=created_by_email,
            created_at=now,
            updated_at=now,
        )
        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    await CourseTemplateDocumentRepository(session).create(row)
        except BaseException:
            # No row, so no files either.
            shutil.rmtree(directory, ignore_errors=True)
            raise

        # Registered only after the commit, so a failed insert never leaves a
        # resolvable template that no row backs.
        register_uploaded_template(
            built.template,
            owner_id=str(created_by) if created_by else None,
            visibility=form.visibility,
        )
        log.info(
            "TEMPLATE_BLUEPRINT_STORED template_id=%s format=%s sections=%s visuals=%s examples=%s assets=%s design_applied=%s",
            template_id,
            source_format,
            len(built.template.sections),
            len(built.template.blueprint.visuals) if built.template.blueprint else 0,
            len(built.template.blueprint.examples) if built.template.blueprint else 0,
            len(built.asset_files),
            built.template.blueprint.design.applied if built.template.blueprint else False,
        )
        return self._to_detail(row, owner_can_manage=True)

    async def list_visible(self, user: User | None) -> list[CourseTemplateDocumentSummary]:
        async with get_session_factory()() as session:
            rows = await CourseTemplateDocumentRepository(session).list_visible(
                user_id=user.id if user is not None else None, is_admin=_is_admin(user)
            )
            return [self._to_summary(row, user) for row in rows]

    async def get(self, template_id: uuid.UUID, user: User | None) -> CourseTemplateDocumentDetail:
        async with get_session_factory()() as session:
            row = await CourseTemplateDocumentRepository(session).get_by_id(template_id)
            if row is None or not _can_see(row, user):
                raise NotFoundError("Template not found")
            return self._to_detail(row, user=user)

    async def set_visibility(
        self, template_id: uuid.UUID, visibility: Visibility, user: User
    ) -> CourseTemplateDocumentSummary:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = CourseTemplateDocumentRepository(session)
                row = await repo.get_by_id(template_id)
                if row is None or not _can_see(row, user):
                    raise NotFoundError("Template not found")
                if not _can_manage(row, user):
                    raise ForbiddenError("Only the uploader or an admin can change who sees this template")
                row.visibility = visibility
                row.updated_at = datetime.now(timezone.utc)
                await repo.update(row)
                summary = self._to_summary(row, user)
        set_uploaded_access(
            f"{UPLOADED_PREFIX}{template_id}",
            is_active=True,
            visibility=visibility,
            owner_id=str(row.created_by) if row.created_by else None,
        )
        return summary

    async def delete(self, template_id: uuid.UUID, user: User) -> None:
        """Hide the template from everyone and stop new courses using it.

        The row and its files stay: courses already made from it still
        resolve and render their template.
        """
        async with get_session_factory()() as session:
            async with session.begin():
                repo = CourseTemplateDocumentRepository(session)
                row = await repo.get_by_id(template_id)
                if row is None or not _can_see(row, user):
                    raise NotFoundError("Template not found")
                if not _can_manage(row, user):
                    raise ForbiddenError("Only the uploader or an admin can delete this template")
                row.is_active = False
                row.updated_at = datetime.now(timezone.utc)
                await repo.update(row)
                visibility, owner = row.visibility, row.created_by
        set_uploaded_access(
            f"{UPLOADED_PREFIX}{template_id}",
            is_active=False,
            visibility=visibility,
            owner_id=str(owner) if owner else None,
        )

    async def asset_file(self, template_id: uuid.UUID, asset_id: str, user: User | None) -> tuple[Path, str]:
        """The stored picture behind a blueprint asset, for the preview."""
        async with get_session_factory()() as session:
            row = await CourseTemplateDocumentRepository(session).get_by_id(template_id)
            if row is None or not _can_see(row, user) or not row.template_json:
                raise NotFoundError("Template not found")
            template = CourseTemplate.model_validate(row.template_json)
        asset = template.blueprint.asset(asset_id) if template.blueprint else None
        if asset is None or not asset.path:
            raise NotFoundError("Picture not found")
        data_dir = get_settings().data_dir
        path = (data_dir / asset.path).resolve()
        if (data_dir / "templates").resolve() not in path.parents or not path.is_file():
            raise NotFoundError("Picture not found")
        return path, asset.content_type or "application/octet-stream"

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

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _preview_markdown(data: bytes, source_format: str, built: BuildResult) -> str:
        if source_format == "md":
            return data.decode("utf-8-sig").strip()
        try:
            markdown = _docx_to_markdown(data)
        except Exception as exc:  # noqa: BLE001 - the preview is not the template
            log.info("Markdown preview of DOCX template failed, using section list: %s", exc)
            markdown = ""
        if markdown:
            return markdown
        return "\n\n".join(f"## {section.label}\n\n{section.guidance}" for section in built.template.sections) or "(no preview)"

    @staticmethod
    def _store_files(
        directory: Path, row_id: uuid.UUID, data: bytes, extension: str, built: BuildResult
    ) -> str:
        """Write the original file and every extracted picture under
        data/templates/{id}/, filling each asset's stored path and size."""
        directory.mkdir(parents=True, exist_ok=False)
        source_name = f"source.{extension}"
        (directory / source_name).write_bytes(data)
        if built.asset_files:
            (directory / "assets").mkdir()
        for asset, blob in built.asset_files:
            name = f"{asset.id.lower()}.{_ASSET_EXTENSIONS.get(asset.content_type, 'png')}"
            (directory / "assets" / name).write_bytes(blob)
            asset.path = f"templates/{row_id}/assets/{name}"
            asset.width, asset.height = _dimensions(blob)
        return f"templates/{row_id}/{source_name}"

    @staticmethod
    def _to_summary(
        row: CourseTemplateDocument, user: User | None = None, *, owner_can_manage: bool = False
    ) -> CourseTemplateDocumentSummary:
        template: CourseTemplate | None = None
        if row.template_json:
            try:
                template = CourseTemplate.model_validate(row.template_json)
            except Exception as exc:  # noqa: BLE001 - a bad row must still list
                log.warning("Stored template %s is invalid: %s", row.id, exc)
        usable = template is not None
        return CourseTemplateDocumentSummary(
            id=row.id,
            name=row.name,
            template_type=row.template_type,
            description=row.description,
            source_format=row.source_format,
            content_format=row.content_format,
            template_id=f"{UPLOADED_PREFIX}{row.id}" if usable else None,
            parse_report=row.parse_report_json,
            created_by=row.created_by_email or (str(row.created_by) if row.created_by else None),
            created_at=row.created_at,
            updated_at=row.updated_at,
            visibility=row.visibility or "shared",
            can_manage=owner_can_manage or _can_manage(row, user),
            usable=usable,
            unusable_reason="" if usable else (
                "This file was uploaded before templates were processed into generation rules, so it "
                "cannot drive a course. Upload it again to use it."
            ),
            sections=[
                {
                    "key": section.key,
                    "label": section.label,
                    "required": section.required,
                    "guidance": section.guidance,
                    "block_types": [block.value for block in section.block_types],
                }
                for section in template.sections
            ] if template is not None else [],
            blueprint=template.blueprint if template is not None else None,
        )

    @classmethod
    def _to_detail(
        cls, row: CourseTemplateDocument, user: User | None = None, *, owner_can_manage: bool = False
    ) -> CourseTemplateDocumentDetail:
        return CourseTemplateDocumentDetail(
            **cls._to_summary(row, user, owner_can_manage=owner_can_manage).model_dump(),
            markdown_content=row.markdown_content,
        )


_service: CourseTemplateDocumentService | None = None


def get_course_template_document_service() -> CourseTemplateDocumentService:
    global _service
    if _service is None:
        _service = CourseTemplateDocumentService()
    return _service
