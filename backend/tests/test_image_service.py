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
from app.core.errors import AIServiceError
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec
from app.schemas.document import Block
from app.services import image_service
from app.services.image_service import (
    DEFAULT_ILLUSTRATION_GUIDANCE,
    MAI_IMAGE_MAX_PROMPT_TOKENS,
    MAX_PROMPT_CHARS,
    PROMPT_CHAR_BUDGET,
    SECTION_INTRO_ILLUSTRATION_GUIDANCE,
    TEXTBOOK_ILLUSTRATION_GUIDANCE,
    ImageRelevanceCheck,
    ImageTextCheck,
    ImageService,
    PromptAssemblyResult,
    _assemble_prompt_with_priority,
    _CONSERVATIVE_CHARS_PER_TOKEN,
    _PromptSegment,
    _shorten_to_budget,
    _validate_prompt_assembly,
    chapter_section_context,
    sanitize_expected_labels,
)
from app.services.wikimedia_image_qa import WikimediaCandidateCheck
from app.services.wikimedia_provider import WikimediaCandidate


def _image_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"prompt": "A prompt", **content})


async def _no_missing_labels(image: bytes, expected_labels: list[str]) -> list[str]:
    """A stub for `ImageService._verify_generated_text` that reports every
    expected label as found - for tests whose subject is something OTHER
    than text correctness (e.g. canvas sizing), where the default mock
    vision check's "nothing detected" response would otherwise trip the
    fail-closed publication gate and omit the image."""
    return []


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
# sanitize_expected_labels - a raster image model cannot reliably bake in a
# multi-symbol equation ("τ = μ·γ̇", "Kn = λ / L_c") as legible text, nor more
# than a handful of labels at once. A real, confirmed case: a fluid-
# mechanics course's textbook illustrations, each asked for 9-15
# `expected_labels` including several full equations, failed the text check
# on nearly every attempt and shipped as generic, off-topic placeholder
# images for most of a chapter - this is the permanent fix, applied once at
# every point `expected_labels` is read so the prompt and the verification
# step always agree on the same (shortened, renderable) list.
# ---------------------------------------------------------------------------


def test_sanitize_expected_labels_drops_multi_symbol_equations():
    labels = [
        "ρ (density)", "τ (shear stress)", "τ = μ·γ̇ (Newtonian)",
        "ν = μ/ρ", "Kn = λ / L_c", "σ (surface tension)",
    ]
    assert sanitize_expected_labels(labels) == ["ρ (density)", "τ (shear stress)", "σ (surface tension)"]


def test_sanitize_expected_labels_keeps_a_simple_symbol_equals_definition():
    """The writer prompt's own template example ("ε = induced emf") - one
    '=' pairing a symbol with a plain-word meaning is exactly what a raster
    model CAN render, unlike a relationship between several symbols."""
    assert sanitize_expected_labels(["ε = induced emf"]) == ["ε = induced emf"]


def test_sanitize_expected_labels_drops_combining_marks_and_superscripts():
    """A real, confirmed case: plain Greek letters (ρ, μ, σ, τ) rendered
    perfectly in a real generation, but "γ̇" (gamma with a COMBINING DOT
    ABOVE - standard shear-rate notation) kept failing text verification
    in every attempt even as the only non-trivial symbol in an otherwise
    maximally simple label set - a floating combining mark, or a tiny
    raised/lowered superscript/subscript digit, is not something a raster
    model can reliably reproduce as legible text, however short the label
    around it otherwise is."""
    labels = [
        "ρ (density)", "γ̇ (shear rate)", "τ–γ̇ curve",
        "Significant drop between 10² and 10⁴ s⁻¹", "μ (dynamic viscosity)",
    ]
    assert sanitize_expected_labels(labels) == ["ρ (density)", "μ (dynamic viscosity)"]


def test_sanitize_expected_labels_drops_a_clause_after_a_colon():
    """A real, confirmed case: these two entries have at most one operator-
    set character each (slipping past the equation check) but read as a
    caption or a verdict, not a short "term: definition" label - every
    extra word after the colon compounds the chance a raster model garbles
    or drops one, and both garbled on nearly every attempt in a real
    course. A short genuine definition ("ε: induced emf") still survives."""
    labels = [
        "Compressible: variable-density model warranted",
        "Rheogram: Apparent viscosity μ_app vs shear rate γ̇",
        "ε: induced emf",
    ]
    assert sanitize_expected_labels(labels) == ["ε: induced emf"]


def test_sanitize_expected_labels_caps_the_count_at_six():
    labels = [f"Label {i}" for i in range(12)]
    out = sanitize_expected_labels(labels)
    assert out == labels[:6]


def test_sanitize_expected_labels_keeps_a_long_plain_english_phrase():
    """Length alone was never the actual problem in the confirmed failures
    (several equations were under 20 characters) - a genuinely long but
    operator-free descriptive phrase is still a label a raster model can
    attempt, and must not be dropped just for its length."""
    long_phrase = "Induced current creates opposing field (Lenz)"
    assert sanitize_expected_labels([long_phrase]) == [long_phrase]


def test_sanitize_expected_labels_is_applied_in_the_generation_prompt():
    template = load_template("technical")
    block = _image_block(
        illustration_style="textbook",
        expected_labels=["ρ (density)", "Kn = λ / L_c", "τ = μ·γ̇ (Newtonian)"],
    )
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert "ρ (density)" in prompt
    assert "Kn = λ / L_c" not in prompt
    assert "τ = μ·γ̇" not in prompt


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
    """Production risk follow-up: a vision-call exception must never be
    silently treated the same as a genuine pass (`[]`) - it is reported as
    `None` (inconclusive), a distinct, never-accidentally-falsy-into-"clean"
    outcome every caller must check for explicitly."""
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    missing = await svc._verify_generated_text(b"png-bytes", ["Some label"])
    assert missing is None  # inconclusive - never confirmed correct, never confirmed wrong


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
    # 4, not 3: the text-free attempt's first "clean" verdict (call 3) is
    # itself double-checked with one independent second opinion (call 4)
    # before being trusted - see _confirm_labels_correct's own docstring.
    assert verify_calls["n"] == 4


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
    """Production risk follow-up: a vision-call exception is never treated
    as "it has text" (that was never the risk here), but it must ALSO never
    be treated as "confirmed genuinely text-free" - `None` is a third,
    explicitly distinct, inconclusive outcome."""
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    found = await svc._verify_image_has_no_text(b"png-bytes")
    assert found is None  # inconclusive - never confirmed clean, never confirmed "has text"


async def test_verify_image_relevance_never_raises_on_a_vision_call_failure(monkeypatch):
    """The third (of three) vision-based QA calls in this class - alongside
    _verify_generated_text/_verify_image_has_no_text, both already proven
    above. Updated (production risk follow-up): `on_topic`/
    `genuinely_educational` still default to True on a vision-call
    exception (a false "confirmed off-topic" claim the image never earned
    would be just as wrong as a false pass) - but `errored=True` now
    explicitly, separately marks this as inconclusive, never a genuine
    pass, so the caller's fail-closed publication gate still catches it."""
    svc = ImageService()

    async def fake_structured(*, schema, **kwargs):
        raise RuntimeError("vision call failed")

    monkeypatch.setattr(svc.ai, "structured", fake_structured)
    result = await svc._verify_image_relevance(b"png-bytes", brief="Explain photosynthesis", course_title="Biology")
    assert result.on_topic is True
    assert result.genuinely_educational is True
    assert result.errored is True


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
    # 3, not 2: the retry's first "clean" verdict (call 2) is itself
    # double-checked with one independent second opinion (call 3) before
    # being trusted - see _confirm_text_free's own docstring.
    assert verify_calls["n"] == 3


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


async def test_a_false_clean_verdict_on_the_first_check_is_caught_by_the_second_opinion():
    """Root-cause fix (production course audit, ThermoDynamics/
    doc_a2f5625a7fd9): a real, confirmed case shipped a diagram-fallback
    illustration with "Precise language & coniJt consistent sign
    conventions" and "undergraduate thermodynamics; mullivtrabile
    calculus" plainly baked in, yet persisted with `text_verified: true`.
    Re-running the exact same vision check against the exact same image
    bytes afterwards correctly detected all the garbled text on every
    fresh call - proving the check itself works, and that the original
    generation's single pass-or-fail call simply got a lucky "clean"
    reading once (LLM vision-judgment variance). This simulates exactly
    that: the first check call reports clean, the second (independent)
    opinion correctly reports the real garbled text - the double-check
    must side with the second, more thorough reading, not the first."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_verify(image):
        calls["n"] += 1
        # First call: a false "clean" reading. Second call: the garbled
        # text is actually detected - matching the real, confirmed case.
        return [] if calls["n"] == 1 else ["Precise language & coniJt consistent sign conventions"]

    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]
    found = await svc._confirm_text_free(b"the-garbled-image-bytes")
    assert found != []
    assert calls["n"] == 2


async def test_confirm_labels_correct_also_catches_a_false_clean_verdict():
    """The `_verify_generated_text` sibling of the test above - same
    false-negative risk, same double-check fix."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_verify(image, expected_labels):
        calls["n"] += 1
        return [] if calls["n"] == 1 else ["Label A"]

    svc._verify_generated_text = fake_verify  # type: ignore[method-assign]
    missing = await svc._confirm_labels_correct(b"the-garbled-image-bytes", ["Label A"])
    assert missing != []
    assert calls["n"] == 2


async def test_confirm_text_free_only_spends_one_extra_call_when_genuinely_clean():
    """The happy path (a genuinely clean image) costs exactly one extra
    verification call, not an unbounded chain - both opinions agree, so
    the double-check stops there."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_verify(image):
        calls["n"] += 1
        return []

    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]
    found = await svc._confirm_text_free(b"a-genuinely-clean-image")
    assert found == []
    assert calls["n"] == 2


async def test_confirm_text_free_skips_the_second_call_when_the_first_already_fails():
    """Asymmetric cost: a first result that already found text is accepted
    immediately - it has already failed, so there is nothing a second
    opinion would usefully change, and no extra call is spent."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_verify(image):
        calls["n"] += 1
        return ["some text"]

    svc._verify_image_has_no_text = fake_verify  # type: ignore[method-assign]
    found = await svc._confirm_text_free(b"an-image-with-text")
    assert found == ["some text"]
    assert calls["n"] == 1


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


async def test_a_generic_pedagogical_section_opener_never_calls_the_image_model(monkeypatch):
    """Root-cause fix (production course audit, ThermoDynamics/
    doc_a2f5625a7fd9): every one of this course's 10 image-generation
    failures was a `section_intro` opener for one of these subject-agnostic
    sections - MAI-Image-2.6 baking garbled pseudo-text into a "strictly no
    text" icon prompt. These now resolve to a deterministic, theme-coloured
    SVG glyph and must never reach the image model (or Wikimedia) at all -
    proven by an AI/search client that raises if ever invoked."""

    async def exploding_image(*, prompt, size=None, phase="image", provider=None):
        raise AssertionError("the image model must never be called for a generic section opener")

    async def exploding_search(query, *, limit, settings, transport=None):
        raise AssertionError("Wikimedia must never be consulted for a generic section opener")

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", exploding_search)
    template = load_template("technical")

    for section_title in (
        "Quiz", "Summary", "Common Mistakes", "Best Practices", "Learning Objectives",
        "Challenge", "Exercise: Interpolation and Dimensional Check",
    ):
        svc = ImageService()
        svc.ai.image = exploding_image  # type: ignore[method-assign]
        block = _image_block(illustration_style="section_intro")
        ok = await svc._generate_illustration(
            course_id="course_generic_icon", block=block, template=template, course_title="Test Course",
            section_title=section_title,
        )

        assert ok is True, section_title
        assert block.content["generated"] is True, section_title
        assert block.content["error"] is None, section_title
        assert block.content["path"].endswith(".svg"), section_title
        assert block.content["image_source"] == "static_icon", section_title
        assert block.content["width"] == 400 and block.content["height"] == 300, section_title
        assert block.content["visual_qa_findings"] == [], section_title


async def test_a_subject_specific_section_opener_still_uses_the_ai_path():
    """The generic-icon short-circuit is narrowly scoped - a section that
    isn't one of the fixed pedagogical names (a course's actual subject
    matter) must keep going through the ordinary AI illustration path
    exactly as before."""
    svc = ImageService()
    calls: list[str] = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls.append(prompt)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block(illustration_style="section_intro")
    template = load_template("technical")
    ok = await svc._generate_illustration(
        course_id="course_subject_specific", block=block, template=template, course_title="Test Course",
        section_title="Core Concepts",
    )
    assert ok is True
    assert calls  # the image model WAS called
    assert block.content.get("image_source") != "static_icon"


async def test_a_content_safety_rejection_retries_once_with_a_generic_safe_prompt_and_succeeds():
    """Real, confirmed bug: Azure's FLUX.2-flex rejected a real illustration
    request with `content_safety_violation` (RAI policy's DallEBlockList_Prompt
    filter) and the block shipped with no image at all for the rest of that
    course's lifetime (`generated: False`), because the old except-block
    treated every exception identically - no retry, no mitigation. This is
    the exact failure this fix addresses: ONE bounded retry with a generic,
    topic-only, explicitly policy-conscious prompt that drops the writer's
    own (most-likely-triggering) free text entirely."""
    svc = ImageService()
    prompts_used: list[str] = []
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        prompts_used.append(prompt)
        if calls["n"] == 1:
            raise AIServiceError(
                'Azure FLUX.2-flex request failed with status 400: '
                '{"error":{"code":"content_safety_violation","message":'
                '"Content violated RAI policy blocking criteria (DallEBlockList_Prompt)."}}'
            )
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(purpose="A graphic depiction of a factory accident")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_safety_retry", block=block, template=template,
        course_title="Industrial Safety 101", chapter_title="Hazard Recognition",
    )

    assert ok is True
    assert calls["n"] == 2
    # The SAME rejected prompt is never resent - the second attempt is a
    # different, generic, safety-conscious one, grounded only in the topic.
    assert prompts_used[0] != prompts_used[1]
    assert "industrial safety 101" in prompts_used[1].lower()
    assert "nothing graphic, violent, political" in prompts_used[1].lower()
    assert block.content.get("generated") is True
    assert block.content.get("error") is None
    # The persisted prompt reflects what was ACTUALLY sent, not the rejected one.
    assert block.content.get("prompt") == prompts_used[1]


async def test_a_content_safety_rejection_that_fails_again_ships_as_failed_not_crashed():
    """The retry is bounded at exactly one attempt - if the generic
    safety-fallback prompt is ALSO rejected, this must still fail safely
    (mark the block ungenerated) rather than raising or looping."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        raise AIServiceError(
            'Azure FLUX.2-flex request failed with status 400: '
            '{"error":{"code":"content_safety_violation","message":"Content violated RAI policy."}}'
        )

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(purpose="Something that keeps getting rejected")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_safety_retry_fails", block=block, template=template, course_title="Test Course",
    )

    assert ok is False
    assert calls["n"] == 2  # the original attempt plus exactly one safety retry, never more
    assert block.content.get("generated") is False
    assert "content safety" in (block.content.get("error") or "").lower() or \
        "rai policy" in (block.content.get("error") or "").lower()


async def test_a_content_safety_rejection_on_a_labelled_textbook_illustration_preserves_and_verifies_its_labels():
    """Real, confirmed gap in the first version of this fix: the safety
    fallback always forced a text-free image, silently losing a textbook
    illustration's essential part-labels (e.g. "Nucleus", "Cell wall") the
    moment the ORIGINAL request happened to trip a content-safety filter -
    an illustration that still needed those labels shipped as a generic,
    unlabelled placeholder instead, with no way to tell the difference.
    The fallback must still ask for (and verify) the exact same labels."""
    svc = ImageService()
    prompts_used: list[str] = []
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        prompts_used.append(prompt)
        if calls["n"] == 1:
            raise AIServiceError(
                'Azure FLUX.2-flex request failed with status 400: '
                '{"error":{"code":"content_safety_violation","message":"Content violated RAI policy."}}'
            )
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        illustration_style="textbook", purpose="A labelled depiction of a dangerous machine failure",
        expected_labels=["Pressure Valve", "Safety Interlock"],
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_safety_retry_labelled", block=block, template=template,
        course_title="Industrial Safety 101",
    )

    # Confirmed-wrong required text from the safety-fallback retry is never
    # published AS a labelled image - the safety-fallback retry is not a
    # special exemption from text verification. Fallback-cascade root-cause
    # fix: this used to then draw a further, deliberately generic/label-free
    # picture and publish it; the image is now withheld instead (no
    # unrelated generic picture in its place).
    assert ok is False
    assert calls["n"] == 2  # original + labelled safety-fallback, never a further generic draw
    # The safety-fallback prompt (attempt 2) still carried the essential
    # labels, not just the generic safety-conscious depiction - proving they
    # were never silently dropped, even though that specific attempt is
    # still correctly rejected on its own merits.
    assert "Pressure Valve" in prompts_used[1] and "Safety Interlock" in prompts_used[1]
    # No image is being published at all, so there is nothing left to make
    # a text claim about - text_verified resets to None, not a stale False.
    assert block.content.get("text_verified") is None
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("error") is None  # a confirmed QA failure, not a technical/provider failure
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)


async def test_a_correct_formula_expression_is_verified_and_shipped_clean():
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(formula_expression="F = m * a")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_formula_correct", block=block, template=template, course_title="Physics",
    )

    assert ok is True
    assert block.content.get("formula_verified") is True
    assert block.content.get("visual_qa_findings") == []


async def test_an_incorrect_formula_expression_is_flagged_but_the_image_still_ships():
    """Mathematical correctness has no safe raster fallback (see formula_qa's
    own docstring on why a false-positive mismatch must never silently
    replace legitimate content) - the image ships as generated, but the
    defect must be visible, not silently accepted as correct."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(formula_expression="F = m / a")  # wrong operator
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_formula_wrong", block=block, template=template, course_title="Physics",
    )

    assert ok is True  # ships anyway - no safe fallback for a formula defect
    assert block.content.get("formula_verified") is False
    findings = block.content.get("visual_qa_findings")
    assert findings and findings[0]["type"] == "formula_mismatch"
    assert findings[0]["resolution"] == "formula_verification_failed"
    assert findings[0]["deterministic"] is True


async def test_formula_verification_never_erases_an_existing_diagram_fallback_finding():
    """Real, confirmed bug this guards against: `visual_qa_findings` is a
    shallow-merged list - persisting this attempt's (empty) formula
    findings on top of a block that ALREADY carries a finding from
    app.services.diagram_service's own fallback (the normal path a
    'diagram' kind block takes into THIS illustration generator) must never
    silently wipe that earlier, unrelated finding out."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        visual_qa_findings=[
            {"type": "off_topic", "detail": "unrelated to the brief",
             "resolution": "fell_back_to_illustration", "deterministic": False}
        ],
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_formula_preserves_existing", block=block, template=template, course_title="Test Course",
    )

    assert ok is True
    findings = block.content.get("visual_qa_findings")
    assert len(findings) == 1
    assert findings[0]["type"] == "off_topic"  # the diagram's own finding survived untouched


async def test_a_stale_formula_finding_is_replaced_not_duplicated_on_a_resolved_re_render():
    """The mirror case: a re-render that FIXES a previously-wrong formula
    must not leave the old 'formula_mismatch' finding stacked alongside
    nothing new - merge_content's shallow-merge contract means this has to
    be handled explicitly, the same reasoning diagram_service.py's own
    identical fix already documents."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(
        formula_expression="F = m * a",  # now correct
        visual_qa_findings=[
            {"type": "formula_mismatch", "detail": "stale from an earlier, wrong attempt",
             "resolution": "formula_verification_failed", "deterministic": True}
        ],
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_formula_resolved", block=block, template=template, course_title="Physics",
    )

    assert ok is True
    assert block.content.get("formula_verified") is True
    assert block.content.get("visual_qa_findings") == []


async def test_a_stale_generic_fallback_finding_is_cleared_on_a_later_clean_success(monkeypatch):
    """A real, confirmed bug: a block that fell back to a generic image
    once kept its 'generic_fallback_used' finding forever, even after a
    LATER regeneration succeeded cleanly with the real, on-topic image -
    misreporting a defect that no longer existed. Mirrors `test_a_stale_
    formula_finding_is_replaced_not_duplicated_on_a_resolved_re_render`
    for the `generic_fallback_published` resolution."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck()  # clean pass this time
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(
        purpose="A labelled diagram of the water cycle",
        visual_qa_findings=[
            {"type": "generic_fallback_used", "detail": "stale from an earlier failed attempt",
             "resolution": "generic_fallback_published", "deterministic": False}
        ],
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_fallback_resolved", block=block, template=template, course_title="Earth Science",
    )

    assert ok is True
    assert block.content.get("visual_qa_findings") == []


async def test_an_off_topic_image_retries_once_and_succeeds_on_a_more_specific_retry(monkeypatch):
    """The gap behind "a visually attractive but irrelevant image must not
    pass merely because an image file exists": the first generation is
    judged off-topic, triggers exactly one regeneration with the brief
    re-emphasised, and the SECOND attempt passes - ships clean, no finding
    persisted."""
    svc = ImageService()
    image_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            if image_calls["n"] == 1:
                return ImageRelevanceCheck(on_topic=False, on_topic_detail="shows an unrelated stock scene")
            return ImageRelevanceCheck(on_topic=True, genuinely_educational=True)
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of the water cycle")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_relevance_retry", block=block, template=template, course_title="Earth Science",
    )

    assert ok is True
    assert image_calls["n"] == 2  # exactly one retry, never unbounded
    assert block.content.get("visual_qa_findings") == []


async def test_a_style_noncompliant_image_gets_a_second_retry_before_falling_back(monkeypatch):
    """A real, confirmed case: `style_compliant` (MAI-Image-2.6 has no
    negative-prompt/style parameter at the API level) failed BOTH the
    original attempt AND its one retry on most of a chapter's textbook
    illustrations, even after `sanitize_expected_labels` fixed their text-
    legibility failures - a single retry wasn't enough to overcome this
    provider's genuine per-attempt style-drift rate. The THIRD attempt
    (second retry) must still be tried before giving up - still bounded,
    never unbounded, but no longer stopping one attempt short of recovery."""
    svc = ImageService()
    image_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            if image_calls["n"] <= 2:
                return ImageRelevanceCheck(
                    style_compliant=False,
                    style_compliant_detail="drifted photorealistic with soft shading",
                )
            return ImageRelevanceCheck()
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of density and viscosity", illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_style_second_retry", block=block, template=template, course_title="Fluid Mechanics",
    )

    assert ok is True
    assert image_calls["n"] == 3  # the original attempt plus BOTH bounded retries, never more
    assert block.content.get("visual_qa_findings") == []


async def test_an_image_that_fails_relevance_after_retry_is_withheld_not_replaced_with_a_generic_picture(
    monkeypatch,
):
    """Fallback-cascade root-cause fix: a confirmed relevance failure (every
    bounded attempt at the real brief judged off-topic/decorative, no
    Wikimedia match either - checked before any of this, at the top of
    `_generate_illustration`) used to get one final, deliberately generic/
    unverified AI picture published in its place. That is exactly the
    "generic decorative placeholder...as a substitute for educational
    content" rule this pipeline must never violate - a reader cannot tell
    an unrelated generic picture apart from a real, verified one. The image
    is now withheld instead (no asset, `omitted_after_qa_failure=True`,
    same compact placeholder a technical failure already gets) - the
    defect is still fully visible via `visual_qa_findings`, just never
    papered over with a picture that doesn't actually depict anything
    real."""
    svc = ImageService()
    draw_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        draw_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(
                on_topic=False, on_topic_detail="depicts an unrelated generic office scene",
                genuinely_educational=False, educational_detail="purely decorative, teaches nothing",
            )
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of the water cycle")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_relevance_fails", block=block, template=template, course_title="Earth Science",
    )

    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("error") is None  # a confirmed QA failure, not a technical/provider failure
    # 1 original + _MAX_RELEVANCE_RETRIES (2) retries = 3 total `_draw`
    # calls, never a further "generic fallback" draw on top of that.
    assert draw_calls["n"] == 3
    findings = block.content.get("visual_qa_findings")
    types = {f["type"] for f in findings}
    # The original image's own relevance findings are KEPT, not replaced -
    # nothing else is shipped in their place, so they are exactly why the
    # image was withheld.
    assert types == {"off_topic_image", "decorative_not_educational", "image_withheld_after_qa_failure"}
    assert all(
        f["resolution"] in ("image_relevance_unverified", "image_withheld_after_qa_failure") for f in findings
    )


async def test_the_full_fallback_cascade_tries_wikimedia_before_withholding(monkeypatch):
    """The exact cascade the fallback system must follow, end to end: (1)
    Wikimedia Commons is tried FIRST, before any AI model call at all; (2)
    when it has no suitable candidate, MAI-Image-2.6 generation is tried,
    with its own bounded verification retries; (3) when every one of THOSE
    also fails its relevance check, the image is withheld - never a further,
    unrelated generic AI picture - with a clear, honest finding explaining
    why. This is the single test that exercises every stage of that cascade
    together, rather than each stage's own dedicated test in isolation."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())
    wikimedia_search_calls = {"n": 0}
    draw_calls = {"n": 0}

    async def no_wikimedia_candidates(query, *, limit, settings, transport=None):
        wikimedia_search_calls["n"] += 1
        return []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        draw_calls["n"] += 1
        return b"png-bytes"

    async def always_off_topic(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(on_topic=False, on_topic_detail="depicts an unrelated scene")
        return schema()

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", no_wikimedia_candidates)
    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc.ai.structured = always_off_topic  # type: ignore[method-assign]

    block = _image_block(purpose="A labelled diagram of the nitrogen cycle")
    ok = await svc._generate_illustration(
        course_id="course_full_cascade", block=block, template=load_template("technical"),
        course_title="Earth Science",
    )

    # (1) Wikimedia was genuinely tried, not skipped.
    assert wikimedia_search_calls["n"] > 0
    # (2) MAI-Image-2.6 was tried, with its bounded retries (1 original + 2
    # relevance retries) - never an unbounded number of attempts.
    assert draw_calls["n"] == 3
    # (3) No image published at all - never a generic, unrelated picture.
    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    withheld = next(f for f in findings if f["type"] == "image_withheld_after_qa_failure")
    # The message is honest and specific about what happened, not generic.
    assert "wikimedia" in withheld["detail"].lower()
    assert "generic" in withheld["detail"].lower()
    assert not any(f["type"] == "generic_fallback_used" for f in findings)


async def test_a_persistently_style_noncompliant_image_still_stops_at_two_retries(monkeypatch):
    """The bounded retry loop must still be BOUNDED - a check that never
    passes gets the original attempt plus exactly `_MAX_RELEVANCE_RETRIES`
    (2) retries, never more, before withholding the image (fallback-cascade
    root-cause fix: no further "generic fallback" draw call on top of that
    - see the sibling withheld-image test for the terminal-state details)."""
    svc = ImageService()
    image_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(
                style_compliant=False, style_compliant_detail="persistently photorealistic",
            )
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram", illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_style_bounded", block=block, template=template, course_title="Fluid Mechanics",
    )

    assert ok is False
    assert block.content.get("omitted_after_qa_failure") is True
    # 1 original + 2 retries that all fail the relevance check = 3 total
    # `_draw` calls, never more - no further "generic fallback" draw on top.
    assert image_calls["n"] == 3


async def test_overlapping_labels_retry_once_and_succeed_on_a_cleaner_retry(monkeypatch):
    """A real, confirmed gap: `_verify_generated_text` only checks that each
    expected label's text appears SOMEWHERE in the image, verbatim - it has
    no notion of where, so a labelled illustration whose callouts collide
    with each other (confirmed on a real generated "rigid-body rotation"
    diagram - "omega (axis)" drawn directly on top of "omega") passed that
    check anyway. `labels_legible` closes that gap via the same bounded
    retry-once contract as on_topic/genuinely_educational."""
    svc = ImageService()
    image_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            if image_calls["n"] == 1:
                return ImageRelevanceCheck(
                    labels_legible=False,
                    labels_legible_detail="the 'omega (axis)' and 'Point P' callouts overlap",
                )
            return ImageRelevanceCheck()
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of rigid-body rotation")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_legibility_retry", block=block, template=template, course_title="Physics",
    )

    assert ok is True
    assert image_calls["n"] == 2  # exactly one retry, never unbounded
    assert block.content.get("visual_qa_findings") == []


async def test_persistently_overlapping_labels_are_withheld_not_shipped_as_a_generic_fallback(monkeypatch):
    """Mirrors `test_an_image_that_fails_relevance_after_retry_is_withheld_
    not_replaced_with_a_generic_picture`, for the `labels_legible`
    dimension: a confirmed, persistent layout defect must never be silently
    shipped (and never silently reported as a different defect) - and,
    per the fallback-cascade root-cause fix, never papered over with an
    unrelated generic picture either. It is withheld, flagged for review."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(
                labels_legible=False,
                labels_legible_detail="every callout overlaps at least one other label",
            )
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of rigid-body rotation")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_legibility_fails", block=block, template=template, course_title="Physics",
    )

    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    types = {f["type"] for f in findings}
    assert types == {"overlapping_or_illegible_labels", "image_withheld_after_qa_failure"}
    assert not any(f["resolution"] == "generic_fallback_published" for f in findings)


async def test_style_noncompliant_textbook_image_retries_once_and_succeeds_on_a_cleaner_retry(monkeypatch):
    """MAI-Image-2.6 (and every other raster provider this pipeline uses)
    has no negative-prompt/style parameter at the API level - the flat 2D
    vector, no-photorealism, no-3D-render style contract
    (TEXTBOOK_ILLUSTRATION_GUIDANCE) lives entirely in prompt text with no
    model-level enforcement. `style_compliant` is the only check that can
    catch the model drifting toward a photorealistic/3D result despite
    that instruction - same bounded retry-once contract as on_topic/
    labels_legible, and only ever asked about for a `textbook`-style
    generation."""
    svc = ImageService()
    image_calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            if image_calls["n"] == 1:
                return ImageRelevanceCheck(
                    style_compliant=False,
                    style_compliant_detail="rendered as a photorealistic 3D scene with studio lighting",
                )
            return ImageRelevanceCheck()
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A simple pulley system", illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_style_retry", block=block, template=template, course_title="Physics",
    )

    assert ok is True
    assert image_calls["n"] == 2  # exactly one retry, never unbounded
    assert block.content.get("visual_qa_findings") == []


async def test_persistently_style_noncompliant_textbook_image_is_withheld_not_shipped_generic(monkeypatch):
    """Fallback-cascade root-cause fix: a confirmed, persistent style-
    compliance failure is withheld, not papered over with an unrelated
    generic picture."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(
                style_compliant=False, style_compliant_detail="still a 3D render after retry",
            )
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A simple pulley system", illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_style_fails", block=block, template=template, course_title="Physics",
    )

    assert ok is False
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    types = {f["type"] for f in findings}
    assert types == {"textbook_style_not_followed", "image_withheld_after_qa_failure"}


async def test_style_compliant_is_never_checked_for_a_non_textbook_illustration(monkeypatch):
    """A plain/default-style illustration never had a style CONTRACT to
    begin with - `style_compliant` must stay unasked (and so never fail
    the publication gate) for anything other than `illustration_style:
    "textbook"`, even if a model answered it false anyway."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(style_compliant=False, style_compliant_detail="irrelevant here")
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A scenic photo-style illustration")  # no illustration_style set
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_style_irrelevant", block=block, template=template, course_title="Geography",
    )

    assert ok is True
    assert block.content.get("visual_qa_findings") == []  # never flagged - the check never applied


async def test_a_qa_failure_is_still_omitted_when_the_relevance_retry_also_fails_technically(monkeypatch):
    """The relevance-recheck retry is one more bounded attempt, not an
    unlimited guarantee - if the provider itself is genuinely unreachable
    on that retry, the loop still exits cleanly on the ORIGINAL confirmed
    defect (fallback-cascade root-cause fix: there is no further "generic
    last-resort" draw call to even attempt any more) and that defect's own
    finding must survive into the final omission."""
    svc = ImageService()
    calls = {"n": 0}

    async def always_failing_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return b"png-bytes"
        raise RuntimeError("provider unreachable for the fallback attempt too")

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(on_topic=False, on_topic_detail="unrelated scene")
        return schema()

    svc.ai.image = always_failing_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(purpose="A labelled diagram of the water cycle")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_fallback_also_fails", block=block, template=template, course_title="Earth Science",
    )

    assert ok is False
    # original attempt, then the relevance-recheck retry (raises instead of
    # returning) - the retry loop catches that and exits on the original
    # confirmed defect; no further draw call is ever attempted.
    assert calls["n"] == 2
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("error") is None  # a confirmed QA failure, not a provider error message
    findings = block.content.get("visual_qa_findings")
    # The ORIGINAL defect is what's recorded, plus the new withheld-image
    # finding - never a "generic fallback" finding, since no such attempt
    # is ever made any more.
    assert any(f["type"] == "off_topic_image" for f in findings)
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)
    assert not any(f["type"] == "generic_fallback_used" for f in findings)


async def test_the_content_safety_fallback_skips_the_relevance_check():
    """The emergency safety-fallback image is deliberately generic/abstract
    by design (see `_safety_fallback_prompt`'s own docstring) - checking
    "is this specifically on-topic" against a picture built NOT to be
    specific would always fail and waste a retry it can never pass."""
    svc = ImageService()
    calls = {"image": 0, "structured": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["image"] += 1
        if calls["image"] == 1:
            raise AIServiceError(
                'Azure FLUX.2-flex request failed with status 400: '
                '{"error":{"code":"content_safety_violation","message":"Content violated RAI policy."}}'
            )
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            calls["structured"] += 1
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc.ai.structured = fake_structured  # type: ignore[method-assign]

    block = _image_block(purpose="A depiction of a dangerous industrial failure")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_safety_skips_relevance", block=block, template=template, course_title="Safety",
    )

    assert ok is True
    assert calls["image"] == 2  # the original attempt plus the safety retry
    assert calls["structured"] == 0  # relevance check never spent on the deliberately-generic fallback


async def test_a_failed_diagram_cannot_bypass_relevance_qa_by_falling_back_to_a_raster_image(monkeypatch):
    """Release-readiness audit, item 4 (fallback integrity) - updated for the
    fallback-cascade root-cause fix: an off-topic-ONLY diagram content-QA
    failure no longer falls back to a raster illustration at all (see
    diagram_service.py's own docstring) - a real, legible, correctly-spelled
    diagram ships flagged for review instead. The two-stage fallback this
    test originally exercised (diagram fails -> falls back to illustration
    -> illustration ALSO fails relevance) still exists for a GARBLED-text
    diagram failure specifically (text itself broken, never shippable
    regardless of topic coverage) - exercised here instead. That fallback
    illustration must NOT silently ship unchecked just because it changed
    code paths - it goes through the exact same `_verify_image_relevance`
    gate as any other illustration, proven here by making BOTH checks fail
    and confirming the final result still carries a real, visible finding
    from EACH stage. Per the fallback-cascade root-cause fix, the illustration
    stage's own confirmed relevance failure, with no Wikimedia match either,
    now WITHHOLDS the image (no generic, unrelated AI picture published in
    its place) rather than shipping a decorative placeholder."""
    svc = ImageService()
    image_calls = {"n": 0}

    def _garbled_diagram_spec() -> DiagramSpec:
        return DiagramSpec(
            kind="flow_chart",
            nodes=[DiagramNode(id="a", label="Step 1"), DiagramNode(id="b", label="Step 2")],
            edges=[DiagramEdge(source="a", target="b")],
        )

    async def fake_structured(*, schema, **kwargs):
        if schema.__name__ == "DiagramSpec":
            return _garbled_diagram_spec()
        if schema.__name__ == "ContentCritique":
            # diagram_content_qa: labels confirmed misspelled/garbled, even
            # after its own one bounded retry inside DiagramService.
            # generate_for_block - the one content-QA failure mode that
            # still falls back to the raster illustration path.
            return schema(
                on_topic=True, specific_enough=True, missing_concepts=[],
                text_legible=False, text_legible_detail="'Stpe 1' is garbled",
            )
        if schema is ImageRelevanceCheck:
            # The RASTER FALLBACK's own relevance check also fails, both
            # the first attempt and its own one bounded retry.
            return ImageRelevanceCheck(on_topic=False, on_topic_detail="still does not depict the brief")
        return schema()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        image_calls["n"] += 1
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(
        kind="diagram", diagram_kind="flow_chart",
        purpose="Explain the water cycle", prompt="Evaporation, condensation, precipitation, collection",
    )
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_fallback_integrity", block=block, template=template, course_title="Earth Science",
    )

    # The raster fallback's OWN relevance check also fails, confirmed, after
    # its own bounded retry, and there is no Wikimedia match either - the
    # image is withheld rather than replaced with an unrelated generic
    # picture, so the page is left with no image, honestly reported.
    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("error") is None  # a confirmed QA failure, not a technical/provider failure
    findings = block.content.get("visual_qa_findings")
    assert findings, "the diagram stage's own finding must still be visible, not silently dropped"
    # Both stages' findings coexist, by design - image_service.py's own
    # `_stale_resolutions` filter only clears a STALE finding from an
    # earlier RE-RENDER of the same block (formula/relevance results that
    # would become wrong once the problem is fixed), never diagram_
    # service.py's own "fell_back_to_illustration" entry, which describes a
    # DIFFERENT, earlier stage (the diagram attempt) - genuinely accurate,
    # still-true context, not something a later illustration-stage finding
    # should silently erase. The illustration stage's OWN relevance finding
    # is KEPT (not replaced) now that nothing is shipped in its place - it
    # is exactly why the image was withheld.
    assert any(f["resolution"] == "fell_back_to_illustration" for f in findings)
    assert any(f["resolution"] == "image_withheld_after_qa_failure" for f in findings)
    assert any(f["type"] == "off_topic_image" for f in findings)
    assert not any(f["resolution"] == "generic_fallback_published" for f in findings)


async def test_the_real_induced_current_garble_is_caught_through_the_full_pipeline_today(monkeypatch):
    """Release-readiness audit, item 3: the EXACT real, confirmed artifact
    this session found on disk (a genuine Azure-generated textbook
    illustration whose caption rendered as "Inducd crrrent creates
    opposing field (Lenz)" instead of "Induced current creates opposing
    field (Lenz)" - the same real case `test_verify_generated_text_reports_
    a_simulated_garbled_read` above already proves `_verify_generated_text`
    catches in isolation). This test proves the SAME real defect is caught
    end-to-end through the actual production call path
    (`_generate_illustration` -> `_generate_with_text_check`), including
    the worst case where even the one bounded retry still comes back
    garbled (matching what was actually observed in the real asset - both
    a garbled and a corrected caption baked into the same image, meaning
    the model never fully self-corrected even once prompted with the exact
    mistake)."""
    svc = ImageService()
    real_garbled = "Inducd crrrent creates opposing field (Lenz)"
    real_correct = "Induced current creates opposing field (Lenz)"

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def fake_structured(*, schema, **kwargs):
        if schema is ImageTextCheck:
            # Every attempt (including the retry) still renders the same
            # real garbled text - the documented worst case this test
            # exists to prove is still handled honestly, not silently
            # accepted.
            return ImageTextCheck(detected_text=[real_garbled, "Lenz's law"])
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", fake_structured)

    block = _image_block(
        illustration_style="textbook",
        purpose="A bar magnet moving toward a coil, inducing a current read on a meter",
        expected_labels=[real_correct, "Lenz's law"],
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_real_induced_current_garble", block=block, template=template,
        course_title="Electromagnetic Induction",
    )

    # Bounded retries are exhausted (not infinite), and the confirmed-wrong
    # labelled attempt is never published. Fallback-cascade root-cause fix:
    # this used to then draw a further, deliberately generic/label-free
    # fallback and ship it instead - the image is now withheld. `text_
    # verified` resets to None (not False): no image is being published at
    # all, so there is nothing left to claim "unverified" about.
    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    assert block.content.get("text_verified") is None

    # And the persisted state is exactly what app.services.quality_framework
    # reads to build a real, human-visible finding - confirmed by feeding
    # this exact block through the same function the quality report uses.
    from app.services.quality_framework import _visual_findings_for_block

    findings = _visual_findings_for_block(f"image:{block.id}", block.content)
    # The old "expected labels unverified" blocker no longer applies - no
    # image was shipped to make that claim about.
    assert not any(f.reason == "image_text_unverified" for f in findings)
    withheld_findings = [f for f in findings if f.reason == "visual_qa_image_withheld_after_qa_failure"]
    assert len(withheld_findings) == 1
    # No image exists at all now - the same "blocker" severity a technical
    # generation failure already gets (see quality_framework.py), not the
    # old fallback's "minor" (which assumed a picture, however generic,
    # still shipped). verification_status is "independently_verified", not
    # "unverified" (production-readiness audit fix) - the FACT that no
    # image exists is directly checkable, not an AI judgment call, so this
    # "blocker" finding is never silently excluded from `QualityReport.
    # approval_blocking_findings`' `verification_status != "unverified"`
    # gate the way an unverified blocker would be.
    assert withheld_findings[0].severity == "blocker"
    assert withheld_findings[0].verification_status == "independently_verified"


async def test_an_ordinary_generation_failure_is_not_mistaken_for_a_content_safety_rejection():
    """Regression guard: an everyday failure (timeout, 500, bad config) must
    NOT trigger the safety-fallback retry - only a confirmed content-policy
    rejection should ever spend that extra call. Asserts exactly one attempt
    is made, matching the pre-existing behaviour for ordinary failures."""
    svc = ImageService()
    calls = {"n": 0}

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        calls["n"] += 1
        raise AIServiceError("Azure FLUX.2-flex request timed out after 4 attempt(s)")

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block()
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_ordinary_failure", block=block, template=template, course_title="Test Course",
    )

    assert ok is False
    assert calls["n"] == 1  # no safety-retry spent on an unrelated failure
    assert block.content.get("generated") is False
    assert "timed out" in (block.content.get("error") or "").lower()


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
    # This test is about canvas sizing, not text correctness - the fail-
    # closed publication gate (production risk follow-up) would otherwise
    # omit this image, since the default mock vision check reports no text
    # detected at all against any `expected_labels`. Stub a clean pass so
    # `ok`/`sizes_used` reflect the sizing behaviour under test.
    svc._verify_generated_text = _no_missing_labels  # type: ignore[method-assign]

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
    # Same reasoning as the wider-canvas test above - stub a clean text pass
    # so the fail-closed publication gate doesn't omit this image over the
    # default mock's "nothing detected" vision response.
    svc._verify_generated_text = _no_missing_labels  # type: ignore[method-assign]

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


async def test_a_persistent_vision_check_exception_withholds_rather_than_shipping_unverified(monkeypatch):
    """Production risk follow-up, Phase 6: a vision-API exception on EVERY
    bounded attempt (not just one that happens to find wrong text) must
    never be silently treated as a pass - proven end-to-end through the real
    `_generate_illustration` call path, not just the low-level
    `_verify_generated_text` unit tests. The confirmed-unverified labelled
    attempt is never published; fallback-cascade root-cause fix: the image
    is withheld rather than shipping a generic, label-free fallback in its
    place."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def always_raising_structured(*, schema, **kwargs):
        raise RuntimeError("vision service unavailable")

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", always_raising_structured)

    block = _image_block(illustration_style="textbook", expected_labels=["Nucleus", "Cell wall"])
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_vision_check_down", block=block, template=template, course_title="Biology",
    )

    assert ok is False
    assert block.content.get("generated") is False
    assert block.content.get("path") is None
    # A genuine operational failure (the provider call itself) would still
    # set `error` - the vision CHECK being unavailable is a QA-step problem,
    # not a generation failure, so `error` stays None throughout.
    assert block.content.get("error") is None
    assert block.content.get("text_verified") is None
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "image_relevance_check_error" for f in findings)
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)


async def test_omitted_after_qa_failure_is_set_only_for_a_qa_omission_not_a_technical_failure(monkeypatch):
    """Layout-defect follow-up: `omitted_after_qa_failure` is the narrow,
    explicit signal app.course.document.layout.image_generation_failed uses
    to give a QA-rejected image the compact placeholder instead of a full
    image-sized blank box (a real, confirmed defect: before this field
    existed, a QA omission measured ~382pt instead of ~36-44pt). Must be
    True ONLY for a confirmed QA omission - never for a genuine provider
    failure (which already gets the compact placeholder via `error` being
    set, a separate, pre-existing mechanism this field must not duplicate
    or interfere with)."""
    svc = ImageService()

    async def failing_image(*, prompt, size=None, phase="image", provider=None):
        raise RuntimeError("provider is down")

    svc.ai.image = failing_image  # type: ignore[method-assign]

    block = _image_block(purpose="Something unrelated to any provider policy")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_technical_failure_not_qa_omission", block=block, template=template,
        course_title="Test Course",
    )

    assert ok is False
    assert block.content.get("error") == "provider is down"
    assert block.content.get("omitted_after_qa_failure") is False


async def test_omitted_after_qa_failure_is_cleared_by_a_later_successful_regeneration(monkeypatch):
    """The mirror case: a block that failed QA on one attempt (flag set
    True, no path) and is later regenerated successfully must not carry the
    stale flag forward - that would wrongly shrink a now-VALID image's
    reserved layout space. `merge_content`'s shallow-merge contract means
    this has to be set explicitly on the success path, the same reasoning
    already established for `visual_qa_findings`'s own stale-entry
    filtering."""
    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def always_clean(*, schema, **kwargs):
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    svc.ai.structured = always_clean  # type: ignore[method-assign]

    # Simulate a block left over from an earlier, failed QA omission.
    block = _image_block(
        purpose="A clean, on-topic illustration", omitted_after_qa_failure=True, generated=False, path=None,
    )
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_regeneration_clears_stale_flag", block=block, template=template,
        course_title="Test Course",
    )

    assert ok is True
    assert block.content.get("path", "").endswith(".png")
    assert block.content.get("omitted_after_qa_failure") is False


async def test_a_qa_failure_does_not_break_generate_missing_or_course_text(monkeypatch):
    """Phase 7/8: course text must survive a visual's QA failure (and,
    fallback-cascade root-cause fix, the image being withheld rather than
    replaced with a generic fallback), and it must not crash the broader
    `generate_missing` batch path (real production entry point, not just
    `_generate_illustration` called directly)."""
    from app.schemas.document import CourseDocument, Page

    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def always_off_topic(*, schema, **kwargs):
        if schema is ImageRelevanceCheck:
            return ImageRelevanceCheck(on_topic=False, on_topic_detail="unrelated scene")
        return schema()

    svc.ai.image = fake_image  # type: ignore[method-assign]
    monkeypatch.setattr(svc.ai, "structured", always_off_topic)

    paragraph = Block(type=BlockType.PARAGRAPH, content={"text": "Mitochondria are the powerhouse of the cell."})
    image_block = _image_block(purpose="A labelled diagram of a mitochondrion")
    document = CourseDocument(
        document_id="d", course_id="course_omission_preserves_text", course_title="Biology",
        template_id="technical", pages=[Page(blocks=[paragraph, image_block])],
    )
    template = load_template("technical")

    count = await svc.generate_missing(document=document, template=template)

    assert count == 0  # withheld, not a successful generation - and the batch still completes cleanly
    assert image_block.content.get("generated") is False
    assert image_block.content.get("path") is None
    assert image_block.content.get("omitted_after_qa_failure") is True
    # Course text is completely unaffected by the adjacent image's omission.
    assert paragraph.content["text"] == "Mitochondria are the powerhouse of the cell."


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
# Priority-based image-generation prompt budget (see this module's own
# "image-generation prompt budget" section) - replaces a flat
# join-then-`[:MAX_PROMPT_CHARS]`-slice that could silently cut the tail off
# the fixed style guidance, CRITICAL FRAMING RULE included, with no signal
# anything was lost. Covers both the unit-level building blocks
# (`_shorten_to_budget`, `_assemble_prompt_with_priority`,
# `_validate_prompt_assembly`) and `build_prompt` itself under the worst-
# case inputs a real course can produce.
# ---------------------------------------------------------------------------


def test_shorten_to_budget_leaves_short_text_untouched():
    assert _shorten_to_budget("short text", 100) == "short text"


def test_shorten_to_budget_never_cuts_mid_word():
    text = "The quick brown fox jumps over the lazy dog. " * 50
    for limit in (10, 37, 60, 120, 400):
        shortened = _shorten_to_budget(text, limit)
        assert len(shortened) <= limit
        core = shortened.replace(" [...shortened for length]", "")
        # Every word in what survives is a complete word from the source -
        # a bare slice would routinely leave a fragment like "jum" or "laz".
        for word in core.split():
            assert word in text.split() or word in (".", "")


def test_shorten_to_budget_prefers_a_sentence_boundary():
    text = "First sentence is here. Second sentence follows. Third one too."
    shortened = _shorten_to_budget(text, 60)
    assert shortened.startswith("First sentence is here.")
    assert "Second" not in shortened


def test_shorten_to_budget_always_marks_that_something_was_cut():
    text = "word " * 200
    shortened = _shorten_to_budget(text, 50)
    assert "[...shortened for length]" in shortened
    assert len(shortened) <= 50


def test_shorten_to_budget_handles_a_budget_too_small_for_the_marker():
    # The marker itself is longer than the budget - must not crash or
    # return something longer than requested.
    assert len(_shorten_to_budget("anything at all", 5)) <= 5


def test_assemble_prompt_priority_1_always_included_in_full():
    """The one hard invariant: however large Priority 2/3 content is,
    Priority 1 is never shortened or dropped."""
    p1_text = "CRITICAL RULE: " + ("x" * 500)
    segments = [
        _PromptSegment("style", p1_text, priority=1),
        _PromptSegment("labels", "y" * 2000, priority=2),
        _PromptSegment("context", "z" * 2000, priority=3),
        _PromptSegment("writer_prompt", "w" * 50_000, priority=3),
    ]
    result = _assemble_prompt_with_priority(segments, budget=1000)
    assert p1_text in result.prompt
    assert result.mandatory_content_intact is True
    assert result.total_chars <= max(1000, len(p1_text) + 2)  # P1 alone may legitimately exceed a tiny budget


def test_assemble_prompt_priority_3_is_the_only_tier_shortened():
    p1_text = "fixed style rules"
    p2_text = "expected labels: A, B, C"
    p3_long = "writer prompt " * 2000
    segments = [
        _PromptSegment("style", p1_text, priority=1),
        _PromptSegment("labels", p2_text, priority=2),
        _PromptSegment("writer_prompt", p3_long, priority=3),
    ]
    result = _assemble_prompt_with_priority(segments, budget=500)
    assert p1_text in result.prompt
    assert p2_text in result.prompt
    assert "writer_prompt" in result.segments_shortened
    assert result.total_chars <= 500


def test_assemble_prompt_priority_2_degrades_by_dropping_whole_segments():
    """Under real pressure, Priority 2 is shortened by dropping whole
    segments from the end of the list - never a mid-label cut."""
    p1_text = "x" * 50
    segments = [
        _PromptSegment("style", p1_text, priority=1),
        _PromptSegment("label_1", "LABEL_ONE", priority=2),
        _PromptSegment("label_2", "LABEL_TWO", priority=2),
        _PromptSegment("label_3", "LABEL_THREE", priority=2),
    ]
    result = _assemble_prompt_with_priority(segments, budget=70)
    assert p1_text in result.prompt
    # Whatever Priority 2 content survived is a COMPLETE segment, never a
    # fragment like "LABEL_TH".
    for name in ("label_1", "label_2", "label_3"):
        if name not in result.segments_dropped:
            seg_text = {"label_1": "LABEL_ONE", "label_2": "LABEL_TWO", "label_3": "LABEL_THREE"}[name]
            assert seg_text in result.prompt


def test_assemble_prompt_empty_segments_are_dropped_cleanly():
    segments = [
        _PromptSegment("style", "the style rules", priority=1),
        _PromptSegment("labels", "", priority=2),
        _PromptSegment("context", "", priority=3),
    ]
    result = _assemble_prompt_with_priority(segments)
    assert result.prompt == "the style rules"
    assert result.segments_shortened == ()
    assert result.segments_dropped == ()


def test_assemble_prompt_mandatory_content_intact_flag():
    """The pathological, structurally-near-impossible case: Priority 1
    content alone already exceeds the budget. Must still be returned in
    full (never partially), with the flag set so the caller can see it."""
    huge_p1 = "CRITICAL FRAMING RULE: " + ("x" * 10_000)
    segments = [_PromptSegment("style", huge_p1, priority=1)]
    result = _assemble_prompt_with_priority(segments, budget=100)
    assert result.prompt == huge_p1  # never partially dropped
    assert result.mandatory_content_intact is False


def test_validate_prompt_assembly_detects_a_missing_marker():
    result = PromptAssemblyResult(
        prompt="some prompt with no critical marker",
        total_chars=10, budget=100,
        segments_shortened=(), segments_dropped=(), mandatory_content_intact=True,
    )
    missing = _validate_prompt_assembly(result, style="textbook")
    assert missing == ["CRITICAL FRAMING RULE"]


def test_validate_prompt_assembly_passes_when_the_marker_is_present():
    result = PromptAssemblyResult(
        prompt="... CRITICAL FRAMING RULE: every element must ...",
        total_chars=10, budget=100,
        segments_shortened=(), segments_dropped=(), mandatory_content_intact=True,
    )
    assert _validate_prompt_assembly(result, style="textbook") == []


def test_provider_limit_is_verified_and_the_budget_stays_safely_inside_it():
    """MAI_IMAGE_MAX_PROMPT_TOKENS is Microsoft's own documented limit for
    the MAI image generations API `prompt` field (see this module's own
    citation) - not a guess. PROMPT_CHAR_BUDGET must stay comfortably
    inside the character-equivalent ceiling that implies, even under the
    module's own deliberately pessimistic chars-per-token ratio."""
    assert MAI_IMAGE_MAX_PROMPT_TOKENS == 32_000
    theoretical_char_ceiling = MAI_IMAGE_MAX_PROMPT_TOKENS * _CONSERVATIVE_CHARS_PER_TOKEN
    assert PROMPT_CHAR_BUDGET < theoretical_char_ceiling
    # Comfortably, not just technically - real headroom, not a near-miss.
    assert PROMPT_CHAR_BUDGET <= theoretical_char_ceiling / 4


# --- build_prompt under worst-case real-world inputs ------------------------

_MAX_TITLE = "T" * 500  # far past _CONTEXT_FIELD_CHAR_CAP - exercises its own existing per-field cap
_VERY_LONG_WRITER_PROMPT = (
    "A richly detailed scene showing every component of the apparatus in sequence. " * 400
)  # ~32,000 characters - deliberately larger than the entire old 4500-char budget on its own
_MANY_LABELS = [f"Label_{i}" for i in range(80)]


def test_build_prompt_normal_length_titles_no_shortening_needed():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook", expected_labels=["n1", "n2", "theta_i"])
    prompt = ImageService.build_prompt(
        block, template, "Ray Optics",
        chapter_title="Reflection and Refraction", section_title="Core Concepts",
        key_concept="Derive Snell's law from Fermat's principle",
    )
    assert "CRITICAL FRAMING RULE" in prompt
    assert "Ray Optics" in prompt
    assert "Reflection and Refraction" in prompt


def test_build_prompt_maximum_length_titles_critical_rule_survives():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook", expected_labels=["n1", "n2"])
    prompt = ImageService.build_prompt(
        block, template, _MAX_TITLE,
        chapter_title=_MAX_TITLE, section_title=_MAX_TITLE, key_concept=_MAX_TITLE,
    )
    assert "CRITICAL FRAMING RULE" in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt  # fully intact, not just the marker


def test_build_prompt_very_long_writer_prompt_critical_rule_survives():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook", prompt=_VERY_LONG_WRITER_PROMPT)
    prompt = ImageService.build_prompt(block, template, "Ray Optics")
    assert "CRITICAL FRAMING RULE" in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt
    assert len(prompt) <= PROMPT_CHAR_BUDGET


def test_build_prompt_many_expected_labels_critical_rule_survives():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook", expected_labels=_MANY_LABELS)
    prompt = ImageService.build_prompt(block, template, "Ray Optics")
    assert "CRITICAL FRAMING RULE" in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt


def test_build_prompt_combined_worst_case_still_protects_mandatory_content():
    """Every stressor at once: maximum-length titles, a ~32,000-character
    writer prompt, and 80 expected labels - combined, this is roughly 7x
    larger than the entire old 4500-char budget on its own. The CRITICAL
    FRAMING RULE, and the complete style guidance it belongs to, must
    still survive in full."""
    template = load_template("technical")
    block = _image_block(
        illustration_style="textbook",
        prompt=_VERY_LONG_WRITER_PROMPT,
        expected_labels=_MANY_LABELS,
    )
    prompt = ImageService.build_prompt(
        block, template, _MAX_TITLE,
        chapter_title=_MAX_TITLE, section_title=_MAX_TITLE, key_concept=_MAX_TITLE,
    )
    assert "CRITICAL FRAMING RULE" in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt
    assert len(prompt) <= PROMPT_CHAR_BUDGET


def test_build_prompt_combined_worst_case_for_every_illustration_style():
    """The same stress test, for the two other styles - each has its own
    mandatory marker (STRICT NO-TEXT RULE for both) that must survive the
    same combined pressure."""
    template = load_template("technical")
    for style, guidance in (
        ("section_intro", SECTION_INTRO_ILLUSTRATION_GUIDANCE),
        ("", DEFAULT_ILLUSTRATION_GUIDANCE),
    ):
        block = _image_block(illustration_style=style, prompt=_VERY_LONG_WRITER_PROMPT)
        prompt = ImageService.build_prompt(
            block, template, _MAX_TITLE,
            chapter_title=_MAX_TITLE, section_title=_MAX_TITLE, key_concept=_MAX_TITLE,
        )
        assert "STRICT NO-TEXT RULE" in prompt
        assert guidance in prompt
        assert len(prompt) <= PROMPT_CHAR_BUDGET


def test_build_prompt_never_exceeds_the_provider_verified_budget():
    """Scenario 6 from this fix's own brief: a prompt whose raw combined
    inputs are FAR larger than the budget must still come out at or under
    it - the structural guarantee `_assemble_prompt_with_priority` exists
    to provide, not just a hope that inputs stay small."""
    template = load_template("technical")
    absurd_prompt = "x" * 200_000
    block = _image_block(illustration_style="textbook", prompt=absurd_prompt, expected_labels=_MANY_LABELS * 5)
    prompt = ImageService.build_prompt(
        block, template, _MAX_TITLE,
        chapter_title=_MAX_TITLE, section_title=_MAX_TITLE, key_concept=_MAX_TITLE,
    )
    assert len(prompt) <= PROMPT_CHAR_BUDGET
    # And that budget itself stays safely inside the real, verified
    # provider limit - see test_provider_limit_is_verified_... above.
    assert PROMPT_CHAR_BUDGET <= MAI_IMAGE_MAX_PROMPT_TOKENS * _CONSERVATIVE_CHARS_PER_TOKEN


def test_build_prompt_logs_an_error_if_a_mandatory_marker_ever_goes_missing(monkeypatch):
    """Defence in depth: if a future edit ever mis-tags Priority 1 content
    (or deletes the marker text itself), `build_prompt` must not silently
    ship the broken prompt - it logs loudly instead (this module's own
    "never fail the whole course" convention applied to prompt validation,
    not swallowed silently)."""
    import app.services.image_service as image_service_module

    errors: list[str] = []
    monkeypatch.setattr(
        image_service_module.log, "error", lambda msg, *args, **kwargs: errors.append(msg % args)
    )
    monkeypatch.setattr(
        image_service_module, "TEXTBOOK_ILLUSTRATION_GUIDANCE",
        "a style guidance block with no framing marker at all",
    )
    template = load_template("technical")
    block = _image_block(illustration_style="textbook")
    ImageService.build_prompt(block, template, "Course Title")
    assert any("missing markers" in message for message in errors)


class _FakeAI:
    def __init__(self, *, is_mock: bool) -> None:
        self.is_mock = is_mock

    async def image(self, *, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    async def structured(self, *, schema, **kwargs):
        # These tests exercise provider selection / mock-vs-real recording,
        # not vision-based QA - a clean schema default (no detected text,
        # on_topic/genuinely_educational both True) lets the no-text/
        # relevance checks every illustration now goes through pass
        # uneventfully, rather than raising AttributeError (this stub never
        # had a real `.structured`) and being caught as an inconclusive
        # vision-check failure by the fail-closed publication gate
        # (production risk follow-up) - which would otherwise omit the
        # image here for a reason unrelated to what each test actually
        # checks.
        return schema()


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


async def test_textbook_illustration_defaults_to_azure_mai_not_gpt_image_1():
    """The migration this test guards: `textbook_image_provider`'s own
    DEFAULT (no override at all) must resolve to "azure_mai" - Azure
    MAI-Image-2.6 - not fall through to the OpenAI gpt-image-1 path, which
    is exactly what this category used to default to before the migration.
    Unrelated illustrations are unaffected (see the next test) - only this
    category's default changed."""
    settings = get_settings().model_copy(update={"image_provider": "openai"})
    assert settings.textbook_image_provider == "azure_mai"  # the new default
    svc = ImageService(ai=_FakeAI(is_mock=False), settings=settings)
    seen_providers = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        seen_providers.append(provider)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    block = _image_block(illustration_style="textbook")
    template = load_template("technical")

    ok = await svc._generate_illustration(
        course_id="course_default_textbook_provider", block=block, template=template, course_title="Test Course",
    )
    assert ok is True
    assert seen_providers == ["azure_mai"]


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


async def test_section_intro_and_default_style_illustrations_default_to_azure_flux_not_gpt_image_1():
    """gpt-image-1 is no longer used anywhere by default - the OpenAI
    account's image-generation credit ran out. `image_provider` (what every
    section_intro/default-style illustration resolves to, since they never
    set the textbook override) now defaults to "azure" (FLUX.2-flex), not
    "openai". These categories don't need MAI-Image-2.6's text-rendering
    strength (section_intro enforces a strict no-text rule), so the cheaper
    FLUX.2-flex provider is the right default for them specifically."""
    settings = get_settings()
    assert settings.image_provider == "azure"
    svc = ImageService(ai=_FakeAI(is_mock=False), settings=settings)
    seen_providers = []

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        seen_providers.append(provider)
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]
    template = load_template("technical")

    for style in ("section_intro", ""):
        block = _image_block(illustration_style=style) if style else _image_block()
        ok = await svc._generate_illustration(
            course_id="course_unaffected_styles", block=block, template=template, course_title="Test Course",
        )
        assert ok is True
    # None == "use settings.image_provider" (azure) - never forced to
    # azure_mai (textbook-only) or openai (no longer the default anywhere).
    assert seen_providers == [None, None]


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


# ---------------------------------------------------------------------------
# Pipeline reliability follow-up: `missing_image_blocks` (an accurate total
# before the batch starts) and `generate_missing`'s `on_progress` callback
# (real per-image progress, persisted as each image finishes - see
# CourseService._finalise's own `_on_image_done`), plus the `_qa_job_id`
# marker VisualReviewService.review_document uses to skip a redundant
# re-check of an image just validated in this same run.
# ---------------------------------------------------------------------------


def test_missing_image_blocks_counts_without_generating_anything():
    from app.schemas.document import CourseDocument, Page

    missing = _image_block(purpose="needs an image")
    already_has_one = _image_block(purpose="already illustrated", path="assets/existing.png")
    document = CourseDocument(
        document_id="d", course_id="course_missing_count", course_title="Biology",
        template_id="technical", pages=[Page(blocks=[missing, already_has_one])],
    )

    targets = ImageService.missing_image_blocks(document)
    assert [b.id for b in targets] == [missing.id]

    forced = ImageService.missing_image_blocks(document, force=True)
    assert {b.id for b in forced} == {missing.id, already_has_one.id}


async def test_generate_missing_on_progress_fires_once_per_image_as_it_finishes():
    """A crash partway through a batch used to lose every image that had
    already succeeded, because nothing fired until the WHOLE batch
    finished. `on_progress` is the hook the caller uses to persist each
    image immediately instead."""
    from app.schemas.document import CourseDocument, Page

    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block_a = _image_block(purpose="A diagram of photosynthesis")
    block_b = _image_block(purpose="A diagram of respiration")
    document = CourseDocument(
        document_id="d", course_id="course_progress", course_title="Biology",
        template_id="technical", pages=[Page(blocks=[block_a, block_b])],
    )
    template = load_template("technical")

    seen: list[tuple[str, bool]] = []

    async def on_progress(block, ok):
        seen.append((block.id, ok))

    count = await svc.generate_missing(
        document=document, template=template, on_progress=on_progress
    )

    assert count == 2
    assert {block_id for block_id, _ in seen} == {block_a.id, block_b.id}
    assert all(ok for _, ok in seen)


async def test_generate_missing_tags_successful_blocks_with_the_given_job_id():
    from app.schemas.document import CourseDocument, Page

    svc = ImageService()

    async def fake_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    svc.ai.image = fake_image  # type: ignore[method-assign]

    block = _image_block(purpose="A diagram of photosynthesis")
    document = CourseDocument(
        document_id="d", course_id="course_job_tag", course_title="Biology",
        template_id="technical", pages=[Page(blocks=[block])],
    )
    template = load_template("technical")

    await svc.generate_missing(document=document, template=template, job_id="job-123")

    assert block.content.get("_qa_job_id") == "job-123"


# ---------------------------------------------------------------------------
# Wikimedia Commons - first priority, before any AI model (see the approved
# plan at C:\Users\Dell\.claude\plans\linear-wobbling-puppy.md and
# ImageService._try_wikimedia_image's own docstring). Disabled by default for
# the whole suite (conftest.py sets ENABLE_WIKIMEDIA_IMAGES=false, since it's
# a real HTTP call MOCK_OPENAI never covers) - every test below turns it back
# on explicitly via its own Settings override and mocks
# app.services.wikimedia_provider directly (never the real network).
# ---------------------------------------------------------------------------


def _wikimedia_settings(**overrides):
    values = {"enable_wikimedia_images": True}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def _wikimedia_candidate(**overrides) -> WikimediaCandidate:
    values = dict(
        title="File:Mitosis diagram.svg",
        file_page_url="https://commons.wikimedia.org/wiki/File:Mitosis_diagram.svg",
        image_url="https://upload.wikimedia.org/commons/x/Mitosis.svg",
        width=1200,
        height=900,
        mime="image/png",
        license_short_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0",
        artist="Jane Doe",
        credit_line="Own work",
        license_ok=True,
    )
    values.update(overrides)
    return WikimediaCandidate(**values)


async def test_wikimedia_hit_skips_the_ai_model_entirely(monkeypatch):
    """Scenario 1: a high-quality, English-labelled candidate is found and
    validated - MAI-Image-2.6 (here, the AI stub's own `.image`) must never
    be called at all."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate()]

    async def fake_fetch(url, *, settings, transport=None):
        return _real_image_bytes("PNG")

    ai_image_calls: list[str] = []

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        ai_image_calls.append(prompt)
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_hit", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "wikimedia"
    assert block.content["generation_mode"] == "wikimedia"
    assert block.content["source_url"] == "https://commons.wikimedia.org/wiki/File:Mitosis_diagram.svg"
    assert block.content["license_name"] == "CC BY-SA 4.0"
    assert block.content["wikimedia_title"] == "File:Mitosis diagram.svg"
    assert "Wikimedia Commons" in block.content["attribution_text"]
    assert block.content["path"]
    assert ai_image_calls == []  # the AI model was never invoked


async def test_wikimedia_miss_falls_through_to_the_ai_model(monkeypatch):
    """Scenario 2: no candidates at all - the existing AI path runs exactly
    as it did before this feature existed."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return []

    ai_image_calls: list[str] = []

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        ai_image_calls.append(prompt)
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="A never-matched, extremely niche topic")
    ok = await svc._generate_illustration(
        course_id="course_wm_miss", block=block, template=load_template("technical"), course_title="Test Course",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert block.content["generation_mode"] == "mock"
    assert len(ai_image_calls) == 1


async def test_wikimedia_candidate_with_non_english_labels_is_rejected(monkeypatch):
    """Scenario 3: a relevant candidate exists but its baked-in labels
    aren't English - rejected by the vision QA check, falls back to the AI
    model rather than shipping a non-English-labelled image."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate()]

    async def fake_fetch(url, *, settings, transport=None):
        return _real_image_bytes("PNG")

    async def fake_structured(*, schema, **kwargs):
        if schema is WikimediaCandidateCheck:
            return schema(english_labels_only=False, english_labels_detail="Labels are in Hindi, not English.")
        return schema()

    ai_image_calls: list[str] = []

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        ai_image_calls.append(prompt)
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    svc.ai.structured = fake_structured  # type: ignore[method-assign]
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_hindi", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert len(ai_image_calls) == 1


async def test_wikimedia_candidate_below_the_resolution_floor_is_rejected_without_a_vision_call(monkeypatch):
    """Scenario 4 (part 1): a deterministic rejection - too small to be a
    real textbook-quality image - costs zero AI calls; never even
    downloaded."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate(width=100, height=80)]

    fetch_calls: list[str] = []

    async def fake_fetch(url, *, settings, transport=None):
        fetch_calls.append(url)
        return _real_image_bytes("PNG")

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_lowres", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert fetch_calls == []  # rejected by the free, deterministic filter - never downloaded


_SAMPLE_SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
    b'width="800" height="600" viewBox="0 0 800 600">'
    b'<rect width="800" height="600" fill="#fff"/></svg>'
)


async def test_wikimedia_svg_candidate_is_accepted_via_a_rasterised_vision_check(monkeypatch):
    """SVG is a first-class candidate now (not excluded) - a real, confirmed
    check against the live Commons API found most of its best educational
    diagrams are SVG, not JPEG/PNG. The vision model itself can't view raw
    SVG markup, so a rasterised copy (never saved - see
    wikimedia_provider.rasterize_svg_for_vision's own docstring) stands in
    for the vision check only; the ORIGINAL SVG bytes are what actually
    gets saved/served."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate(mime="image/svg+xml", width=800, height=600)]

    async def fake_fetch(url, *, settings, transport=None):
        return _SAMPLE_SVG

    rasterize_calls: list[bytes] = []

    async def fake_rasterize(data, *, settings):
        rasterize_calls.append(data)
        return _real_image_bytes("PNG")  # a real decodable image, standing in for the screenshot

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    monkeypatch.setattr(image_service.wikimedia_provider, "rasterize_svg_for_vision", fake_rasterize)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_svg", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "wikimedia"
    assert rasterize_calls == [_SAMPLE_SVG]  # the vision check saw a rasterised copy...
    assert block.content["path"].endswith(".svg")  # ...but the ORIGINAL svg is what was saved
    saved_path = svc.storage.asset_abs_path("course_wm_svg", block.content["path"])
    assert saved_path.read_bytes() == _SAMPLE_SVG
    # Dimensions came from the SVG's own markup (width/height), never PIL
    # (which cannot open SVG at all).
    assert block.content["width"] == 800
    assert block.content["height"] == 600


async def test_wikimedia_svg_candidate_falls_back_to_ai_when_rasterisation_fails(monkeypatch):
    """Rasterisation (Playwright not installed, a launch failure, a
    malformed SVG) is best-effort - a failure here skips this one
    candidate exactly like a failed download would, never crashes the
    lookup."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate(mime="image/svg+xml", width=800, height=600)]

    async def fake_fetch(url, *, settings, transport=None):
        return _SAMPLE_SVG

    async def fake_rasterize(data, *, settings):
        return None  # rasterisation failed

    ai_image_calls: list[str] = []

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        ai_image_calls.append(prompt)
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    monkeypatch.setattr(image_service.wikimedia_provider, "rasterize_svg_for_vision", fake_rasterize)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_svg_fail", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert len(ai_image_calls) == 1


async def test_wikimedia_candidate_with_an_unreusable_license_is_rejected(monkeypatch):
    """Scenario 4 (part 2): a deterministic rejection on licensing - "All
    Rights Reserved" is never usable regardless of visual quality."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        return [_wikimedia_candidate(license_short_name="All Rights Reserved", license_ok=False)]

    fetch_calls: list[str] = []

    async def fake_fetch(url, *, settings, transport=None):
        fetch_calls.append(url)
        return _real_image_bytes("PNG")

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_badlicense", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert fetch_calls == []


async def test_wikimedia_picks_the_first_eligible_candidate_that_passes_qa(monkeypatch):
    """Scenario 5: of several search results, the deterministic filter
    excludes the unlicensed one and the first surviving, QA-passing
    candidate is the one actually used."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())
    unlicensed = _wikimedia_candidate(title="File:Unlicensed.png", license_ok=False)
    good = _wikimedia_candidate(title="File:GoodOne.png")

    async def fake_search(query, *, limit, settings, transport=None):
        return [unlicensed, good]

    async def fake_fetch(url, *, settings, transport=None):
        return _real_image_bytes("PNG")

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_multi", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["wikimedia_title"] == "File:GoodOne.png"


async def test_wikimedia_api_failure_falls_back_to_the_ai_model(monkeypatch):
    """Scenario 6: Wikimedia itself errors out - course generation is
    unaffected, the AI model takes over automatically."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    async def fake_search(query, *, limit, settings, transport=None):
        raise RuntimeError("Commons is unreachable")

    ai_image_calls: list[str] = []

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        ai_image_calls.append(prompt)
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_fail", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert len(ai_image_calls) == 1


async def test_a_failed_wikimedia_query_does_not_abandon_the_remaining_distinct_queries(monkeypatch):
    """Root-cause fix: a single transient/volume-driven failure on the
    FIRST query (see wikimedia_provider.py's own module docstring on the
    real production 403s this closes) must not give up on Wikimedia
    entirely for this block - the remaining, genuinely different queries
    still each get a real attempt, and a later one succeeding is still
    used instead of falling through to the AI model."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())

    seen_queries: list[str] = []

    async def fake_search(query, *, limit, settings, transport=None):
        seen_queries.append(query)
        if len(seen_queries) == 1:
            raise RuntimeError("simulated transient Wikimedia failure on the first query")
        return [_wikimedia_candidate()]

    async def fake_fetch(url, *, settings, transport=None):
        return _real_image_bytes("PNG")

    async def fail_if_called(*, prompt, size=None, phase="image", provider=None):
        raise AssertionError("the AI model must never be used when a later Wikimedia query succeeds")

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    monkeypatch.setattr(image_service.wikimedia_provider, "fetch_image_bytes", fake_fetch)
    svc.ai.image = fail_if_called  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_partial_fail", block=block, template=load_template("technical"),
        course_title="Biology", key_concept="Mitosis Stages",
    )
    assert ok is True
    assert block.content["image_source"] == "wikimedia"
    assert len(seen_queries) >= 2  # the first (failed) query didn't stop the second from being tried


def test_wikimedia_concurrency_defaults_to_one_and_is_separate_from_image_concurrency():
    """Root-cause fix: a bursty multi-block concurrent search pattern (up
    to `image_concurrency` simultaneous Commons requests from one IP) was
    itself the actual trigger behind Wikimedia's real production 403s - see
    wikimedia_provider.py's own module docstring. Deliberately kept low and
    independent of image_concurrency."""
    settings = get_settings()
    assert settings.concurrency_for("wikimedia") == 1
    assert settings.concurrency_for("wikimedia") != settings.concurrency_for("image")


async def test_wikimedia_cache_hit_skips_search_and_download(monkeypatch, storage):
    """Scenario 7: an equivalent later request reuses the cached image -
    zero new search/download calls."""
    data = _real_image_bytes("PNG")
    meta = {
        "image_source": "wikimedia",
        "source_url": "https://commons.wikimedia.org/wiki/File:X.png",
        "license_name": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "attribution_text": "Jane Doe — CC BY-SA 4.0, via Wikimedia Commons",
        "wikimedia_title": "File:X.png",
    }
    storage.save_wikimedia_cache_entry("depicts mitosis stages", data, meta, extension="png")

    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings(), storage=storage)
    search_calls: list[str] = []

    async def fake_search(query, *, limit, settings, transport=None):
        search_calls.append(query)
        return []

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)

    block = _image_block(purpose="Depicts mitosis stages")
    ok = await svc._generate_illustration(
        course_id="course_wm_cache", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "wikimedia"
    assert block.content["wikimedia_title"] == "File:X.png"
    assert search_calls == []  # cache hit - never even searched


async def test_wikimedia_is_never_tried_for_a_section_intro_illustration(monkeypatch):
    """A section-intro illustration's own fixed-crop resize only ever
    applies to an AI-generated image today - keeping that path unchanged is
    the lowest-risk scoping choice (see the approved plan)."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=_wikimedia_settings())
    search_calls: list[str] = []

    async def fake_search(query, *, limit, settings, transport=None):
        search_calls.append(query)
        return [_wikimedia_candidate()]

    async def fake_ai_image(*, prompt, size=None, phase="image", provider=None):
        return b"png-bytes"

    monkeypatch.setattr(image_service.wikimedia_provider, "search_commons", fake_search)
    svc.ai.image = fake_ai_image  # type: ignore[method-assign]

    block = _image_block(purpose="Depicts mitosis stages", illustration_style="section_intro")
    ok = await svc._generate_illustration(
        course_id="course_wm_section_intro", block=block, template=load_template("technical"), course_title="Biology",
    )
    assert ok is True
    assert block.content["image_source"] == "generated"
    assert search_calls == []  # Wikimedia was never even consulted


def test_a_wikimedia_sourced_block_with_a_path_is_skipped_as_already_generated():
    """Scenario 9: resume/dedup already works for free - `missing_image_
    blocks`'s existing `block.content.get("path")` gate treats a Wikimedia-
    sourced block exactly like an AI-generated one."""
    from app.schemas.document import CourseDocument, Page

    block = _image_block(purpose="Depicts mitosis stages")
    block.content["path"] = "assets/image_001.png"
    block.content["image_source"] = "wikimedia"
    document = CourseDocument(
        document_id="d", course_id="course_resume_wm", course_title="Biology",
        template_id="technical", pages=[Page(blocks=[block])],
    )
    assert ImageService.missing_image_blocks(document) == []


# ---------------------------------------------------------------------------
# Replace Image root-cause fix: genuine image-to-image editing for raster
# illustrations (ImageService._edit_illustration / AIClient.image_edit).
# See app.course.document.patcher's own `_pending_edit` contract: set only
# when an illustration block already has a real picture on disk and is
# staying `kind: "illustration"`.
# ---------------------------------------------------------------------------


def _editable_illustration_block(svc, course_id, **overrides):
    """An illustration block whose `path` points to a REAL saved asset -
    `_edit_illustration` reads this exact file off disk, so a test exercising
    it (not just mocking storage) needs a genuine saved file, not a bare
    string."""
    relative = svc.storage.save_asset(course_id, _real_image_bytes("PNG"), extension="png")
    content = {
        "kind": "illustration",
        "prompt": "fix the overlapping numbers and correct the mathematical symbols",
        "path": relative,
        "asset_id": relative.rsplit("/", 1)[-1],
        "generated": True,
        "width": 64,
        "height": 64,
        "_pending_edit": True,
        **overrides,
    }
    return Block(type=BlockType.IMAGE, content=content), relative


async def test_pending_edit_sends_the_original_image_bytes_to_image_edit():
    """The core requirement: the EXISTING picture's real bytes reach
    `AIClient.image_edit`, not merely a text description of it."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block, relative = _editable_illustration_block(svc, "course_edit_bytes")
    expected_bytes = svc.storage.asset_abs_path("course_edit_bytes", relative).read_bytes()

    calls: list[dict] = []

    async def fake_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        calls.append({"prompt": prompt, "image": image})
        return _real_image_bytes("PNG")

    svc.ai.image_edit = fake_image_edit  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_bytes", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is True
    assert len(calls) >= 1
    assert calls[0]["image"] == expected_bytes


async def test_pending_edit_sends_the_complete_correction_prompt():
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    correction = (
        "Correct all mathematical symbols, fix overlapping labels, improve spacing, "
        "preserve the original objects and overall composition, and make the image "
        "professionally designed."
    )
    block, _ = _editable_illustration_block(svc, "course_edit_prompt", prompt=correction)

    calls: list[dict] = []

    async def fake_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        calls.append({"prompt": prompt})
        return _real_image_bytes("PNG")

    svc.ai.image_edit = fake_image_edit  # type: ignore[method-assign]

    await svc.generate_for_block(
        course_id="course_edit_prompt", block=block, template=load_template("technical"), course_title="Course",
    )
    # Every clause of the user's multi-part instruction reaches the provider -
    # never silently dropped by truncation or a conflicting default.
    assert correction in calls[0]["prompt"]


async def test_pending_edit_success_replaces_the_asset_and_clears_the_flag():
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block, original_relative = _editable_illustration_block(svc, "course_edit_success")
    edited_bytes = _real_image_bytes("PNG")

    async def fake_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        return edited_bytes

    svc.ai.image_edit = fake_image_edit  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_success", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is True
    assert block.content["generated"] is True
    assert block.content["error"] is None
    assert block.content["_pending_edit"] is False
    assert block.content["path"]
    saved = svc.storage.asset_abs_path("course_edit_success", block.content["path"]).read_bytes()
    assert saved == edited_bytes


async def test_pending_edit_failure_never_calls_text_to_image_generation():
    """The strict requirement: on an edit failure, the system must NEVER
    silently fall back to a fresh, unrelated text-to-image generation."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block, _ = _editable_illustration_block(svc, "course_edit_fail")

    async def failing_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        raise AIServiceError("simulated provider edit failure")

    async def fail_if_called(*, prompt, size=None, phase="image", provider=None):
        raise AssertionError("text-to-image generation must never be used as a silent fallback for a failed edit")

    svc.ai.image_edit = failing_image_edit  # type: ignore[method-assign]
    svc.ai.image = fail_if_called  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_fail", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is False
    assert "simulated provider edit failure" in block.content["error"]


async def test_pending_edit_failure_preserves_the_original_image_untouched():
    """The original image must remain in place and valid until a successful
    replacement is ready - never cleared, never silently lost."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block, original_relative = _editable_illustration_block(svc, "course_edit_preserve")
    original_bytes = svc.storage.asset_abs_path("course_edit_preserve", original_relative).read_bytes()

    async def failing_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        raise AIServiceError("simulated provider edit failure")

    svc.ai.image_edit = failing_image_edit  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_preserve", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is False
    assert block.content["path"] == original_relative
    assert block.content["generated"] is True  # unchanged from before the failed edit
    still_on_disk = svc.storage.asset_abs_path("course_edit_preserve", block.content["path"]).read_bytes()
    assert still_on_disk == original_bytes


async def test_pending_edit_with_a_missing_source_file_fails_clearly_without_calling_the_provider():
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block = Block(
        type=BlockType.IMAGE,
        content={
            "kind": "illustration", "prompt": "fix X", "path": "assets/does_not_exist.png",
            "generated": True, "_pending_edit": True,
        },
    )

    async def fail_if_called(*, prompt, image, size=None, phase="image", provider=None):
        raise AssertionError("the provider must never be called when the source image can't be read")

    svc.ai.image_edit = fail_if_called  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_missing_file", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is False
    assert "Could not read" in block.content["error"]


async def test_pending_edit_disabled_by_flag_falls_through_to_fresh_generation():
    """`enable_image_editing=False` reproduces the pre-this-feature
    behaviour exactly: a fresh text-to-image generation, not a failure."""
    settings = get_settings()
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=settings)
    block, _ = _editable_illustration_block(svc, "course_edit_disabled")

    async def fail_if_edit_called(*, prompt, image, size=None, phase="image", provider=None):
        raise AssertionError("image_edit must never be called while the feature flag is off")

    generate_calls: list[str] = []

    async def fake_generate(*, prompt, size=None, phase="image", provider=None):
        generate_calls.append(prompt)
        return _real_image_bytes("PNG")

    svc.ai.image_edit = fail_if_edit_called  # type: ignore[method-assign]
    svc.ai.image = fake_generate  # type: ignore[method-assign]
    settings.enable_image_editing = False
    try:
        ok = await svc.generate_for_block(
            course_id="course_edit_disabled", block=block, template=load_template("technical"), course_title="Course",
        )
    finally:
        settings.enable_image_editing = True
    assert ok is True
    assert len(generate_calls) >= 1
    assert block.content["_pending_edit"] is False


async def test_only_the_selected_block_is_affected_by_an_edit():
    """A successful edit must never touch any OTHER block's content."""
    svc = ImageService(ai=_FakeAI(is_mock=True), settings=get_settings())
    block, _ = _editable_illustration_block(svc, "course_edit_scoped")
    other_block = _image_block(purpose="An unrelated illustration")
    other_snapshot = dict(other_block.content)

    async def fake_image_edit(*, prompt, image, size=None, phase="image", provider=None):
        return _real_image_bytes("PNG")

    svc.ai.image_edit = fake_image_edit  # type: ignore[method-assign]

    ok = await svc.generate_for_block(
        course_id="course_edit_scoped", block=block, template=load_template("technical"), course_title="Course",
    )
    assert ok is True
    assert other_block.content == other_snapshot
