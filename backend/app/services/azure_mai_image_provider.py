"""Microsoft Foundry MAI-Image-2.6 image generation.

A separate provider `OpenAIClient.image()` (app.services.openai_service)
dispatches to when `settings.image_provider == "azure_mai"` - kept in its
own module, deliberately NOT merged into app.services.azure_image_provider
(the existing FLUX.2-flex provider), because the two APIs' request/auth
contract genuinely differs, confirmed against the official docs
(https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/
use-foundry-models-mai-image):

  - Auth header: MAI wants `api-key: <key>`, not FLUX's `Authorization:
    Bearer <key>`.
  - Request body: MAI's `model` field must be the DEPLOYMENT NAME (the name
    assigned when the model was deployed in Foundry), not a literal model
    string; it also documents no `n` field (FLUX sends `"n": 1` - an
    undocumented field on MAI's endpoint risks a 400 per its own
    troubleshooting table: "a parameter isn't supported by the deployed
    model").
  - Size limits: MAI documents a HARD minimum of 768px per dimension and a
    total-pixel-count ceiling (2,359,296 for MAI-Image-2.6, equivalent to
    1536x1536) - FLUX's provider enforces no such limit, so this module
    validates it explicitly rather than letting a generic 400 surface.

The response shape IS identical to FLUX's - `{"data": [{"b64_json": "..."}]}`
- so the decode step below matches azure_image_provider._decode_image_bytes
exactly; this is confirmed from the docs, not assumed.

Public contract: `generate_image(prompt=..., size=..., settings=...) ->
bytes` - the exact same (prompt, style) -> PNG bytes shape `AIClient.image()`
already promises every caller, so nothing above this function needs to know
which provider actually answered. Never makes a real network call unless
actually invoked - safe to import even when Azure isn't configured at all
(every azure_mai_image_* setting defaults to "", validated here at call
time, not at import time).
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
# server-side failures, mirroring azure_image_provider's own policy.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_ERROR_BODY_PREVIEW_CHARS = 300

# 429s: up to this many attempts, waiting the provider's `Retry-After` when it
# sends one, otherwise somewhere in this window (jittered so parallel requests
# do not all come back at the same instant). Same budget as FLUX's provider -
# both share the same Azure-side rate-limiting shape (RPM quota tiers).
_RATE_LIMIT_ATTEMPTS = 6
_RATE_LIMIT_WAIT_MIN = 20.0
_RATE_LIMIT_WAIT_MAX = 30.0

# Documented MAI-Image-2.6 constraints (see this module's own docstring for
# the source) - both dimensions at least this many pixels...
_MIN_DIMENSION = 768
# ...and the total pixel count at or below this ceiling (2,359,296 ==
# 1536x1536). This is the MAI-Image-2.6/2.6-Flash limit specifically; the
# 2.5 family's ceiling is lower (1,048,576) but this provider only ever
# targets 2.6, so one constant is correct here rather than a per-model table
# nothing in this codebase would ever exercise the other branch of.
_MAX_PIXEL_COUNT = 2_359_296


def parse_image_size(size: str) -> tuple[int, int]:
    """"1024x1024" -> (1024, 1024), with MAI-Image-2.6's own documented
    bounds enforced (minimum 768px per side, max 2,359,296 total pixels) -
    failing clearly here, before any network call, rather than letting
    Azure's own generic 400 surface for a size this provider could have
    rejected up front. Raises AIServiceError on anything that doesn't
    cleanly parse or that violates those bounds."""
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
    if width < _MIN_DIMENSION or height < _MIN_DIMENSION:
        raise AIServiceError(
            f"Invalid image size {size!r} for MAI-Image-2.6: both width and height must be "
            f"at least {_MIN_DIMENSION}px"
        )
    if width * height > _MAX_PIXEL_COUNT:
        raise AIServiceError(
            f"Invalid image size {size!r} for MAI-Image-2.6: width x height ({width * height}) "
            f"exceeds the {_MAX_PIXEL_COUNT}px limit"
        )
    return width, height


def _backoff_seconds(attempt: int, *, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter, capped at 30s - unless the provider
    told us exactly how long to wait (`Retry-After` on a 429), in which case
    that takes priority. Same policy as azure_image_provider's own."""
    if retry_after is not None and retry_after > 0:
        return retry_after
    return min(2**attempt + random.random(), 30)


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


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _ERROR_BODY_PREVIEW_CHARS else text[:_ERROR_BODY_PREVIEW_CHARS] + "..."


def _decode_image_bytes(response: httpx.Response) -> bytes:
    """MAI-Image-2.6's documented response shape - `{"data":
    [{"b64_json": "..."}]}` - identical to FLUX's; only the decoded PNG
    bytes ever leave this module, matching exactly what OpenAIClient.image()
    already returns for every other provider."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIServiceError("Azure MAI-Image-2.6 returned a response that was not valid JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AIServiceError("Azure MAI-Image-2.6 response is missing the expected 'data[0]' entry")
    b64 = data[0].get("b64_json")
    if not b64 or not isinstance(b64, str):
        raise AIServiceError("Azure MAI-Image-2.6 response is missing 'data[0].b64_json'")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AIServiceError("Azure MAI-Image-2.6 returned 'b64_json' that was not valid base64") from exc


async def generate_image(
    *, prompt: str, size: str, settings: Settings, transport: httpx.BaseTransport | None = None
) -> bytes:
    """POSTs to `settings.azure_mai_image_endpoint` with the MAI-Image-2.6
    request shape (model=deployment name, prompt, width, height - no `n`,
    see this module's own docstring), authenticating with the `api-key`
    header the docs specify (not FLUX's `Authorization: Bearer`). Retries a
    throttled (429) request up to 6 times with a long wait, and a
    transiently-failed one up to `settings.max_call_retries` times (the same
    retry BUDGET azure_image_provider uses, for consistency), and returns
    the decoded PNG bytes. The API key is read once from `settings` (itself
    populated from the backend's own .env / real environment - never
    hardcoded here) into the request's own `api-key` header and is never
    otherwise touched: never logged, never included in a raised exception's
    message, never part of what this function returns.

    `transport` is a test-only injection point (the standard httpx pattern -
    `httpx.MockTransport`) for exercising the real request-building/retry/
    decode logic above without ever touching the real network; production
    callers never pass it, so `httpx.AsyncClient` uses its own real
    transport exactly as before."""
    if not settings.azure_mai_image_endpoint.strip():
        raise AIServiceError("AZURE_MAI_IMAGE_ENDPOINT is not configured")
    if not settings.azure_mai_image_api_key.strip():
        raise AIServiceError("AZURE_MAI_IMAGE_API_KEY is not configured")
    if not settings.azure_mai_image_deployment.strip():
        raise AIServiceError("AZURE_MAI_IMAGE_DEPLOYMENT is not configured")

    width, height = parse_image_size(size)
    body = {
        "model": settings.azure_mai_image_deployment,
        "prompt": prompt,
        "width": width,
        "height": height,
    }
    headers = {
        "Content-Type": "application/json",
        "api-key": settings.azure_mai_image_api_key,
    }
    attempts = max(settings.max_call_retries, 1)
    # Rate limiting is a quota window, not a glitch: a short retry lands in the
    # same window and fails again, so it gets its own, longer budget and wait.
    # Everything else keeps the ordinary (small) budget.
    failures = 0
    throttled = 0
    last_status: int | None = None

    async with httpx.AsyncClient(timeout=settings.request_timeout, transport=transport) as client:
        while True:
            try:
                response = await client.post(settings.azure_mai_image_endpoint, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                failures += 1
                if failures >= attempts:
                    raise AIServiceError(f"Azure MAI-Image-2.6 request timed out after {attempts} attempt(s)") from exc
                await asyncio.sleep(_backoff_seconds(failures))
                continue
            except httpx.HTTPError as exc:
                # A connection-level failure (DNS, refused, reset, ...) -
                # the exception's own message is from httpx/the OS, never
                # anything this function constructed from the request
                # itself, so it can't echo the api-key header back.
                failures += 1
                if failures >= attempts:
                    raise AIServiceError(f"Azure MAI-Image-2.6 request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(_backoff_seconds(failures))
                continue

            if response.status_code // 100 == 2:
                return _decode_image_bytes(response)

            last_status = response.status_code
            if response.status_code == 429:
                throttled += 1
                if throttled < _RATE_LIMIT_ATTEMPTS:
                    await asyncio.sleep(_rate_limit_wait(_retry_after_seconds(response)))
                    continue
            elif response.status_code in _RETRYABLE_STATUS_CODES:
                failures += 1
                if failures < attempts:
                    await asyncio.sleep(_backoff_seconds(failures))
                    continue
            raise AIServiceError(
                f"Azure MAI-Image-2.6 request failed with status {response.status_code}: "
                f"{_truncate(response.text)}"
            )

    # Unreachable in practice (the loop above always returns or raises),
    # kept only as a defensive final guard rather than an implicit `None`.
    raise AIServiceError(f"Azure MAI-Image-2.6 request failed after {attempts} attempt(s) (last status {last_status})")
