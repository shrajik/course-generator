"""Microsoft Azure AI Foundry FLUX.2-flex image generation.

A separate provider `OpenAIClient.image()` (app.services.openai_service)
dispatches to when `settings.image_provider == "azure"` - kept in its own
module rather than inlined into openai_service.py because its request/
response shape, error handling and retry policy are genuinely different
from the OpenAI SDK's own (a raw HTTP call via httpx, not an SDK client),
not because the two providers share no code worth reusing.

Public contract: `generate_image(prompt=..., size=..., settings=...) ->
bytes` - the exact same (prompt, style) -> PNG bytes shape `AIClient.image()`
already promises every caller, so nothing above this function needs to know
which provider actually answered. Never makes a real network call unless
actually invoked - safe to import even when Azure isn't configured at all
(every Azure setting defaults to "", validated here at call time, not at
import time).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import random

import httpx

from app.core.config import Settings
from app.core.errors import AIServiceError

# Status codes worth retrying - a throttling response (429) and transient
# server-side failures, mirroring the spirit of OpenAIClient._is_retryable
# without reusing it directly: that method's own retry-worthiness check
# reads attributes specific to the OpenAI SDK's own exception types
# (`status_code`/`status` on the exception itself), which a raw
# `httpx.Response`/`httpx.HTTPStatusError` doesn't expose the same way.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_ERROR_BODY_PREVIEW_CHARS = 300


def parse_image_size(size: str) -> tuple[int, int]:
    """"1024x1024" -> (1024, 1024). Azure's FLUX.2-flex request body wants
    separate integer `width`/`height` fields, never the single "WxH" string
    OpenAI's own `size` parameter uses (see Settings.image_size's own
    contract, which this function never changes - it only translates at the
    point of use). Raises AIServiceError on anything that doesn't cleanly
    parse - failing clearly here is the whole point, rather than silently
    asking Azure to generate the wrong size."""
    text = (size or "").strip().lower()
    parts = text.split("x")
    if len(parts) != 2:
        raise AIServiceError(f"Invalid image size {size!r}: expected the form '<width>x<height>', e.g. '1024x1024'")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise AIServiceError(f"Invalid image size {size!r}: width and height must be integers") from exc
    if width <= 0 or height <= 0:
        raise AIServiceError(f"Invalid image size {size!r}: width and height must be positive")
    return width, height


def _backoff_seconds(attempt: int, *, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter, capped at 30s - unless the provider
    told us exactly how long to wait (`Retry-After` on a 429), in which case
    that takes priority, same as OpenAIClient._with_retries' own
    `_retry_after` rule for the OpenAI path."""
    if retry_after is not None and retry_after > 0:
        return retry_after
    return min(2**attempt + random.random(), 30)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _ERROR_BODY_PREVIEW_CHARS else text[:_ERROR_BODY_PREVIEW_CHARS] + "..."


def _decode_image_bytes(response: httpx.Response) -> bytes:
    """Azure's own response shape - `{"data": [{"b64_json": "..."}]}` - is
    never handed back to a caller as-is (the user's own explicit "Do not
    return the Azure response directly to callers"); only the decoded PNG
    bytes ever leave this module, matching exactly what OpenAIClient.image()
    already returns for the OpenAI path."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIServiceError("Azure FLUX.2-flex returned a response that was not valid JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AIServiceError("Azure FLUX.2-flex response is missing the expected 'data[0]' entry")
    b64 = data[0].get("b64_json")
    if not b64 or not isinstance(b64, str):
        raise AIServiceError("Azure FLUX.2-flex response is missing 'data[0].b64_json'")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AIServiceError("Azure FLUX.2-flex returned 'b64_json' that was not valid base64") from exc


async def generate_image(
    *, prompt: str, size: str, settings: Settings, transport: httpx.BaseTransport | None = None
) -> bytes:
    """POSTs to `settings.azure_flux_endpoint` with the FLUX.2-flex request
    shape (model/width/height/n), retries a throttled or transiently-failed
    request up to `settings.max_call_retries` times (the same retry BUDGET
    the OpenAI path uses, for consistency - see this module's own docstring
    for why the retry-worthiness check itself isn't shared code), and
    returns the decoded PNG bytes. The API key is read once from `settings`
    (itself populated from the backend's own .env / real environment -
    never hardcoded here) into the request's own Authorization header and
    is never otherwise touched: never logged, never included in a raised
    exception's message, never part of what this function returns.

    `transport` is a test-only injection point (the standard httpx pattern -
    `httpx.MockTransport`) for exercising the real request-building/retry/
    decode logic above without ever touching the real network; production
    callers never pass it, so `httpx.AsyncClient` uses its own real
    transport exactly as before."""
    if not settings.azure_flux_endpoint.strip():
        raise AIServiceError("AZURE_FLUX_ENDPOINT is not configured")
    if not settings.azure_flux_api_key.strip():
        raise AIServiceError("AZURE_FLUX_API_KEY is not configured")

    width, height = parse_image_size(size)
    body = {"prompt": prompt, "model": "FLUX.2-flex", "width": width, "height": height, "n": 1}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.azure_flux_api_key}",
    }
    attempts = max(settings.max_call_retries, 1)
    last_status: int | None = None

    async with httpx.AsyncClient(timeout=settings.request_timeout, transport=transport) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(settings.azure_flux_endpoint, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                if attempt == attempts:
                    raise AIServiceError(f"Azure FLUX.2-flex request timed out after {attempts} attempt(s)") from exc
                await asyncio.sleep(_backoff_seconds(attempt))
                continue
            except httpx.HTTPError as exc:
                # A connection-level failure (DNS, refused, reset, ...) -
                # the exception's own message is from httpx/the OS, never
                # anything this function constructed from the request
                # itself, so it can't echo the Authorization header back.
                if attempt == attempts:
                    raise AIServiceError(f"Azure FLUX.2-flex request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code // 100 == 2:
                return _decode_image_bytes(response)

            last_status = response.status_code
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < attempts:
                await asyncio.sleep(_backoff_seconds(attempt, retry_after=_retry_after_seconds(response)))
                continue
            raise AIServiceError(
                f"Azure FLUX.2-flex request failed with status {response.status_code}: "
                f"{_truncate(response.text)}"
            )

    # Unreachable in practice (the loop above always returns or raises),
    # kept only as a defensive final guard rather than an implicit `None`.
    raise AIServiceError(f"Azure FLUX.2-flex request failed after {attempts} attempt(s) (last status {last_status})")
