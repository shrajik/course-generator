"""Edit Visuals Part 2: suggested overlay labels for a text-free raster
illustration - app.services.image_service.ImageService._suggest_overlay_labels
and its wiring into _generate_illustration. The labels are a separate,
client-rendered layer (see app.schemas.blocks.OverlayLabel) - the image
itself is never asked to bake text in, unaffected by any of this."""

from __future__ import annotations

from app.core.config import get_settings
from app.core.errors import AIServiceError
from app.course.templates.registry import load_template
from app.render.html_renderer import render_document_html
from app.schemas.blocks import BlockType
from app.schemas.document import CourseDocument, DocumentMeta, Page
from app.schemas.document import Block
from app.services.image_service import (
    ImageRelevanceCheck,
    ImageService,
    ImageTextCheck,
    OverlayLabelSuggestions,
    SuggestedOverlayLabel,
)


def _image_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"prompt": "A prompt", **content})


async def _fake_image(*, prompt, size=None, phase="image", provider=None) -> bytes:
    return b"png-bytes"


def _passing_structured(overlay_labels: list[SuggestedOverlayLabel] | None = None):
    """A fake `ai.structured` that passes every QA schema cleanly and
    returns the given overlay-label suggestions (or none)."""

    async def fake_structured(*, schema, **kwargs):
        if schema is OverlayLabelSuggestions:
            return OverlayLabelSuggestions(labels=overlay_labels or [])
        if schema is ImageTextCheck:
            return ImageTextCheck()
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck()
        return schema()

    return fake_structured


# --- _suggest_overlay_labels in isolation -------------------------------------


async def test_suggest_overlay_labels_is_a_no_op_for_a_blank_brief():
    svc = ImageService()
    result = await svc._suggest_overlay_labels(b"png-bytes", brief="", course_title="Biology")
    assert result.labels == []


async def test_suggest_overlay_labels_never_raises_on_a_vision_call_failure(monkeypatch):
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    result = await svc._suggest_overlay_labels(b"png-bytes", brief="Explain the water cycle", course_title="Biology")
    assert result.labels == []  # inconclusive is treated the same as "nothing suggested", never an error


# --- wiring into _generate_illustration ---------------------------------------


async def test_a_successful_illustration_gets_its_suggested_labels_persisted():
    svc = ImageService()
    svc.ai.image = _fake_image  # type: ignore[method-assign]
    svc.ai.structured = _passing_structured(
        [SuggestedOverlayLabel(text="Transformer", x_pct=30, y_pct=50, style="label")]
    )

    block = _image_block(purpose="Explain the magnetic field around a transformer")
    template = load_template("technical")
    ok = await svc._generate_illustration(
        course_id="course_overlay_success", block=block, template=template, course_title="Physics",
    )

    assert ok is True
    labels = block.content.get("overlay_labels")
    assert labels == [{"id": "ol0", "text": "Transformer", "x_pct": 30.0, "y_pct": 50.0, "style": "label", "color": ""}]


async def test_blank_suggested_labels_are_filtered_out():
    svc = ImageService()
    svc.ai.image = _fake_image  # type: ignore[method-assign]
    svc.ai.structured = _passing_structured(
        [SuggestedOverlayLabel(text="  ", x_pct=10, y_pct=10), SuggestedOverlayLabel(text="Real label", x_pct=20, y_pct=20)]
    )

    block = _image_block(purpose="A labelled apparatus")
    template = load_template("technical")
    await svc._generate_illustration(
        course_id="course_overlay_blank", block=block, template=template, course_title="Physics",
    )

    labels = block.content.get("overlay_labels")
    assert len(labels) == 1 and labels[0]["text"] == "Real label"


async def test_section_intro_illustrations_never_get_suggested_labels():
    """Deliberately simple, single-subject, no-label-wanted images by
    design (SECTION_INTRO_ILLUSTRATION_GUIDANCE) - suggesting labels for
    them would contradict their own brief."""
    svc = ImageService()
    svc.ai.image = _fake_image  # type: ignore[method-assign]
    svc.ai.structured = _passing_structured(
        [SuggestedOverlayLabel(text="Should never appear", x_pct=50, y_pct=50)]
    )

    block = _image_block(purpose="A section intro icon", illustration_style="section_intro")
    template = load_template("technical")
    await svc._generate_illustration(
        course_id="course_overlay_section_intro", block=block, template=template, course_title="Course",
    )

    assert block.content.get("overlay_labels") == []


async def test_the_overlay_suggestion_setting_is_a_clean_off_switch():
    settings = get_settings().model_copy(update={"enable_image_overlay_suggestions": False})
    svc = ImageService(settings=settings)
    svc.ai.image = _fake_image  # type: ignore[method-assign]

    calls = {"overlay": 0}

    async def fake_structured(*, schema, **kwargs):
        if schema is OverlayLabelSuggestions:
            calls["overlay"] += 1
            return OverlayLabelSuggestions(labels=[SuggestedOverlayLabel(text="x", x_pct=1, y_pct=1)])
        if schema is ImageTextCheck:
            return ImageTextCheck()
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck()
        return schema()

    svc.ai.structured = fake_structured  # type: ignore[method-assign]

    block = _image_block(purpose="A depiction of a pulley system")
    template = load_template("technical")
    await svc._generate_illustration(
        course_id="course_overlay_disabled", block=block, template=template, course_title="Physics",
    )

    assert calls["overlay"] == 0  # the call was never made at all, not just filtered after the fact
    assert block.content.get("overlay_labels") == []


async def test_the_content_safety_fallback_never_gets_suggested_labels():
    """The safety-fallback image is deliberately generic/abstract (see
    `_safety_fallback_prompt`) - labels drawn from the ORIGINAL specific
    brief would name parts this generic picture doesn't actually show."""
    svc = ImageService()
    calls = {"image": 0, "overlay": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["image"] += 1
        if calls["image"] == 1:
            raise AIServiceError(
                'Azure FLUX.2-flex request failed with status 400: '
                '{"error":{"code":"content_safety_violation","message":"Content violated RAI policy."}}'
            )
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is OverlayLabelSuggestions:
            calls["overlay"] += 1
            return OverlayLabelSuggestions(labels=[SuggestedOverlayLabel(text="x", x_pct=1, y_pct=1)])
        if schema is ImageTextCheck:
            return ImageTextCheck()
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck()
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc.ai.structured = fake_structured  # type: ignore[method-assign]

    block = _image_block(purpose="A depiction of a dangerous industrial failure")
    template = load_template("technical")
    ok = await svc._generate_illustration(
        course_id="course_overlay_safety", block=block, template=template, course_title="Safety",
    )

    assert ok is True
    assert calls["overlay"] == 0
    assert block.content.get("overlay_labels") == []


# --- export rendering (course.html.j2) -----------------------------------------


def _illustration_document(*, overlay_labels: list[dict] | None = None) -> CourseDocument:
    block = Block(
        id="b1", type=BlockType.IMAGE,
        content={
            "kind": "illustration", "path": "assets/image_001.png", "alt": "A transformer",
            "width": 800, "height": 600,
            **({"overlay_labels": overlay_labels} if overlay_labels is not None else {}),
        },
        layout={"x": 0, "y": 0, "width": 666, "height": 400, "z_index": 0},
    )
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[Page(id="p1", page_number=1, kind="content", blocks=[block])],
    )


def test_export_renders_overlay_labels_at_the_right_position():
    template = load_template("technical_v1")
    document = _illustration_document(
        overlay_labels=[
            {"id": "ol0", "text": "Transformer", "x_pct": 30, "y_pct": 50, "style": "label", "color": ""},
            {"id": "ol1", "text": "Magnetic field", "x_pct": 70, "y_pct": 20, "style": "callout", "color": "#dc2626"},
        ]
    )
    html = render_document_html(document, template)
    assert 'class="figure-frame" style="aspect-ratio:800/600"' in html
    assert "Transformer" in html and "Magnetic field" in html
    assert "left:30.0%; top:50.0%;" in html
    assert "border-color:#dc2626; color:#dc2626;" in html


def test_export_renders_a_plain_image_unchanged_when_there_are_no_overlay_labels():
    template = load_template("technical_v1")
    document = _illustration_document(overlay_labels=[])
    html = render_document_html(document, template)
    assert 'class="figure-frame"' not in html
    assert '<img class="figure" src="../assets/image_001.png" alt="A transformer">' in html


def test_export_renders_a_plain_image_when_intrinsic_dimensions_are_missing():
    """No width/height stored (shouldn't happen for a real generated image,
    but defensively) - falls back to the plain, non-aspect-ratio markup
    rather than an invalid `aspect-ratio:None/None`."""
    template = load_template("technical_v1")
    block = Block(
        id="b1", type=BlockType.IMAGE,
        content={
            "kind": "illustration", "path": "assets/image_001.png", "alt": "x",
            "overlay_labels": [{"id": "ol0", "text": "x", "x_pct": 50, "y_pct": 50, "style": "label", "color": ""}],
        },
        layout={"x": 0, "y": 0, "width": 666, "height": 400, "z_index": 0},
    )
    document = CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[Page(id="p1", page_number=1, kind="content", blocks=[block])],
    )
    html = render_document_html(document, template)
    assert 'class="figure-frame"' not in html


