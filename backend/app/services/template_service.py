"""Template management: create / edit(version) / list / archive.

Templates are DB rows (see app.db.models.Template) rather than the two static
JSON files under app/course/templates/ - those two stay exactly as they are
and keep working as the built-in defaults (see app.course.templates.registry,
which merges both sources). A "template memory" (Phase 4 of the FRD - past
template configurations) is just this table's own version history: nothing
else needs to remember what a template used to look like.

Editing NEVER mutates a row a course might already reference - it inserts a
new version and flips which one is "current" for its slug, so a course keeps
resolving to the exact template content it was generated with.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.errors import ConflictError, NotFoundError
from app.course.templates.registry import refresh_db_templates
from app.db.models import Template
from app.db.repositories.templates import TemplateRepository
from app.db.session import get_session_factory
from app.schemas.memory import (
    TemplateCreateRequest,
    TemplateRecord,
    TemplateUpdateRequest,
    slugify,
)
from app.schemas.template import CourseTemplate
from app.services.memory_embedding_sync import sync_embedding
from app.services.memory_embedding_text import template_text


class TemplateService:
    async def create(
        self, request: TemplateCreateRequest, *, created_by: uuid.UUID | None = None
    ) -> TemplateRecord:
        slug = slugify(request.name)
        async with get_session_factory()() as session:
            async with session.begin():
                repo = TemplateRepository(session)
                if await repo.get_current_for_slug(slug) is not None:
                    raise ConflictError(
                        f"A template named '{request.name}' already exists - "
                        "use update to create a new version of it instead"
                    )
                template_id = f"{slug}_v1"
                config = self._canonical_config(
                    request.config, template_id=template_id, name=request.name,
                    kind=request.kind, description=request.description,
                )
                now = datetime.now(timezone.utc)
                row = Template(
                    slug=slug,
                    version=1,
                    template_id=template_id,
                    name=request.name,
                    kind=request.kind,
                    description=request.description,
                    template_json=config.model_dump(mode="json"),
                    is_current=True,
                    is_archived=False,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                await repo.create(row)
                record = self._to_record(row)
        await refresh_db_templates()
        await sync_embedding("template", row.id, template_text(row))
        return record

    async def update(
        self,
        slug: str,
        request: TemplateUpdateRequest,
        *,
        created_by: uuid.UUID | None = None,
    ) -> TemplateRecord:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = TemplateRepository(session)
                current = await repo.get_current_for_slug(slug)
                if current is None:
                    raise NotFoundError(f"No template with slug '{slug}'")

                name = request.name or current.name
                description = (
                    request.description if request.description is not None else current.description
                )
                base_config = request.config or CourseTemplate.model_validate(current.template_json)
                new_template_id = f"{slug}_v{current.version + 1}"
                config = self._canonical_config(
                    base_config, template_id=new_template_id, name=name,
                    kind=current.kind, description=description,
                )

                await repo.clear_current(slug)
                now = datetime.now(timezone.utc)
                row = Template(
                    slug=slug,
                    version=current.version + 1,
                    template_id=new_template_id,
                    name=name,
                    kind=current.kind,
                    description=description,
                    template_json=config.model_dump(mode="json"),
                    is_current=True,
                    is_archived=current.is_archived,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                await repo.create(row)
                record = self._to_record(row)
        await refresh_db_templates()
        # `row` is the brand-new version - its embedding is independent of
        # any previous version's row, which is never touched here.
        await sync_embedding("template", row.id, template_text(row))
        return record

    async def archive(self, slug: str, *, archived: bool = True) -> TemplateRecord:
        async with get_session_factory()() as session:
            async with session.begin():
                repo = TemplateRepository(session)
                current = await repo.get_current_for_slug(slug)
                if current is None:
                    raise NotFoundError(f"No template with slug '{slug}'")
                current.is_archived = archived
                current.updated_at = datetime.now(timezone.utc)
                await repo.update(current)
                record = self._to_record(current)
        await refresh_db_templates()
        return record

    async def get_by_template_id(self, template_id: str) -> TemplateRecord:
        async with get_session_factory()() as session:
            row = await TemplateRepository(session).get_by_template_id(template_id)
            if row is None:
                raise NotFoundError(f"No template '{template_id}'")
            return self._to_record(row)

    async def get_current(self, slug: str) -> TemplateRecord:
        async with get_session_factory()() as session:
            row = await TemplateRepository(session).get_current_for_slug(slug)
            if row is None:
                raise NotFoundError(f"No template with slug '{slug}'")
            return self._to_record(row)

    async def list_versions(self, slug: str) -> list[TemplateRecord]:
        async with get_session_factory()() as session:
            rows = await TemplateRepository(session).list_versions(slug)
            if not rows:
                raise NotFoundError(f"No template with slug '{slug}'")
            return [self._to_record(row) for row in rows]

    async def list_current(
        self, *, include_archived: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[list[TemplateRecord], int]:
        async with get_session_factory()() as session:
            rows = await TemplateRepository(session).list_current(include_archived=include_archived)
            records = [self._to_record(row) for row in rows]
            return records[offset : offset + limit], len(records)

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _canonical_config(
        config: CourseTemplate, *, template_id: str, name: str, kind: str, description: str
    ) -> CourseTemplate:
        """Keep the embedded CourseTemplate payload self-consistent with the
        row's own id/name/kind/description columns - the registry relies on
        `config.template_id` matching exactly what was requested."""
        return config.model_copy(
            update={"template_id": template_id, "name": name, "kind": kind, "description": description}
        )

    @staticmethod
    def _to_record(row: Template) -> TemplateRecord:
        return TemplateRecord(
            id=row.id,
            slug=row.slug,
            version=row.version,
            template_id=row.template_id,
            name=row.name,
            kind=row.kind,
            description=row.description,
            is_current=row.is_current,
            is_archived=row.is_archived,
            created_by=str(row.created_by) if row.created_by else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
            config=CourseTemplate.model_validate(row.template_json),
        )


_service: TemplateService | None = None


def get_template_service() -> TemplateService:
    global _service
    if _service is None:
        _service = TemplateService()
    return _service
