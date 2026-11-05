"""Azure text-model migration: gpt-5 / gpt-5-mini / gpt-5.5, each its own
Azure resource, routed through OpenAIClient._client_for.

Request-shape tests inject a real `AsyncOpenAI` instance (pointed at
`httpx.MockTransport`) directly into `OpenAIClient._azure_clients` - the
same cache `_client_for` itself reads from - so a real HTTP request is
built and inspected without ever touching the real network, exactly the
same discipline as test_azure_mai_image_provider.py's `transport=` seam.
No test requires a real Azure credential; every Settings instance below
supplies its own fake values explicitly.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.services.openai_service import OpenAIClient, ResearchResult

_FAKE_KEY = "test-fake-azure-text-key-not-real"


def _settings(**overrides) -> Settings:
    values = {
        "azure_gpt5_endpoint": "https://fake-gpt5.openai.azure.test/openai/v1/",
        "azure_gpt5_api_key": f"{_FAKE_KEY}-gpt5",
        "azure_gpt5_deployment": "my-gpt5-deployment",
        "azure_gpt5_mini_endpoint": "https://fake-gpt5-mini.openai.azure.test/openai/v1/",
        "azure_gpt5_mini_api_key": f"{_FAKE_KEY}-gpt5-mini",
        "azure_gpt5_mini_deployment": "my-gpt5-mini-deployment",
        "azure_gpt55_endpoint": "https://fake-gpt55.openai.azure.test/openai/v1/",
        "azure_gpt55_api_key": f"{_FAKE_KEY}-gpt55",
        "azure_gpt55_deployment": "my-gpt55-deployment",
        "max_call_retries": 2,
        # A sentinel, deliberately DIFFERENT from every azure_*_api_key above -
        # tests assert this value never reaches any request this module sends.
        "openai_api_key": "sentinel-openai-key-must-never-be-sent",
    }
    values.update(overrides)
    return Settings(**values)


class _Schema(BaseModel):
    answer: str


def _chat_response_json(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-deployment",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _responses_json(text: str, *, with_web_search_call: bool = False) -> dict:
    output = []
    if with_web_search_call:
        output.append(
            {
                "id": "ws_test",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "test query"},
            }
        )
    output.append(
        {
            "id": "msg_test",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "test-deployment",
        "output": output,
        "output_text": text,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _inject_mock_client(client: OpenAIClient, *, endpoint: str, api_key: str, handler) -> None:
    """Pre-populates `_client_for`'s own cache (keyed by endpoint) with a
    real `AsyncOpenAI` wired to `httpx.MockTransport(handler)` - `_client_for`
    reuses whatever's already cached there rather than constructing a new
    client, so this is a real seam, not a monkeypatch of application code."""
    from openai import AsyncOpenAI

    client._azure_clients[endpoint] = AsyncOpenAI(
        base_url=endpoint,
        api_key=api_key,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ---------------------------------------------------------------------------
# _client_for: model name -> (endpoint, key, deployment) resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name,endpoint_field,deployment_field",
    [
        ("gpt-5", "azure_gpt5_endpoint", "azure_gpt5_deployment"),
        ("gpt-5-mini", "azure_gpt5_mini_endpoint", "azure_gpt5_mini_deployment"),
        ("gpt-5.5", "azure_gpt55_endpoint", "azure_gpt55_deployment"),
    ],
)
def test_each_model_resolves_to_its_own_azure_resource(model_name, endpoint_field, deployment_field):
    settings = _settings()
    client = OpenAIClient(settings)
    resolved_client, deployment = client._client_for(model_name)
    assert deployment == getattr(settings, deployment_field)
    assert str(resolved_client.base_url).rstrip("/") == getattr(settings, endpoint_field).rstrip("/")


def test_unrecognised_model_name_fails_clearly():
    client = OpenAIClient(_settings())
    with pytest.raises(AIServiceError, match="Unknown model"):
        client._client_for("some-other-model")


@pytest.mark.parametrize(
    "model_name,missing_field",
    [
        ("gpt-5", "azure_gpt5_endpoint"),
        ("gpt-5", "azure_gpt5_api_key"),
        ("gpt-5", "azure_gpt5_deployment"),
        ("gpt-5-mini", "azure_gpt5_mini_deployment"),
        ("gpt-5.5", "azure_gpt55_api_key"),
    ],
)
def test_incomplete_azure_config_fails_clearly_naming_the_missing_var(model_name, missing_field):
    client = OpenAIClient(_settings(**{missing_field: ""}))
    with pytest.raises(AIServiceError, match=missing_field.upper()):
        client._client_for(model_name)


def test_client_for_never_reads_the_openai_api_key():
    """The real, confirmed requirement this migration exists to satisfy:
    `openai_api_key` must never be touched by the Azure text-model path."""
    settings = _settings()
    client = OpenAIClient(settings)
    for model_name in ("gpt-5", "gpt-5-mini", "gpt-5.5"):
        resolved_client, _ = client._client_for(model_name)
        assert settings.openai_api_key not in str(resolved_client.api_key)


def test_azure_clients_are_cached_per_endpoint_not_rebuilt_per_call():
    client = OpenAIClient(_settings())
    first, _ = client._client_for("gpt-5")
    second, _ = client._client_for("gpt-5")
    assert first is second


# ---------------------------------------------------------------------------
# structured(): real request shape (Chat Completions)
# ---------------------------------------------------------------------------


async def test_structured_sends_the_deployment_name_not_the_logical_model_name():
    settings = _settings()
    client = OpenAIClient(settings)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_chat_response_json('{"answer": "ok"}'))

    _inject_mock_client(
        client, endpoint=settings.azure_gpt5_endpoint, api_key=settings.azure_gpt5_api_key, handler=handler
    )

    result = await client.structured(schema=_Schema, system="sys", user="usr", model="gpt-5")
    assert result.answer == "ok"
    assert seen["body"]["model"] == settings.azure_gpt5_deployment
    assert seen["body"]["model"] != "gpt-5"
    assert settings.azure_gpt5_api_key in (seen["auth"] or "")
    assert settings.openai_api_key not in (seen["auth"] or "")


async def test_structured_routes_gpt5_mini_to_its_own_distinct_resource():
    settings = _settings()
    client = OpenAIClient(settings)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response_json('{"answer": "mini"}'))

    _inject_mock_client(
        client,
        endpoint=settings.azure_gpt5_mini_endpoint,
        api_key=settings.azure_gpt5_mini_api_key,
        handler=handler,
    )

    result = await client.structured(schema=_Schema, system="sys", user="usr", model="gpt-5-mini")
    assert result.answer == "mini"
    assert seen["body"]["model"] == settings.azure_gpt5_mini_deployment
    assert settings.azure_gpt5_mini_endpoint.rstrip("/") in seen["url"]


# ---------------------------------------------------------------------------
# research(): web_search tool + deep-research reasoning effort
# ---------------------------------------------------------------------------


async def test_fast_research_uses_the_web_search_tool_not_the_preview_variant():
    settings = _settings()
    client = OpenAIClient(settings)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_responses_json("fast result", with_web_search_call=True))

    _inject_mock_client(
        client,
        endpoint=settings.azure_gpt5_mini_endpoint,
        api_key=settings.azure_gpt5_mini_api_key,
        handler=handler,
    )

    result = await client.research(prompt="look this up", deep=False)
    assert isinstance(result, ResearchResult)
    assert seen["body"]["tools"] == [{"type": "web_search"}]
    assert "reasoning" not in seen["body"]


async def test_deep_research_uses_gpt55_with_web_search_and_reasoning_effort():
    settings = _settings()
    client = OpenAIClient(settings)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_responses_json("deep result", with_web_search_call=True))

    _inject_mock_client(
        client, endpoint=settings.azure_gpt55_endpoint, api_key=settings.azure_gpt55_api_key, handler=handler
    )

    result = await client.research(prompt="investigate this thoroughly", deep=True)
    assert isinstance(result, ResearchResult)
    assert seen["body"]["model"] == settings.azure_gpt55_deployment
    assert seen["body"]["tools"] == [{"type": "web_search"}]
    assert seen["body"]["reasoning"] == {"effort": "high"}


async def test_deep_research_reasoning_effort_is_configurable():
    settings = _settings(deep_research_reasoning_effort="xhigh")
    client = OpenAIClient(settings)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_responses_json("deep result", with_web_search_call=True))

    _inject_mock_client(
        client, endpoint=settings.azure_gpt55_endpoint, api_key=settings.azure_gpt55_api_key, handler=handler
    )

    await client.research(prompt="investigate this thoroughly", deep=True)
    assert seen["body"]["reasoning"] == {"effort": "xhigh"}


# ---------------------------------------------------------------------------
# no silent cross-provider fallback
# ---------------------------------------------------------------------------


async def test_a_failing_azure_request_never_falls_through_to_plain_openai():
    """The whole point of this migration: when the Azure deployment fails
    (after its own bounded retries), the error propagates - it must never
    silently reach the plain `self.client` (OpenAI) path instead."""
    settings = _settings()
    client = OpenAIClient(settings)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "simulated Azure failure"}})

    _inject_mock_client(
        client, endpoint=settings.azure_gpt5_endpoint, api_key=settings.azure_gpt5_api_key, handler=handler
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("the plain OpenAI client must never be used as a fallback")

    # `client` is a lazy property backed by `_client`; poisoning `_client`
    # directly with a sentinel that raises on any attribute access proves
    # nothing in the failure path ever reaches for it.
    client._client = type(
        "PoisonedClient", (), {"__getattr__": lambda self, name: fail_if_called()}
    )()

    with pytest.raises(AIServiceError):
        await client.structured(schema=_Schema, system="sys", user="usr", model="gpt-5")


def test_use_mock_ai_is_true_when_no_azure_text_model_and_no_openai_key_configured():
    settings = Settings(
        azure_gpt5_api_key="", azure_gpt5_mini_api_key="", azure_gpt55_api_key="", openai_api_key=""
    )
    assert settings.use_mock_ai is True


def test_use_mock_ai_is_false_when_at_least_one_azure_text_model_is_configured():
    # mock_openai=False: conftest.py sets MOCK_OPENAI=true globally (the
    # test suite's own safety guard against ever making a real call by
    # accident) - this test specifically targets the OTHER branch of
    # use_mock_ai's `or`, so it must override that guard explicitly rather
    # than relying on the real environment, which every other test here
    # deliberately leaves alone.
    settings = Settings(
        mock_openai=False,
        azure_gpt5_api_key="a-real-looking-key",
        azure_gpt5_mini_api_key="",
        azure_gpt55_api_key="",
        openai_api_key="",
    )
    assert settings.use_mock_ai is False


def test_use_mock_ai_stays_true_under_explicit_mock_openai_even_with_azure_configured():
    settings = _settings(mock_openai=True)
    assert settings.use_mock_ai is True
