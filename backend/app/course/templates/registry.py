"""Template registry - loads and validates template configurations.

Three sources, merged:
* Static JSON files under this directory (`technical_v1`, `non_technical_v1`)
  - the built-in defaults.
* DB-backed admin templates (app.db.models.Template,
  app.services.template_service) - created/edited through the admin API.
  Every version is immutable once created, so a course that stored
  `template_id="my_template_v3"` keeps resolving to exactly that content
  forever, even after a newer version becomes current.
* Uploaded templates (`uploaded:{uuid}`, app.db.models.CourseTemplateDocument)
  - DOCX/Markdown files processed once into a template with a generation
  blueprint (app.course.templates.blueprint).

Two different questions are answered here. `load_template` resolves any id a
course was ever pinned to - archived, superseded and deleted templates
included, so existing courses keep rendering. `check_selectable` decides
whether a NEW course may use an id, and says why not.

`load_template()` stays a plain, synchronous, cheap lookup - every existing
caller (writer, reviewer, PDF export, dozens of call sites) calls it
synchronously today, and keeping it that way avoids an async refactor with a
huge blast radius. DB and uploaded templates are instead kept in small
in-process caches, refreshed right after a write in this process, at startup,
periodically (`refresh_templates_periodically`, for writes made by other
workers) and on a cache miss in async entry points (`ensure_template_loaded`)
- never on the synchronous read path, so a slow or unavailable database can
never block a template lookup.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.schemas.course import TEMPLATE_IDS
from app.schemas.template import CourseTemplate

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent
_FILES = {
    "technical_v1": "technical_v1.json",
    "non_technical_v1": "non_technical_v1.json",
}
BUILT_IN_TEMPLATE_IDS = frozenset(_FILES)

# template_id -> (config, is_current, is_archived). Populated by
# refresh_db_templates(); empty (and therefore a no-op) until the database is
# available and something has actually been created - existing behaviour is
# byte-identical when no custom templates exist.
_db_templates: dict[str, tuple[CourseTemplate, bool, bool]] = {}

# Uploaded DOCX templates, addressed as `uploaded:{uuid}`. Same in-process
# cache strategy as _db_templates above (refreshed explicitly, never on the
# read path) so `load_template` stays synchronous and a slow database can
# never block a template lookup. Empty until something is uploaded, so
# behaviour with no uploads is byte-identical to before.
UPLOADED_PREFIX = "uploaded:"
_uploaded_templates: dict[str, CourseTemplate] = {}


@dataclass(frozen=True)
class UploadedAccess:
    """Who may start a NEW course from an uploaded template. Resolution by
    id (`load_template`) ignores this on purpose: a course already pinned to
    a template keeps working after the template is deleted or made private."""

    is_active: bool
    visibility: str  # private | shared
    owner_id: str | None


_uploaded_access: dict[str, UploadedAccess] = {}


def is_uploaded_template_id(template_id: str) -> bool:
    return template_id.startswith(UPLOADED_PREFIX)


@lru_cache(maxsize=64)
def load_template(template_id: str) -> CourseTemplate:
    """Load a template by id (`technical_v1`), kind (`technical`), or a
    DB-backed versioned id (`my_template_v3`)."""
    resolved = TEMPLATE_IDS.get(template_id, template_id)

    uploaded = _uploaded_templates.get(resolved)
    if uploaded is not None:
        return uploaded

    cached = _db_templates.get(resolved)
    if cached is not None:
        return cached[0]

    filename = _FILES.get(resolved)
    if filename is None:
        if is_uploaded_template_id(resolved):
            # Distinct message: the row exists but has no usable parse (a
            # Markdown-only upload, or one that predates the parser), which
            # is a different problem from a typo'd id.
            raise NotFoundError(
                f"Uploaded template '{template_id}' has no parsed configuration. "
                "Re-upload the .docx file to regenerate it."
            )
        raise NotFoundError(
            f"Unknown template '{template_id}'. Available: {sorted(available_template_ids())}"
        )
    path = TEMPLATES_DIR / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailedError(f"Template file {filename} is unreadable: {exc}") from exc
    try:
        template = CourseTemplate.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ValidationFailedError(f"Template {filename} is invalid: {exc}") from exc
    if template.template_id != resolved:
        raise ValidationFailedError(
            f"Template id mismatch in {filename}: {template.template_id} != {resolved}"
        )
    return template


def available_template_ids() -> list[str]:
    current_db_ids = [tid for tid, (_, is_current, is_archived) in _db_templates.items() if is_current and not is_archived]
    return sorted({*_FILES, *current_db_ids})


def available_templates() -> list[CourseTemplate]:
    """Every selectable (current, non-archived) template - static defaults
    first, then DB-backed ones. Used by the course-creation picker."""
    static = [load_template(tid) for tid in _FILES]
    db_current = [
        config for config, is_current, is_archived in _db_templates.values() if is_current and not is_archived
    ]
    return static + db_current


async def refresh_db_templates() -> None:
    """Reload every DB-backed template version into the in-process cache.

    Called after every template create/update/archive (so the change is
    usable immediately) and once, best-effort, at app startup. Never raises:
    a database that is unavailable or has no `templates` table yet (e.g.
    USE_DATABASE=false, or the migration hasn't run) just leaves the cache
    empty, which is exactly today's behaviour.
    """
    from app.core.config import get_settings

    if not get_settings().use_database:
        return
    try:
        from app.db.models import Template
        from app.db.session import get_session_factory
        from sqlalchemy import select

        async with get_session_factory()() as session:
            rows = (await session.scalars(select(Template))).all()
    except Exception as exc:  # noqa: BLE001 - memory is advisory, never fatal
        log.warning("Could not refresh DB-backed templates: %s", exc)
        return

    fresh: dict[str, tuple[CourseTemplate, bool, bool]] = {}
    for row in rows:
        try:
            config = CourseTemplate.model_validate(row.template_json)
        except Exception as exc:  # noqa: BLE001 - one bad row must not break the rest
            log.warning("Skipping invalid stored template '%s': %s", row.template_id, exc)
            continue
        fresh[row.template_id] = (config, row.is_current, row.is_archived)

    _db_templates.clear()
    _db_templates.update(fresh)
    load_template.cache_clear()


def clear_cache() -> None:
    load_template.cache_clear()


def register_uploaded_template(
    template: CourseTemplate,
    *,
    owner_id: str | None = None,
    visibility: str = "shared",
    is_active: bool = True,
) -> None:
    """Make a just-parsed uploaded template resolvable immediately.

    Called straight after an upload commits so the user can select and use it
    in the same session, without waiting for a refresh.
    """
    _uploaded_templates[template.template_id] = template
    _uploaded_access[template.template_id] = UploadedAccess(is_active, visibility, owner_id)
    load_template.cache_clear()
    log.info(
        "TEMPLATE_LOADED template_id=%s source=uploaded sections=%s",
        template.template_id,
        len(template.sections),
    )


def is_known_template_id(template_id: str) -> bool:
    """Whether `load_template` can resolve this id right now.

    Used to validate `template_id_override` on course creation. Deliberately
    separate from `available_templates()`, which backs the built-in picker and
    must not be flooded with every uploaded document.
    """
    resolved = TEMPLATE_IDS.get(template_id, template_id)
    return (
        resolved in _FILES
        or resolved in _uploaded_templates
        or resolved in _db_templates
    )


async def refresh_uploaded_templates() -> None:
    """Load every uploaded template that has a stored parse, plus who may use it.

    Same contract as `refresh_db_templates`: best-effort, never raises, and a
    single unparseable row is skipped rather than breaking the rest.
    """
    from app.core.config import get_settings

    if not get_settings().use_database:
        return
    try:
        from sqlalchemy import select

        from app.db.models import CourseTemplateDocument
        from app.db.session import get_session_factory

        # Every row, deleted ones included: courses pinned to a deleted
        # template must still render. Who may start a new course from one is
        # decided separately (`check_selectable`).
        async with get_session_factory()() as session:
            rows = (await session.scalars(select(CourseTemplateDocument))).all()
    except Exception as exc:  # noqa: BLE001 - never fatal, mirrors refresh_db_templates
        log.warning("Could not refresh uploaded templates: %s", exc)
        return

    fresh: dict[str, CourseTemplate] = {}
    access: dict[str, UploadedAccess] = {}
    for row in rows:
        template_id = f"{UPLOADED_PREFIX}{row.id}"
        access[template_id] = UploadedAccess(
            bool(row.is_active), getattr(row, "visibility", None) or "shared",
            str(row.created_by) if row.created_by else None,
        )
        if row.template_json is None:
            continue
        try:
            fresh[template_id] = CourseTemplate.model_validate(row.template_json)
        except Exception as exc:  # noqa: BLE001 - one bad row must not break the rest
            log.warning("Skipping invalid uploaded template '%s': %s", row.id, exc)

    _uploaded_templates.clear()
    _uploaded_templates.update(fresh)
    _uploaded_access.clear()
    _uploaded_access.update(access)
    load_template.cache_clear()
    log.info("Loaded %s uploaded template(s)", len(fresh))


def set_uploaded_access(template_id: str, *, is_active: bool, visibility: str, owner_id: str | None) -> None:
    """Record a delete or visibility change made in this process."""
    _uploaded_access[template_id] = UploadedAccess(is_active, visibility, owner_id)


def check_selectable(template_id: str, *, user_id: str | None, is_admin: bool) -> None:
    """Raise ValidationFailedError unless a NEW course may use this template.

    Stricter than `is_known_template_id`, which only says an id resolves:
    an archived or superseded admin template, and a deleted, unparsed or
    someone else's private upload, all resolve (existing courses need them)
    but cannot start a new course. Never falls back to another template.
    """
    resolved = TEMPLATE_IDS.get(template_id, template_id)
    if resolved in _FILES:
        return
    if is_uploaded_template_id(resolved):
        access = _uploaded_access.get(resolved)
        if access is None:
            raise ValidationFailedError(f"Unknown template_id_override '{template_id}'")
        if not access.is_active:
            raise ValidationFailedError(f"Template '{template_id}' was deleted and cannot be used for new courses.")
        if access.visibility != "shared" and not is_admin and access.owner_id != user_id:
            raise ValidationFailedError(f"Template '{template_id}' is private to the person who uploaded it.")
        if resolved not in _uploaded_templates:
            raise ValidationFailedError(
                f"Template '{template_id}' could not be read when it was uploaded, so it cannot drive a course. "
                "Upload the file again to process it."
            )
        return
    cached = _db_templates.get(resolved)
    if cached is not None:
        _config, is_current, is_archived = cached
        if is_archived:
            raise ValidationFailedError(f"Template '{template_id}' is archived and cannot be used for new courses.")
        if not is_current:
            raise ValidationFailedError(
                f"Template '{template_id}' is an older version; choose the current version of this template."
            )
        return
    raise ValidationFailedError(
        f"Unknown template_id_override '{template_id}'",
        details={"available": sorted(available_template_ids())},
    )


async def refresh_all_templates() -> None:
    await refresh_db_templates()
    await refresh_uploaded_templates()


async def ensure_template_loaded(template_id: str) -> None:
    """Refresh the caches when this process has not seen `template_id` yet.

    Each backend worker keeps its own cache; a template created or uploaded
    through another worker is picked up here on first use instead of failing
    until the next periodic refresh.
    """
    if not is_known_template_id(template_id):
        await refresh_all_templates()


async def refresh_templates_periodically(interval_seconds: float) -> None:
    """Keep this worker's caches in step with writes made by other workers."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await refresh_all_templates()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.warning("Periodic template refresh failed: %s", exc)
