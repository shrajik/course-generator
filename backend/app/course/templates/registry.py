"""Template registry - loads and validates the JSON template configurations."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.core.errors import NotFoundError, ValidationFailedError
from app.schemas.course import TEMPLATE_IDS
from app.schemas.template import CourseTemplate

TEMPLATES_DIR = Path(__file__).parent
_FILES = {
    "technical_v1": "technical_v1.json",
    "non_technical_v1": "non_technical_v1.json",
}


@lru_cache(maxsize=8)
def load_template(template_id: str) -> CourseTemplate:
    """Load a template by id (`technical_v1`) or kind (`technical`)."""
    resolved = TEMPLATE_IDS.get(template_id, template_id)
    filename = _FILES.get(resolved)
    if filename is None:
        raise NotFoundError(
            f"Unknown template '{template_id}'. Available: {sorted(_FILES)}"
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


def available_templates() -> list[CourseTemplate]:
    return [load_template(tid) for tid in _FILES]


def clear_cache() -> None:
    load_template.cache_clear()
