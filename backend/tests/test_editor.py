"""Regression tests for two real, confirmed editor bugs reported from a real
document (doc_55f8d16b4a37):

Bug 1: "convert this into an image" on comparison-shaped prose (a "rolling
suitcase vs. sliding a box" explanation) got routed to a structured
comparison diagram - no AI image model was ever called, even though the
user expected a real illustration. This is EDITOR_SYSTEM's own shape
classification working as designed for the generic/ambiguous phrasing, but
there was no way for a user to explicitly ask for a real picture instead.
Fix: `requests_real_illustration_over_diagram` (editor.py) is a conservative,
zero-extra-LLM-call detector that only fires on wording that goes further
than "convert this into an image" and explicitly asks for a real/actual/
photographic/AI picture, or explicitly rules out a diagram/chart - the bare
generic phrasing is left alone so the morning's shape-classification work
still applies for ambiguous instructions.

Bug 2: re-issuing "convert this into an image" on a block that's ALREADY an
image (e.g. the comparison diagram the first conversion produced) returned
ZERO operations and surfaced as "The editor returned no applicable
operations for this instruction" - neither EDITOR_SYSTEM rule recognised the
combination of "convert into an image" wording with an already-image
selection. Fix: EDITOR_SYSTEM now explicitly maps that combination onto
`replace_image`. This half is a prompt-text-only check, same pattern as the
other EDITOR_SYSTEM regression tests in test_patch.py - it does not prove
the model complies, only that the instruction now exists.
"""

from __future__ import annotations

from app.agents.editor import requests_real_illustration_over_diagram
from app.agents.prompts import EDITOR_SYSTEM, editor_user
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block


# --- Bug 1: explicit-illustration-override classifier -----------------------


def test_explicit_illustration_request_is_detected():
    assert requests_real_illustration_over_diagram("generate a real AI image of this, not a diagram")
    assert requests_real_illustration_over_diagram("I want an actual photo, not a chart")
    assert requests_real_illustration_over_diagram("draw a picture of this instead")
    assert requests_real_illustration_over_diagram("no diagram please, just a real image")
    assert requests_real_illustration_over_diagram("give me a photo of the apparatus")
    assert requests_real_illustration_over_diagram("make this photorealistic")


def test_generic_convert_to_image_wording_does_not_trigger_the_override():
    """Regression guard: the morning's shape-classification work must still
    run for the documented ambiguous/generic phrasing."""
    assert not requests_real_illustration_over_diagram("convert this into an image")
    assert not requests_real_illustration_over_diagram("turn this into a diagram")
    assert not requests_real_illustration_over_diagram("make this a flowchart")
    assert not requests_real_illustration_over_diagram("fix the overlapping labels")
    assert not requests_real_illustration_over_diagram("")


def test_explicit_illustration_detection_is_case_insensitive():
    assert requests_real_illustration_over_diagram("GENERATE A REAL IMAGE, NOT A DIAGRAM")


def test_editor_user_injects_override_note_only_when_the_instruction_is_explicit():
    template = load_template("technical_v1")
    block = Block(type=BlockType.PARAGRAPH, content={"text": "Sliding vs rolling."})

    with_override = editor_user(
        "Course", template, [block], [], "generate a real image of this, not a diagram",
        force_illustration_override=True,
    )
    assert "EXPLICIT OVERRIDE" in with_override
    assert 'kind: "illustration"' in with_override

    without_override = editor_user(
        "Course", template, [block], [], "convert this into an image",
        force_illustration_override=False,
    )
    assert "EXPLICIT OVERRIDE" not in without_override


# --- Bug 2: re-converting an existing image/diagram block -------------------


def test_editor_system_resolves_re_conversion_of_an_existing_image_block_to_replace_image():
    """Prompt-text-only check (same pattern as the other EDITOR_SYSTEM
    regression tests): the instruction must now explicitly cover the case
    where the selection is already an image block and the user re-issues a
    convert/turn/redo wording, mapping it to replace_image rather than
    leaving the model with no recognised rule at all."""
    assert 'already "image"' in EDITOR_SYSTEM
    assert "`replace_image`, not `replace_block`" in EDITOR_SYSTEM
    assert "no applicable operations" in EDITOR_SYSTEM
    assert "resolve it to either `replace_image` (most cases)" in EDITOR_SYSTEM
