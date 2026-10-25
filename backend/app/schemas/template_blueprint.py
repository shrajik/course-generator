"""A template's content-and-visual generation blueprint.

A `CourseTemplate` says which sections a chapter has and how it looks. The
blueprint adds what an uploaded DOCX or Markdown file teaches about *how the
course is written and illustrated*: writing rules, which visuals are needed
and where, worked examples that show the expected pattern, and assets the
document allows to be reused.

It is optional on `CourseTemplate` (None = no blueprint), so every template
made before it - the two built-ins, admin JSON templates, earlier uploads -
behaves exactly as before and their prompts stay byte-identical.

Everything here is *data parsed from a user's file*. It reaches the AI as
rules inside a clearly bounded prompt section and can never change the course
topic, audience, grounding rules or system rules (see
app.course.templates.blueprint.brief).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BLUEPRINT_SCHEMA_VERSION = 1

VisualKind = Literal["any", "diagram", "illustration", "concept_experience", "chart", "photo", "screenshot"]
Placement = Literal["anywhere", "after_text", "before_text", "end_of_section"]
VisualScope = Literal["chapter", "each_section", "section"]
Support = Literal["full", "partial", "none"]
Aspect = Literal["", "landscape", "portrait", "square"]


class TextRules(BaseModel):
    """How the text is written. Every field may be empty: a rule the file
    does not state is left unset, never guessed."""

    model_config = ConfigDict(extra="forbid")

    tone: str = ""
    reading_level: str = ""
    depth: str = ""
    words_per_chapter: int | None = None
    paragraph_length: str = ""
    section_length: str = ""
    explanation_patterns: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    terminology: list[str] = Field(default_factory=list)
    assessments: list[str] = Field(default_factory=list)
    introductions: list[str] = Field(default_factory=list)
    conclusions: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)
    # Which of the fields above were inferred from the file's examples rather
    # than stated in it - shown to the user so they can tell the two apart.
    inferred: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        data = self.model_dump(exclude={"inferred"})
        return not any(value for value in data.values())


class VisualRule(BaseModel):
    """One visual the template asks for."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "V1", stable within the blueprint; the writer echoes it back
    scope: VisualScope = "chapter"
    section_key: str = ""  # only for scope == "section"
    kind: VisualKind = "any"
    diagram_kind: str = ""  # one of app.schemas.diagram.DIAGRAM_KINDS, or ""
    instruction: str = ""  # the template's own words, clipped
    required: bool = False
    placement: Placement = "anywhere"
    labels: list[str] = Field(default_factory=list)
    labelled: bool = False
    caption_required: bool = False
    count: int | None = None
    aspect: Aspect = ""
    aspect_text: str = ""  # as written, e.g. "16:9"
    asset_id: str = ""  # reuse this template asset instead of generating
    origin: Literal["stated", "inferred"] = "stated"
    support: Support = "full"
    note: str = ""  # why support is partial/none, shown to the user


class TemplateExample(BaseModel):
    """A worked example from the file: a pattern to follow, never content to copy."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "E1"
    section_key: str = ""
    kind: Literal["text", "image", "code", "table"] = "text"
    text: str = ""
    caption: str = ""
    asset_id: str = ""
    pattern: list[str] = Field(default_factory=list)  # e.g. ["definition", "analogy"]


class TemplateAsset(BaseModel):
    """An image extracted from a DOCX template."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "A1"
    path: str = ""  # relative to settings.data_dir
    content_type: str = ""
    width: int | None = None
    height: int | None = None
    caption: str = ""
    context: str = ""  # the text next to it in the file
    section_key: str = ""
    role: Literal["example", "reusable"] = "example"


class DesignSpec(BaseModel):
    """The source document's look, kept apart from the teaching rules.

    `applied` is False unless the uploader ticked "apply this document's
    design" or the document itself explicitly asks for it - a Canva-style
    ebook teaches structure, not colours.
    """

    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    reason: str = ""  # "uploader" | "document" | ""
    extracted: dict[str, Any] = Field(default_factory=dict)


class TemplateBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = BLUEPRINT_SCHEMA_VERSION
    source_format: Literal["docx", "md", "json"] = "json"
    text: TextRules = Field(default_factory=TextRules)
    visuals: list[VisualRule] = Field(default_factory=list)
    # False = the template forbids decorative section-opening pictures, so
    # the writer's default `section_intro` illustration is switched off.
    # None = the template says nothing, default behaviour stays.
    section_intro_images: bool | None = None
    examples: list[TemplateExample] = Field(default_factory=list)
    assets: list[TemplateAsset] = Field(default_factory=list)
    design: DesignSpec = Field(default_factory=DesignSpec)
    # What the reader could not use, and why - surfaced on the upload preview.
    warnings: list[str] = Field(default_factory=list)
    # Lines dropped because they try to change system behaviour.
    ignored: list[str] = Field(default_factory=list)
    # Missing or contradictory requirements found by validation.
    problems: list[str] = Field(default_factory=list)

    def visual(self, rule_id: str) -> VisualRule | None:
        return next((rule for rule in self.visuals if rule.id == rule_id), None)

    def asset(self, asset_id: str) -> TemplateAsset | None:
        return next((asset for asset in self.assets if asset.id == asset_id), None)

    def reusable_asset(self, asset_id: str) -> TemplateAsset | None:
        asset = self.asset(asset_id)
        return asset if asset is not None and asset.role == "reusable" else None
