"""Structured learner profile (client requirement: "Creator Interface" panels).

Replaces nothing - `CourseInput.target_audience` (free text) stays exactly as
it was. This is an *optional* addition: when a course carries a profile, the
pedagogy layer (app/course/pedagogy.py) turns it into a constraints brief that
the planner/writer/reviewer prompts receive. Courses created before this
feature have `learner_profile is None` and generate byte-identical prompts.

Every field is defaulted so a partially-filled profile from an older stored
row still validates (see `_coerce_course_input` in app/db/service.py).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Panel A - learner demographics.
AgeGroup = Literal["KG_PRIMARY", "MIDDLE_SECONDARY", "HIGHER_ED", "PROFESSIONAL"]
EduLevel = Literal["PRIMARY", "SECONDARY", "BACHELORS", "MASTERS_PHD"]

AGE_GROUP_LABELS: dict[str, str] = {
    "KG_PRIMARY": "KG to Primary (ages 5-10)",
    "MIDDLE_SECONDARY": "Middle/Secondary (ages 11-17)",
    "HIGHER_ED": "Higher Education (undergraduate & post-graduate)",
    "PROFESSIONAL": "Corporate/Professional (working adults & job seekers)",
}

EDU_LEVEL_LABELS: dict[str, str] = {
    "PRIMARY": "Primary schooling",
    "SECONDARY": "Secondary schooling",
    "BACHELORS": "Bachelor's degree",
    "MASTERS_PHD": "Master's or doctorate",
}


class LearnerProfile(BaseModel):
    """Panels A-C of the creator interface, as data.

    `extra="forbid"` matches `CourseInput`; every field is defaulted so adding
    fields later stays backward compatible.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Panel A: demographics ----------------------------------------------
    age_group: AgeGroup = "PROFESSIONAL"
    edu_level: EduLevel = "BACHELORS"

    # --- Panel B: the cognitive bridge --------------------------------------
    # Free text rather than an enum: the client's examples (STEM, COMMERCE,
    # FINANCE, SOFTWARE_ENG, ...) are illustrative, and B2B clients will have
    # their own domain vocabulary. Comparison is case/whitespace-insensitive -
    # see `domain_match_index` in app/course/pedagogy.py.
    source_domain: str = Field(default="", max_length=120)
    target_domain: str = Field(default="", max_length=120)

    # --- Panel C: methodology & style sliders -------------------------------
    jargon_density: float = Field(default=0.5, ge=0.0, le=1.0)
    scaffolding_depth: float = Field(default=0.5, ge=0.0, le=1.0)
    gamification_index: float = Field(default=0.2, ge=0.0, le=1.0)
    # Hard word cap for a single text block ("delivery chunk size"). Bounded
    # generously - the per-archetype defaults live in app/course/pedagogy.py.
    chunk_word_cap: int = Field(default=450, ge=50, le=2000)

    # The client's UAT item 5.1 "Manual Override": selecting an age group
    # normally snaps the sliders to that archetype's baseline. When the
    # designer has deliberately moved them, the frontend sets this so later
    # age-group changes stop overwriting their choices. Backend-visible only
    # so the brief can say the configuration was hand-tuned.
    manual_override: bool = False
