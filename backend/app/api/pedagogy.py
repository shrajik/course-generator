"""Read-only pedagogy lookup data for the creator interface.

The archetype baselines live in app/course/pedagogy.py as the single source of
truth for both the prompts and the UI. Serving them here means the frontend's
slider defaults cannot drift out of sync with the rules the writing agents are
actually given (client UAT 5.1, "Slider Coordination").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_current_user_if_db_enabled
from app.course.pedagogy import ARCHETYPES, slider_defaults
from app.db.models import User
from app.schemas.learner import AGE_GROUP_LABELS, EDU_LEVEL_LABELS

router = APIRouter(prefix="/api/pedagogy", tags=["pedagogy"])


class ArchetypeDefaults(BaseModel):
    age_group: str
    label: str
    instructional_model: str
    session_length: str
    jargon_density: float
    scaffolding_depth: float
    gamification_index: float
    chunk_word_cap: int


class PedagogyDefaultsResponse(BaseModel):
    age_groups: list[ArchetypeDefaults]
    edu_levels: dict[str, str]


@router.get("/defaults", response_model=PedagogyDefaultsResponse)
async def get_pedagogy_defaults(
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> PedagogyDefaultsResponse:
    """Baseline slider positions per learner archetype, for the Create Course form.

    Authenticated wherever identity exists, but unauthenticated in offline
    (`USE_DATABASE=false`) mode, matching the course routes this form posts to
    - these are static lookup constants, not anyone's data.
    """
    return PedagogyDefaultsResponse(
        age_groups=[
            ArchetypeDefaults(
                age_group=age_group,
                label=AGE_GROUP_LABELS[age_group],
                instructional_model=rules.instructional_model,
                session_length=rules.session_length,
                **slider_defaults(age_group),
            )
            for age_group, rules in ARCHETYPES.items()
        ],
        edu_levels=dict(EDU_LEVEL_LABELS),
    )
