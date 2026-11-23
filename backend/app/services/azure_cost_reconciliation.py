"""Azure Cost Management reconciliation for MAI-Image-2.6 (admin-only).

MAI-Image-2.6's generation response never returns a usage/token-count field
(see app.core.pricing's module docstring, point 2), so this app cannot price
it exactly from its own data the way it prices every other model. This
module asks Azure's own Cost Management API what it actually billed instead
- the one place the real number genuinely lives, regardless of what the
generation API's response shape does or doesn't report.

Two things confirmed live (against the real subscription, 2026-11-23) that
shaped this design rather than being assumed from docs:

1. SHARED RESOURCE, SEPARATE METERS. Every azure_*_endpoint setting in
   app.core.config (gpt-5, gpt-5-mini, gpt-5.5, embeddings, FLUX.2-flex,
   MAI-Image-2.6) points at the SAME Azure resource host - so grouping Cost
   Management's query by `ResourceId` would mix every model's spend
   together, including a wholly separate "Grounding with Bing" meter
   observed on the same resource. Grouping by the `Product` dimension
   instead cleanly isolates MAI-Image-2.6, because Azure still meters each
   MODEL separately even within one shared resource - confirmed live, the
   account's real `Product` values include three MAI-Image-2.6-specific
   entries (`_MAI_IMAGE_PRODUCT_PREFIX` below) alongside entries like "Azure
   OpenAI GPT5 - ..." and "Azure BFL Flux Models - ...". No residual-
   subtraction approximation is needed - this is a real, exact isolation.
2. RATE LIMITING IS REAL AND TIGHT. Two Cost Management queries issued only
   seconds apart both came back 429 during live testing. This module makes
   exactly ONE query per `fetch_mai_image_actual_cost_inr` call and expects
   its caller (the admin reconciliation endpoint) to be called infrequently
   by a human, not polled - never call this in a loop or per-page-load
   without a cache in front of it.

Also confirmed live: Cost Management's own ingestion lag means the most
recent day or two of a queried window can report an artificially low/
incomplete total - `reconciliation_window` excludes them from the default
window by construction (see DEFAULT_INGESTION_LAG_DAYS).

Every failure mode here (not configured, auth failure, request failure,
rate-limited past budget, unexpected response shape) returns a
`CostQueryOutcome` with `total_inr=None` and a stated `error` - never a
fabricated 0 and never a raised exception, so a temporarily-unreachable
Cost Management API degrades the admin endpoint to "unavailable, here's why"
rather than breaking it.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_COST_QUERY_URL_TEMPLATE = (
    "https://management.azure.com/subscriptions/{subscription}"
    "/providers/Microsoft.CostManagement/query?api-version={api_version}"
)
# Confirmed live, 2026-11-23 - the Query API's documented, valid dimension
# names are NOT what common examples suggest ("MeterName"/"MeterSubCategory"
# both 400); "Product" and "Meter" are the real ones. "Product" is used here
# because its values are the more stable, human-readable billing names.
_COST_QUERY_API_VERSION = "2025-03-01"

# See this module's own docstring, point 1. Matched by PREFIX, not an exact
# set, so a region change (the live names end "- US East") doesn't silently
# stop matching - confirmed live real values as of 2026-11-23:
#   "MAI Models - Image 2.6 Image Output Glbl - US East"
#   "MAI Models - Image 2.6 Text Input Glbl - US East"
#   "MAI Models - Image 2.6 Image Input Glbl - US East"
# (the third exists because of edit_image/correction calls sending an input
# image - app.core.pricing doesn't price those separately today, but Azure's
# real billing already does, and this reconciliation captures that for free.)
_MAI_IMAGE_PRODUCT_PREFIX = "MAI Models - Image 2.6"

# Confirmed live: two queries seconds apart both 429'd - a short, cautious
# budget, since this module is meant to be called rarely (see docstring).
_RATE_LIMIT_ATTEMPTS = 4
_RATE_LIMIT_WAIT_MIN = 15.0
_RATE_LIMIT_WAIT_MAX = 25.0

# See this module's own docstring, final paragraph before the code.
DEFAULT_INGESTION_LAG_DAYS = 2
DEFAULT_LOOKBACK_DAYS = 30


@dataclass
class CostQueryOutcome:
    """Either a real total (`total_inr` set, `error` None) or a clearly
    stated reason it couldn't be fetched (`total_inr` None, `error` set) -
    never a fabricated 0 for a real failure, same philosophy as
    app.core.pricing.estimate_call_cost_usd returning None for an
    unrecognised model rather than guessing."""

    total_inr: float | None
    error: str | None = None


def reconciliation_window(
    *,
    today: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lag_days: int = DEFAULT_INGESTION_LAG_DAYS,
) -> tuple[date, date]:
    """The default [start, end) window for a reconciliation run: the
    `lookback_days` days ending `lag_days` days ago - never includes "today"
    or the day before by default, since Cost Management's own ingestion lag
    can make those look artificially cheap (see module docstring). `today`
    is a test seam; production callers always omit it."""
    anchor = today or datetime.now(timezone.utc).date()
    end = anchor - timedelta(days=lag_days)
    start = end - timedelta(days=lookback_days)
    return start, end


def measured_inr_per_image(total_inr: float, image_count: int) -> float | None:
    """`total_inr / image_count`, or None when there were no real images to
    divide by (never a division-by-zero crash, never a fabricated 0 - an
    empty window means "no measurement", not "free")."""
    if image_count <= 0:
        return None
    return total_inr / image_count


def _is_configured(settings: Settings) -> bool:
    return bool(
        settings.azure_tenant_id.strip()
        and settings.azure_client_id.strip()
        and settings.azure_client_secret.strip()
        and settings.azure_subscription_id.strip()
    )


def _backoff_seconds(attempt: int) -> float:
    return min(2**attempt + random.random(), 20)


def _rate_limit_wait(retry_after: float | None) -> float:
    if retry_after is not None and retry_after > 0:
        return retry_after
    return random.uniform(_RATE_LIMIT_WAIT_MIN, _RATE_LIMIT_WAIT_MAX)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fetch_access_token(settings: Settings, *, client: httpx.AsyncClient) -> str | None:
    """OAuth2 client-credentials token for Azure Resource Manager
    (scope https://management.azure.com/.default) - the service principal
    only needs the "Cost Management Reader" RBAC role on the subscription,
    never any Azure OpenAI/Foundry access. Returns None (never raises) on
    any failure - the caller reports it as a CostQueryOutcome.error, since
    this reconciliation feature failing must never crash the admin endpoint
    it feeds. The client secret is read once from `settings` into the
    request body and never otherwise touched - not logged, not included in
    any returned error message (only httpx's own exception type name, or
    Azure's bare status code, ever appears in a message here)."""
    url = _TOKEN_URL_TEMPLATE.format(tenant=settings.azure_tenant_id)
    try:
        response = await client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "scope": "https://management.azure.com/.default",
            },
        )
    except httpx.HTTPError as exc:
        log.warning("Azure Cost Management token request failed: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("Azure Cost Management token request failed with status %s", response.status_code)
        return None
    try:
        token = response.json()["access_token"]
    except (ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


async def fetch_mai_image_actual_cost_inr(
    *,
    settings: Settings,
    window_start: date,
    window_end: date,
    transport: httpx.BaseTransport | None = None,
) -> CostQueryOutcome:
    """Real MAI-Image-2.6 cost (INR - Azure's own native billing currency
    for this account, never converted here) actually billed by Azure
    between `window_start` (inclusive) and `window_end` (exclusive), summed
    across its real billing meters (see _MAI_IMAGE_PRODUCT_PREFIX) - grouped
    by `Product` specifically so the other models/services sharing this
    app's one Azure resource never contaminate the total (see this module's
    own docstring, point 1).

    Returns CostQueryOutcome(total_inr=None, error=...) for every failure
    mode (not configured, auth failure, request failure, rate-limited past
    budget, unexpected response shape) - never raises, never fabricates a 0.
    `transport` is the standard httpx.MockTransport test-injection point,
    unused in production (see test_azure_cost_reconciliation.py)."""
    if not _is_configured(settings):
        return CostQueryOutcome(
            total_inr=None,
            error=(
                "Azure Cost Management reconciliation is not configured "
                "(AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET/AZURE_SUBSCRIPTION_ID)"
            ),
        )

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{window_start.isoformat()}T00:00:00+00:00",
            "to": f"{window_end.isoformat()}T00:00:00+00:00",
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "Product"}],
        },
    }
    url = _COST_QUERY_URL_TEMPLATE.format(
        subscription=settings.azure_subscription_id, api_version=_COST_QUERY_API_VERSION
    )

    async with httpx.AsyncClient(timeout=settings.request_timeout, transport=transport) as client:
        token = await _fetch_access_token(settings, client=client)
        if token is None:
            return CostQueryOutcome(total_inr=None, error="Azure AD authentication failed")

        headers = {"Authorization": f"Bearer {token}"}
        attempts = max(settings.max_call_retries, 1)
        failures = 0
        throttled = 0
        response: httpx.Response | None = None
        while True:
            try:
                response = await client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                failures += 1
                if failures >= attempts:
                    return CostQueryOutcome(
                        total_inr=None, error=f"Cost Management request failed: {type(exc).__name__}"
                    )
                await asyncio.sleep(_backoff_seconds(failures))
                continue

            if response.status_code == 200:
                break
            if response.status_code == 429:
                throttled += 1
                if throttled >= _RATE_LIMIT_ATTEMPTS:
                    return CostQueryOutcome(total_inr=None, error="Cost Management request rate-limited repeatedly")
                await asyncio.sleep(_rate_limit_wait(_retry_after_seconds(response)))
                continue
            return CostQueryOutcome(
                total_inr=None, error=f"Cost Management request failed with status {response.status_code}"
            )

    assert response is not None  # the loop above always returns or breaks with a response set
    try:
        payload = response.json()
        columns = [column.get("name") for column in payload["properties"]["columns"]]
        rows = payload["properties"]["rows"]
        cost_idx = columns.index("Cost")
        product_idx = columns.index("Product")
    except (KeyError, ValueError, TypeError):
        return CostQueryOutcome(total_inr=None, error="Unexpected Cost Management response shape")

    total = sum(
        float(row[cost_idx])
        for row in rows
        if isinstance(row[product_idx], str) and row[product_idx].startswith(_MAI_IMAGE_PRODUCT_PREFIX)
    )
    return CostQueryOutcome(total_inr=total, error=None)
