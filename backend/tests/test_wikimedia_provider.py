"""Wikimedia Commons search/download provider.

Everything here runs against `httpx.MockTransport` - never the real network,
same discipline as test_azure_image_provider.py.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.services import wikimedia_provider
from app.services.wikimedia_provider import (
    _vision_viewport,
    fetch_image_bytes,
    search_commons,
    svg_dimensions,
)

_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nnot a real png, just test bytes"


def _settings(**overrides) -> Settings:
    values = {"max_call_retries": 3, "wikimedia_user_agent": "test-agent/1.0 (test@example.com)"}
    values.update(overrides)
    return Settings(**values)


def _page(
    title: str,
    *,
    url: str = "https://upload.wikimedia.org/commons/x/File.png",
    description_url: str = "https://commons.wikimedia.org/wiki/File:File.png",
    width: int = 1200,
    height: int = 900,
    mime: str = "image/png",
    license_short_name: str = "CC BY-SA 4.0",
    license_url: str = "https://creativecommons.org/licenses/by-sa/4.0",
    artist: str = "Jane Doe",
    credit: str = "Own work",
) -> dict:
    extmetadata = {}
    if license_short_name:
        extmetadata["LicenseShortName"] = {"value": license_short_name}
    if license_url:
        extmetadata["LicenseUrl"] = {"value": license_url}
    if artist:
        extmetadata["Artist"] = {"value": artist}
    if credit:
        extmetadata["Credit"] = {"value": credit}
    return {
        "title": title,
        "imageinfo": [{
            "url": url,
            "descriptionurl": description_url,
            "width": width,
            "height": height,
            "mime": mime,
            "extmetadata": extmetadata,
        }],
    }


def _search_response(*pages: dict, status_code: int = 200) -> httpx.Response:
    body = {"query": {"pages": {str(i): page for i, page in enumerate(pages)}}}
    return httpx.Response(status_code, json=body)


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(wikimedia_provider.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_sends_a_descriptive_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers.get("user-agent")
        return _search_response()

    await search_commons(
        "mitosis", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert seen["user_agent"] == "test-agent/1.0 (test@example.com)"


async def test_search_restricts_to_the_file_namespace():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return _search_response()

    await search_commons(
        "mitosis", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert seen["params"]["gsrnamespace"] == "6"
    assert seen["params"]["gsrsearch"] == "mitosis"


async def test_empty_query_returns_no_candidates_without_a_network_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _search_response()

    result = await search_commons(
        "   ", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == []
    assert called["n"] == 0


async def test_a_well_formed_page_becomes_a_candidate_with_correct_fields():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _search_response(_page("File:Mitosis diagram.svg"))

    [candidate] = await search_commons(
        "mitosis", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert candidate.title == "File:Mitosis diagram.svg"
    assert candidate.image_url == "https://upload.wikimedia.org/commons/x/File.png"
    assert candidate.file_page_url == "https://commons.wikimedia.org/wiki/File:File.png"
    assert candidate.width == 1200 and candidate.height == 900
    assert candidate.license_short_name == "CC BY-SA 4.0"


async def test_artist_html_markup_is_stripped_for_a_clean_attribution_line():
    def handler(_request: httpx.Request) -> httpx.Response:
        page = _page("File:X.png", artist='<a href="//en.wikipedia.org/wiki/Jane">Jane Doe</a>')
        return _search_response(page)

    [candidate] = await search_commons(
        "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert candidate.artist == "Jane Doe"
    assert "<a" not in candidate.attribution_text()
    assert "Jane Doe" in candidate.attribution_text()


async def test_a_page_with_no_imageinfo_is_skipped_not_crashed_on():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _search_response({"title": "File:Broken.png"})  # no "imageinfo" key at all

    result = await search_commons(
        "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == []


async def test_no_search_results_is_an_empty_list_not_an_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {}})

    result = await search_commons(
        "a genuinely unmatched query", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert result == []


# ---------------------------------------------------------------------------
# license allow/deny
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "license_short_name,expected",
    [
        ("Public Domain", True),
        ("CC0 1.0", True),
        ("CC BY 4.0", True),
        ("CC BY-SA 4.0", True),
        ("CC BY-NC 4.0", False),  # non-commercial - never reusable here
        ("CC BY-ND 4.0", False),  # no-derivatives
        ("All Rights Reserved", False),
        ("", False),  # missing/unclear license is never treated as usable
    ],
)
async def test_license_allow_list(license_short_name, expected):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _search_response(_page("File:X.png", license_short_name=license_short_name))

    [candidate] = await search_commons(
        "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert candidate.license_ok is expected


# ---------------------------------------------------------------------------
# retry / failure behaviour
# ---------------------------------------------------------------------------


async def test_429_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(429, json={"error": "throttled"})
        return _search_response(_page("File:X.png"))

    result = await search_commons(
        "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert len(result) == 1
    assert attempts["n"] == 2


async def test_robot_policy_403_is_retried_then_succeeds():
    """Root-cause fix: Wikimedia's own volume/abuse-policy block returns
    403 (not 429), with wording explicitly framing it as a recoverable,
    volume-driven state ("contact us if you need higher volume") rather
    than a permanent ban - this must get the same bounded retry a 5xx
    already did, not fail immediately and permanently on the first 403."""
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(
                403,
                text=(
                    "Please respect our robot policy https://w.wiki/4wJS. If you think you are "
                    "incorrectly blocked, please contact noc@wikimedia.org"
                ),
            )
        return _search_response(_page("File:X.png"))

    result = await search_commons(
        "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert len(result) == 1
    assert attempts["n"] == 2


async def test_persistent_robot_policy_403_raises_clearly_after_exhausting_retries():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Please respect our robot policy")

    with pytest.raises(AIServiceError, match="403"):
        await search_commons(
            "x", limit=5, settings=_settings(max_call_retries=2), transport=httpx.MockTransport(handler)
        )


async def test_persistent_5xx_raises_after_exhausting_retries():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    with pytest.raises(AIServiceError):
        await search_commons(
            "x", limit=5, settings=_settings(max_call_retries=2), transport=httpx.MockTransport(handler)
        )


async def test_malformed_json_raises_a_clear_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(AIServiceError):
        await search_commons(
            "x", limit=5, settings=_settings(), transport=httpx.MockTransport(handler)
        )


async def test_fetch_image_bytes_returns_the_raw_response_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_IMAGE_BYTES)

    data = await fetch_image_bytes(
        "https://upload.wikimedia.org/commons/x/File.png",
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert data == _IMAGE_BYTES


# ---------------------------------------------------------------------------
# SVG support - svg_dimensions / _vision_viewport (rasterize_svg_for_vision
# itself needs a real/Playwright-driven browser, so it's exercised directly
# against the live API in manual verification, not here - see the approved
# plan's own "no live-network testing in this sandbox" limitation, now
# resolved by the real check this fix was built from).
# ---------------------------------------------------------------------------


def test_svg_dimensions_reads_explicit_width_and_height():
    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"></svg>'
    assert svg_dimensions(data) == (500, 300)


def test_svg_dimensions_strips_css_units():
    data = b'<svg width="500px" height="300pt"></svg>'
    assert svg_dimensions(data) == (500, 300)


def test_svg_dimensions_falls_back_to_viewbox_when_width_height_are_missing():
    data = b'<svg viewBox="0 0 640 480"></svg>'
    assert svg_dimensions(data) == (640, 480)


def test_svg_dimensions_falls_back_to_viewbox_when_width_is_a_percentage():
    data = b'<svg width="100%" height="100%" viewBox="0 0 640 480"></svg>'
    assert svg_dimensions(data) == (640, 480)


def test_svg_dimensions_is_none_when_nothing_usable_is_present():
    assert svg_dimensions(b"<svg></svg>") == (None, None)


def test_svg_dimensions_never_raises_on_garbage_input():
    assert svg_dimensions(b"\xff\xfe not even xml") == (None, None)
    assert svg_dimensions(b"") == (None, None)


def test_vision_viewport_preserves_aspect_ratio_and_caps_the_long_side():
    viewport = _vision_viewport(2361, 409)
    assert viewport["width"] == 1400
    assert round(viewport["height"] / viewport["width"], 2) == round(409 / 2361, 2)


def test_vision_viewport_floors_a_tiny_intrinsic_size():
    viewport = _vision_viewport(50, 50)
    assert viewport["width"] >= 400 and viewport["height"] >= 400


def test_vision_viewport_falls_back_to_a_square_when_no_size_is_known():
    assert _vision_viewport(None, None) == {"width": 1024, "height": 1024}
