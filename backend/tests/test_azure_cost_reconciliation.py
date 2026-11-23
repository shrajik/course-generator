"""Azure Cost Management reconciliation for MAI-Image-2.6
(app.services.azure_cost_reconciliation).

Everything here runs against `httpx.MockTransport` - never the real network.
No test requires AZURE_CLIENT_SECRET to be set in the real environment;
every Settings instance below supplies its own fake values explicitly, and
nothing ever asserts on the literal secret value appearing anywhere except
the one request field it's supposed to reach.

Mirrors test_azure_mai_image_provider.py's structure/conventions.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.core.config import Settings
from app.services import azure_cost_reconciliation
from app.services.azure_cost_reconciliation import (
    CostQueryOutcome,
    fetch_mai_image_actual_cost_inr,
    measured_inr_per_image,
    reconciliation_window,
)

_FAKE_TENANT = "11111111-1111-1111-1111-111111111111"
_FAKE_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_FAKE_CLIENT_SECRET = "test-fake-azure-client-secret-not-real"
_FAKE_SUBSCRIPTION = "33333333-3333-3333-3333-333333333333"
_FAKE_TOKEN = "test-fake-access-token-not-real"

_TOKEN_URL = f"https://login.microsoftonline.com/{_FAKE_TENANT}/oauth2/v2.0/token"
_QUERY_URL = (
    f"https://management.azure.com/subscriptions/{_FAKE_SUBSCRIPTION}"
    "/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
)


def _settings(**overrides) -> Settings:
    values = {
        "azure_tenant_id": _FAKE_TENANT,
        "azure_client_id": _FAKE_CLIENT_ID,
        "azure_client_secret": _FAKE_CLIENT_SECRET,
        "azure_subscription_id": _FAKE_SUBSCRIPTION,
        "max_call_retries": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": _FAKE_TOKEN, "expires_in": 3600})


def _query_response(rows: list[list], *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "properties": {
                "columns": [{"name": "Cost"}, {"name": "Product"}, {"name": "Currency"}],
                "rows": rows,
            }
        },
    )


# Real product names confirmed live against the real subscription - see this
# module's own docstring for the "- US East" prefix-matching rationale.
_REAL_MAI_ROWS = [
    [2507.14219379121, "MAI Models - Image 2.6 Image Output Glbl - US East", "INR"],
    [323.524467787251, "MAI Models - Image 2.6 Text Input Glbl - US East", "INR"],
    [3.88533300000001, "MAI Models - Image 2.6 Image Input Glbl - US East", "INR"],
]
_OTHER_MODEL_ROWS = [
    [6783.74795750002, "Azure BFL Flux Models - Flex - US East", "INR"],
    [1829.76028414701, "Azure OpenAI GPT5 - GPT 5 outpt Glbl - US East", "INR"],
    [787.015439000003, "Grounding with Bing - Search", "INR"],
]


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    """Retry backoff normally sleeps for real seconds - never needed here
    since MockTransport responds instantly; this only removes wall-clock
    waiting from the test run, it changes no retry/decision logic."""

    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(azure_cost_reconciliation.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# reconciliation_window / measured_inr_per_image - pure functions
# ---------------------------------------------------------------------------


def test_reconciliation_window_excludes_the_most_recent_lag_days_by_default():
    start, end = reconciliation_window(today=date(2026, 11, 23))
    assert end == date(2026, 11, 21)  # today minus the 2-day default ingestion lag
    assert start == date(2026, 10, 22)  # end minus the 30-day default lookback


def test_reconciliation_window_respects_custom_lookback_and_lag():
    start, end = reconciliation_window(today=date(2026, 11, 23), lookback_days=7, lag_days=1)
    assert end == date(2026, 11, 22)
    assert start == date(2026, 11, 15)


def test_measured_inr_per_image_divides_total_by_count():
    assert measured_inr_per_image(2834.5519945784613, 161) == pytest.approx(17.60591300980411)


def test_measured_inr_per_image_is_none_for_zero_images_not_a_crash():
    assert measured_inr_per_image(100.0, 0) is None
    assert measured_inr_per_image(100.0, -1) is None


# ---------------------------------------------------------------------------
# fetch_mai_image_actual_cost_inr
# ---------------------------------------------------------------------------


async def test_not_configured_returns_a_clear_error_without_any_network_call():
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(500)

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(azure_client_secret=""),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert isinstance(outcome, CostQueryOutcome)
    assert outcome.total_inr is None
    assert outcome.error is not None
    assert called["count"] == 0


async def test_successful_query_sums_only_the_mai_image_product_rows(capsys):
    """The real, confirmed shared-resource isolation: other models' rows in
    the same response must never be added into the total."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            assert _FAKE_CLIENT_SECRET in request.content.decode()
            return _token_response()
        assert str(request.url) == _QUERY_URL
        assert request.headers["authorization"] == f"Bearer {_FAKE_TOKEN}"
        body = json.loads(request.content)
        assert body["dataset"]["grouping"] == [{"type": "Dimension", "name": "Product"}]
        return _query_response(_REAL_MAI_ROWS + _OTHER_MODEL_ROWS)

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.error is None
    assert outcome.total_inr == pytest.approx(2834.5519945784613)

    # The secret must never leak into this test's own captured output.
    captured = capsys.readouterr()
    assert _FAKE_CLIENT_SECRET not in captured.out
    assert _FAKE_CLIENT_SECRET not in captured.err


async def test_auth_failure_is_reported_clearly_not_as_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.total_inr is None
    assert outcome.error == "Azure AD authentication failed"


async def test_a_rate_limited_request_retries_then_succeeds():
    attempts = {"query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return _token_response()
        attempts["query"] += 1
        if attempts["query"] < 2:
            return httpx.Response(429, headers={"retry-after": "1"})
        return _query_response(_REAL_MAI_ROWS)

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.error is None
    assert outcome.total_inr == pytest.approx(2834.5519945784613)
    assert attempts["query"] == 2


async def test_rate_limited_past_budget_returns_a_clear_error_not_an_infinite_loop():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return _token_response()
        return httpx.Response(429)

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.total_inr is None
    assert "rate-limited" in outcome.error


async def test_unexpected_response_shape_returns_a_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return _token_response()
        return httpx.Response(200, json={"somethingElse": True})

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.total_inr is None
    assert outcome.error == "Unexpected Cost Management response shape"


async def test_an_empty_result_set_is_a_real_zero_not_an_error():
    """No MAI-Image-2.6 product rows in the window (e.g. nothing generated)
    is a genuine zero total, not a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _TOKEN_URL:
            return _token_response()
        return _query_response(_OTHER_MODEL_ROWS)

    outcome = await fetch_mai_image_actual_cost_inr(
        settings=_settings(),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 21),
        transport=httpx.MockTransport(handler),
    )
    assert outcome.error is None
    assert outcome.total_inr == 0.0
