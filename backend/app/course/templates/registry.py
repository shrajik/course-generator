"""Template registry - loads and validates template configurations.

Two sources, merged:
* Static JSON files under this directory (`technical_v1`, `non_technical_v1`)
  - the built-in defaults, unchanged since before the AI memory layer.
* DB-backed templates (app.db.models.Template, app.services.template_service)
  - created/edited through the admin API. Every version is immutable once
  created, so a course that stored `template_id="my_template_v3"` keeps
  resolving to exactly that content forever, even after a newer version
  becomes current.

`load_template()` stays a plain, synchronous, cheap lookup - every existing
caller (writer, reviewer, PDF export, dozens of call sites) calls it
synchronously today, and keeping it that way avoids an async refactor with a
huge blast radius. DB templates are instead kept in a small in-process cache,
refreshed explicitly (`refresh_db_templates()`) right after any write and
once, best-effort, at app startup - never on the read path, so a slow or
unavailable database can never block a template lookup.
"""

from __future__ import annotations

import json
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

# template_id -> (config, is_current, is_archived). Populated by
# refresh_db_templates(); empty (and therefore a no-op) until the database is
# available and something has actually been created - existing behaviour is
# byte-identical when no custom templates exist.
_db_templates: dict[str, tuple[CourseTemplate, bool, bool]] = {}


@lru_cache(maxsize=64)
def load_template(template_id: str) -> CourseTemplate:
    """Load a template by id (`technical_v1`), kind (`technical`), or a
    DB-backed versioned id (`my_template_v3`)."""
    resolved = TEMPLATE_IDS.get(template_id, template_id)

    cached = _db_templates.get(resolved)
    if cached is not None:
        return cached[0]

    filename = _FILES.get(resolved)
    if filename is None:
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
