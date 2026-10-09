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

from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.services.image_service import (
    DEFAULT_ILLUSTRATION_GUIDANCE,
    MAX_PROMPT_CHARS,
    TEXTBOOK_ILLUSTRATION_GUIDANCE,
    ImageTextCheck,
    ImageService,
    chapter_section_context,
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


def test_course_title_always_travels_with_a_do_not_render_instruction():
    """A quoted course/subject name risks being read as a title to render
    literally into the picture (confirmed real case: a "HUMAN BIOLOGY"
    banner baked across an illustration) - no longer dropped entirely (see
    chapter_section_context's own docstring for why it has to be present),
    but it must never appear without the explicit "never render any of this
    as visible text" instruction immediately preceding it."""
    template = load_template("technical")
    block = _image_block()
    prompt = ImageService.build_prompt(block, template, "Human Biology")
    assert "Human Biology" in prompt
    assert "never render any of this as visible text" in prompt
    # The instruction must come BEFORE the subject name, not after.
    assert prompt.index("never render any of this as visible text") < prompt.index("Human Biology")


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


def test_textbook_illustration_tells_the_model_to_render_expected_labels():
    """A real, confirmed gap: `expected_labels` was previously used ONLY to
    verify the finished image after the fact, never to actually ask the
    model to draw them - a real case shipped an unlabelled magnetic-flux
    apparatus illustration despite the writer having named the labels it
    needed. The generation prompt itself must now explicitly request them."""
    template = load_template("technical")
    block = _image_block(
        illustration_style="textbook",
        expected_labels=["B", "n̂", "+", "-"],
    )
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert "B" in prompt
    assert "must include these exact labels" in prompt


def test_expected_labels_are_not_injected_for_the_strict_no_text_styles():
    """section_intro and the default style both carry their own STRICT
    NO-TEXT RULE - injecting a "must include these labels" line there would
    directly contradict it if `expected_labels` were ever populated by
    mistake. Scoped to style == "textbook" only, matching the only style
    this field is actually wired up for (ImageService._generate_illustration's
    `is_textbook and expected_labels` branch)."""
    template = load_template("technical")
    for style in ("section_intro", ""):
        block = _image_block(illustration_style=style, expected_labels=["B", "n"])
        prompt = ImageService.build_prompt(block, template, "Course Title")
        assert "must include these exact labels" not in prompt


def test_textbook_illustration_with_no_expected_labels_has_no_labels_line():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook")
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert "must include these exact labels" not in prompt


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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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


async def test_verify_image_has_no_text_reports_whatever_text_was_detected(monkeypatch):
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        assert schema is ImageTextCheck
        return ImageTextCheck(detected_text=["coan oicts", "medership"])

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    found = await svc._verify_image_has_no_text(b"png-bytes")
    assert found == ["coan oicts", "medership"]


async def test_verify_image_has_no_text_returns_empty_when_genuinely_clean(monkeypatch):
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        return ImageTextCheck(detected_text=[])

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    found = await svc._verify_image_has_no_text(b"png-bytes")
    assert found == []


async def test_verify_image_has_no_text_never_raises_on_a_vision_call_failure(monkeypatch):
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    found = await svc._verify_image_has_no_text(b"png-bytes")
    assert found == []  # a QA-step failure is never treated as "it has text"


async def test_generate_text_free_illustration_returns_the_first_attempt_when_already_clean():
    svc = ImageService()
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        return b"first-attempt-bytes"

    async def fake_verify(image):
        return []

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_text_free_illustration("a prompt", block_id="b1")
    assert data == b"first-attempt-bytes"
    assert verified is True
    assert calls["n"] == 1


async def test_generate_text_free_illustration_retries_once_if_text_survives(monkeypatch):
    """The exact real bug this hardens: a flowchart-shaped prompt's
    "text-free" fallback still rendered garbled labels ("coan oicts",
    "medership" - a real, confirmed case) because the old code never
    checked. The first text-free attempt is verified; if text survived
    anyway, one more, more emphatic attempt is made and verified too."""
    svc = ImageService()
    prompts_used: list[str] = []
    verify_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return f"attempt-{len(prompts_used)}".encode()

    async def fake_verify(image):
        verify_calls["n"] += 1
        # first attempt still has text, the emphatic retry comes back clean
        return ["coan oicts"] if verify_calls["n"] == 1 else []

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_text_free_illustration("a prompt", block_id="b1")
    assert data == b"attempt-2"
    assert verified is True
    assert len(prompts_used) == 2
    assert "zero text" in prompts_used[1].lower()  # the retry is the more emphatic prompt
    assert verify_calls["n"] == 2  # the retry IS verified too, not trusted blindly


async def test_generate_text_free_illustration_honestly_reports_unverified_if_still_has_text():
    """Bounded at 2 attempts, never unlimited - whatever the final attempt
    produces ships, but with an HONEST verified=False if text is still
    there, never silently accepted as correct just because attempts ran
    out (the same contract _generate_with_text_check already keeps)."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return f"attempt-{len(prompts_used)}".encode()

    async def fake_verify(image):
        return ["coan oicts"]  # every attempt, including the emphatic retry, still has text

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]

    data, verified = await svc._generate_text_free_illustration("a prompt", block_id="b1")
    assert data == b"attempt-2"
    assert verified is False  # honestly reported as unverified, not claimed correct
    assert len(prompts_used) == 2  # bounded - never a third attempt


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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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
    # The mock AI client's own structured() call reports no text detected,
    # so this clean first attempt is genuinely verified, not left at the
    # old None ("nothing was checked") - see _generate_text_free_illustration.
    assert block.content.get("text_verified") is True


async def test_default_style_illustration_is_now_verified_text_free_too():
    """A real, confirmed gap: the default (blank `illustration_style`)
    picture carries its own STRICT NO-TEXT RULE (see
    DEFAULT_ILLUSTRATION_GUIDANCE) but previously had ZERO verification,
    unlike the force-text-free diagram-fallback case - trusted on faith
    even though it's exactly as checkable. It must now route through the
    same verified text-free path."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block()  # illustration_style unset - the default style
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_default_verified", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert "do not render any text" in prompts_used[0].lower()
    assert block.content.get("text_verified") is True


async def test_section_intro_illustration_is_now_verified_text_free_too():
    """The exact real, confirmed case: small section-intro icons shipped
    with "sourrces", "Fraith magntised field", "toleente", "Teretore",
    "Mechanician" baked in, because this style previously had ZERO
    verification despite its own STRICT NO-TEXT RULE (see
    SECTION_INTRO_ILLUSTRATION_GUIDANCE). It must now route through the
    same verified text-free path as every other no-text style."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(illustration_style="section_intro")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_section_intro_verified", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert "do not render any text" in prompts_used[0].lower()
    assert block.content.get("text_verified") is True


async def test_textbook_style_with_no_expected_labels_still_uses_the_plain_unverified_path():
    """Regression guard: `textbook` is the one style genuinely allowed to
    carry essential labels - when the writer didn't name any
    (`expected_labels` empty), there is nothing to verify, so it must keep
    using the plain, single, unverified call - never the text-FREE path
    (which would wrongly strip labels a textbook picture is allowed to
    have), and never left with a false "nothing was checked" blurred into
    a False/True verdict."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(illustration_style="textbook")  # no expected_labels
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_textbook_no_labels", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert len(prompts_used) == 1
    assert "do not render any text" not in prompts_used[0].lower()
    assert block.content.get("text_verified") is None


async def test_a_content_rich_textbook_illustration_gets_a_wider_canvas():
    """A real, confirmed bug: a 4-label composition (several icons side by
    side) got drawn wider than a square 1024x1024 canvas could hold,
    slicing the first and last items off at the edges mid-label. Giving a
    content-rich textbook illustration (4+ expected labels) real extra
    width up front - not just hoping the model rearranges into a grid on
    its own - is the more reliable fix."""
    svc = ImageService()
    sizes_used: list[str | None] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        sizes_used.append(size)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        illustration_style="textbook",
        expected_labels=["A", "B", "C", "D"],  # 4 labels - the threshold
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_wide_canvas", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert sizes_used[0] == "1536x1024"


async def test_a_sparse_textbook_illustration_keeps_the_default_square_canvas():
    """Regression guard: a typical 1-3 label picture (a single apparatus,
    not a multi-icon composition) must keep using the default size -
    widening every textbook illustration regardless of content would be
    wasteful and pointless for a picture that was never cramped."""
    svc = ImageService()
    sizes_used: list[str | None] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        sizes_used.append(size)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(illustration_style="textbook", expected_labels=["N", "S", "B"])
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_default_canvas", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    assert sizes_used[0] != "1536x1024"


async def test_generate_for_block_forces_text_free_when_the_diagram_path_fails(monkeypatch):
    """End-to-end through the real dispatch: a diagram block whose structured
    generation fails must fall back to a TEXT-FREE illustration, never the
    unprotected, label-heavy prompt the raster model reliably garbles."""
    svc = ImageService()
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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


async def test_generate_for_block_forces_text_free_when_diagram_generation_is_disabled(monkeypatch):
    """A real, confirmed gap this fixes: `enable_diagram_generation=False`
    used to leave `structured_fallback` at its default `False`, since the
    block never even reached the "generation was attempted and failed"
    branch - so a labelled flowchart-shaped prompt reached the raster model
    completely unprotected. `structured_fallback` must now start True for
    ANY diagram/concept_experience-kind block, before the feature flag is
    even checked."""
    svc = ImageService()
    svc.settings.enable_diagram_generation = False
    prompts_used: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        prompts_used.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        kind="diagram", diagram_kind="flow_chart",
        purpose="Show the RAG request pipeline", prompt="User query, retrieve, LLM, answer",
    )
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_flag_off", block=block, template=template, course_title="Test Course"
    )

    assert ok is True
    assert len(prompts_used) == 1
    assert "do not render any text" in prompts_used[0].lower()


# ---------------------------------------------------------------------------
# Chapter/section context propagation (see chapter_section_context) - the
# confirmed root cause: `build_prompt` used to accept `course_title` but
# never use it, so an image request could carry NO deterministic
# chapter/subject signal at all if the writer's own free-text `image_prompt`
# happened to be vague.
# ---------------------------------------------------------------------------


def test_context_propagates_subject_chapter_and_section_into_the_final_prompt():
    template = load_template("technical")
    block = _image_block(illustration_style="section_intro")
    prompt = ImageService.build_prompt(
        block, template, "Advanced Python",
        chapter_title="Decorators and Functional Tools", section_title="Learning Objectives",
    )
    assert "Advanced Python" in prompt
    assert "Decorators and Functional Tools" in prompt
    assert "Learning Objectives" in prompt
    assert "never render any of this as visible text" in prompt


def test_context_is_omitted_cleanly_when_not_available():
    """No chapter/section known (e.g. a block outside any chapter) must never
    produce a dangling "Chapter: " with nothing after it."""
    template = load_template("technical")
    block = _image_block()
    prompt = ImageService.build_prompt(block, template, "")
    assert "Chapter:" not in prompt
    assert "Section:" not in prompt
    assert "Course:" not in prompt


def test_a_generic_writer_prompt_still_carries_real_chapter_context():
    """The exact failure mode this fixes: the writer left `image_prompt`
    blank, so `draft_to_content`'s own fallback put a generic section
    heading ("Learning Objectives") into `purpose` - previously the ONLY
    subject signal the image model ever saw. The deterministic context line
    must still carry the REAL chapter/subject regardless."""
    template = load_template("technical")
    block = _image_block(illustration_style="section_intro", prompt="", purpose="Learning Objectives")
    prompt = ImageService.build_prompt(
        block, template, "Advanced Python",
        chapter_title="Preparing Your Development Environment", section_title="Learning Objectives",
    )
    assert "Preparing Your Development Environment" in prompt
    assert "Advanced Python" in prompt


def test_different_chapters_produce_different_prompts_for_identical_block_content():
    """Chapter isolation: the SAME generic block content must still resolve
    to a different final prompt per chapter, so a learner can't end up with
    what is structurally "the same visual request" for two unrelated topics."""
    template = load_template("technical")
    block = _image_block(illustration_style="section_intro", prompt="", purpose="Learning Objectives")
    python_prompt = ImageService.build_prompt(
        block, template, "Advanced Python", chapter_title="Decorators", section_title="Learning Objectives",
    )
    biology_prompt = ImageService.build_prompt(
        block, template, "Human Biology", chapter_title="Photosynthesis", section_title="Learning Objectives",
    )
    assert python_prompt != biology_prompt
    assert "Decorators" in python_prompt and "Decorators" not in biology_prompt
    assert "Photosynthesis" in biology_prompt and "Photosynthesis" not in python_prompt


def test_chapter_section_context_walks_the_document_in_order():
    """Unit test for the helper itself: each block gets the nearest
    preceding level-1 heading as its chapter and the nearest preceding
    heading of any other level as its section - a new chapter heading resets
    the current section back to blank."""
    from app.schemas.document import CourseDocument, Page

    blocks = [
        Block(type=BlockType.HEADING, content={"text": "Chapter 1: Python Basics", "level": 1}),
        Block(type=BlockType.HEADING, content={"text": "Learning Objectives", "level": 2}),
        (img1 := _image_block()),
        Block(type=BlockType.HEADING, content={"text": "Concept", "level": 2}),
        (img2 := _image_block()),
        Block(type=BlockType.HEADING, content={"text": "Chapter 2: Functions", "level": 1}),
        (img3 := _image_block()),
    ]
    document = CourseDocument(
        document_id="d", course_id="c", course_title="Advanced Python", template_id="technical",
        pages=[Page(blocks=blocks)],
    )
    context = chapter_section_context(document)
    assert context[img1.id] == ("Chapter 1: Python Basics", "Learning Objectives", "")
    assert context[img2.id] == ("Chapter 1: Python Basics", "Concept", "")
    # Chapter 2's own heading resets the section back to blank - no stale
    # section title leaking across a chapter boundary.
    assert context[img3.id] == ("Chapter 2: Functions", "", "")


def test_key_concept_is_found_even_when_the_objectives_block_comes_after_the_image():
    """A real, confirmed pattern in generated course data: a section_intro
    image sits BEFORE the chapter's own `learning_objectives` block, not
    after it - a naive "nearest preceding" walk (like chapter/section
    title) would miss it entirely for that image. `key_concept` must still
    resolve it, since it's collected over the whole chapter, not just what
    came before a given block."""
    from app.schemas.blocks import BlockType as BT
    from app.schemas.document import CourseDocument, Page

    blocks = [
        Block(type=BT.HEADING, content={"text": "Newton's Laws of Motion", "level": 1}),
        Block(type=BT.HEADING, content={"text": "Learning Objectives", "level": 2}),
        (img := _image_block(illustration_style="section_intro")),
        Block(
            type=BT.LEARNING_OBJECTIVES,
            content={"items": ["Explain how force relates to acceleration for a fixed mass"]},
        ),
    ]
    document = CourseDocument(
        document_id="d", course_id="c", course_title="Physics Fundamentals", template_id="technical",
        pages=[Page(blocks=blocks)],
    )
    context = chapter_section_context(document)
    assert context[img.id] == (
        "Newton's Laws of Motion", "Learning Objectives",
        "Explain how force relates to acceleration for a fixed mass",
    )


def test_key_concept_falls_back_to_summary_key_takeaways_when_no_objectives_exist():
    from app.schemas.blocks import BlockType as BT
    from app.schemas.document import CourseDocument, Page

    blocks = [
        Block(type=BT.HEADING, content={"text": "Cellular Respiration", "level": 1}),
        (img := _image_block()),
        Block(type=BT.SUMMARY, content={"key_takeaways": ["Mitochondria convert glucose and oxygen into ATP"]}),
    ]
    document = CourseDocument(
        document_id="d", course_id="c", course_title="Human Biology", template_id="technical",
        pages=[Page(blocks=blocks)],
    )
    context = chapter_section_context(document)
    assert context[img.id][2] == "Mitochondria convert glucose and oxygen into ATP"


def test_key_concept_flows_into_the_final_prompt():
    template = load_template("technical")
    block = _image_block(illustration_style="section_intro", prompt="", purpose="")
    prompt = ImageService.build_prompt(
        block, template, "Physics Fundamentals",
        chapter_title="Newton's Laws of Motion", section_title="Concept",
        key_concept="Explain how force relates to acceleration for a fixed mass",
    )
    assert "This chapter teaches: Explain how force relates to acceleration for a fixed mass" in prompt


# ---------------------------------------------------------------------------
# Mock vs real generation mode - must never be ambiguous. `AIClient.is_mock`
# already exists for exactly this purpose (course_service already uses it
# for text generation); illustrations now record and log it too.
# ---------------------------------------------------------------------------


class _FakeAI:
    def __init__(self, *, is_mock: bool) -> None:
        self.is_mock = is_mock

    async def image(self, *, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"


async def test_mock_generation_is_recorded_as_mock_not_silently_as_real():
    svc = ImageService(ai=_FakeAI(is_mock=True))
    block = _image_block()
    template = load_template("technical")
    ok = await svc._generate_illustration(
        course_id="course_mock_mode", block=block, template=template, course_title="Test Course",
    )
    assert ok is True
    assert block.content["generation_mode"] == "mock"


async def test_real_generation_is_recorded_as_real():
    svc = ImageService(ai=_FakeAI(is_mock=False))
    block = _image_block()
    template = load_template("technical")
    ok = await svc._generate_illustration(
        course_id="course_real_mode", block=block, template=template, course_title="Test Course",
    )
    assert ok is True
    assert block.content["generation_mode"] == "real"


# ---------------------------------------------------------------------------
# textbook-style illustrations override the configured image provider - see
# Settings.textbook_image_provider's own docstring for the real, confirmed
# comparison this is based on (the configured default missed/garbled labels
# on 2 of 3 real topics; the override provider got every label right on all
# 3), so label-carrying textbook illustrations never inherit a weaker
# default just because that's what decorative illustrations are set to use.
# ---------------------------------------------------------------------------


async def test_textbook_illustration_uses_the_textbook_image_provider_override():
    settings = get_settings().model_copy(
        update={"image_provider": "azure", "textbook_image_provider": "openai"}
    )
    svc = ImageService(ai=_FakeAI(is_mock=False), settings=settings)
    seen_providers = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        seen_providers.append(provider)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block(illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_textbook_provider", block=block, template=template, course_title="Test Course",
    )
    assert ok is True
    assert seen_providers == ["openai"]


async def test_non_textbook_illustration_does_not_override_the_provider():
    """Regression guard: the override is scoped to `illustration_style ==
    "textbook"` only - a decorative/section_intro illustration must keep
    using whatever the configured default is (None here means "use
    settings.image_provider", not a forced override)."""
    settings = get_settings().model_copy(
        update={"image_provider": "azure", "textbook_image_provider": "openai"}
    )
    svc = ImageService(ai=_FakeAI(is_mock=False), settings=settings)
    seen_providers = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        seen_providers.append(provider)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()  # no illustration_style set
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_default_provider", block=block, template=template, course_title="Test Course",
    )
    assert ok is True
    assert seen_providers == [None]


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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
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

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return original

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block()
    template = load_template("technical")

    await svc._generate_illustration(
        course_id="course_bytes_unchanged", block=block, template=template, course_title="Test Course"
    )

    saved_path = svc.storage.asset_abs_path("course_bytes_unchanged", block.content["path"])
    assert saved_path.read_bytes() == original
