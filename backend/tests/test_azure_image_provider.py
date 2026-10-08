"""Microsoft Azure AI Foundry FLUX.2-flex image provider.

Everything here runs against `httpx.MockTransport` - never the real
network - and `OpenAIClient.image()`'s provider dispatch (openai / azure /
unsupported). No test requires AZURE_FLUX_API_KEY to be set in the real
environment; every Settings instance below supplies its own fake values
explicitly, and nothing ever prints or asserts on the literal key value -
only that it reaches the Authorization header unmodified.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.services import azure_image_provider
from app.services.azure_image_provider import generate_image, parse_image_size
from app.services.openai_service import OpenAIClient

_FAKE_KEY = "test-fake-azure-key-not-real"
_ENDPOINT = "https://example-foundry.azure.test/openai/images/generations"
_PNG_BYTES = b"\x89PNG\r\n\x1a\nnot a real png, just test bytes"


def _settings(**overrides) -> Settings:
    values = {
        "image_provider": "azure",
        "azure_flux_endpoint": _ENDPOINT,
        "azure_flux_api_key": _FAKE_KEY,
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

    monkeypatch.setattr(azure_image_provider.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# request shape
# ---------------------------------------------------------------------------


async def test_correct_endpoint_is_called():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["url"] == _ENDPOINT


async def test_authorization_header_is_created_correctly_without_leaking_the_key(capsys):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["auth"] == f"Bearer {_FAKE_KEY}"
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


async def test_model_is_exactly_flux_2_flex():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["model"] == "FLUX.2-flex"


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


async def test_n_is_1():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["n"] == 1


async def test_prompt_is_passed_unchanged():
    seen = {}
    prompt = "a single chess knight on a minimalist board, dark-blue and teal, no text"

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return _b64_response()

    await generate_image(prompt=prompt, size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert seen["body"]["prompt"] == prompt


# ---------------------------------------------------------------------------
# response decoding
# ---------------------------------------------------------------------------


async def test_valid_b64_json_is_decoded_into_the_expected_bytes():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _b64_response(_PNG_BYTES)

    result = await generate_image(
        prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == _PNG_BYTES


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


# ---------------------------------------------------------------------------
# clear failures (never retried, never silently wrong)
# ---------------------------------------------------------------------------


async def test_non_retryable_4xx_produces_a_clear_error_without_retrying():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(AIServiceError, match="400"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler)
        )
    assert calls["count"] == 1


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


@pytest.mark.parametrize("bad_size", ["", "1024", "1024x", "x768", "widexhigh", "1024x1024x1"])
async def test_invalid_image_size_produces_a_clear_error(bad_size):
    with pytest.raises(AIServiceError):
        parse_image_size(bad_size)


async def test_missing_endpoint_fails_clearly_without_any_request():
    with pytest.raises(AIServiceError, match="AZURE_FLUX_ENDPOINT"):
        await generate_image(
            prompt="p", size="1024x1024",
            settings=_settings(azure_flux_endpoint=""),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


async def test_missing_api_key_fails_clearly_without_any_request():
    with pytest.raises(AIServiceError, match="AZURE_FLUX_API_KEY"):
        await generate_image(
            prompt="p", size="1024x1024",
            settings=_settings(azure_flux_api_key=""),
            transport=httpx.MockTransport(lambda r: _b64_response()),
        )


# ---------------------------------------------------------------------------
# OpenAIClient.image() provider dispatch
# ---------------------------------------------------------------------------


async def test_image_provider_openai_still_uses_the_existing_openai_implementation(monkeypatch):
    settings = Settings(image_provider="openai", openai_api_key="test-fake-openai-key")
    client = OpenAIClient(settings)

    called = {"openai": False, "azure": False}

    async def fake_openai(**_kwargs):
        called["openai"] = True
        return b"openai-bytes"

    async def fake_azure(**_kwargs):
        called["azure"] = True
        return b"azure-bytes"

    monkeypatch.setattr(client, "_image_openai", fake_openai)
    monkeypatch.setattr(client, "_image_azure", fake_azure)

    result = await client.image(prompt="p")
    assert result == b"openai-bytes"
    assert called == {"openai": True, "azure": False}


async def test_image_provider_azure_uses_the_azure_implementation(monkeypatch):
    settings = Settings(image_provider="azure", azure_flux_endpoint=_ENDPOINT, azure_flux_api_key=_FAKE_KEY)
    client = OpenAIClient(settings)

    called = {"openai": False, "azure": False}

    async def fake_openai(**_kwargs):
        called["openai"] = True
        return b"openai-bytes"

    async def fake_azure(**_kwargs):
        called["azure"] = True
        return b"azure-bytes"

    monkeypatch.setattr(client, "_image_openai", fake_openai)
    monkeypatch.setattr(client, "_image_azure", fake_azure)

    result = await client.image(prompt="p")
    assert result == b"azure-bytes"
    assert called == {"openai": False, "azure": True}


async def test_unsupported_image_provider_fails_clearly(monkeypatch):
    settings = Settings(image_provider="not-a-real-provider")
    client = OpenAIClient(settings)

    async def fail_if_called(**_kwargs):
        raise AssertionError("should never be called for an unsupported provider")

    monkeypatch.setattr(client, "_image_openai", fail_if_called)
    monkeypatch.setattr(client, "_image_azure", fail_if_called)

    with pytest.raises(AIServiceError, match="not-a-real-provider"):
        await client.image(prompt="p")


async def test_image_provider_defaults_to_openai_when_unset(monkeypatch):
    """Settings.image_provider's own default - confirms OpenAI stays the
    default behaviour with no configuration changes at all."""
    settings = Settings(openai_api_key="test-fake-openai-key")
    assert settings.image_provider == "openai"
    client = OpenAIClient(settings)

    async def fake_openai(**_kwargs):
        return b"openai-bytes"

    async def fail_if_called(**_kwargs):
        raise AssertionError("azure path should never be used by default")

    monkeypatch.setattr(client, "_image_openai", fake_openai)
    monkeypatch.setattr(client, "_image_azure", fail_if_called)

    assert await client.image(prompt="p") == b"openai-bytes"


# ---------------------------------------------------------------------------
# rate limiting (429): a longer budget and a longer wait than other failures
# ---------------------------------------------------------------------------


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []

    async def _record(seconds, *_a, **_k):
        recorded.append(seconds)

    monkeypatch.setattr(azure_image_provider.asyncio, "sleep", _record)
    return recorded


async def test_429_waits_the_provider_retry_after(waits):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "41"}, json={"error": "throttled"})
        return _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert waits == [41.0]


async def test_429_without_retry_after_waits_20_to_30_seconds(waits):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, json={"error": "throttled"}) if calls["count"] < 4 else _b64_response()

    await generate_image(prompt="p", size="1024x1024", settings=_settings(), transport=httpx.MockTransport(handler))
    assert len(waits) == 3 and all(20 <= wait <= 30 for wait in waits)


async def test_429_is_attempted_six_times_regardless_of_max_call_retries(waits):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, json={"error": "throttled"})

    with pytest.raises(AIServiceError, match="429"):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(max_call_retries=2),
            transport=httpx.MockTransport(handler),
        )
    assert calls["count"] == 6


async def test_other_errors_keep_the_short_backoff(waits):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    with pytest.raises(AIServiceError):
        await generate_image(
            prompt="p", size="1024x1024", settings=_settings(max_call_retries=3),
            transport=httpx.MockTransport(handler),
        )
    assert len(waits) == 2 and all(wait < 20 for wait in waits)


def test_image_concurrency_defaults_to_two():
    assert Settings().concurrency_for("image") == 2
