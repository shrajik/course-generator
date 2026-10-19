"""Instructional-design models a creator can pick for a course.

Client requirement (Methodologies section 2): five models - ADDIE, Bloom's
Taxonomy, SAM, Gagne's Nine Events and Kirkpatrick's Four Levels - each with
its own required inputs. This is the data; app/course/design_models.py turns
it into prompt rules, reviewer checks and the plan documents.

Optional on `CourseInput`: a course without it (every course created before
this feature) generates prompts that are byte-identical to what they were.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DesignModelId = Literal["addie", "bloom", "sam", "gagne", "kirkpatrick"]
BloomLevel = Literal["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
DataSources = Literal["lms_only", "lms_and_kpis"]

DESIGN_MODEL_LABELS: dict[str, str] = {
    "addie": "ADDIE",
    "bloom": "Bloom's Taxonomy",
    "sam": "SAM (Successive Approximation Model)",
    "gagne": "Gagne's Nine Events of Instruction",
    "kirkpatrick": "Kirkpatrick's Four Levels",
}


class DesignModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[DesignModelId] = Field(default_factory=list)

    # SAM - "ask if unclear" becomes a required choice once SAM is selected.
    sam_loop: Literal["SAM1", "SAM2"] | None = None
    sam_review_ready: str = Field(default="", max_length=500)

    # Gagne - a short lesson compresses the nine events (keeps 2, 4, 6, 8).
    microlearning: bool = False

    # Bloom - optional target depth; blank means a full spread.
    bloom_target_level: BloomLevel | None = None

    # Kirkpatrick - the goal the course supports, and what data can be collected.
    business_goal: str = Field(default="", max_length=1000)
    data_sources: DataSources = "lms_only"

    @model_validator(mode="after")
    def _check(self) -> "DesignModels":
        # Order and duplicates carry no meaning; keep the first of each.
        self.models = list(dict.fromkeys(self.models))
        if "sam" in self.models and self.sam_loop is None:
            raise ValueError("SAM needs a loop type: SAM1 (small project) or SAM2 (larger project)")
        if "kirkpatrick" in self.models and not self.business_goal.strip():
            raise ValueError("Kirkpatrick needs the business or performance goal the course supports")
        return self

    def has(self, model: str) -> bool:
        return model in self.models
