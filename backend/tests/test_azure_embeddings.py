"""Azure embedding deployment (e.g. text-embedding-3-large), routed through
OpenAIClient._embedding_client - purely additive alongside the pre-existing
direct-OpenAI embedding path (see Settings.azure_embedding_* docstring).

Same discipline as test_azure_text_models.py: a real `AsyncOpenAI` instance
pointed at `httpx.MockTransport` is injected directly into
`OpenAIClient._azure_clients` (the same cache `_embedding_client` itself
reads from), so a real HTTP request is built and inspected without ever
touching the real network or a live credential.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.services.openai_service import OpenAIClient

_FAKE_KEY = "test-fake-azure-embedding-key-not-real"


def _settings(**overrides) -> Settings:
    values = {
        "azure_embedding_endpoint": "https://fake-embed.services.ai.azure.test/openai/v1/",
        "azure_embedding_api_key": _FAKE_KEY,
        "azure_embedding_deployment": "my-text-embedding-3-large",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "max_call_retries": 2,
        # A sentinel, deliberately different from the Azure key above - tests
        # assert this value never reaches a request once Azure is configured.
        "openai_api_key": "sentinel-openai-key-must-never-be-sent",
    }
    values.update(overrides)
    return Settings(**values)


def _embedding_response_json(n: int, dims: int = 1536) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.1] * dims} for i in range(n)
        ],
        "model": "test-deployment",
        "usage": {"prompt_tokens": 10, "total_tokens": 10},
    }


def _inject_mock_client(client: OpenAIClient, *, endpoint: str, handler) -> None:
    from openai import AsyncOpenAI

    client._azure_clients[endpoint] = AsyncOpenAI(
        base_url=endpoint,
        api_key="unused-cached-client-already-has-its-transport",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# --- _embedding_client: resolution ------------------------------------------


def test_fully_configured_azure_embedding_resolves_to_its_own_resource():
    settings = _settings()
    client = OpenAIClient(settings)
    resolved_client, model_name, provider = client._embedding_client(None)
    assert provider == "azure"
    assert model_name == settings.azure_embedding_deployment
    assert str(resolved_client.base_url).rstrip("/") == settings.azure_embedding_endpoint.rstrip("/")


@pytest.mark.parametrize(
    "missing_field",
    ["azure_embedding_endpoint", "azure_embedding_api_key", "azure_embedding_deployment"],
)
def test_an_incompletely_configured_azure_deployment_falls_back_to_openai(missing_field):
    """Unlike the three REQUIRED Azure text models (which raise on a partial
    config, since there's no other way to reach them), embeddings already
    have a working, pre-existing fallback - a half-set Azure deployment
    must never crash embedding generation outright, only skip Azure and use
    the direct OpenAI client, exactly as if Azure were never configured."""
    settings = _settings(**{missing_field: ""})
    client = OpenAIClient(settings)
    resolved_client, model_name, provider = client._embedding_client(None)
    assert provider == "openai"
    assert model_name == settings.embedding_model


def test_no_azure_embedding_configured_is_byte_identical_to_before():
    settings = _settings(
        azure_embedding_endpoint="", azure_embedding_api_key="", azure_embedding_deployment=""
    )
    client = OpenAIClient(settings)
    resolved_client, model_name, provider = client._embedding_client(None)
    assert provider == "openai"
    assert model_name == settings.embedding_model
    assert resolved_client is client.client  # the same plain OpenAI client property


def test_an_explicit_model_override_only_applies_to_the_openai_fallback():
    # Azure configured: the override must be ignored - the deployment name
    # is what the API call's `model` field must carry once Azure is active.
    azure_settings = _settings()
    azure_client = OpenAIClient(azure_settings)
    _, model_name, provider = azure_client._embedding_client("some-other-logical-name")
    assert provider == "azure"
    assert model_name == azure_settings.azure_embedding_deployment

    # No Azure configured: the override is honoured, same as before this
    # feature existed.
    plain_settings = _settings(azure_embedding_endpoint="")
    plain_client = OpenAIClient(plain_settings)
    _, model_name, provider = plain_client._embedding_client("some-other-logical-name")
    assert provider == "openai"
    assert model_name == "some-other-logical-name"


# --- embed(): the actual request shape --------------------------------------


async def test_embed_sends_the_deployment_name_and_requests_the_configured_dimensions():
    settings = _settings(embedding_dimensions=1536)
    client = OpenAIClient(settings)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_embedding_response_json(2))

    _inject_mock_client(client, endpoint=settings.azure_embedding_endpoint, handler=handler)

    vectors = await client.embed(texts=["alpha", "beta"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 1536
    assert captured["body"]["model"] == settings.azure_embedding_deployment
    assert captured["body"]["dimensions"] == 1536
    assert captured["body"]["input"] == ["alpha", "beta"]
    # The sentinel OpenAI key must never reach the Azure request.
    assert settings.openai_api_key not in captured["auth"]


async def test_embed_without_azure_configured_never_sends_a_dimensions_field():
    """The pre-existing direct-OpenAI path is left byte-for-byte unchanged -
    some embedding models (e.g. text-embedding-ada-002) reject an unknown
    `dimensions` field outright, so this must never be sent unless Azure
    (text-embedding-3-family only) is actually what's being called."""
    settings = _settings(
        azure_embedding_endpoint="", azure_embedding_api_key="", azure_embedding_deployment="",
        openai_api_key="a-real-looking-openai-key",
    )
    client = OpenAIClient(settings)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_embedding_response_json(1))

    client._client = None
    import openai

    client._client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    vectors = await client.embed(texts=["alpha"])

    assert len(vectors) == 1
    assert captured["body"]["model"] == settings.embedding_model
    assert "dimensions" not in captured["body"]


async def test_empty_texts_makes_no_request_regardless_of_provider():
    client = OpenAIClient(_settings())
    assert await client.embed(texts=[]) == []
