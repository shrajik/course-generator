"""Wikimedia Commons search - the first-priority image source.

`ImageService._try_wikimedia_image` (app.services.image_service) calls this
module BEFORE ever reaching an AI image model (MAI-Image-2.6/FLUX.2-flex/
gpt-image-1, see app.services.azure_mai_image_provider and
app.services.azure_image_provider): search Commons for a candidate, let the
caller validate it (deterministic license/resolution filter here, a vision-
model relevance/quality check in app.services.wikimedia_image_qa), and only
fall through to an AI generation when nothing suitable turns up.

Mirrors azure_mai_image_provider.py's own shape deliberately - same retry/
backoff policy, same `transport: httpx.BaseTransport | None` test-injection
point (httpx.MockTransport in tests, never the real network), same
"truncate error bodies, never raise anything that could leak a secret"
discipline. Commons' read API is keyless, so there's no API key to leak
here, but the rest of that discipline still applies - and real production
risk does exist on the request side: Wikimedia's API etiquette policy
requires a descriptive User-Agent identifying the caller and a real contact
method (settings.wikimedia_user_agent) - an unconfigured/generic one risks
being rate-limited or blocked.

Root-cause investigation (a real, confirmed production failure: every live
Commons request from this app's own deployment came back 403 "Please
respect our robot policy... contact noc@wikimedia.org", reproduced from
both the bare host and the actual Docker container, i.e. not a sandbox
artifact) - compared directly against a separate, independently working
reference implementation (PrepOwl's own Wikimedia integration) rather than
guessed at. The two are NOT meaningfully different at the HTTP-client
level - PrepOwl also uses a plain `httpx.AsyncClient` with a custom
User-Agent, no special TLS/transport handling for its search calls either -
so swapping transport libraries or impersonating a browser's TLS
fingerprint was never the actual fix (and deliberately never attempted;
Wikimedia's bot policy is to be respected, not bypassed). The real,
structural differences were about REQUEST VOLUME AND PATTERN, which is
exactly what an abuse/volume-based edge block keys on:
1. PrepOwl's live path never calls Commons search automatically at all -
   its default, "working in production" behaviour is a pure local-database
   lookup against an ADMIN-curated, human-reviewed cache; a live Commons
   call only happens from an explicit, infrequent admin action, or from an
   auto-serve path that is OFF by default. This app, by contrast, called
   Commons search live, automatically, for every illustration-kind image
   block of every course generation - far higher, fully automated volume.
2. PrepOwl never issues more than ONE Commons request at a time. This
   module's own search used to run inside `ImageService`'s per-block
   worker pool, gated only by the shared "image" concurrency limiter
   (4 by default) - up to 4 simultaneous Commons requests from one IP
   whenever several image blocks generated at once, an unmistakably
   automated-burst pattern. Fixed by `settings.wikimedia_concurrency`
   (default 1, deliberately separate from `image_concurrency`) - see
   `ImageService.generate_missing`'s own worker, which now acquires this
   limiter around every Commons-touching call, keeping every live request
   genuinely serial regardless of how many images generate concurrently.
3. This app's own default `wikimedia_user_agent` was the literal, unfilled
   placeholder string ("+set a real contact URL/email") - non-compliant
   with Wikimedia's own policy by construction, unlike PrepOwl's real,
   specific one. Fixed by giving `Settings.wikimedia_user_agent` a real
   default (see that field's own docstring).
4. A 403 response was previously NOT in this module's own retryable status
   set at all - a single transient/volume-driven block failed the query
   immediately and permanently, with no backoff, even though Wikimedia's
   own error text explicitly frames this as a recoverable, volume-based
   state ("contact us if you need higher volume"), not a permanent ban.
   403 now gets the same bounded exponential-backoff retry as a 5xx.

Public contract: `search_commons(query, ...) -> list[WikimediaCandidate]`,
`fetch_image_bytes(url, ...) -> bytes`, `svg_dimensions(data) -> (w, h)` and
`rasterize_svg_for_vision(data, ...) -> bytes | None`. Never makes a real
network call unless actually invoked - safe to import unconditionally, same
as every other provider module in this package.

SVG candidates: a real, confirmed check against the live Commons API found
that a large share of its best educational diagrams (anatomy, biology,
physics apparatus) are published as SVG, not JPEG/PNG - excluding them
entirely (an earlier version of this module did) meant Wikimedia rarely won
for exactly the "textbook diagram" topics this feature targets. SVG needs
two things neither PIL nor the vision-check call (app.services.
wikimedia_image_qa, which base64-encodes raw bytes as a `data:image/png`
URL) can do on their own: `svg_dimensions` reads the SVG's own declared
size directly from its markup (no PIL - PIL cannot open SVG at all), and
`rasterize_for_vision` renders it to a real PNG screenshot via the same
headless Chromium `app.services.pdf_service` already uses for PDF export
(no new dependency) - ONLY for the vision model to look at; the original
SVG bytes are still what gets saved/served if the candidate passes (crisper
at any zoom than a raster copy would be, and this app's own diagrams are
already served as SVG today, so the asset pipeline already handles it).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.core.logging import get_logger

log = get_logger(__name__)

# Status codes worth retrying - same policy as azure_mai_image_provider,
# plus 403: Wikimedia's own edge returns 403 (not 429) for its volume/abuse
# policy block ("Please respect our robot policy..."), and its own wording
# explicitly frames that as a recoverable, volume-driven state, not a
# permanent ban - see this module's own docstring for the full root-cause
# investigation behind this.
_RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
_ERROR_BODY_PREVIEW_CHARS = 300

# Only the File: namespace, never an article/category/talk page.
_FILE_NAMESPACE = 6

# A reasonably permissive "this is genuinely reusable" allow-list, matched
# against `extmetadata.LicenseShortName` (case-insensitive substring) - see
# this module's own docstring and the plan's "Known limitations" on this
# being a reasonable default, not legal advice. Deliberately excludes
# "non-commercial", "no derivatives", "fair use" and anything unrecognised.
_LICENSE_ALLOW_SUBSTRINGS = (
    "public domain", "pd", "cc0",
    "cc by", "cc-by", "by-sa", "by sa",
)
_LICENSE_DENY_SUBSTRINGS = ("nc", "nd", "non-commercial", "noncommercial", "no derivatives")


@dataclass
class WikimediaCandidate:
    """One Commons File: page's own imageinfo, trimmed to what
    ImageService/wikimedia_image_qa actually need. `license_ok` is decided
    HERE (deterministic, free) so callers never need to re-parse
    extmetadata themselves."""

    title: str
    file_page_url: str
    image_url: str
    width: int
    height: int
    mime: str
    license_short_name: str
    license_url: str
    artist: str
    credit_line: str
    license_ok: bool = field(default=False)

    def attribution_text(self) -> str:
        """A short, pre-formatted credit line for display next to the
        image - never fabricated beyond what Commons' own metadata states;
        an unknown artist/credit degrades to the bare license name rather
        than inventing an author."""
        who = self.artist.strip() or self.credit_line.strip()
        if who and self.license_short_name:
            return f"{who} — {self.license_short_name}, via Wikimedia Commons"
        if self.license_short_name:
            return f"{self.license_short_name}, via Wikimedia Commons"
        return "Via Wikimedia Commons"


def _is_license_ok(license_short_name: str) -> bool:
    text = (license_short_name or "").strip().lower()
    if not text:
        return False
    if any(deny in text for deny in _LICENSE_DENY_SUBSTRINGS):
        return False
    return any(allow in text for allow in _LICENSE_ALLOW_SUBSTRINGS)


def _meta_value(extmetadata: dict, key: str) -> str:
    entry = extmetadata.get(key) if isinstance(extmetadata, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    # Commons stores some values (notably Artist/Credit) as a snippet of
    # HTML (e.g. `<a href="...">Name</a>`) - strip tags for a clean display
    # string rather than leaking markup into the attribution line.
    if not isinstance(value, str):
        return ""
    import re as _re

    return _re.sub(r"<[^>]+>", "", value).strip()


def _candidate_from_page(page: dict) -> WikimediaCandidate | None:
    title = str(page.get("title") or "").strip()
    infos = page.get("imageinfo")
    if not title or not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
        return None
    info = infos[0]
    url = str(info.get("url") or "").strip()
    if not url:
        return None
    extmetadata = info.get("extmetadata") or {}
    license_short_name = _meta_value(extmetadata, "LicenseShortName")
    return WikimediaCandidate(
        title=title,
        file_page_url=str(info.get("descriptionurl") or "").strip(),
        image_url=url,
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        mime=str(info.get("mime") or "").strip(),
        license_short_name=license_short_name,
        license_url=_meta_value(extmetadata, "LicenseUrl"),
        artist=_meta_value(extmetadata, "Artist"),
        credit_line=_meta_value(extmetadata, "Credit"),
        license_ok=_is_license_ok(license_short_name),
    )


def _backoff_seconds(attempt: int, *, retry_after: float | None = None) -> float:
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


async def _request_with_retries(
    client: httpx.AsyncClient, *, url: str, params: dict | None, settings: Settings, label: str
) -> httpx.Response:
    """Shared GET-with-retry for both the search and the download call -
    same bounded-attempts/backoff policy as azure_mai_image_provider's own
    request loop, just factored out since both calls here need it."""
    attempts = max(settings.max_call_retries, 1)
    failures = 0
    while True:
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            failures += 1
            if failures >= attempts:
                raise AIServiceError(f"Wikimedia {label} request timed out after {attempts} attempt(s)") from exc
            await asyncio.sleep(_backoff_seconds(failures))
            continue
        except httpx.HTTPError as exc:
            failures += 1
            if failures >= attempts:
                raise AIServiceError(f"Wikimedia {label} request failed: {type(exc).__name__}") from exc
            await asyncio.sleep(_backoff_seconds(failures))
            continue

        if response.status_code // 100 == 2:
            return response
        if response.status_code in _RETRYABLE_STATUS_CODES:
            failures += 1
            if failures < attempts:
                await asyncio.sleep(_backoff_seconds(failures, retry_after=_retry_after_seconds(response)))
                continue
        raise AIServiceError(
            f"Wikimedia {label} request failed with status {response.status_code}: {_truncate(response.text)}"
        )


async def search_commons(
    query: str,
    *,
    limit: int,
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
) -> list[WikimediaCandidate]:
    """Searches Commons' File: namespace for `query`, returning candidates
    in the API's own relevance order. Never raises on "nothing found" (an
    empty list is a perfectly normal result) - only on a genuine request
    failure after every retry, which the caller (ImageService) always
    catches and treats as "fall back to the AI model", never as a reason to
    fail the whole block."""
    query = query.strip()
    if not query:
        return []
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": _FILE_NAMESPACE,
        "gsrlimit": max(1, min(limit, 50)),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
    }
    headers = {"User-Agent": settings.wikimedia_user_agent}
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers, transport=transport) as client:
        response = await _request_with_retries(
            client, url=settings.wikimedia_api_base, params=params, settings=settings, label="search"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIServiceError("Wikimedia search returned a response that was not valid JSON") from exc
    pages = ((payload.get("query") or {}).get("pages") or {}) if isinstance(payload, dict) else {}
    candidates = [
        candidate
        for candidate in (_candidate_from_page(page) for page in pages.values() if isinstance(page, dict))
        if candidate is not None
    ]
    return candidates


async def fetch_image_bytes(
    url: str, *, settings: Settings, transport: httpx.BaseTransport | None = None
) -> bytes:
    """Downloads a candidate's own full-resolution image bytes - a plain
    GET (no auth), same bounded retry policy as the search call."""
    headers = {"User-Agent": settings.wikimedia_user_agent}
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers, transport=transport) as client:
        response = await _request_with_retries(
            client, url=url, params=None, settings=settings, label="image download"
        )
    return response.content


# Matches a bare numeric attribute value, optionally followed by an
# absolute CSS length unit (px/pt/mm/cm/in) - SVG width/height can legally
# carry one; this module only ever needs the number itself, same
# "intrinsic pixel size for the layout engine" contract ImageService.
# _dimensions keeps for every raster format. Deliberately does NOT match a
# "%" suffix - a percentage is relative to its CONTAINER, not an intrinsic
# size at all, so "100%" must fall through to viewBox below, never be
# misread as the literal number 100 (a real, confirmed bug this guards
# against: Commons' own multilingual SVGs routinely use width="100%"
# height="100%" with the real size only in viewBox).
_SVG_LENGTH_RE = re.compile(r"([\d.]+)\s*(?:px|pt|mm|cm|in)?\s*$")


def _svg_length(raw: str | None) -> int | None:
    if not raw or raw.strip().endswith("%"):
        return None
    match = _SVG_LENGTH_RE.match(raw.strip())
    if not match:
        return None
    try:
        return round(float(match.group(1)))
    except ValueError:
        return None


def svg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """The SVG's own declared intrinsic size, read directly from its
    markup - PIL cannot open SVG at all (confirmed; it raises
    UnidentifiedImageError), so `ImageService._dimensions`'s usual PIL
    decode is never an option here. Prefers explicit `width`/`height`
    attributes on the root `<svg>` tag; falls back to `viewBox`'s own
    width/height (its last two numbers) when those are missing or
    percentage-based, the same fallback every SVG renderer uses. `None`
    for either (never a guess) when nothing usable is present - the layout
    engine already treats a missing intrinsic size as "fall back to a
    default aspect ratio", the same case a corrupt/unusual raster image
    hitting `_dimensions`'s own except-branch already produces."""
    try:
        text = data[:8192].decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 - a decode failure is just "no dimensions found"
        return None, None
    root_match = re.search(r"<svg\b[^>]*>", text, re.IGNORECASE | re.DOTALL)
    root = root_match.group(0) if root_match else text
    width = _svg_length(_attr(root, "width"))
    height = _svg_length(_attr(root, "height"))
    if width and height:
        return width, height
    view_box = _attr(root, "viewBox")
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            vb_w, vb_h = _svg_length(parts[2]), _svg_length(parts[3])
            if vb_w and vb_h:
                return vb_w, vb_h
    return None, None


def _attr(tag_text: str, name: str) -> str | None:
    match = re.search(rf'{name}\s*=\s*"([^"]*)"', tag_text, re.IGNORECASE)
    return match.group(1) if match else None


async def rasterize_svg_for_vision(data: bytes, *, settings: Settings) -> bytes | None:
    """Renders SVG `data` to a real PNG screenshot, ONLY so the vision
    model in app.services.wikimedia_image_qa has actual pixels to look at
    - no vision API can view raw SVG markup (confirmed:
    `OpenAIClient.structured`'s own `image=` path base64-encodes whatever
    bytes it's given as a `data:image/png` URL unconditionally, so passing
    raw SVG text through it would silently claim to be a PNG it isn't).
    The ORIGINAL SVG bytes are still what gets saved/served if the
    candidate passes this check - this rasterised copy is thrown away
    either way. Reuses the exact same headless-Chromium launch convention
    `app.services.pdf_service.PdfService.export_pdf` already uses (no new
    dependency - Playwright is already required for PDF export). Returns
    `None` on ANY failure (Playwright not installed, a malformed SVG, a
    launch timeout) - never raises; the caller treats that exactly like a
    failed download, skipping this one candidate rather than failing the
    whole lookup."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright is not installed - cannot rasterise an SVG Wikimedia candidate for review")
        return None
    # Sized to the SVG's OWN aspect ratio (never a fixed square) and capped
    # on its longer side - a confirmed real gap in an earlier version of
    # this function: a wide diagram (e.g. a 2361x409 mitosis-stages strip)
    # screenshotted at a fixed 1024x1024 viewport got cropped to its first
    # ~40%, hiding most of the actual content from the vision model.
    width, height = svg_dimensions(data)
    viewport = _vision_viewport(width, height)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            svg_path = Path(tmp_dir) / "candidate.svg"
            svg_path.write_bytes(data)
            # Loaded through a tiny HTML wrapper, as an <img>, rather than
            # navigating straight to the SVG file - a real, confirmed gap:
            # a standalone SVG document's own browser-default sizing
            # ignored the viewport entirely and rendered at the SVG's own
            # native declared pixel size (e.g. a 2361x409 diagram stayed
            # 2361x409 regardless of viewport), so a smaller viewport just
            # cropped it instead of showing it scaled down. `object-fit:
            # contain` on an <img> guarantees the WHOLE image is scaled to
            # fit the viewport, preserving aspect ratio, exactly like the
            # finished page's own `<img>` rendering of this same asset
            # once it ships (see ImageBlock.tsx) - deterministic regardless
            # of what the SVG's own internal width/height/viewBox say.
            html_path = Path(tmp_dir) / "candidate.html"
            html_path.write_text(
                "<!doctype html><html><head><style>"
                "html,body{margin:0;padding:0;background:#fff;}"
                "img{display:block;width:100vw;height:100vh;object-fit:contain;}"
                "</style></head><body><img src=\"candidate.svg\"></body></html>",
                encoding="utf-8",
            )
            launch_kwargs: dict[str, object] = {"args": ["--no-sandbox"]}
            executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            if executable:
                launch_kwargs["executable_path"] = executable
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs)
                try:
                    page = await browser.new_page(viewport=viewport)
                    await page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=10000)
                    # NOT full_page=True: a raw standalone SVG document
                    # loaded directly (no surrounding HTML) could report a
                    # degenerate/oversized layout size to Playwright,
                    # which hangs capturing it - a real, confirmed timeout
                    # (same class of bug already hit and fixed once before
                    # in this project for a different standalone-SVG
                    # check). The HTML wrapper's own fixed 100vw/100vh
                    # <img> above already fills exactly the viewport, so a
                    # plain viewport-bound screenshot already captures the
                    # whole thing.
                    return await page.screenshot(type="png", timeout=10000)
                finally:
                    await browser.close()
    except Exception as exc:  # noqa: BLE001 - rasterisation is best-effort, never fatal
        log.warning("Could not rasterise an SVG Wikimedia candidate for review: %s", exc)
        return None


_VISION_MAX_SIDE = 1400
_VISION_MIN_SIDE = 400
_VISION_FALLBACK = {"width": 1024, "height": 1024}


def _vision_viewport(width: int | None, height: int | None) -> dict[str, int]:
    """A Playwright `viewport` sized to the SVG's own aspect ratio, capped
    to `_VISION_MAX_SIDE` on its longer side (keeps the screenshot - and
    the base64 payload the vision call sends - a reasonable size) and
    floored at `_VISION_MIN_SIDE` (a tiny intrinsic size, e.g. an icon-
    sized SVG, would otherwise produce an unreadably small screenshot).
    Falls back to a plain square when the SVG declared no usable size at
    all - still correct, just not aspect-aware."""
    if not width or not height:
        return dict(_VISION_FALLBACK)
    scale = min(_VISION_MAX_SIDE / max(width, height), 1.0)
    scaled_w, scaled_h = max(round(width * scale), 1), max(round(height * scale), 1)
    if max(scaled_w, scaled_h) < _VISION_MIN_SIDE:
        up = _VISION_MIN_SIDE / max(scaled_w, scaled_h)
        scaled_w, scaled_h = round(scaled_w * up), round(scaled_h * up)
    return {"width": scaled_w, "height": scaled_h}
