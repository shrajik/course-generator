"""ImageService.build_prompt - the raster illustration prompt. Two fixed,
universal styles (neither depends on the per-template `image_guidance` any
more - see DEFAULT_ILLUSTRATION_GUIDANCE's own docstring for why):
`illustration_style: "textbook"` (a companion picture placed alongside a
diagram/concept_experience visual - see WRITER_SYSTEM/EDITOR_SYSTEM in
app.agents.prompts) uses TEXTBOOK_ILLUSTRATION_GUIDANCE, and every other
illustration uses DEFAULT_ILLUSTRATION_GUIDANCE - a strict, subject-agnostic
"zero text anywhere" rule, the architecturally cleaner fix for AI-garbled
baked-in text than catching it after generation. Also covers the
post-generation text-accuracy check (ImageService._verify_generated_text /
_generate_with_text_check) - a real reported bug: AI-generated illustrations
sometimes misspell or garble baked-in text ("Inducd crrrent" instead of
"Induced current")."""

from __future__ import annotations

from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.services.image_service import (
    DEFAULT_ILLUSTRATION_GUIDANCE,
    MAX_PROMPT_CHARS,
    TEXTBOOK_ILLUSTRATION_GUIDANCE,
    ImageTextCheck,
    ImageService,
)


def _image_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"prompt": "A prompt", **content})


def _real_image_bytes(image_format: str) -> bytes:
    """A genuine, tiny, PIL-decodable image encoded in `image_format`
    ("PNG"/"JPEG"/"WEBP") - unlike the `b"png-bytes"` placeholder the other
    tests in this file use (which deliberately ISN'T a real image, since
    those tests never inspect the saved extension), these are real enough
    for `ImageService._detect_extension`'s own `Image.open(...).format`
    check to genuinely identify."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_default_illustration_uses_the_strict_no_text_guidance():
    template = load_template("technical")
    block = _image_block()
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert DEFAULT_ILLUSTRATION_GUIDANCE in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE not in prompt


def test_default_illustration_keeps_the_full_guidance_for_a_realistic_prompt():
    """A real writer-authored `image_prompt` is never as short as the bare
    "A prompt" fixture every other test in this file uses - MAX_PROMPT_CHARS
    must leave enough room for a genuinely long, concrete one (a real
    confirmed case: a ~450-character prompt describing a multi-part
    photosynthesis scene) to coexist with the complete
    DEFAULT_ILLUSTRATION_GUIDANCE, not just a trivial one-liner. A truncated
    guidance could silently drop its own FINAL CHECK section or the
    RELATIONSHIP DIRECTION rule for exactly the prompts that need them most."""
    template = load_template("technical")
    realistic_prompt = (
        "A cross-section of a green leaf showing one large chloroplast with visible "
        "internal membrane layers, sunlight rays entering from above directly into "
        "the chloroplast, a water-droplet shaped cluster and a small gas-bubble "
        "cluster flowing into the chloroplast from opposite sides, and a glucose "
        "ring-molecule shape with a paired two-circle oxygen molecule flowing out "
        "the top. Polished scientific textbook illustration style, bright natural "
        "colors, no text"
    )
    block = _image_block(prompt=realistic_prompt)
    prompt = ImageService.build_prompt(block, template, "Advanced Biology")
    assert len(prompt) < MAX_PROMPT_CHARS
    assert DEFAULT_ILLUSTRATION_GUIDANCE in prompt
    assert realistic_prompt in prompt


def test_default_illustration_never_mentions_the_course_title():
    """A quoted course/subject name risks being read as a title to render
    literally into the picture (confirmed real case: a "HUMAN BIOLOGY"
    banner baked across an illustration) - dropped entirely now, for both
    styles, not just "textbook"."""
    template = load_template("technical")
    block = _image_block()
    prompt = ImageService.build_prompt(block, template, "Human Biology")
    assert "Human Biology" not in prompt


def test_textbook_illustration_style_uses_fixed_textbook_guidance():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook")
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt
    assert DEFAULT_ILLUSTRATION_GUIDANCE not in prompt


def test_illustration_style_is_case_and_whitespace_insensitive():
    template = load_template("technical")
    block = _image_block(illustration_style="  Textbook  ")
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt


def test_diagram_kind_ignored_for_illustration_style():
    """A "diagram" block never reaches build_prompt in real use (see
    ImageService.generate_for_block), but the function itself must not
    special-case anything else about the block - only illustration_style."""
    template = load_template("technical")
    block = _image_block(kind="diagram", illustration_style="textbook")
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt


# ---------------------------------------------------------------------------
# post-generation text accuracy check
# ---------------------------------------------------------------------------


async def test_verify_generated_text_skips_the_check_when_no_labels_expected():
    """No expected_labels means no text was promised - nothing to verify,
    and no AI call spent checking for it."""
    svc = ImageService()
    missing = await svc._verify_generated_text(b"png-bytes", [])
    assert missing == []


async def test_verify_generated_text_reports_a_simulated_garbled_read(monkeypatch):
    """Patch the AI client to simulate a real garbled read (the offline mock
    itself always returns an empty transcription - see mock_ai._image_text_check)
    and confirm the mismatch is correctly reported."""
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        assert schema is ImageTextCheck
        return ImageTextCheck(detected_text=["Inducd crrrent", "Lenz's law"])

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    missing = await svc._verify_generated_text(b"png-bytes", ["Induced current", "Lenz's law"])
    assert missing == ["Induced current"]  # found verbatim; the garbled one is reported


async def test_verify_generated_text_never_shows_the_model_what_the_labels_should_say(monkeypatch):
    """A real, confirmed failure: a rendered "Chlo-rlorphyit" was reported
    back as the clean "Chloroplast" because the verification prompt told the
    model what the text was supposed to say right before asking it to read
    the text - a textbook confirmation-bias / priming setup. Neither the
    expected labels themselves, nor any indication of what "correct" looks
    like, may appear anywhere in the prompt sent to the transcribing call."""
    svc = ImageService()
    captured: dict[str, str] = {}

    async def fake_structured(*, schema, system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return ImageTextCheck(detected_text=[])

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    await svc._verify_generated_text(b"png-bytes", ["Chloroplast", "Sunlight", "Glucose (C6H12O6)"])

    combined = captured["system"] + captured["user"]
    assert "Chloroplast" not in combined
    assert "Sunlight" not in combined
    assert "Glucose" not in combined


async def test_verify_generated_text_never_raises_on_a_vision_call_failure(monkeypatch):
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    missing = await svc._verify_generated_text(b"png-bytes", ["Some label"])
    assert missing == []  # a QA-step failure is never treated as a text error


async def test_generate_with_text_check_returns_the_first_attempt_when_it_already_passes():
    svc = ImageService()
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image"):
        calls["n"] += 1
        return b"first-attempt-bytes"

    async def fake_verify(image, expected_labels):
        return []

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_generated_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_with_text_check("a prompt", ["Label A"], block_id="b1")
    assert data == b"first-attempt-bytes"
    assert verified is True
    assert calls["n"] == 1


async def test_generate_with_text_check_retries_once_then_accepts_a_corrected_attempt():
    svc = ImageService()
    prompts_used: list[str] = []
    verify_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image"):
        prompts_used.append(prompt)
        return f"attempt-{len(prompts_used)}".encode()

    async def fake_verify(image, expected_labels):
        verify_calls["n"] += 1
        # first attempt fails, the retry (second call) passes
        return ["Label A"] if verify_calls["n"] == 1 else []

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_generated_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_with_text_check("a prompt", ["Label A"], block_id="b1")
    assert data == b"attempt-2"
    assert verified is True
    assert len(prompts_used) == 2
    assert "Label A" in prompts_used[1]  # the retry's prompt names the exact mistake


async def test_generate_with_text_check_falls_back_to_a_verified_text_free_image():
    """If two real (labelled) attempts both come back wrong, the third asks
    for no text at all - but that attempt is VERIFIED too, not trusted on
    faith (a real, confirmed case: a "text-free" request still rendered a
    garbled "Oxgeen"). Here the text-free attempt comes back clean, so it's
    accepted as verified without needing the fourth, most-emphatic attempt."""
    svc = ImageService()
    prompts_used: list[str] = []
    verify_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image"):
        prompts_used.append(prompt)
        return f"attempt-{len(prompts_used)}".encode()

    async def fake_verify(image, expected_labels):
        verify_calls["n"] += 1
        # first two labelled attempts fail, the text-free (3rd) attempt is clean
        return ["Label A"] if verify_calls["n"] <= 2 else []

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_generated_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_with_text_check("a prompt", ["Label A"], block_id="b1")
    assert data == b"attempt-3"
    assert verified is True
    assert len(prompts_used) == 3
    assert "do not render any text" in prompts_used[2].lower()
    assert verify_calls["n"] == 3  # the text-free attempt IS verified, not trusted blindly


async def test_generate_with_text_check_makes_one_final_emphatic_attempt_if_text_free_still_fails():
    """The exact real bug this hardens: a "text-free" attempt that still
    renders incorrect text (a confirmed real case - "Oxygen" survived as
    "Oxgeen" on what was meant to be a text-free image). One further,
    more emphatic text-free attempt is made - bounded at 4 total, never
    unlimited - and whatever it produces ships with an HONEST
    `verified=False` if it's still wrong, never silently accepted as
    correct just because attempts ran out."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image"):
        prompts_used.append(prompt)
        return f"attempt-{len(prompts_used)}".encode()

    async def fake_verify(image, expected_labels):
        return ["Label A"]  # every single attempt, including both text-free ones, still fails

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_generated_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_with_text_check("a prompt", ["Label A"], block_id="b1")
    assert data == b"attempt-4"
    assert verified is False  # honestly reported as unverified, not claimed correct
    assert len(prompts_used) == 4  # bounded - never a fifth attempt
    assert "zero text" in prompts_used[3].lower()


# ---------------------------------------------------------------------------
# force_text_free - the raster fallback from a failed diagram/concept_experience
# must never risk baked-in, unverified text (the confirmed cause of real
# garbled diagrams like "Balid prompt", "eparates prompt text", "Fallute")
# ---------------------------------------------------------------------------


async def test_generate_illustration_forces_a_text_free_prompt_on_a_structured_fallback():
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image"):
        prompts_used.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        kind="diagram", diagram_kind="flow_chart",
        purpose="Show the RAG request pipeline", prompt="User query, retrieve, LLM, answer",
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_force_text_free", block=block, template=template,
        course_title="Test Course", force_text_free=True,
    )

    assert ok is True
    assert len(prompts_used) == 1
    assert "do not render any text" in prompts_used[0].lower()


async def test_generate_for_block_forces_text_free_when_the_diagram_path_fails(monkeypatch):
    """End-to-end through the real dispatch: a diagram block whose structured
    generation fails must fall back to a TEXT-FREE illustration, never the
    unprotected, label-heavy prompt the raster model reliably garbles."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image"):
        prompts_used.append(prompt)
        return b"png-bytes"

    async def failing_diagram(self, **kwargs):
        return False

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(type(svc.diagrams), "generate_for_block", failing_diagram)

    block = _image_block(
        kind="diagram", diagram_kind="flow_chart",
        purpose="Show the RAG request pipeline", prompt="User query, retrieve, LLM, answer",
    )
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_force_text_free_e2e", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert len(prompts_used) == 1
    assert "do not render any text" in prompts_used[0].lower()


# ---------------------------------------------------------------------------
# ImageService._detect_extension / saved-asset extension - the real,
# reported bug: Azure's FLUX.2-flex (see azure_image_provider.py) returns
# JPEG-encoded bytes, but every illustration used to be saved under a
# hardcoded ".png" regardless of its actual encoding. OpenAI's gpt-image-1
# already reliably returns PNG, so its own behaviour is unaffected either
# way - these tests confirm that explicitly too.
# ---------------------------------------------------------------------------


def test_detect_extension_identifies_png():
    assert ImageService._detect_extension(_real_image_bytes("PNG")) == "png"


def test_detect_extension_identifies_jpeg_as_jpg():
    assert ImageService._detect_extension(_real_image_bytes("JPEG")) == "jpg"


def test_detect_extension_identifies_webp():
    assert ImageService._detect_extension(_real_image_bytes("WEBP")) == "webp"


def test_detect_extension_falls_back_to_png_for_unrecognised_bytes():
    assert ImageService._detect_extension(b"not a real image at all") == "png"


async def test_jpeg_bytes_from_the_provider_are_saved_with_a_jpg_extension():
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image"):
        return _real_image_bytes("JPEG")

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_jpeg_extension", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content["path"].endswith(".jpg")
    assert not block.content["path"].endswith(".png")


async def test_png_bytes_from_the_provider_are_still_saved_with_a_png_extension():
    """The existing, still-correct OpenAI behaviour - unchanged."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image"):
        return _real_image_bytes("PNG")

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_png_extension", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content["path"].endswith(".png")


async def test_webp_bytes_from_the_provider_are_saved_with_a_webp_extension():
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image"):
        return _real_image_bytes("WEBP")

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_webp_extension", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content["path"].endswith(".webp")


async def test_unrecognised_bytes_fall_back_to_png_without_crashing_generation():
    """Matches the existing error-handling contract: a format this module
    can't identify must never abort the block (the same "never block on
    metadata" philosophy `_dimensions` already follows) - it ships under
    the long-standing, still-safe ".png" default instead."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image"):
        return b"this is not a decodable image format"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_corrupt_extension", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert block.content["generated"] is True
    assert block.content["error"] is None
    assert block.content["path"].endswith(".png")


async def test_the_original_bytes_are_saved_unchanged_not_re_encoded():
    """Extension detection must only INSPECT the bytes, never re-encode
    them - the saved file's own bytes must be byte-for-byte identical to
    what the provider returned."""
    svc = ImageService()
    original = _real_image_bytes("JPEG")

    async def fake_image(*, prompt, size=None, phase="image"):
        return original

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    await svc._generate_illustration(
        course_id="course_bytes_unchanged", block=block, template=template, course_title="Test Course"
    )

    saved_path = svc.storage.asset_abs_path("course_bytes_unchanged", block.content["path"])
    assert saved_path.read_bytes() == original
