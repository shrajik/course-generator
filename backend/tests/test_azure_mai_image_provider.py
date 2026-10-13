"""Microsoft Foundry MAI-Image-2.6 image provider.

Everything here runs against `httpx.MockTransport` - never the real
network - and `OpenAIClient.image()`'s provider dispatch (openai / azure /
azure_mai / unsupported). No test requires AZURE_MAI_IMAGE_API_KEY to be set
in the real environment; every Settings instance below supplies its own
fake values explicitly, and nothing ever prints or asserts on the literal
key value - only that it reaches the `api-key` header unmodified.

Mirrors test_azure_image_provider.py's structure (the existing FLUX.2-flex
provider's test file) but asserts MAI-Image-2.6's own documented contract,
which genuinely differs from FLUX's - see azure_mai_image_provider.py's own
docstring for the confirmed differences (api-key header vs Bearer,
deployment name in the body, no `n` field, documented size bounds).
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.services import azure_mai_image_provider
from app.services.azure_mai_image_provider import generate_image, parse_image_size
from app.services.openai_service import OpenAIClient

_FAKE_KEY = "test-fake-azure-mai-key-not-real"
_ENDPOINT = "https://example-foundry.services.ai.azure.test/mai/v1/images/generations"
_DEPLOYMENT = "my-mai-image-26-deployment"
_PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png, just test bytes"


def _settings(**overrides) -> Settings:
    values = {
        "image_provider": "azure_mai",
        "azure_mai_image_endpoint": _ENDPOINT,
        "azure_mai_image_api_key": _FAKE_KEY,
        "azure_mai_image_deployment": _DEPLOYMENT,
        "max_call_retries": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _b64_response(data: bytes = _PNG_BYTES, status_code: int = 200) -> httpx.Response:
    body = {"data": [{"b64_json": base64.b64encode(data).decode("ascii")}]}
    return httpx.Response(status_code, json=body)


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    """Retry backoff normally sleeps for real seconds - never needed here
    since MockTransport responds instantly; this only removes wall-clock
    waiting from the test run, it changes no retry/decision logic."""

    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(azure_mai_image_provider.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# request shape - confirmed against the official MAI-Image-2.6 docs
# ---------------------------------------------------------------------------


async def test_correct_endpoint_is_called():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["url"] == _ENDPOINT


async def test_api_key_header_is_used_not_authorization_bearer(capsys):
    """The real, confirmed contract difference from FLUX.2-flex: MAI wants
    `api-key`, not `Authorization: Bearer`."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers.get("api-key")
        seen["authorization"] = request.headers.get("authorization")
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["api_key"] == _FAKE_KEY
    assert seen["authorization"] is None
    # The key must never land in captured stdout/stderr from this test run.
    captured = capsys.readouterr()
    assert _FAKE_KEY not in captured.out
    assert _FAKE_KEY not in captured.err


async def test_content_type_is_application_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["content_type"] == "application/json"


async def test_model_field_is_the_deployment_name_not_a_literal_model_string():
    """The other real, confirmed contract difference from FLUX.2-flex: MAI's
    `model` field must be the caller's deployment name, never a hardcoded
    model string like "MAI-Image-2.6"."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["model"] == _DEPLOYMENT


async def test_n_field_is_not_sent():
    """MAI's documented request parameters are model/prompt/width/height
    only - no `n` (unlike FLUX.2-flex, which sends `"n": 1`). An
    undocumented field risks a 400 per MAI's own troubleshooting table."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert "n" not in seen["body"]


def test_1024x1024_becomes_width_1024_height_1024():
    assert parse_image_size("1024x1024") == (1024, 1024)


async def test_size_string_is_translated_to_width_and_height_in_the_real_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["width"] == 1024
    assert seen["body"]["height"] == 1024
    assert "size" not in seen["body"]


async def test_prompt_is_passed_unchanged():
    seen = {}
    prompt = "a flat 2D vector textbook illustration of a neural network, labelled layers"

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt=prompt, size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["prompt"] == prompt


# ---------------------------------------------------------------------------
# documented size bounds (768px minimum per side, 2,359,296px ceiling)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_size", ["", "1024", "1024x", "x768", "widexhigh", "1024x1024x1"])
async def test_malformed_image_size_produces_a_clear_error(bad_size):
    with pytest.raises(AIServiceError):
        parse_image_size(bad_size)


@pytest.mark.parametrize("too_small", ["767x1024", "1024x767", "512x512"])
async def test_a_dimension_below_768px_fails_clearly_without_any_request(too_small):
    with pytest.raises(AIServiceError, match="768"):
        await generate_image(
            prompt="p", size=too_small, settings=_settings(),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


async def test_width_times_height_over_the_pixel_ceiling_fails_clearly_without_any_request():
    # 1600 * 1600 = 2,560,000 > 2,359,296 (MAI-Image-2.6's documented ceiling)
    with pytest.raises(AIServiceError, match="2359296|2,359,296"):
        await generate_image(
            prompt="p", size="1600x1600", settings=_settings(),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


async def test_the_exact_ceiling_1536x1536_is_accepted():
    width, height = parse_image_size("1536x1536")
    assert width * height == 2_359_296


# ---------------------------------------------------------------------------
# response decoding - identical shape to FLUX, confirmed from the docs
# ---------------------------------------------------------------------------


async def test_valid_b64_json_is_decoded_into_the_expected_bytes():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _b64_response(_PNG_BYTES)

    result = await generate_image(
        prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == _PNG_BYTES


async def test_missing_b64_json_produces_a_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{}]})

    with pytest.raises(AIServiceError, match="b64_json"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )


async def test_invalid_base64_produces_a_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": "not-valid-base64!!!"}]})

    with pytest.raises(AIServiceError, match="base64"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


async def test_429_is_retried_then_succeeds():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"error": "throttled"})
        return _b64_response()

    result = await generate_image(
        prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == _PNG_BYTES
    assert calls["count"] == 2


async def test_transient_5xx_is_retried_then_succeeds():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return _b64_response()

    result = await generate_image(
        prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == _PNG_BYTES
    assert calls["count"] == 2


async def test_retries_are_bounded_by_max_call_retries():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, json={"error": "unavailable"})

    with pytest.raises(AIServiceError):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(max_call_retries=3),
            transport=httpx.MockTransport(handler),
        )
    assert calls["count"] == 3


async def test_timeout_is_retried_then_raises_a_clear_error_when_exhausted():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    with pytest.raises(AIServiceError, match="timed out"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(max_call_retries=2),
            transport=httpx.MockTransport(handler),
        )


# ---------------------------------------------------------------------------
# clear failures (never retried, never silently wrong) - matching MAI's own
# documented troubleshooting table (401/404/400)
# ---------------------------------------------------------------------------


async def test_401_unauthorized_produces_a_clear_error_without_retrying():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": "invalid api key"})

    with pytest.raises(AIServiceError, match="401"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )
    assert calls["count"] == 1


async def test_404_not_found_produces_a_clear_error_without_retrying():
    """Per MAI's own documented cause: an incorrect deployment name or
    endpoint URL."""
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, json={"error": "deployment not found"})

    with pytest.raises(AIServiceError, match="404"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )
    assert calls["count"] == 1


async def test_400_bad_request_produces_a_clear_error_without_retrying():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(AIServiceError, match="400"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )
    assert calls["count"] == 1


async def test_missing_endpoint_fails_clearly_without_any_request():
    with pytest.raises(AIServiceError, match="AZURE_MAI_IMAGE_ENDPOINT"):
        await generate_image(
            prompt="p", size="1024x1024",
            settings=_settings(azure_mai_image_endpoint=""),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


async def test_missing_api_key_fails_clearly_without_any_request():
    with pytest.raises(AIServiceError, match="AZURE_MAI_IMAGE_API_KEY"):
        await generate_image(
            prompt="p", size="1024x1024",
            settings=_settings(azure_mai_image_api_key=""),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


async def test_missing_deployment_fails_clearly_without_any_request():
    with pytest.raises(AIServiceError, match="AZURE_MAI_IMAGE_DEPLOYMENT"):
        await generate_image(
            prompt="p", size="1024x1024",
            settings=_settings(azure_mai_image_deployment=""),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


# ---------------------------------------------------------------------------
# OpenAIClient.image() provider dispatch
# ---------------------------------------------------------------------------


async def test_image_provider_azure_mai_uses_the_azure_mai_implementation(monkeypatch):
    settings = Settings(
        image_provider="azure_mai",
        azure_mai_image_endpoint=_ENDPOINT,
        azure_mai_image_api_key=_FAKE_KEY,
        azure_mai_image_deployment=_DEPLOYMENT,
    )
    client = OpenAIClient(settings)

    called = {"openai": False, "azure": False, "azure_mai": False}

    async def fake_openai(**_kwargs):
        called["openai"] = True
        return b"openai-bytes"

    async def fake_azure(**_kwargs):
        called["azure"] = True
        return b"azure-bytes"

    async def fake_azure_mai(**_kwargs):
        called["azure_mai"] = True
        return b"azure-mai-bytes"

    monkeypatch.setattr(client, "_image_openai", fake_openai)
    monkeypatch.setattr(client, "_image_azure", fake_azure)
    monkeypatch.setattr(client, "_image_azure_mai", fake_azure_mai)

    result = await client.image(prompt="p")
    assert result == b"azure-mai-bytes"
    assert called == {"openai": False, "azure": False, "azure_mai": True}


async def test_image_provider_param_azure_mai_overrides_the_configured_default(monkeypatch):
    """This is exactly how ImageService routes a textbook-style illustration
    - `provider="azure_mai"` passed per-call, regardless of the configured
    `image_provider` default, and never mutating settings."""
    settings = Settings(image_provider="openai", openai_api_key="test-fake-openai-key")
    client = OpenAIClient(settings)

    called = {"openai": False, "azure_mai": False}

    async def fake_openai(**_kwargs):
        called["openai"] = True
        return b"openai-bytes"

    async def fake_azure_mai(**_kwargs):
        called["azure_mai"] = True
        return b"azure-mai-bytes"

    monkeypatch.setattr(client, "_image_openai", fake_openai)
    monkeypatch.setattr(client, "_image_azure_mai", fake_azure_mai)

    result = await client.image(prompt="p", provider="azure_mai")
    assert result == b"azure-mai-bytes"
    assert called == {"openai": False, "azure_mai": True}
    assert settings.image_provider == "openai"  # untouched


async def test_unsupported_image_provider_error_message_lists_azure_mai(monkeypatch):
    settings = Settings(image_provider="not-a-real-provider")
    client = OpenAIClient(settings)

    async def fail_if_called(**_kwargs):
        raise AssertionError("should never be called for an unsupported provider")

    monkeypatch.setattr(client, "_image_openai", fail_if_called)
    monkeypatch.setattr(client, "_image_azure", fail_if_called)
    monkeypatch.setattr(client, "_image_azure_mai", fail_if_called)

    with pytest.raises(AIServiceError, match="azure_mai"):
        await client.image(prompt="p")


async def test_azure_mai_failure_is_never_silently_retried_against_openai(monkeypatch):
    """The whole point of this migration: when Azure MAI-Image-2.6 fails
    (after its own bounded retries), the error propagates - it must never
    silently fall through to gpt-image-1 and spend OpenAI credits instead."""
    settings = Settings(
        image_provider="azure_mai",
        azure_mai_image_endpoint=_ENDPOINT,
        azure_mai_image_api_key=_FAKE_KEY,
        azure_mai_image_deployment=_DEPLOYMENT,
    )
    client = OpenAIClient(settings)

    async def failing_azure_mai(**_kwargs):
        raise AIServiceError("Azure MAI-Image-2.6 request failed with status 500: simulated failure")

    async def fail_if_called(**_kwargs):
        raise AssertionError("gpt-image-1 path must never be used as a silent fallback")

    monkeypatch.setattr(client, "_image_azure_mai", failing_azure_mai)
    monkeypatch.setattr(client, "_image_openai", fail_if_called)

    with pytest.raises(AIServiceError, match="simulated failure"):
        await client.image(prompt="p")
