"""API shapes for uploaded course templates (DOCX or Markdown).

An upload is processed into a `CourseTemplate` with a blueprint
(app.schemas.template / app.schemas.template_blueprint); these models are the
list, detail and upload views of it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.template_blueprint import TemplateBlueprint

TemplateType = Literal["technical", "non_technical"]
Visibility = Literal["private", "shared"]


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
    # None when the row has no processed template (only uploads from before
    # blueprints) - the UI then disables "Use" instead of offering a
    # template that cannot be loaded.
    template_id: str | None = None
    # What processing detected and could not use.
    # See app.course.templates.blueprint.builder.BuildResult.report().
    parse_report: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    visibility: Visibility = "shared"
    # Whether the current user may delete it or change who can see it.
    can_manage: bool = False
    usable: bool = True
    unusable_reason: str = ""
    # The processed sections and blueprint: the "what the system understood"
    # preview. Empty/None for rows without a processed template.
    sections: list[dict[str, Any]] = Field(default_factory=list)
    blueprint: TemplateBlueprint | None = None


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
    visibility: Visibility = "private"
    # Copy the DOCX's fonts, colours, page size and header/footer into the
    # courses made from it. Off by default: a template teaches structure and
    # patterns, not a look.
    apply_design: bool = False


class CourseTemplateDocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Visibility


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
