"""Admin API response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.roles import Role


UserRole = Literal[Role.AUTHOR.value, Role.EDITOR_REVIEWER.value, Role.MANAGER.value, Role.ADMIN.value]


class AdminDashboardResponse(BaseModel):
    total_users: int
    total_courses: int
    system_status: str


class AdminUserResponse(BaseModel):
    id: UUID
    email: str
    role: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class UpdateUserRoleRequest(BaseModel):
    role: UserRole = Field(description="The role to assign to the user.")


class UpdateUserStatusRequest(BaseModel):
    is_active: bool = Field(description="Whether the user account is active.")


class AdminCourseResponse(BaseModel):
    id: UUID
    course_id: str
    document_id: str
    title: str
    status: str
    # Where the course is in expert review: draft | in_review | changes_requested | approved.
    review_status: str = "draft"
    template_id: str
    owner: str | None = None
    target_audience: str | None = None
    language: str | None = None
    tone: str | None = None
    toc_count: int = 0
    chapters_count: int = 0
    has_blueprint: bool = False
    has_document: bool = False
    last_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # Additive reflection of CourseRecord.quality_snapshot.outcome (see its
    # docstring in app.schemas.course) - distinct from `status`, which never
    # changes because of a quality finding. `None` when no snapshot exists
    # (never generated, or evaluation itself failed at that time).
    quality_outcome: Literal["passed", "failed"] | None = None
    run_state: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AdminCourseListResponse(BaseModel):
    courses: list[AdminCourseResponse]
    total: int
    limit: int
    offset: int
    statuses: list[str] = Field(default_factory=list)


class ModelUsageRecord(BaseModel):
    """One real AI call made while generating a course - the admin-facing
    shape of `app.core.metrics.CallRecord`, persisted into a
    `GenerationRun.payload_json["model_usage"]` row at the end of a run (see
    CourseService._finalise). MOM Phase 2.2 - AI Model Transparency.

    `provider`/`used_fallback` distinguish "which Azure resource/deployment
    actually answered" and "was this the originally-requested model or the
    configured fallback" from the bare model name alone. A deterministic
    stage (an SVG diagram, a concept_experience HTML render) never appears
    here at all - it never calls a model, so there's nothing to log; it is
    not represented as a fabricated "deterministic" entry, it is simply
    absent, same as the real CallRecord list this is read from."""

    kind: str
    purpose: str
    model: str
    provider: str = ""
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    # Real output resolution in megapixels, for an image call only - see
    # app.core.metrics.CallRecord.megapixels and app.core.pricing (the only
    # reader of this field, for FLUX.2-flex's real per-megapixel billing).
    megapixels: float = 0.0
    retries: int = 0
    failed: bool = False
    used_fallback: bool = False
    timestamp: str = ""
    # Estimated real-money cost of this one call, in INR - None when `model`
    # isn't a recognised, priced model (see app.core.pricing.estimate_call_
    # cost_usd; never a fabricated 0). `cost_estimated` is True only for the
    # MAI-Image-2.6 approximation (see app.core.pricing's module docstring,
    # point 2) - the UI must show this differently from an exact figure.
    cost_inr: float | None = None
    cost_estimated: bool = False


class AdminModelUsageResponse(BaseModel):
    course_id: str
    job_id: str | None = None
    run_state: str | None = None
    run_created_at: datetime | None = None
    calls: list[ModelUsageRecord] = Field(default_factory=list)
    # Real-money cost breakdown for this run's calls (see app.core.pricing) -
    # by_model_inr sums to total_inr by construction; both are 0 for a run
    # with no calls, never fabricated for one that has calls this app can't
    # price (see has_unpriced_models).
    by_model_inr: dict[str, float] = Field(default_factory=dict)
    total_cost_inr: float = 0.0
    # True when at least one call's `model` has no entry in app.core.pricing's
    # rate tables (an unrecognised/new model, or MockAIClient's "mock") - the
    # total above is a real sum of only the priced calls, not an admission
    # that nothing is missing.
    has_unpriced_models: bool = False
    # True when at least one call's cost used the documented MAI-Image-2.6
    # estimate rather than an exact rate (see app.core.pricing, point 2).
    has_estimated_models: bool = False


class MaiImageCostReconciliation(BaseModel):
    """Real-vs-estimate comparison for MAI-Image-2.6's per-image cost (see
    app.services.azure_cost_reconciliation and app.core.pricing's module
    docstring, point 2) - cost-optimization follow-up. `available=False`
    whenever Azure Cost Management couldn't be reached (not configured, auth
    failure, rate-limited, unexpected response, ...); every "real" field is
    then None/0, never a fabricated number - `unavailable_reason` says why,
    and the static estimate is still returned either way so the UI always
    has at least one number to show."""

    available: bool
    unavailable_reason: str | None = None
    window_start: date | None = None
    window_end: date | None = None
    # Real count of MAI-Image-2.6 calls this app itself recorded in the
    # window - the denominator behind measured_inr_per_image.
    real_image_count: int = 0
    # Real total Azure actually billed for MAI-Image-2.6 in the window (INR,
    # Azure's own native billing currency for this account) - None whenever
    # `available` is False.
    actual_total_cost_inr: float | None = None
    # actual_total_cost_inr / real_image_count - None when either the query
    # failed or there were zero real images in the window (never a
    # division-by-zero fabrication).
    measured_inr_per_image: float | None = None
    # app.core.pricing.MAI_IMAGE_ESTIMATED_USD_PER_IMAGE, in both units, for
    # direct side-by-side comparison against the measured figures above -
    # always present regardless of `available`, since it needs no live call.
    static_estimate_inr_per_image: float
    static_estimate_usd_per_image: float
