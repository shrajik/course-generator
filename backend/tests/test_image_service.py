"""ImageService.build_prompt - the raster illustration prompt, and how
`illustration_style: "textbook"` swaps the template's own decorative
image_guidance for the fixed TEXTBOOK_ILLUSTRATION_GUIDANCE (a companion
picture placed alongside a diagram/concept_experience visual - see
WRITER_SYSTEM/EDITOR_SYSTEM in app.agents.prompts). Also covers the
post-generation text-accuracy check (ImageService._verify_generated_text /
_generate_with_text_check) - a real reported bug: AI-generated illustrations
sometimes misspell or garble baked-in text ("Inducd crrrent" instead of
"Induced current")."""

from __future__ import annotations

from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.services.image_service import ImageTextCheck, TEXTBOOK_ILLUSTRATION_GUIDANCE, ImageService


def _image_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"prompt": "A prompt", **content})


def test_default_illustration_uses_template_image_guidance():
    template = load_template("technical")
    block = _image_block()
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert template.image_guidance in prompt
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE not in prompt


def test_textbook_illustration_style_uses_fixed_textbook_guidance():
    template = load_template("technical")
    block = _image_block(illustration_style="textbook")
    prompt = ImageService.build_prompt(block, template, "Course Title")
    assert TEXTBOOK_ILLUSTRATION_GUIDANCE in prompt
    assert template.image_guidance not in prompt


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
