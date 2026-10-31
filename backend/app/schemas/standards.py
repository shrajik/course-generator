"""Packaging/delivery-tracking standards (Content Studio Phase 1 - backend
data-model foundation).

Scope deliberately narrow: this models WHICH packaging standard a course
targets and the handful of facts that standard needs (an LMS name, an
authoring tool, whether tracking happens beyond the LMS session, whether a
Learning Record Store is confirmed) - never a validation or export engine.
`app.course.compliance` reads these fields (when present) to describe the
real choice in its QA/metadata output instead of the permanent "no packaging
standard selected" placeholder it used before this field existed; nothing
here generates an actual SCORM/xAPI package.

Optional and additive throughout, same contract as `DesignModels`/
`StyleGuide` on `CourseInput`: a course created before this field existed
has `packaging is None` and every downstream consumer (compliance.py,
prompts.py) falls back to its pre-existing behaviour exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PackagingStandard = Literal["scorm_1_2", "scorm_2004", "xapi", "cmi5", "aicc", "lti"]
AuthoringTool = Literal["articulate_storyline", "rise_360", "captivate", "custom_html5"]

PACKAGING_STANDARD_LABELS: dict[str, str] = {
    "scorm_1_2": "SCORM 1.2",
    "scorm_2004": "SCORM 2004",
    "xapi": "xAPI",
    "cmi5": "cmi5",
    "aicc": "AICC",
    "lti": "LTI",
}

AUTHORING_TOOL_LABELS: dict[str, str] = {
    "articulate_storyline": "Articulate Storyline",
    "rise_360": "Rise 360",
    "captivate": "Captivate",
    "custom_html5": "Custom HTML5",
}


class PackagingSettings(BaseModel):
    """Packaging & tracking choices for one course. Every field is optional/
    defaulted so a partially-filled selection still validates, same
    contract as `LearnerProfile`/`StyleGuide`."""

    model_config = ConfigDict(extra="forbid")

    standard: PackagingStandard | None = None
    target_lms: str = Field(default="", max_length=200)
    authoring_tool: AuthoringTool | None = None
    # Mobile, simulations or real-world practice tracked outside the LMS
    # session - only meaningful (and only reliably possible) with xAPI/cmi5;
    # see app.course.compliance's own note on SCORM being unable to record
    # it. This field states the INTENT; it never validates that `standard`
    # actually supports it - that judgment call belongs to the UI/reviewer,
    # not a hard schema rule, since a course can legitimately select this
    # before finalising the standard.
    track_beyond_lms: bool = False
    # Whether a Learning Record Store is confirmed to receive xAPI
    # statements - distinct from `track_beyond_lms` (the ask) vs this (the
    # delivery destination actually being ready).
    lrs_confirmed: bool = False


# --- accessibility (Content Studio Phase 2) ----------------------------------

AccessibilityTarget = Literal["wcag21_aa", "wcag22_aa"]
ExtraAccessibilityRequirement = Literal["section_508", "en_301_549", "pdf_ua"]

ACCESSIBILITY_TARGET_LABELS: dict[str, str] = {
    "wcag21_aa": "WCAG 2.1 AA",
    "wcag22_aa": "WCAG 2.2 AA",
}

EXTRA_ACCESSIBILITY_LABELS: dict[str, str] = {
    "section_508": "Section 508 (US federal clients)",
    "en_301_549": "EN 301 549 (EU public sector)",
    "pdf_ua": "PDF/UA (any PDF deliverable)",
}


class AccessibilitySettings(BaseModel):
    """Accessibility choices for one course. `app.course.compliance` already
    runs a real WCAG contrast check and a fixed set of "always applied"
    checks (alt text, heading order, ...) regardless of this model - these
    fields only widen WHAT gets checked (the target level, extra regulatory
    requirements, and additional brand colours to contrast-check beyond the
    document's own theme colours), never replace the existing checks."""

    model_config = ConfigDict(extra="forbid")

    compliance_target: AccessibilityTarget = "wcag22_aa"
    extra_requirements: list[ExtraAccessibilityRequirement] = Field(default_factory=list)
    # Hex strings (e.g. "#0E6E66") checked against white, same WCAG formula
    # app.course.compliance.contrast_ratio already uses for the document's
    # own theme colours - this just widens the set of colours it checks.
    brand_colors: list[str] = Field(default_factory=list, max_length=20)


# --- metadata (Content Studio Phase 2) ---------------------------------------

MetadataSchema = Literal["lom", "dublin_core"]


class MetadataSettings(BaseModel):
    """Metadata choices for one course. `app.course.compliance` already
    builds a full LOM or Dublin Core block for every report - these fields
    only set a stored DEFAULT schema (an explicit `?schema=` request still
    overrides it per-call, unchanged) and the two pieces of information this
    codebase had no place to record before: an organisation's own tagging
    convention and an explicit owner/requester name, distinct from whichever
    user happens to be signed in when the report is requested."""

    model_config = ConfigDict(extra="forbid")

    # None keeps today's behaviour: every caller that never set a
    # preference still gets "lom" by default.
    schema_preference: MetadataSchema | None = None
    tagging_conventions: str = Field(default="", max_length=500)
    owner_override: str = Field(default="", max_length=200)
