"""Small, readable, prefixed identifiers. No database sequences needed."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

_SUFFIX_LEN = 12


def new_suffix() -> str:
    return uuid.uuid4().hex[:_SUFFIX_LEN]


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_suffix()}"


def course_id() -> str:
    return new_id("crs")


def document_id_for_course(course_id_value: str) -> str:
    """Documents are derived from their course so lookups need no index table."""
    return f"doc_{suffix_of(course_id_value)}"


def course_id_for_document(document_id_value: str) -> str:
    return f"crs_{suffix_of(document_id_value)}"


def suffix_of(identifier: str) -> str:
    return identifier.split("_", 1)[-1]


def block_id() -> str:
    return new_id("block")


def page_id(page_number: int) -> str:
    return f"page_{page_number}"


def slugify(value: str, *, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:max_length] or "untitled"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()
