"""Uploaded course template documents (DOCX/Markdown -> normalized Markdown).

Deliberately separate from schemas/template.py (the JSONB block-schema the
generation pipeline reads) and schemas/memory.py's TemplateRecord - this is a
prose reference document, not a pipeline config, and shares nothing with
either shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TemplateType = Literal["technical", "non_technical"]


class CourseTemplateDocumentSummary(BaseModel):
    """List/card view - no markdown_content, which can be large."""

    model_config = ConfigDict(extra="allow")

    id: UUID
    name: str
    template_type: TemplateType
    description: str
    source_format: Literal["docx", "md"]
    content_format: Literal["markdown"]
    # The registry id to pass as `template_id_override` on course creation.
    # None when the document has no usable parse (a Markdown upload, or a
    # DOCX the parser could not read) - the UI uses this to tell the two
    # apart rather than offering a template that cannot be loaded.
    template_id: str | None = None
    # What the DOCX parser detected, could not reproduce, and did not
    # recognise. See app.course.templates.docx_parser.parser.report().
    parse_report: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class CourseTemplateDocumentDetail(CourseTemplateDocumentSummary):
    """Detail/preview view - includes the normalized Markdown content."""

    markdown_content: str


class CourseTemplateDocumentListResponse(BaseModel):
    templates: list[CourseTemplateDocumentSummary]


class CourseTemplateDocumentUploadForm(BaseModel):
    """Validated multipart form fields (the file itself arrives separately as
    an UploadFile - see api/course_templates.py)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    template_type: TemplateType
    description: str = Field(default="", max_length=2000)


# --- default template auto-classification ------------------------------------
# Backs the hardcoded "Default Template" option on the Templates page: it has
# no fixed type of its own, so picking it classifies the course as Technical
# or Non-Technical from its title/audience. Deliberately not part of the
# generation pipeline - only resolves which `TemplateKind` the pipeline (still
# untouched) receives.


class TemplateTypeClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_title: str = Field(min_length=1, max_length=300)
    target_audience: str = Field(default="", max_length=2000)


class TemplateTypeClassification(BaseModel):
    """`model_config = extra="ignore"` and every field defaulted so the
    offline mock AI client's `model_validate({})` still produces a valid,
    deterministic result (see app/services/mock_ai.py)."""

    model_config = ConfigDict(extra="ignore")

    template_type: TemplateType = "technical"
    reasoning: str = ""
