"""Image generation for `image` blocks.

The writer decides *where* a visual helps and supplies the prompt; this service
turns those prompts into asset files under `data/courses/{course_id}/assets/`
and writes the relative path back into the Course Document. A block marked
`kind == "diagram"` is delegated to `DiagramService` first (a structured, on-
theme SVG); a block marked `kind == "concept_experience"` (behind
`settings.enable_concept_experience_visuals`) is delegated to
`ConceptVisualService` (a deterministic, static HTML+CSS visual - see the
approved plan at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md);
everything else - and any of these that doesn't work out - gets a raster
illustration from the image model, as before.
"""

from __future__ import annotations

import asyncio
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import phase as metrics_phase
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, BlockType, merge_content
from app.schemas.document import Block, CourseDocument
from app.schemas.template import CourseTemplate
from app.services.concept_visual_service import ConceptVisualService
from app.services.diagram_service import DiagramService
from app.services.formula_qa import evaluate_formula
from app.services.image_text_ocr import find_ground_truth_mismatches, run_ocr
from app.services.memory_service import MemoryService, get_memory_service
from app.services.numeric_consistency_qa import (
    IDENTIFIER_VALUE_NEEDS_REVIEW,
    find_identifier_value_issues,
    find_numeric_contradictions,
)
from app.services.openai_service import AIClient, get_ai_client
from app.services.quality_framework import visual_qa_finding_entries
from app.render.section_icons import match_generic_section_icon, render_section_icon_svg
from app.services.storage_service import StorageService, get_storage
from app.services import wikimedia_provider
from app.services.wikimedia_image_qa import candidate_passed, evaluate_candidate
from app.services.wikimedia_provider import WikimediaCandidate

log = get_logger(__name__)

MAX_PROMPT_CHARS = 4500

# Defensive last-resort ceiling at the ACTUAL provider call (`_draw`), not a
# replacement for `PROMPT_CHAR_BUDGET` (16_000, enforced once at initial
# `build_prompt` assembly). Root cause this closes: every bounded RETRY
# this module runs (the relevance retry loop in `_generate_illustration`,
# the missing-label retries in `_generate_with_text_check`) builds its next
# prompt by APPENDING a fresh "IMPORTANT: ..."/"ABSOLUTELY CRITICAL: ..."
# correction onto the previous attempt's already near-budget prompt,
# without ever re-applying the budget - a real, confirmed production case
# (a dense two-concept textbook illustration brief) compounded across 2
# relevance retries into a 21,356-character prompt that Azure's API
# rejected outright (a hard ~20,000-char limit, undocumented in this
# codebase until this failure), turning a quality problem (the image not
# quite matching the brief) into a total generation failure (no image at
# all). Truncated from the FRONT (keeping the tail): the front is the
# generic style/guidance boilerplate assembled once at the start, while
# every retry's freshest, most specific correction is always appended at
# the END - keeping the tail is what keeps the retry's whole point (telling
# the model exactly what the previous attempt got wrong) intact even when
# this safety net has to trim something. In ordinary use (prompt already
# under PROMPT_CHAR_BUDGET) this is always a no-op.
_PROVIDER_PROMPT_HARD_LIMIT = 19_000

# A raster image model (MAI-Image-2.6, and every other current text-to-image
# model alike) can reliably bake in a short symbol/term and its plain-word
# definition ("ρ (density)", "ε = induced emf" - the writer prompt's own
# template example) but not a multi-symbol mathematical RELATIONSHIP between
# several quantities ("τ = μ·γ̇ (Newtonian)", "Kn = λ / L_c", "D/Dt = ∂/∂t +
# (u·∇)") - a real, confirmed case: a fluid-mechanics course's textbook
# illustrations, each asked for 9-15 `expected_labels` including several
# full equations like these, failed the post-generation text-legibility
# check on nearly every attempt (not a style problem - `style_compliant`
# passed on most of these) and burned their one bounded retry before
# falling back to a generic, off-topic placeholder image - so the reader
# never saw the actual requested diagram at all, on most of a chapter's
# illustrations. `image_formula_expression` already exists as the one-
# equation, separately-and-correctly-verified channel for a genuine
# governing equation (see WRITER_SYSTEM's own guidance) - `expected_labels`
# only needs to carry what raster text generation can actually deliver.
# Counting characters from this operator set is a cheap, reliable proxy for
# "this reads as an equation between symbols", not just a labelled term -
# `_` and `^` are included because a subscript/superscript baked into raster
# text is exactly as unreliable as any other multi-symbol notation.
_LABEL_OPERATOR_CHARS = "=/·*+^_∂∇×→∫Σ"
# Raised from 6 (visual-generation-quality fix): the original 6-label
# ceiling was a blunt proxy for "don't let a raster model garble dense
# text" - but the real, confirmed garbling cases (see this module's own
# history) were never about the COUNT of labels, they were about individual
# labels being equation-heavy, multi-clause, or built from hard-to-render
# glyphs (all still screened out, per-label, by `_is_renderable_label`
# below, completely unchanged). A content-rich apparatus (several
# components, several directions/symbols) genuinely needs more than 6
# short, simple labels to be properly taught - capping every image at 6
# regardless of subject is exactly why this app's textbook images read as
# sparser/less complete than a free-form Playground prompt with no such
# ceiling. 24 is a generous upper bound, not a target - the writer still
# only lists as many labels as a given apparatus genuinely needs (a simple
# single-component picture should still use far fewer). The safety net
# against garbling is now the mandatory verify-and-retry text-accuracy
# check (`_relevance_failed`'s `text_accurately_spelled`, see
# `ImageRelevanceCheck`), which runs regardless of label count, not a raw
# count ceiling.
_MAX_EXPECTED_LABELS = 24
# How many extra attempts `_generate_illustration` gives a textbook image
# that fails `_verify_image_relevance` before giving up and shipping a
# generic fallback - see that retry loop's own comment for the real,
# confirmed case this covers.
_MAX_RELEVANCE_RETRIES = 2
# Generous on plain length - a genuine real case needed a 46-character
# descriptive phrase with zero math notation ("Induced current creates
# opposing field (Lenz)") and is exactly the kind of label a raster model
# CAN attempt; length alone was never the actual problem in the confirmed
# failures (several were under 20 characters: "Kn = λ / L_c"). The operator
# count is the real signal - this cap only catches genuine runaway text.
_MAX_LABEL_CHARS = 70


_SUPERSCRIPT_SUBSCRIPT_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻₀₁₂₃₄₅₆₇₈₉"


def _has_hard_to_render_glyph(label: str) -> bool:
    """A real, confirmed case: plain Greek letters (ρ, μ, σ, τ) rendered
    perfectly in a real generation, but "γ̇" (gamma with a COMBINING DOT
    ABOVE, U+0307 - the standard shear-rate notation) kept failing text
    verification in every attempt even as the ONLY non-trivial symbol in
    an otherwise maximally simple label set. A combining mark is a second,
    floating glyph stacked onto its base character at render time, not a
    single precomposed letterform - exactly the kind of fine typographic
    detail a raster image model cannot reliably reproduce as legible,
    correctly-spelled text. Unicode superscript/subscript characters ("10²",
    "s⁻¹") are the same problem in a different guise - tiny raised/lowered
    glyphs, not normal-sized text."""
    if any(ch in _SUPERSCRIPT_SUBSCRIPT_CHARS for ch in label):
        return True
    return any(unicodedata.combining(ch) != 0 for ch in label)


def _is_renderable_label(label: str) -> bool:
    operator_count = sum(label.count(ch) for ch in _LABEL_OPERATOR_CHARS)
    if len(label) > _MAX_LABEL_CHARS or operator_count > 1 or _has_hard_to_render_glyph(label):
        return False
    # A "term: short definition" pair ("ε: induced emf") is exactly the
    # allowed pattern, same as "ε = induced emf" - but a real, confirmed
    # case shows the writer sometimes fills that same shape with a whole
    # CLAUSE or verdict instead of a definition ("Compressible: variable-
    # density model warranted", "Rheogram: Apparent viscosity μ_app vs
    # shear rate γ̇") - still only one operator-set character (or none) by
    # the check above, so it slipped through, and these consistently
    # garbled: every extra word compounds the chance a raster model drops
    # or misspells one of them. More than 2 words after the colon is no
    # longer a short definition.
    if ":" in label and len(label.split(":", 1)[1].split()) > 2:
        return False
    return True


def sanitize_expected_labels(labels: list[str]) -> list[str]:
    """Drop any label too equation-like for raster text to render, then cap
    the remainder to `_MAX_EXPECTED_LABELS` - applied once, at every point
    `expected_labels` is read, so the SAME shortened list is both what the
    image model is asked to draw and what the post-generation check
    verifies (never a prompt asking for 15 labels while verification only
    checks 6, or vice versa)."""
    return [label for label in labels if _is_renderable_label(label)][:_MAX_EXPECTED_LABELS]


# --- image-generation prompt budget (priority-based) -------------------------
#
# This section covers ONLY `ImageService.build_prompt`'s own final assembly -
# the prompt actually sent to the image-generation model. `MAX_PROMPT_CHARS`
# above is a separate, unrelated budget used for QA "brief" strings (the
# Wikimedia search/validation brief, the post-generation relevance-check
# brief) that are never sent to an image-generation model at all; those are
# deliberately untouched here.
#
# Root cause this replaces: `build_prompt` used to assemble
# `[context_line, writer_prompt, labels_line, style_guidance,
# negative_constraints]`, join them, then blindly slice the WHOLE result to
# `MAX_PROMPT_CHARS` (4500). Since `style_guidance` was concatenated near the
# END, a long combined course/chapter/section/key_concept context plus a
# verbose writer `image_prompt` could silently cut off the TAIL of the fixed
# style guidance - for `TEXTBOOK_ILLUSTRATION_GUIDANCE` specifically, that
# tail is the CRITICAL FRAMING RULE, the single rule most directly
# responsible for catching the real, confirmed label-overlap defect this
# module's own history already documents (see TEXTBOOK_ILLUSTRATION_GUIDANCE's
# FORMULA BLOCK section). A silent `[:n]` slice gave no log line, no error,
# nothing - the first sign would have been another garbled/overlapping image.
#
# Fix: `_assemble_prompt_with_priority` below builds the prompt from
# explicitly prioritised `_PromptSegment`s instead of one flat string-then-
# slice. Priority 1 (the fixed style/negative-constraint rules - every
# "never do X" and "must do Y" instruction, CRITICAL FRAMING RULE included)
# is never shortened or dropped, full stop. Priority 2 (expected labels) is
# preserved whenever the budget allows. Priority 3 (the per-block
# course/chapter/section/key_concept context and the writer's own free-text
# prompt) is the ONLY tier ever shortened, and only word/sentence-boundary
# aware (`_shorten_to_budget`), never a bare `[:n]` slice - see both
# functions' own docstrings.
#
# Provider limit, verified rather than assumed (per the "do not guess" brief
# this fix was built to): Microsoft's own documentation for the MAI image
# generations API states, for the `prompt` request parameter, "Maximum
# context length: 32,000 tokens." -
# https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image
# (fetched 2026-11-07; applies to MAI-Image-2.6, MAI-Image-2.6-Flash and the
# MAI-Image-2.5 family alike - this module's `textbook_image_provider`
# setting routes every "textbook"-style illustration through exactly this
# API). This is a TOKEN limit, not a character limit, and no tokenizer is
# called here to convert exactly (that would be an extra per-prompt CPU cost
# for a problem budget headroom alone already solves - see "no unnecessary
# additional LLM calls" in this fix's own brief). Converting with a
# deliberately pessimistic ratio instead: real GPT-family tokenizers average
# roughly 4 characters per token for English prose; mixed technical/math/
# symbol-heavy text (`\sin`, `\theta_t`, `n_1`, single combining-diacritic
# glyphs like `n̂`) tokenizes LESS efficiently, so erring toward FEWER
# characters per token is the safe direction - this module uses 3.
MAI_IMAGE_MAX_PROMPT_TOKENS = 32_000  # the verified, documented provider limit - see above
_CONSERVATIVE_CHARS_PER_TOKEN = 3  # deliberately pessimistic; see this section's own comment

# The actual assembly target `_assemble_prompt_with_priority` builds against.
# Deliberately far below the ~96,000-char ceiling the two constants above
# imply (32,000 x 3) - this is not "spend the whole budget", it is "stay
# inside a budget an order of magnitude larger than any real combined prompt
# needs, so the priority system only ever has to shorten Priority 3 content
# in a genuinely pathological case (a test fixture built to force it), never
# in ordinary use." A documented, verified multiple of the old, unverified
# MAX_PROMPT_CHARS (4500) - not an arbitrary bump (see this fix's own
# constraint against that).
PROMPT_CHAR_BUDGET = 16_000


@dataclass(frozen=True)
class _PromptSegment:
    """One named piece of an image-generation prompt, tagged with how it
    may be treated when the assembled prompt would otherwise exceed
    `PROMPT_CHAR_BUDGET`.

    Priority 1 - never truncated or dropped: the fixed style/negative-
    constraint rules (CRITICAL FRAMING RULE, STRICT NO-TEXT RULE, label-
    overlap/formula-correctness requirements, "no watermarks") and the
    expected-labels instruction - the labels list is promoted here
    (visual-generation-quality fix) because its length is inherently
    bounded (`_MAX_EXPECTED_LABELS` short, individually-capped entries), so
    protecting it fully can never risk the hard provider character budget.
    This is the one hard invariant `_assemble_prompt_with_priority` keeps.

    Priority 2 - preserved whenever the budget allows, shortened only under
    genuine pressure (and then by dropping whole segments from the end of
    the list, never truncating one mid-word): currently unused by
    `build_prompt`, kept as a real middle tier for any future segment that
    genuinely warrants "preserve when possible, drop whole before
    truncating" rather than "never drop" or "shorten first".

    Priority 3 - the only tier ever shortened to make room, and always via
    `_shorten_to_budget` (word/sentence-boundary aware), never a bare
    slice: the writer's own free-text `image_prompt`/`purpose`/`caption`
    and the per-block course/chapter/section/key_concept `context` line.
    Unlike `expected_labels`, the writer's own prompt has no length
    ceiling, so it must stay in this gracefully-shortenable tier rather
    than Priority 1 - but it is listed BEFORE `context` in `build_prompt`'s
    own segment list (visual-generation-quality fix), and this function's
    own Priority-3 handling grants earlier segments first claim on
    whatever budget remains - so the writer's actual educational content
    is shortened last and least, while `context` (a synthesized restatement
    of information the writer's own prompt already carries in substance)
    genuinely is the least load-bearing segment in the prompt, and absorbs
    the cut first and most."""

    name: str
    text: str
    priority: int  # 1 = never truncate, 2 = preserve when possible, 3 = optimise first


def _shorten_to_budget(text: str, max_chars: int) -> str:
    """Fits `text` into `max_chars` WITHOUT a bare `text[:max_chars]` slice -
    the exact failure class this fix exists to remove (a mid-word, mid-
    sentence cut with no indication anything was lost). Prefers the last
    complete SENTENCE boundary at or before the limit (so what survives
    reads as whole, coherent prose, not a dangling fragment); falls back to
    the last complete WORD boundary only when no sentence boundary leaves
    enough of the text intact to be worth keeping. Always appends a visible
    marker, so a shortened segment is never silently indistinguishable from
    a complete one - in the prompt itself, in a log line, and in a test
    asserting against it."""
    if len(text) <= max_chars:
        return text
    marker = " [...shortened for length]"
    budget = max_chars - len(marker)
    if budget <= 0:
        return ""
    window = text[:budget]
    best = -1
    for terminator in (". ", "! ", "? ", "\n"):
        idx = window.rfind(terminator)
        if idx == -1:
            continue
        end = idx + (1 if terminator == "\n" else len(terminator))
        if end > best:
            best = end
    if best < budget * 0.4:  # too little kept to be worth preferring over a word boundary
        space = window.rfind(" ")
        if space > 0:
            best = space
    if best <= 0:
        best = budget
    return text[:best].rstrip() + marker


@dataclass(frozen=True)
class PromptAssemblyResult:
    """What `_assemble_prompt_with_priority` actually did, returned
    alongside the prompt string itself - so a caller can log it, and a test
    can assert against exactly what happened, instead of re-deriving "was
    anything shortened" by diffing strings after the fact."""

    prompt: str
    total_chars: int
    budget: int
    segments_shortened: tuple[str, ...]  # names of any Priority 3 segment that had to be shortened
    segments_dropped: tuple[str, ...]  # names of any Priority 2 segment dropped entirely under pressure
    mandatory_content_intact: bool  # False only if Priority 1 content alone could not fit - see below


def _assemble_prompt_with_priority(
    segments: list[_PromptSegment], *, budget: int = PROMPT_CHAR_BUDGET
) -> PromptAssemblyResult:
    """Builds the final prompt from `segments`, honouring each one's
    priority - the structured replacement for the old flat-string-then-
    `[:MAX_PROMPT_CHARS]`-slice approach (see this module's own "image-
    generation prompt budget" section for the real, confirmed risk that
    fixed).

    Priority 1 segments are always included in full - the one hard
    invariant this function keeps, no matter how large the other segments
    are. Priority 2 is included in full whenever there's room, and only
    ever shortened by dropping whole segments from the END of the list
    (never a mid-label cut) once nothing else is left to trim. Priority 3
    is the only tier ever shortened mid-segment, word/sentence-boundary
    aware (`_shorten_to_budget`), and only after every Priority 1/2 segment
    already has its full space reserved - today that is the writer's own
    free-text prompt and the synthesized course/chapter/section context
    line (see `build_prompt`'s own segment list); EARLIER segments in this
    tier get first claim on whatever budget remains, which is why
    `build_prompt` lists the writer's own prompt before `context` - the
    writer's actual content is shortened last and least, the synthesized
    context line absorbs the cut first. The exact labels required
    (`expected_labels`) are Priority 1 and are never the thing this
    function trims at all, since that field's length is inherently
    bounded and can never itself risk the hard character budget.

    Segments with empty text are dropped before anything else (matches the
    previous `if part` filter) and never counted as "shortened" or
    "dropped" - there was never anything there to preserve."""
    present = [s for s in segments if s.text]
    p1 = [s for s in present if s.priority == 1]
    p2 = [s for s in present if s.priority == 2]
    p3 = [s for s in present if s.priority == 3]

    p1_text = "\n".join(s.text for s in p1)
    mandatory_intact = len(p1_text) <= budget
    if not mandatory_intact:
        # Unreachable in ordinary use - every Priority 1 segment is fixed,
        # authored text, individually far smaller than `budget` (see this
        # module's own constants) - but per this fix's own brief, a
        # mandatory instruction is never silently dropped to make room:
        # log loudly (never raise - a bad image prompt must never fail the
        # whole course generation run, the same convention
        # `generate_for_block`'s own error handling already keeps
        # throughout this module) and return it in full regardless of the
        # overrun, so the caller can see exactly what happened rather than
        # receiving a silently-incomplete prompt.
        log.error(
            "Image prompt assembly: mandatory (Priority 1) content alone is %d chars, "
            "exceeding the %d-char budget - this should be structurally impossible. "
            "Returning it in full rather than silently dropping any of it.",
            len(p1_text), budget,
        )

    remaining = max(budget - len(p1_text) - (1 if p1_text else 0), 0)
    p2_text = ""
    dropped: list[str] = []
    if p2:
        candidate = "\n".join(s.text for s in p2)
        if len(candidate) <= remaining:
            p2_text = candidate
            remaining = max(remaining - len(candidate) - 1, 0)
        else:
            # Keep whole segments from the FRONT of the list (the writer's
            # own ordering - typically most-important-first) until no more
            # fit; a segment that doesn't fit whole is dropped entirely,
            # never cut mid-label.
            kept: list[str] = []
            used = 0
            still_fitting = True
            for seg in p2:
                cost = len(seg.text) + (1 if kept else 0)
                if still_fitting and used + cost <= remaining:
                    kept.append(seg.text)
                    used += cost
                else:
                    still_fitting = False
                    dropped.append(seg.name)
            p2_text = "\n".join(kept)
            remaining = max(remaining - used, 0)

    p3_text = ""
    shortened: list[str] = []
    if p3:
        candidate = "\n".join(s.text for s in p3)
        if len(candidate) <= remaining:
            p3_text = candidate
        else:
            pieces: list[str] = []
            share = remaining
            for seg in p3:
                cost_separator = 1 if pieces else 0
                if share - cost_separator <= 0:
                    shortened.append(seg.name)
                    continue
                available = share - cost_separator
                if len(seg.text) <= available:
                    pieces.append(seg.text)
                    share -= len(seg.text) + cost_separator
                else:
                    piece = _shorten_to_budget(seg.text, available)
                    if piece:
                        pieces.append(piece)
                    shortened.append(seg.name)
                    share = 0
            p3_text = "\n".join(pieces)

    prompt = "\n".join(part for part in (p1_text, p2_text, p3_text) if part)
    return PromptAssemblyResult(
        prompt=prompt,
        total_chars=len(prompt),
        budget=budget,
        segments_shortened=tuple(shortened),
        segments_dropped=tuple(dropped),
        mandatory_content_intact=mandatory_intact,
    )


# Pre-generation validation: the literal, named marker this fix's own brief
# calls out by name ("Verify that the CRITICAL FRAMING RULE has not been
# removed") plus the equivalent anchor for each of the other two styles -
# an extra, independent check on top of `mandatory_content_intact` above
# (which only confirms Priority 1 content was INCLUDED, never that a future
# edit didn't accidentally tag the wrong text as Priority 1 in the first
# place). Checked against the FINAL assembled prompt, not the source
# constant, so this also catches a regression in the segment-building code
# itself, not just in `_assemble_prompt_with_priority`.
_MANDATORY_PROMPT_MARKERS: dict[str, tuple[str, ...]] = {
    "textbook": ("CRITICAL FRAMING RULE",),
    "section_intro": ("STRICT NO-TEXT RULE",),
    "": ("STRICT NO-TEXT RULE", "RELATIONSHIP DIRECTION MUST BE CORRECT"),
}


def _validate_prompt_assembly(result: PromptAssemblyResult, *, style: str) -> list[str]:
    """Returns the names of every mandatory marker missing from
    `result.prompt` for this illustration `style` - empty when everything
    required is present. Never raises; the caller logs and decides (see
    `build_prompt`) - consistent with this module's own "a bad image must
    never fail the whole course" convention, applied here to "a bad PROMPT
    must never silently fail the whole course" instead."""
    missing = [marker for marker in _MANDATORY_PROMPT_MARKERS.get(style, ()) if marker not in result.prompt]
    if missing or not result.mandatory_content_intact:
        log.error(
            "Image prompt validation failed for style=%r: missing markers=%s, "
            "mandatory_content_intact=%s, total_chars=%d/%d, shortened=%s, dropped=%s",
            style, missing, result.mandatory_content_intact, result.total_chars, result.budget,
            result.segments_shortened, result.segments_dropped,
        )
    elif result.segments_shortened or result.segments_dropped:
        log.info(
            "Image prompt assembly optimised for style=%r: total_chars=%d/%d, shortened=%s, dropped=%s",
            style, result.total_chars, result.budget, result.segments_shortened, result.segments_dropped,
        )
    return missing


# Wikimedia Commons is not tried for these illustration styles - see
# ImageService._try_wikimedia_image's own docstring for why (section_intro's
# own fixed-crop resize only ever applies to an AI-generated image today;
# keeping that path exactly as it was before this feature existed is the
# lowest-risk choice).
_WIKIMEDIA_EXCLUDED_STYLES = ("section_intro",)
# At most this many downloaded-and-vision-checked candidates per query -
# the deterministic license/resolution/mime filter already did the cheap
# elimination; this bounds how many AI vision calls one query can ever cost
# ("avoid unnecessary sequential AI calls").
_WIKIMEDIA_MAX_CANDIDATES_CHECKED = 2

_CONTEXT_FIELD_CHAR_CAP = 120  # a title is always short; this only guards against a pathological outlier


_KEY_CONCEPT_CHAR_CAP = 200


# Cap on `source_excerpt` (chapter_section_context's new 4th element) - a
# section's real prose, concatenated, for source-grounding verification (see
# app.services.diagram_content_qa/ImageService._verify_image_relevance). Not
# one isolated paragraph: the WHOLE section's prose (every real-text block
# under the same heading, wherever it sits relative to the image - see the
# function's own docstring on why) - a real, specifically-requested
# requirement that a fact appearing later in the same section must still be
# available, never missed just because it wasn't in the one paragraph
# immediately before the image. Capped only as a token-budget safety net
# (matching this module's existing MAX_PROMPT_CHARS=4500 QA-brief budget,
# not the much larger PROMPT_CHAR_BUDGET=16000 reserved for the actual
# generation prompt) - a pathologically long section is truncated, never
# silently dropped, so verification still runs against *most* of the real
# content rather than none of it.
_SOURCE_EXCERPT_CHAR_CAP = 4000


def chapter_section_context(document: CourseDocument) -> dict[str, tuple[str, str, str, str]]:
    """Maps every block id to (chapter_title, section_title, key_concept, source_excerpt).

    `chapter_title`/`section_title` are the nearest preceding level-1/other
    heading's text, walking the document in its own natural page/block
    order (see `CourseDocument.iter_blocks`) - exactly what the writer
    already put in the document's own heading blocks (every chapter's
    first block is a level-1 heading with the chapter title - see
    WRITER_SYSTEM), so this generalises to any subject with zero per-topic
    logic.

    `key_concept` is a further, smaller fix for a real gap the chapter
    title alone leaves open: "Newton's Laws of Motion" tells an image model
    WHICH chapter, never WHAT TO ACTUALLY DEPICT - a generic "physics"
    visual satisfies the chapter noun without teaching the law itself. The
    writer already produces exactly that missing detail, as real sentences,
    in the chapter's own `learning_objectives` block (confirmed in real
    generated course data, e.g. "Select and configure a reproducible
    environment (venv, Poetry, pipx, or Docker)" - concrete and mechanism-
    level, never just the chapter name) - falling back to the chapter's own
    `summary.key_takeaways` when no objectives block exists. This is a
    SECOND pass over the same walk (not a per-block lookup) because that
    block can sit anywhere in the chapter, often AFTER the very
    section_intro image that would most benefit from it - a real, confirmed
    case: in generated course data, the "Learning Objectives" section's own
    opening image is the block immediately BEFORE the objectives list, not
    after it. Still zero new AI calls, zero per-subject rules - purely
    reusing content the writer already wrote for this exact chapter."""
    chapter_title = ""
    section_title = ""
    titles: dict[str, tuple[str, str]] = {}
    chapter_key_concept: dict[str, str] = {}
    chapter_fallback_concept: dict[str, str] = {}
    # Accumulated per (chapter_title, section_title) - the REAL prose text of
    # every TEXTUAL_BLOCK_TYPES block seen anywhere in that section, in
    # document order, regardless of whether it sits before or after the
    # image block(s) that will later be verified against it (see
    # _SOURCE_EXCERPT_CHAR_CAP's own comment on why "the whole section", not
    # one preceding paragraph). A single extra pass-sized dict, not a second
    # walk - built in the SAME walk as the existing chapter/section/key-
    # concept bookkeeping above.
    section_prose: dict[tuple[str, str], list[str]] = {}
    for _, block in document.iter_blocks():
        if block.type is BlockType.HEADING:
            text = str(block.content.get("text") or "").strip()
            level = block.content.get("level") or 2
            if text:
                if level <= 1:
                    chapter_title = text
                    section_title = ""  # a new chapter starts with no section yet
                else:
                    section_title = text
        titles[block.id] = (chapter_title, section_title)
        if not chapter_title:
            continue
        if block.type is BlockType.LEARNING_OBJECTIVES and chapter_title not in chapter_key_concept:
            items = [str(item).strip() for item in (block.content.get("items") or []) if str(item).strip()]
            if items:
                chapter_key_concept[chapter_title] = items[0][:_KEY_CONCEPT_CHAR_CAP]
        elif block.type is BlockType.SUMMARY and chapter_title not in chapter_fallback_concept:
            takeaways = [
                str(item).strip() for item in (block.content.get("key_takeaways") or []) if str(item).strip()
            ]
            if takeaways:
                chapter_fallback_concept[chapter_title] = takeaways[0][:_KEY_CONCEPT_CHAR_CAP]
        if block.type in TEXTUAL_BLOCK_TYPES:
            text = str(block.content.get("text") or "").strip()
            if text:
                section_prose.setdefault((chapter_title, section_title), []).append(text)
    excerpt_cache: dict[tuple[str, str], str] = {
        key: "\n\n".join(parts)[:_SOURCE_EXCERPT_CHAR_CAP] for key, parts in section_prose.items()
    }
    return {
        block_id: (
            ch, se, chapter_key_concept.get(ch) or chapter_fallback_concept.get(ch, ""),
            excerpt_cache.get((ch, se), ""),
        )
        for block_id, (ch, se) in titles.items()
    }

# PIL's own `Image.format` value (always uppercase, e.g. "JPEG" - never
# "JPG") -> the file extension that format should be saved/served under -
# see ImageService._detect_extension. Anything not listed here (an unknown
# or unsupported format) falls back to "png" there, never raises.
_IMAGE_FORMAT_EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}

# Appended when an image absolutely must not risk baked-in text - see
# _generate_with_text_check's final fallback and _generate_illustration's
# `force_text_free` path. A text-free image is trivially, guaranteedly free
# of text errors; a raster model asked to spell a dozen flowchart-style
# labels is not.
_NO_TEXT_SUFFIX = (
    "\n\nIMPORTANT: do not render any text, labels, letters, numbers or "
    "writing anywhere in the image - a completely text-free illustration."
)

# Substrings that show up in the provider's own error body for a content-
# policy rejection (confirmed real case: Azure FLUX.2-flex returning
# `{"error":{"code":"content_safety_violation","message":"Content violated
# RAI policy blocking criteria (DallEBlockList_Prompt)."}}`, wrapped into
# AIServiceError's message by azure_image_provider.py/azure_mai_image_
# provider.py's `f"...request failed with status {code}: {body}"`). Checked
# as a plain substring match (not a dedicated exception type) so this works
# the same for either provider without either one needing to know about a
# safety-specific exception class - the raw body text already carries the
# signal, consistently, across both.
_CONTENT_SAFETY_MARKERS = ("content_safety_violation", "content safety", "rai policy")


def _is_content_safety_violation(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _CONTENT_SAFETY_MARKERS)


class ImageTextCheck(BaseModel):
    """What a vision-capable model reports actually seeing rendered in a
    generated image - see ImageService._verify_generated_text. Deliberately
    just a flat list of whatever text is visible, exactly as it appears
    (typos included) - the ground truth to compare against is
    `expected_labels` on the block itself, not anything this schema knows
    about the topic."""

    detected_text: list[str] = Field(default_factory=list)


class ImageRelevanceCheck(BaseModel):
    """A vision-capable model's judgment on whether a generated image
    actually depicts its own brief - see ImageService._verify_image_relevance.
    A model-based judgment call (never deterministic, unlike formula_qa's
    AST comparison or diagram_content_qa's structural checks): there is no
    pixel-level ground truth for "is this about photosynthesis" without
    real object recognition, which this codebase doesn't have. `on_topic`/
    `genuinely_educational` default to True so a vision-call FAILURE never
    masquerades as a CONFIRMED relevance problem (a false "off-topic" claim
    the image never earned) - but `errored` (production risk follow-up)
    separately, explicitly records that the check could not actually run,
    so a caller can still tell "genuinely verified clean" from "never
    verified at all" instead of conflating the two into one confident-
    looking default."""

    on_topic: bool = True
    on_topic_detail: str = ""
    genuinely_educational: bool = True
    educational_detail: str = ""
    # A real, confirmed gap: `_verify_generated_text` only checks that each
    # expected label's TEXT appears somewhere, verbatim - it has no notion
    # of WHERE on the canvas it landed. A labelled illustration can pass
    # that check while two callouts are drawn directly on top of each other
    # (or on top of the image's own title/caption), unreadable - confirmed
    # on a real generated "rigid-body rotation" diagram whose "omega (axis)"
    # and "Point P" callouts collided with other labels. Defaults to True so
    # a text-free image (nothing to collide) never fails this for free, and
    # so a vision-call failure (`errored=True`) never masquerades as a
    # confirmed layout defect either - same contract as `on_topic`/
    # `genuinely_educational`.
    labels_legible: bool = True
    labels_legible_detail: str = ""
    # Only asked about (and only ever False) when this was a
    # `illustration_style: "textbook"` generation - see
    # `_verify_image_relevance`'s `textbook_style` parameter. MAI-Image-2.6
    # (and every other raster provider this pipeline uses) has no negative-
    # prompt/style parameter at the API level - the flat 2D vector, no-
    # photorealism, no-3D-render, nothing-cropped-at-the-edge style
    # contract (TEXTBOOK_ILLUSTRATION_GUIDANCE below) lives entirely in
    # prompt text with no model-level enforcement, so this is the only
    # mechanism that can catch - and automatically retry - the model
    # drifting toward a photorealistic/3D/cropped result despite that
    # instruction. Defaults to True for the same "a vision-call failure
    # never masquerades as a confirmed defect" reason `on_topic`/
    # `genuinely_educational` already document.
    style_compliant: bool = True
    style_compliant_detail: str = ""
    # Visual-generation-quality fix: closes a real gap - `_verify_generated_
    # text` can only check spelling against an EXPECTED label list, so a
    # `textbook`-style image whose block never had `image_expected_labels`
    # populated (the writer's own field, not always filled in) previously
    # shipped with its baked-in text completely unverified. This reuses the
    # SAME existing vision call (zero new LLM calls) to ask, generally,
    # whether any text actually visible in the image is garbled/misspelled -
    # no ground-truth list required, so it runs unconditionally for every
    # `textbook_style` image regardless of whether expected_labels was ever
    # set. Only ever asked about for `textbook_style` (every other style
    # carries its own STRICT NO-TEXT RULE and is separately confirmed
    # text-free by `_confirm_text_free`). Same safe-default contract as
    # `style_compliant`/`on_topic`: defaults True so a vision-call failure
    # never masquerades as a confirmed defect.
    text_accurately_spelled: bool = True
    text_accurately_spelled_detail: str = ""
    errored: bool = False
    # Canonical Visual Spec milestone - source-grounding, ONLY ever asked
    # about when a real `source_excerpt` (the course's own chapter prose)
    # was available to judge against; both default to the safe "nothing
    # wrong" value when no excerpt was given (see `_verify_image_relevance`'s
    # own source-grounding section). IMPORTANT, and distinct from every
    # other field above: this is NEVER a claim that the image (or the
    # course's own text) is scientifically correct - only whether the
    # image's depicted claims are CONSISTENT WITH what the course's own
    # prose already says. A real scientific error already present in the
    # source text would not be caught here; that is a different, harder
    # problem this check deliberately does not attempt (see `diagram_
    # content_qa`'s matching fields for the full reasoning, shared verbatim).
    contradicts_source_text: bool = False
    contradicts_source_text_detail: str = ""
    unsupported_claims: list[str] = Field(default_factory=list)


class SuggestedOverlayLabel(BaseModel):
    text: str = ""
    x_pct: float = 50.0
    y_pct: float = 50.0
    style: str = "label"  # "title" | "callout" | "label"


class OverlayLabelSuggestions(BaseModel):
    """Edit Visuals Part 2 - a vision-capable model's suggested default
    overlay labels for a just-generated, text-free illustration (see
    ImageService._suggest_overlay_labels). Best-effort and always optional:
    a person edits/replaces/clears these freely in the editor afterward, and
    an empty list here (a blank brief, a disabled setting, or a failed call)
    is never an error - it just means the course ships with no pre-suggested
    labels."""

    labels: list[SuggestedOverlayLabel] = Field(default_factory=list)


# Fixed, universal guidance for the DEFAULT ("" / decorative) illustration
# style - see ImageContent.illustration_style. Replaces the old per-template
# `CourseTemplate.image_guidance` (still used, unchanged, to inform the
# WRITER's own higher-level brief-writing in app.agents.prompts - only the
# final, generation-time prompt this constant feeds into changed). The
# architecturally cleaner fix for the AI-garbled-text problem this session
# spent several turns hardening a verify-and-retry system around: an
# illustration that never asks for ANY text in the first place can never
# garble it, by construction, rather than hoping a generation attempt spells
# things right and catching it after the fact. The precise terms a reader
# needs still come from the surrounding course text and the deterministic
# diagram/concept_experience visual this illustration is a companion to,
# same as always - this constant governs only the purely decorative,
# non-"textbook" illustration case.
DEFAULT_ILLUSTRATION_GUIDANCE = """\
PRIORITY: educational meaning > concept clarity > correct visual relationships > textbook-quality composition > visual engagement > aesthetics. A beautiful image that does not teach the concept is a failure.

TEXTBOOK-QUALITY, NOT GENERIC AI ART
- Look like a modern textbook diagram or educational infographic designed for this concept, not a stock illustration or generic AI-art look. Every major element must represent something real from the topic, never decoration that teaches nothing. Pick the structure the concept needs - process, cycle, timeline, cross-section, comparison, mechanism, cause-effect, hierarchy, concept map, graph/geometry, layered diagram, or a hybrid - never the same card/grid or "title + boxes + arrows" by default; make a process's transformation, a comparison's differences, a hierarchy's parent/child, or a timeline's order visually obvious.
- Visually interesting without becoming decorative: vary shape, scale and grouping only where it helps understanding - every flourish must serve the concept, never clutter.

CONCEPT CLARITY FIRST
- Identify THIS concept's real components and relationship, then depict THAT with recognisable shapes/objects for its actual parts - a student should infer the core idea from the image alone.
- Banned as the primary subject unless genuinely part of THIS concept: target/bullseye, checkmark, trophy, light bulb, generic people, a generic laptop/browser/code-screen UI mockup (empty window chrome or wireframe blocks), generic gears, vague glowing/overlapping circles, random 3D shapes, or any filler unconnected to the concept's own components. A real screen/terminal is fine when it's the genuine artifact being taught AND shows concrete, concept-specific content - never bare chrome. Small/supporting images follow this same rule - reinforce one real entity/step/relationship, never random decoration.

RELATIONSHIP DIRECTION MUST BE CORRECT
- For any comparison, increase/decrease, faster/slower, larger/smaller or causal/physical relationship: arrows, motion, size and position must match the ACTUAL direction, never one readable as the opposite.
- Show compared cases as distinct, side by side - not one pushing/causing the other - unless interaction IS the concept. E.g. same force on two masses: the lower-mass object shows the greater motion, never the higher-mass one, and never as a collision.

STRICT NO-TEXT RULE
- Zero text anywhere, including on screens/books/signage (show blank) - no words, numbers, equations or writing-like symbols. Shape, position, grouping and color must carry every distinction a label would (image models routinely garble baked-in text).

STYLE
- Flat vector or clean semi-realistic illustration, bright professional colors, the concept's own components as the focal subject, soft-gradient background, high contrast, landscape 16:9, nothing cropped.

ANY TOPIC - substitute the topic's own real parts, never leave these generic
- Technical: real components connected as they relate (a function wrapped by a decorator, a client/server exchange) as nodes/arrows/layered boxes.
- Science/math: the real phenomenon, apparatus or relationship involved - a cell, a molecule, glassware, a shape/curve - never a generic icon.
- History/social studies: scenes, artifacts, people in period-appropriate settings specific to the actual event/era.
- Language/business/arts/health/general: a scene, object or interaction genuinely tied to THIS concept, not a stock stand-in.

FINAL CHECK, all must pass: (1) premium textbook diagram, not generic AI art? (2) pasted onto an unrelated chapter, still looks at home - too generic? (3) caption/heading hidden, can a student still guess what this teaches? (4) every element doing real educational work, not filling space? Confirm zero text anywhere.\
"""

# Fixed (not per-template) guidance for a "textbook" companion illustration -
# see ImageContent.illustration_style. Deliberately the opposite of a
# template's own image_guidance (which favours flat/decorative style for the
# ordinary illustration case): this picture's whole job is to be an accurate,
# recognisable depiction of the actual subject (an organ, an experiment
# apparatus, a molecule), placed alongside - never instead of - the
# structured diagram/concept_experience visual that already explains its
# structure, so it needs its own, different style contract.
TEXTBOOK_ILLUSTRATION_GUIDANCE = (
    "STYLE, exactly: a flat 2D vector illustration / modern educational "
    "infographic, the kind printed in a well-produced digital textbook or "
    "e-learning platform - crisp clean line art, solid or simply-shaded flat "
    "colors, no photorealistic shading, no 3D render, no soft ambient "
    "studio lighting, no moody/atmospheric glow. This is a firm style "
    "requirement, not a suggestion - a semi-realistic or 3D-rendered picture "
    "fails the brief even if otherwise accurate. Depict the real subject "
    "clearly (the apparatus, organism, structure or mechanism itself), not "
    "an abstract diagram of boxes and arrows standing in for it.\n"
    "BACKGROUND: pure white or one single soft pastel tint, completely flat "
    "and solid - never a dark background, never a vignette, never a glow or "
    "gradient behind the subject. If the composition uses multiple panels "
    "(see below), each panel may use its own distinct pale pastel tint "
    "(e.g. pale blue, pale lavender, pale mint) to tell them apart, still "
    "flat and solid, never a gradient within a single panel.\n"
    "LABELS: default to small rounded callout-box labels (short pill-shaped "
    "boxes with a thin leader line connecting each one to the exact part/"
    "vector/direction it names) rather than text floating loosely on the "
    "picture - the real subject stays the focal illustration, the callouts "
    "are a light annotation layer on top of it. Label each distinct part "
    "ONCE, at its clearest instance - never repeat the same label on every "
    "occurrence of a repeated element.\n"
    "MULTI-PANEL COMPOSITIONS: when the brief covers several closely related "
    "sub-concepts that build on each other (e.g. a field around a source, "
    "then the effect that field produces, then the governing law - or a "
    "topic with several standard sub-sections, like a machine's principle, "
    "its governing rule, its output waveform and its energy conversion), "
    "compose them as separate bordered panels arranged in a clean grid "
    "within the one image, each panel with its own short, bold section "
    "title in a colored header strip at its top (e.g. \"Magnetic Field\", "
    "\"Induced Current\") and its own pastel background tint - this "
    "in-panel section title is a structural label for that panel's own "
    "sub-topic, not the image's overall course/chapter caption, so it's "
    "expected and welcome here even though the rule below still applies to "
    "the course/chapter name itself. Use as many panels as the subject "
    "genuinely organizes into - a simple single-mechanism concept needs "
    "only one panel, while a multi-stage process or a topic with several "
    "real, distinct sub-sections may need four or more; never pad a simple "
    "concept with extra panels it doesn't need, and never cram several "
    "unrelated sub-concepts into one panel just to keep the panel count "
    "low.\n"
    "FORMULA BLOCK: if the concept has one key governing equation, you may "
    "include it once, in its own clearly bordered box (distinct from the "
    "illustration panels), set in large clean mathematical notation, with "
    "each symbol it uses defined immediately below in short \"symbol = "
    "meaning\" lines (exactly like a textbook's own equation callout) - "
    "spell every defined term correctly and completely, nothing truncated "
    "or cut off. Its title badge sits in its own strip ABOVE the equation, "
    "never overlapping the equation or any symbol - a confirmed failure "
    "rendered a \"Snell's law\" badge directly over its own equation. No "
    "label may ever overlap another label, a title, or the equation.\n"
    "Avoid unnecessary or excessive text beyond callouts/panel titles/the "
    "formula block - never paragraphs baked into the image. Do not render "
    "the overall course/chapter/subject name anywhere in the image - the "
    "page's own caption is shown separately outside the picture; this is "
    "different from a multi-panel composition's own short per-panel "
    "section titles, which belong inside the image as described above.\n"
    "CRITICAL FRAMING RULE: every element (panels, icons, shapes, callout "
    "boxes, text, the formula block) must sit with clear, generous margin "
    "on all four sides of the canvas and fit completely inside the frame; "
    "nothing may touch, crowd or extend past any edge. A real, confirmed "
    "failure mode: a prompt asking for several side-by-side items (e.g. 3-4 "
    "icon+label groups in a row) rendered wider than the canvas could hold, "
    "so the first and last items were sliced off at the left/right edges "
    "mid-way through their own labels. If the subject naturally has several "
    "distinct parts to show side by side, either keep the count small "
    "enough (2-3 at most) to comfortably fit with margin to spare, or "
    "arrange them in a grid/stack (or as the separate panels described "
    "above) instead of a single wide row - never let the composition's own "
    "content decide the canvas is too small after the fact."
)


# Fixed (not per-template) guidance for a "section_intro" illustration - see
# ImageContent.illustration_style. The writer's own `image_prompt` for this
# style already follows the exact wording contract (<=40 words, one clear
# subject, ends with "no text, no letters, no labels" - see WRITER_SYSTEM).
# This constant used to also pin every section_intro image to one fixed
# dark-blue/teal palette "for consistency" - dropped because, across a real
# course, that made every section's icon look like the same reused neon
# asset regardless of subject (a confirmed, reported problem), which is a
# worse outcome than a style that varies by topic the way DEFAULT_
# ILLUSTRATION_GUIDANCE's illustrations already do. Only the genuinely
# cross-topic rules (no text, one clear centred subject) remain fixed here.
SECTION_INTRO_ILLUSTRATION_GUIDANCE = (
    "STRICT NO-TEXT RULE: the image must contain zero text of any kind - no "
    "words, letters, numbers, labels, captions, titles, watermarks or logos, "
    "and no writing rendered on any object, screen, sign or surface within "
    "the scene either.\n"
    "STYLE: clean modern digital illustration, with colors and tone that "
    "genuinely fit this specific subject - never the same fixed palette "
    "reused across unrelated topics. One single clear subject, centred, "
    "with no clutter and no unnecessary background detail.\n"
    "If the subject above is a symbolic/abstract mechanism with no "
    "physical form (code, data structures, algorithms, math), it has "
    "already been translated into one concrete object or scene that "
    "behaves the same way - draw exactly THAT object/scene, specific and "
    "recognisable, never a generic glowing shape, bar, orb or abstract "
    "icon standing in for it."
)


class ImageService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.storage = storage or get_storage()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()
        self.diagrams = DiagramService(self.ai, self.storage, self.settings)
        self.concept_visuals = ConceptVisualService(self.ai, self.storage, self.settings, self.memory)

    # --- prompt -----------------------------------------------------------
    @staticmethod
    def build_prompt(
        block: Block,
        template: CourseTemplate,
        course_title: str,
        *,
        chapter_title: str = "",
        section_title: str = "",
        key_concept: str = "",
    ) -> str:
        """`chapter_title`/`section_title`/`key_concept` (see
        `chapter_section_context`) are the deterministic fix for a real,
        confirmed failure mode: the writer LLM sees full chapter/course
        context when it invents `image_prompt`/`purpose`, but that's free
        text it may or may not actually use - if it writes something vague
        (or leaves the field blank, in which case `draft_to_content` falls
        back to the block's own generic title like "Learning Objectives"),
        the image model previously received NO chapter/subject signal at
        all and produced a plausible-but-generic result. This line
        guarantees every request structurally carries that context
        regardless of what the writer's own free text happened to say -
        never a hardcoded per-subject mapping, since it's built from
        exactly the chapter/section titles (and, for `key_concept`, the
        chapter's own learning objective) already written into this
        document, for whatever subject that is.

        `key_concept` specifically closes a narrower, still-real gap: a
        chapter TITLE alone ("Newton's Laws of Motion") tells the model
        WHICH chapter, never WHAT TO ACTUALLY DEPICT - a generic "physics"
        visual technically satisfies the chapter noun. One concrete,
        already-written objective sentence ("explain how force relates to
        acceleration") gives the model an actual mechanism to draw, not
        just a subject label."""
        content = block.content
        style = (content.get("illustration_style") or "").strip().lower()
        if style == "textbook":
            style_guidance = TEXTBOOK_ILLUSTRATION_GUIDANCE
        elif style == "section_intro":
            style_guidance = SECTION_INTRO_ILLUSTRATION_GUIDANCE
        else:
            style_guidance = DEFAULT_ILLUSTRATION_GUIDANCE
        context_bits = []
        if course_title.strip():
            context_bits.append(f"Course: {course_title.strip()[:_CONTEXT_FIELD_CHAR_CAP]}")
        if chapter_title.strip():
            context_bits.append(f"Chapter: {chapter_title.strip()[:_CONTEXT_FIELD_CHAR_CAP]}")
        if section_title.strip():
            context_bits.append(f"Section: {section_title.strip()[:_CONTEXT_FIELD_CHAR_CAP]}")
        if key_concept.strip():
            context_bits.append(f"This chapter teaches: {key_concept.strip()[:_KEY_CONCEPT_CHAR_CAP]}")
        # Never a bare "for the course 'X'" framing line - a real
        # illustration model tends to read a quoted subject name as a title
        # to render literally into the picture (confirmed: without a
        # qualifier like this, a "textbook" heart illustration once came
        # back with a "HUMAN BIOLOGY" banner baked across the top). The
        # explicit "do not render" instruction travels WITH the context
        # itself, never relying only on the general no-text rule below.
        context_line = (
            "Educational context, for your understanding only - never render any of this "
            "as visible text, a title or a banner in the image - " + " | ".join(context_bits)
            if context_bits
            else ""
        )
        # `expected_labels` was previously used ONLY to verify the finished
        # image after the fact (see _verify_generated_text) - never actually
        # told the image model to draw them in the first place. The writer's
        # own free-text `image_prompt` might describe the scene without
        # explicitly asking for every specific label, so a well-populated
        # `expected_labels` list could still ship unlabelled (a real,
        # confirmed case: a magnetic-flux apparatus illustration rendered
        # with unlabelled arrows). Stating the exact expected words directly
        # in the generation prompt closes that gap - the verify/retry loop
        # below still catches a misspelling or omission either way. Scoped
        # to style == "textbook" specifically - the only style this field is
        # actually wired up for (see `_generate_illustration`'s `is_textbook
        # and expected_labels` branch); `section_intro` and the default style
        # both carry their own STRICT NO-TEXT RULE, so injecting a "must
        # include these labels" line there would directly contradict it if
        # `expected_labels` were ever populated by mistake.
        expected_labels = (
            sanitize_expected_labels(
                [str(label).strip() for label in (content.get("expected_labels") or []) if str(label).strip()]
            )
            if style == "textbook"
            else []
        )
        labels_line = (
            "This image must include these exact labels, spelled correctly and clearly "
            "legible, placed next to the part/vector/direction each one names: "
            + ", ".join(expected_labels)
            + ". Label each one ONCE, at its clearest instance - never repeat the same label "
            "on every occurrence of a repeated element (e.g. one field line out of a drawn "
            "group, not every line in it). These are the only labels to add - the picture "
            "must still read as clean and uncluttered, exactly as the style below describes, "
            "not covered in repeated text."
            if expected_labels
            else ""
        )
        # Priority-based assembly (see this module's own "image-generation
        # prompt budget" section) - never a flat join-then-slice.
        #
        # Visual-generation-quality fix: this used to rank `writer_prompt`
        # (the writer's own subject-specific content - the actual diagram
        # structure, components, relationships and scientific detail) at
        # the SAME lowest-protected tier as the generic `context` line,
        # reasoning it was "the most variable, least load-bearing-for-
        # correctness content in the prompt" - backwards for what this app
        # actually needs: under budget pressure, the one thing that should
        # never be the first casualty is the content that makes THIS image
        # teach THIS concept.
        #
        # `expected_labels` (the exact labels required) is promoted to
        # Priority 1 - its length is inherently bounded (at most
        # `_MAX_EXPECTED_LABELS` short entries, each capped at
        # `_MAX_LABEL_CHARS`), so protecting it fully can never risk
        # blowing the hard provider character budget the way an unbounded
        # free-text field could.
        #
        # `writer_prompt` (the writer's own free-text description - the
        # actual subject, structure, components and relationships) stays at
        # Priority 3 - a writer-authored string has no length ceiling, so it
        # must remain gracefully shortenable (`_shorten_to_budget`, word/
        # sentence-boundary aware, never dropped outright) to preserve the
        # hard "never exceed the provider's real character budget"
        # guarantee (see the `build_prompt` worst-case tests below). What
        # changes is its ORDER relative to `context`: it is listed BEFORE
        # `context` now, and `_assemble_prompt_with_priority`'s own Priority-
        # 3 handling grants EARLIER segments first claim on whatever budget
        # remains - so `writer_prompt` is shortened last and least, and
        # `context` (a synthesized course/chapter/section/key_concept line -
        # genuinely redundant restatement of information the writer's own
        # prompt already carries in substance) absorbs the cut first and
        # most. In practice the total of all segments is almost always far
        # under the 16,000-char budget, so this reordering is a correctness
        # fix for the rare edge case, not something that visibly changes
        # most prompts today.
        segments = [
            _PromptSegment("style_guidance", style_guidance, priority=1),
            _PromptSegment(
                "negative_constraints", "Do not include watermarks, signatures or lorem ipsum text.", priority=1,
            ),
            _PromptSegment("expected_labels", labels_line, priority=1),
            _PromptSegment(
                "writer_prompt",
                content.get("prompt") or content.get("purpose") or content.get("caption") or "",
                priority=3,
            ),
            _PromptSegment("context", context_line, priority=3),
        ]
        result = _assemble_prompt_with_priority(segments)
        _validate_prompt_assembly(result, style=style)
        return result.prompt

    @staticmethod
    def _dimensions(data: bytes) -> tuple[int | None, int | None]:
        """Intrinsic size so the layout engine can reserve the right box."""
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                return image.width, image.height
        except Exception:  # pragma: no cover - never block on metadata
            return None, None

    @staticmethod
    def _detect_extension(data: bytes) -> str:
        """The real encoded format of `data`, mapped to the file extension
        it should be saved/served under - OpenAI's gpt-image-1 always
        returns PNG, but a real, confirmed case: Azure's FLUX.2-flex (see
        azure_image_provider.py) returns JPEG-encoded bytes by default,
        with no explicit request for one. Saving every illustration under
        a hardcoded ".png" regardless of its actual encoding left a real
        mismatch between a file's extension and its content - and, since
        the asset-serving route infers its Content-Type header from the
        extension alone, a wrong HTTP header too. Never re-encodes the
        image itself (see `_generate_illustration`, which saves `data`
        completely unchanged) - this only inspects it to pick the correct
        extension. Falls back to "png" - the existing, still-correct
        default for the OpenAI path - for anything unrecognised, so a
        provider's own output quirk can never crash generation."""
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                fmt = (image.format or "").upper()
        except Exception as exc:  # noqa: BLE001 - never block generation on a format-sniff failure
            log.warning("Could not detect the generated image's format - saving as png: %s", exc)
            return "png"
        return _IMAGE_FORMAT_EXTENSIONS.get(fmt, "png")

    # A section_intro illustration always displays at this exact small size
    # (see app.course.document.layout.SECTION_INTRO_IMAGE_WIDTH/ASPECT) - the
    # configured image model only accepts a small fixed set of REQUEST sizes
    # (1024x1024/1024x1536/1536x1024/auto for gpt-image-1), never an
    # arbitrary small one directly, so this always runs on the generated
    # image AFTER the fact rather than being requested up front.
    _SECTION_INTRO_WIDTH = 400
    _SECTION_INTRO_HEIGHT = 300

    @classmethod
    def _resize_section_intro(cls, data: bytes) -> bytes:
        """Center-crops to 4:3 then resizes to the fixed small target size,
        so the persisted asset's own intrinsic width/height (read back by
        `_dimensions` right after this) always exactly matches what
        `image_box_height`'s aspect-ratio math expects - never a mismatch
        that would leave the PDF export's reserved box a slightly wrong
        size for the actual picture."""
        try:
            from io import BytesIO

            from PIL import Image

            target_w, target_h = cls._SECTION_INTRO_WIDTH, cls._SECTION_INTRO_HEIGHT
            target_ratio = target_w / target_h
            with Image.open(BytesIO(data)) as image:
                image = image.convert("RGB")
                w, h = image.size
                current_ratio = w / h
                if current_ratio > target_ratio:
                    new_w = round(h * target_ratio)
                    left = (w - new_w) // 2
                    image = image.crop((left, 0, left + new_w, h))
                elif current_ratio < target_ratio:
                    new_h = round(w / target_ratio)
                    top = (h - new_h) // 2
                    image = image.crop((0, top, w, top + new_h))
                image = image.resize((target_w, target_h), Image.LANCZOS)
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001 - never block on a resize failure
            log.warning("section_intro resize failed, shipping the original size instead: %s", exc)
            return data

    # --- text accuracy ------------------------------------------------------
    async def _verify_generated_text(self, image: bytes, expected_labels: list[str]) -> list[str] | None:
        """Asks a vision-capable model to read back every piece of text it
        can actually see in `image`, then reports which of `expected_labels`
        never showed up verbatim (case/whitespace-insensitive) - a missing
        label means either it's absent or - just as likely, since image
        models garble baked-in text - misspelled into something that no
        longer matches. Returns the empty list when everything expected was
        genuinely confirmed found; never raises (a vision-check failure is
        never treated as "confirmed wrong" either - a genuinely garbled
        image gets a real chance to be flagged the NEXT time this runs).
        Returns `None` - never `[]` - when the check itself could not
        complete (the vision call raised): production risk follow-up, a
        real, confirmed prior bug - `None` collapsing to the SAME falsy
        value as `[]` let an operational vision-check failure be silently
        treated as "every label verified correct." Every caller must check
        `missing == []` for a genuine pass, never a bare `not missing`.

        Deliberately never shows `expected_labels` to the model doing the
        transcribing - a confirmed real failure (a "Chlo-rlorphyit" render
        was reported back as the clean "Chloroplast") traced to exactly
        that: telling the model what the text is "supposed to be" right
        before asking it to read the text primes it to report what it
        expects rather than what is actually rendered, the same
        confirmation-bias failure mode a human proofreader has reading their
        own writing. The comparison against `expected_labels` happens
        afterwards, in plain Python, against a transcription the model
        produced with no knowledge of what "correct" looks like."""
        if not expected_labels:
            return []
        normalize = lambda s: " ".join(s.strip().lower().split())  # noqa: E731
        try:
            result = await self.ai.structured(
                schema=ImageTextCheck,
                system=(
                    "You inspect an image for text accuracy. List every distinct "
                    "piece of text visible in it, transcribed exactly as it "
                    "actually appears letter-by-letter - including any spelling "
                    "mistakes, garbled letters, reversed characters or duplicated "
                    "words. You do not know what the text is supposed to say; "
                    "only report what is literally rendered, even if it looks "
                    "like a misspelled or nonsensical word."
                ),
                user="Transcribe every piece of text you can actually read in the attached image.",
                model=self.settings.diagram_model,
                purpose="image_text_check",
                phase="image",
                image=image,
            )
        except Exception as exc:  # noqa: BLE001 - never block on a QA-step failure
            log.warning("Image text check failed (inconclusive, never treated as a pass): %s", exc)
            return None
        detected = {normalize(text) for text in result.detected_text}
        return [label for label in expected_labels if normalize(label) not in detected]

    async def _confirm_text_free(self, data: bytes) -> list[str] | None:
        """Root-cause fix (production course audit, ThermoDynamics/
        doc_a2f5625a7fd9): a SINGLE vision call reporting "no text found" is
        not reliable enough to trust on its own for a result this
        consequential. Confirmed, reproduced case: block_aab38b60c031's
        shipped image (`assets/image_001.jpg`) has "Precise language &
        coniJt consistent sign conventions" and "undergraduate
        thermodynamics; mullivtrabile calculus" baked in, plainly visible -
        yet it was persisted with `text_verified: true`. Re-running
        `_verify_image_has_no_text` against that EXACT SAME image bytes
        after the fact correctly detects all 14 pieces of garbled text on
        every fresh call - proving the check function itself works; the
        failure was a false-negative on ONE of the (at most two) vision
        calls actually made during the original generation, i.e. LLM
        vision-judgment variance, not a logic bug. The costs of being wrong
        are asymmetric: a false POSITIVE (reporting text that isn't there)
        only wastes one extra vision call here; a false NEGATIVE (reporting
        clean when text is baked in) ships exactly the misleading content
        this whole verification exists to prevent - so a `[]` ("clean")
        first result is re-checked with one independent second opinion
        before being trusted, while any non-empty first result is accepted
        immediately (it has already failed; no need to spend a second call
        confirming that)."""
        first = await self._verify_image_has_no_text(data)
        if first != []:
            return first
        return await self._verify_image_has_no_text(data)

    async def _confirm_labels_correct(self, data: bytes, expected_labels: list[str]) -> list[str] | None:
        """The `_verify_generated_text` sibling of `_confirm_text_free` -
        same reasoning, same asymmetric-cost justification for the extra
        call, applied to the labelled-illustration path instead of the
        text-free one."""
        first = await self._verify_generated_text(data, expected_labels)
        if first != []:
            return first
        return await self._verify_generated_text(data, expected_labels)

    async def _verify_image_has_no_text(self, image: bytes) -> list[str] | None:
        """For a force-text-free image (see `_generate_text_free_illustration`)
        - the same vision-model transcription technique `_verify_generated_text`
        uses, inverted: there's no specific label to check against here, the
        ask is simply "did ANY text survive onto an image that was explicitly
        requested text-free". The original request behind a text-free
        fallback is always a labelled diagram/flowchart a raster model
        couldn't reliably spell (see `generate_for_block`'s own docstring on
        `structured_fallback`) - so any text that does show up is
        presumptively more of that same garbled, unreliable lettering, not a
        message worth keeping. Returns whatever text was actually detected
        (empty list = genuinely confirmed text-free); never raises. Returns
        `None` - never `[]` - when the check itself could not complete
        (production risk follow-up: a vision-call exception must never be
        silently treated as "confirmed genuinely text-free," the same
        ERROR-vs-PASS conflation fixed in `_verify_generated_text`). Callers
        must check `found == []` for a genuine pass, never a bare `not
        found`."""
        try:
            result = await self.ai.structured(
                schema=ImageTextCheck,
                system=(
                    "You inspect an image to check whether it contains ANY readable "
                    "text at all - words, labels, letters, numbers, captions, "
                    "anything resembling writing, however small or faint. List every "
                    "distinct piece of text you can see, even a single short word or "
                    "a garbled/partial one. If the image is genuinely free of any "
                    "text, report an empty list."
                ),
                user="List every piece of text visible in the attached image, or an empty list if there is none.",
                model=self.settings.diagram_model,
                purpose="image_no_text_check",
                phase="image",
                image=image,
            )
        except Exception as exc:  # noqa: BLE001 - never block on a QA-step failure
            log.warning("Image no-text check failed (inconclusive, never treated as confirmed clean): %s", exc)
            return None
        return [text for text in result.detected_text if text.strip()]

    async def _verify_image_relevance(
        self, image: bytes, *, brief: str, course_title: str, textbook_style: bool = False,
        source_excerpt: str = "",
    ) -> ImageRelevanceCheck:
        """Does this generated image actually depict its own brief, and does
        it read as genuinely educational rather than purely decorative or
        unrelated? Unlike `_verify_generated_text` (a plain string-match
        against a transcription), there is no deterministic ground truth
        for this - it is necessarily a vision-model judgment call, the
        image-pipeline counterpart to `diagram_content_qa.evaluate_content`'s
        LLM-judged semantic check for diagrams. Never raises (a vision-check
        failure returns `errored=True`, never a confirmed "off-topic" claim
        the image never earned - but also never silently indistinguishable
        from a genuine pass; see `ImageRelevanceCheck.errored`'s own
        docstring, production risk follow-up).

        `textbook_style=True` adds a fourth judgment, `style_compliant` -
        see `ImageRelevanceCheck.style_compliant`'s own docstring for why
        this is the only check that can catch a MAI-Image-2.6 generation
        drifting away from the required flat-vector textbook look (no
        negative-prompt/style parameter exists at the API level to enforce
        it instead).

        `source_excerpt` (Canonical Visual Spec milestone), when given, adds
        the source-grounding judgment (`contradicts_source_text`/
        `unsupported_claims`) to this SAME call - zero new API calls for the
        common case, the same reasoning `diagram_content_qa.evaluate_content`
        documents for its own matching fields. Blank (the default, and the
        deliberate no-op for a `section_intro` illustration - see this
        method's own caller) skips asking the question at all, exactly like
        an empty `brief` already skips the whole call."""
        if not brief.strip():
            return ImageRelevanceCheck()
        style_question = (
            " (4) `style_compliant` - this image was required to be a flat 2D vector textbook "
            "illustration (crisp line art, flat/simply-shaded colors, no photorealistic shading, "
            "no 3D render, no soft studio lighting or glow, nothing cropped at or crowding the "
            "canvas edge). Does it actually look that way? Answer false if it instead reads as "
            "photorealistic, 3D-rendered, has a glow/gradient/dark background, or has any panel, "
            "icon, label or text cut off at an edge."
            if textbook_style else ""
        )
        spelling_question = (
            " (5) `text_accurately_spelled` - if this image has ANY text, labels or callouts "
            "baked into it, is every piece of it spelled correctly and legible letter-by-letter - "
            "no garbled, reversed, duplicated or nonsensical characters (e.g. \"Irrlstior\" instead "
            "of \"Indicator\")? Answer false if any word is misspelled or garbled, true if there is "
            "no text at all or every piece of text is correctly spelled. Name the exact garbled "
            "word in `text_accurately_spelled_detail`."
            if textbook_style else ""
        )
        grounding_question = (
            " (6) `contradicts_source_text` - the course's own written text (below) describes "
            "this subject. Does this image depict any entity, relationship, direction, or value "
            "that DIRECTLY CONTRADICTS something the source text states (not merely absent from "
            "it - a reasonable illustrative detail a textbook author would add is NOT a "
            "contradiction)? Answer true only for a genuine, specific conflict, and name it "
            "exactly in `contradicts_source_text_detail`. Separately, list in `unsupported_"
            "claims` any specific thing the image clearly depicts as fact that the source text "
            "simply does not address either way (neither confirms nor denies) - leave this empty "
            "if the image's claims are all either confirmed by the source text or are ordinary, "
            "reasonable illustrative elaboration. IMPORTANT: you are judging CONSISTENCY with "
            "the course's own text, never independent scientific truth - if the source text "
            "itself were wrong, that is a different problem this judgment does not cover."
            if source_excerpt.strip() else ""
        )
        source_block = (
            f"\n\nCOURSE'S OWN TEXT (the ground truth to check consistency against - not an "
            f"external authority, just what this course itself already says):\n{source_excerpt}"
            if source_excerpt.strip() else ""
        )
        try:
            return await self.ai.structured(
                schema=ImageRelevanceCheck,
                system=(
                    "You are a strict reviewer judging whether a generated educational image "
                    "actually depicts what it was asked to depict. Look at the attached image "
                    "and judge three things: (1) `on_topic` - does the image's actual visual "
                    "content genuinely, specifically depict the stated brief, not a generic, "
                    "unrelated, or merely thematically-adjacent picture? (2) `genuinely_"
                    "educational` - does this read as a real teaching aid for the brief, not a "
                    "purely decorative/stock-photo-style image that happens not to contradict "
                    "it? (3) `labels_legible` - if the image has ANY text, labels or callout "
                    "boxes baked into it, is every one of them cleanly legible and clearly "
                    "separated - no two labels overlapping or colliding with each other, with a "
                    "title/caption bar, or with the picture's own linework? A label that is "
                    "partly covered by another label or box, or sits crammed on top of other "
                    "text so it's hard to read, means `labels_legible` is false. If the image "
                    "has no text at all, `labels_legible` is true by default - there is nothing "
                    "to collide." + style_question + spelling_question + grounding_question + " Be concrete in every "
                    "detail field: name exactly what the image actually shows and why that does "
                    "or doesn't match, never a bare \"could be better\"."
                ),
                user=f"COURSE: {course_title}\n\nBRIEF (what this image is supposed to depict):\n{brief}"
                f"{source_block}\n\nJudge the attached image against this brief.",
                model=self.settings.diagram_model,
                purpose="image_relevance_check",
                phase="image",
                image=image,
            )
        except Exception as exc:  # noqa: BLE001 - never block on a QA-step failure
            log.warning("Image relevance check failed (inconclusive, never treated as a pass): %s", exc)
            return ImageRelevanceCheck(errored=True)

    async def _suggest_overlay_labels(
        self, image: bytes, *, brief: str, course_title: str
    ) -> OverlayLabelSuggestions:
        """Edit Visuals Part 2 - default overlay-label suggestions for a
        just-generated, text-free illustration (the image itself carries no
        baked-in text; these are a separate, client-rendered layer - see
        ImageContent.overlay_labels). A vision-model judgment call, the same
        shape as `_verify_image_relevance` right above: blank brief is a
        no-op, never raises (a failed call returns an empty list, exactly
        like "no labels suggested" - this is always best-effort, a person
        can add/edit/remove labels by hand regardless)."""
        if not brief.strip():
            return OverlayLabelSuggestions()
        try:
            return await self.ai.structured(
                schema=OverlayLabelSuggestions,
                system=(
                    "You are annotating a generated educational illustration with a small set of "
                    "labels, the way a textbook caption-layer would - the picture itself has NO "
                    "text baked into it, so your labels are the only text a learner will see "
                    "pointing at its parts. Look at the attached image and name 2-5 short labels "
                    "(1-4 words each) for real, clearly visible components or parts of the image - "
                    "never a component that isn't actually depicted, never a generic restatement "
                    "of the whole image's subject. For each label, estimate `x_pct`/`y_pct` (0-100, "
                    "0,0 is the top-left corner of the image) pointing at roughly where that part "
                    "sits in the picture. Set `style` to \"title\" only for a single label naming "
                    "the whole picture's subject (at most one), \"callout\" for a label that needs "
                    "a short phrase, \"label\" (the default) for a single part name. If the image "
                    "has no clearly distinct labellable parts, return an empty list - never invent "
                    "one to fill a quota."
                ),
                user=f"COURSE: {course_title}\n\nBRIEF (what this image depicts):\n{brief}\n\n"
                "Suggest overlay labels for the attached image.",
                model=self.settings.diagram_model,
                purpose="image_overlay_suggestions",
                phase="image",
                image=image,
            )
        except Exception as exc:  # noqa: BLE001 - never block generation on a suggestion failure
            log.warning("Overlay label suggestion failed (no labels suggested, not an error): %s", exc)
            return OverlayLabelSuggestions()

    async def _generate_text_free_illustration(
        self, prompt: str, *, block_id: str, provider: str | None = None, size: str | None = None
    ) -> tuple[bytes, bool]:
        """Every illustration style that isn't `textbook` - `section_intro`
        and the default/blank style alike - carries its own STRICT NO-TEXT
        RULE (see SECTION_INTRO_ILLUSTRATION_GUIDANCE/
        DEFAULT_ILLUSTRATION_GUIDANCE), and so does the force-text-free
        diagram/concept_experience fallback (see `_generate_illustration`/
        `generate_for_block`) - but a raster image model does not reliably
        comply with "no text" 100% of the time (the same confirmed failure
        mode `_generate_with_text_check` already documents for the labelled
        path: an "Oxygen" label survived as "Oxgeen" on what was meant to be
        a text-free attempt). Trusting an unverified "text-free" image on
        faith risks shipping exactly the garbled lettering this rule exists
        to avoid - two real, confirmed cases: a flowchart-shaped fallback
        image with "coan oicts"/"medership" baked in, and - because
        `section_intro` previously had NO verification at all, unlike the
        force-text-free fallback - a set of small section-intro icons that
        shipped with "sourrces", "Fraith magntised field", "toleente",
        "Teretore", "Mechanician" baked in. Verified, with one bounded
        retry using a more emphatic prompt - never unlimited - and the
        honest `text_verified` result returned either way, the same
        contract `_generate_with_text_check` already keeps for the labelled
        path. A "clean" verdict from either bounded attempt is itself
        double-checked via `_confirm_text_free` before being trusted - see
        its own docstring for the real, confirmed false-negative case
        (block_aab38b60c031/doc_a2f5625a7fd9) this closes."""
        size = size or self.settings.image_size
        data = await self._draw(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size, provider=provider)
        found = await self._confirm_text_free(data)
        if found == []:
            return data, True

        log.info(
            "Text-free image for %s still unresolved (%s) - one final, more emphatic attempt",
            block_id, found if found is not None else "vision check inconclusive",
        )
        final_prompt = (
            f"{prompt}\n\nABSOLUTELY CRITICAL: this image must contain ZERO text of any kind - "
            "no letters, no words, no numbers, no labels, no captions, nothing resembling "
            "writing anywhere in the image, not even small or decorative text. A previous "
            "attempt still rendered some text - this time, render no text at all, full stop."
        )
        data = await self._draw(prompt=final_prompt, size=size, provider=provider)
        found = await self._confirm_text_free(data)
        if found == []:
            return data, True
        if found is None:
            log.warning(
                "Text-free image for %s could not be verified (vision check inconclusive) after every "
                "bounded attempt - never treated as confirmed clean", block_id,
            )
        else:
            log.warning(
                "Text-free image for %s still has text after every bounded attempt (%s) - "
                "flagged, not silently accepted as correct", block_id, found,
            )
        # Deliberately `found == []`, not `not found` - see `_generate_with_
        # text_check`'s identical final line for why `None` must never
        # collapse into the same "falsy -> verified" outcome as `[]`.
        return data, found == []

    # --- generation -------------------------------------------------------
    async def generate_for_block(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
        chapter_title: str = "",
        section_title: str = "",
        key_concept: str = "",
        source_excerpt: str = "",
    ) -> bool:
        """Diagram/concept_experience blocks try their structured path first
        and fall back to the raster illustration path on any failure - a
        block must never end up with nothing just because a specialised path
        had trouble.

        `source_excerpt` (Canonical Visual Spec milestone): the real chapter
        prose this block's visual sits alongside - see `chapter_section_
        context`'s own docstring for how it's gathered (the WHOLE section,
        not one paragraph). Threaded into the structured-diagram content-QA
        critique and the raster-illustration relevance check so both can
        verify a visual's claims are actually grounded in what the course
        itself says, not just internally well-formed or "on topic" in a
        generic sense - see `diagram_content_qa.evaluate_content`'s and
        `_verify_image_relevance`'s own docstrings for what this check does
        and, importantly, does NOT claim (source-consistency, never
        independent scientific truth). Blank is always a safe no-op -
        exactly the same degrade-gracefully contract `chapter_title`/
        `section_title`/`key_concept` already keep.
        """
        kind = block.content.get("kind")
        if kind == "toc":
            # A course-contents page (app.render.toc_renderer) is built once,
            # deterministically, alongside the rest of the document (see
            # app.course.document.builder._toc_page) - it needs the full
            # chapter list, which isn't available at this single-block level,
            # so there's nothing useful to (re)generate here. Reporting
            # success (rather than falling through to the AI illustration
            # path below) is what stops an edit that happens to clear this
            # block's `path` from silently replacing the colorful contents
            # grid with an unrelated raster image.
            return True
        if block.content.get("template_asset"):
            return self._reuse_template_asset(course_id=course_id, block=block, template=template)
        # Starts True for ANY diagram/concept_experience-kind block, not just
        # ones where generation was actually attempted-and-failed - a real,
        # confirmed gap: when `enable_diagram_generation`/
        # `enable_concept_experience_visuals` is off, the block never even
        # reaches the structured path below, so this must already assume
        # "this was never really an illustration request" before that check
        # runs, or a labelled-diagram-shaped prompt reaches the raster model
        # WITHOUT the text-free guard - the exact mechanism behind a real
        # observed garbled-text diagram. Only set back to False below, and
        # only for a genuine `kind == "illustration"` block from the start.
        structured_fallback = kind in ("diagram", "concept_experience")
        if kind == "diagram" and self.settings.enable_diagram_generation:
            ok = await self.diagrams.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title,
                source_excerpt=source_excerpt,
            )
            if ok:
                return True
            # Root-cause fix (Phase 2 card-grid audit): a retired-"schematic"
            # hint (see DiagramService.generate_for_block's own comment on
            # this exact rerouting) deliberately sets `illustration_style:
            # "textbook"` on the block BEFORE returning False here - that is
            # NOT a failed diagram attempt whose text-heavy authoring prompt
            # should be stripped text-free (`force_text_free`'s whole
            # purpose), it's a considered decision that this content needs
            # the ordinary, label-VERIFIED textbook illustration path, same
            # as a block the writer authored as `illustration_style:
            # "textbook"` from the start. Without this override,
            # `force_text_free=True` below would still force the text-free
            # path regardless, silently discarding the labels this reroute
            # just recovered from the diagram prompt.
            if block.content.get("illustration_style") == "textbook":
                structured_fallback = False
            log.info("Falling back to a raster illustration for %s", block.id)
        elif kind == "concept_experience" and self.settings.enable_concept_experience_visuals:
            ok = await self.concept_visuals.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
        if kind == "illustration" and block.content.get("_pending_edit"):
            if self.settings.enable_image_editing:
                # Replace Image root-cause fix: a `replace_image` correction
                # on an illustration that already has a real picture - see
                # `_edit_illustration`'s own docstring for the full
                # contract. Deliberately NEVER falls through to
                # `_generate_illustration` (text-to-image) below on
                # failure - that strict "no silent fallback to an unrelated
                # fresh generation" contract is the whole point of this
                # branch, not an oversight.
                return await self._edit_illustration(course_id=course_id, block=block)
            # Editing is off by operator choice (Settings.enable_image_
            # editing's own docstring) - this is NOT a failure, so it DOES
            # fall through to the ordinary fresh-generation path below,
            # exactly reproducing the pre-this-feature behaviour. The stale
            # flag is cleared first so a later, unrelated regeneration
            # never mistakes it for a still-pending correction.
            block.content = merge_content(block.type, block.content, {"_pending_edit": False})
        return await self._generate_illustration(
            course_id=course_id,
            block=block,
            template=template,
            course_title=course_title,
            chapter_title=chapter_title,
            section_title=section_title,
            key_concept=key_concept,
            force_text_free=structured_fallback,
            source_excerpt=source_excerpt,
        )

    def _reuse_template_asset(self, *, course_id: str, block: Block, template: CourseTemplate) -> bool:
        """Copy a picture from the course's template into the course.

        Only an asset the template blueprint marks as reusable (a logo, or a
        picture the template says to include) is ever copied; an example
        picture is never reused as course content. Anything else is an honest
        failure on the block - never a generated stand-in reported as the
        template's own picture.
        """
        asset_id = str(block.content.get("template_asset") or "")
        blueprint = template.blueprint
        asset = blueprint.reusable_asset(asset_id) if blueprint is not None else None
        if asset is None or not asset.path:
            reason = f"Template picture {asset_id} is not available for reuse"
            block.content = merge_content(block.type, block.content, {"error": reason, "generated": False})
            log.warning("%s (block %s, template %s)", reason, block.id, template.template_id)
            return False
        source = (self.settings.data_dir / asset.path).resolve()
        templates_root = (self.settings.data_dir / "templates").resolve()
        if templates_root not in source.parents or not source.is_file():
            block.content = merge_content(
                block.type, block.content, {"error": f"Template picture {asset_id} is missing", "generated": False}
            )
            return False
        data = source.read_bytes()
        relative = self.storage.save_asset(course_id, data, extension=self._detect_extension(data))
        width, height = self._dimensions(data)
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": False,
                "error": None,
                "width": width,
                "height": height,
            },
        )
        log.info("Reused template picture %s for block %s", asset_id, block.id)
        return True

    async def _draw(self, *, prompt: str, size: str | None, provider: str | None = None) -> bytes:
        """One image-model call. `provider` is passed only when one was chosen, so an image client that
        predates the option (or a test double) keeps working unchanged."""
        if len(prompt) > _PROVIDER_PROMPT_HARD_LIMIT:
            log.warning(
                "Image prompt (%d chars) exceeds the provider's hard limit - truncating to the "
                "last %d chars (see _PROVIDER_PROMPT_HARD_LIMIT's own comment)",
                len(prompt), _PROVIDER_PROMPT_HARD_LIMIT,
            )
            prompt = prompt[-_PROVIDER_PROMPT_HARD_LIMIT:]
        if provider is None:
            return await self.ai.image(prompt=prompt, size=size)
        return await self.ai.image(prompt=prompt, size=size, provider=provider)

    def _request_size(self, block: Block) -> str:
        """The image-model request size: the configured default, or the
        shape a template blueprint asked for."""
        return {
            "landscape": "1536x1024",
            "portrait": "1024x1536",
            "square": "1024x1024",
        }.get(str(block.content.get("aspect") or ""), self.settings.image_size)

    async def _generate_with_text_check(
        self,
        prompt: str,
        expected_labels: list[str],
        *,
        block_id: str,
        provider: str | None = None,
        size: str | None = None,
    ) -> tuple[bytes, bool, list[str]]:
        """Generate a textbook illustration, verify its text is actually
        correct (see _verify_generated_text), and - if it isn't - retry ONCE
        with the specific mistakes fed back into the prompt plus a stronger
        spelling instruction. If it's STILL wrong after that, the third
        attempt asks for no text at all - but a real image model does not
        reliably comply with "no text" 100% of the time (a confirmed real
        case: an "Oxygen" label survived as "Oxgeen" on what was meant to be
        a text-free attempt), so that attempt is verified too rather than
        trusted on faith. If it's STILL not clean, one final, more
        emphatic text-free attempt is made and accepted as-is - four
        attempts is the bound, never unlimited - but ALWAYS returned
        alongside whether it actually passed verification, so a caller can
        never mistake "we stopped retrying" for "this was confirmed
        correct" (see `_generate_illustration`'s `text_verified` field).
        Returns (image bytes, text_verified, missing_labels). `text_verified`
        is True only when some attempt's check GENUINELY completed and found
        every label correct (`missing == []`) - a vision-check exception
        (`missing is None`, see `_verify_generated_text`) is never treated
        the same as a genuine pass, at any of the four bounded attempts
        below; it falls through to the next attempt exactly like a
        confirmed wrong answer would, and the final, honest `text_verified`
        is False either way. A "clean" (`missing == []`) verdict from any of
        the four bounded attempts below is itself double-checked via
        `_confirm_labels_correct` before being trusted - see
        `_confirm_text_free`'s own docstring for the real, confirmed
        false-negative case this pattern closes.

        `missing_labels` (Best Candidate Image Tracking / multi-error
        correction fix) is the LAST bounded attempt's own `missing` list
        (empty when `text_verified` is True, or when the check itself never
        completed) - real, known ground truth (each entry is one of the
        caller's OWN `expected_labels`, not a guess) that a caller can feed
        into a LATER retry's correction prompt so a second, independent
        correction pass addresses every confirmed-wrong label at once,
        never just the first/most obvious one."""
        size = size or self.settings.image_size
        data = await self._draw(prompt=prompt, size=size, provider=provider)
        missing = await self._confirm_labels_correct(data, expected_labels)
        if missing == []:
            return data, True, []

        log.info(
            "Image text check failed for %s (missing/misspelled: %s) - retrying", block_id,
            missing if missing is not None else "vision check inconclusive",
        )
        retry_prompt = (
            f"{prompt}\n\nThe previous attempt got this text wrong or left it out: "
            f"{', '.join(missing or expected_labels)}. This exact text must appear, spelled correctly, "
            "letter by letter, in large clearly legible lettering: "
            f"{', '.join(expected_labels)}. Double-check every letter before finalizing - "
            "a near-miss spelling (an extra, missing, or swapped letter) is still wrong."
        )
        data = await self._draw(prompt=retry_prompt, size=size, provider=provider)
        missing = await self._confirm_labels_correct(data, expected_labels)
        if missing == []:
            return data, True, []

        log.info(
            "Image text check failed again for %s (%s) - attempting a text-free image", block_id,
            missing if missing is not None else "vision check inconclusive",
        )
        data = await self._draw(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size, provider=provider)
        # Trust nothing on faith: a "text-free" request can still come back
        # with partial, wrong text (the confirmed "Oxgeen" case) - checked
        # against the exact same expected labels, since any of them
        # appearing wrong is exactly as bad as it was on a labelled attempt.
        missing = await self._confirm_labels_correct(data, expected_labels)
        if missing == []:
            return data, True, []

        log.info(
            "Text-free attempt for %s still unresolved (%s) - one final, more emphatic attempt",
            block_id, missing if missing is not None else "vision check inconclusive",
        )
        final_prompt = (
            f"{prompt}\n\nABSOLUTELY CRITICAL: this image must contain ZERO text of any kind - "
            "no letters, no words, no numbers, no labels, no captions, nothing resembling "
            "writing anywhere in the image, not even small or decorative text. A previous "
            "attempt at a text-free version still rendered some incorrect text - this time, "
            "render no text at all, full stop."
        )
        data = await self._draw(prompt=final_prompt, size=size, provider=provider)
        missing = await self._confirm_labels_correct(data, expected_labels)
        # Whatever this final, bounded attempt produced is what ships - but
        # the caller is told the honest truth about whether it's clean.
        # `missing == []` is deliberately NOT `not missing`: that would also
        # be True for `None` (a vision-check exception, Python's own
        # falsiness rule for None), silently treating "could not verify" as
        # "verified correct" - the exact conflation this fix exists to close.
        return data, missing == [], (missing or [])

    @staticmethod
    def _safety_fallback_prompt(
        *,
        course_title: str,
        chapter_title: str,
        section_title: str,
        key_concept: str,
        block_topic: str = "",
        expected_labels: list[str] | None = None,
    ) -> str:
        """A generic, policy-conscious prompt for the ONE bounded retry after
        a content-safety rejection (see `_is_content_safety_violation`'s own
        docstring on why this drops the writer's free-text prompt entirely).
        Deliberately abstract/symbolic rather than depicting anything
        specific enough to risk the same or a different policy rule.

        `block_topic` (visual-generation policy fix): a SHORT, block-specific
        summary - this block's own `purpose`/`caption`, never the full
        rejected `prompt` text itself (still dropped entirely, same
        reasoning as always) - preferred over `key_concept` when available.
        `key_concept` is computed once per CHAPTER (see `chapter_key_concept`
        above), so several structurally-unrelated blocks in the same chapter
        that all hit this fallback used to receive the exact same generic
        topic and end up as the same generic image - a real, confirmed case
        (three different decision workflows in one "Atomic Theory" chapter
        all rendered as one identical abstract picture). A short caption/
        purpose sentence is block-specific without being the detailed,
        technical request most likely to have triggered the rejection in
        the first place.

        `expected_labels` (a textbook illustration's essential part-labels,
        e.g. "Nucleus", "Cell wall") are preserved here rather than silently
        dropped like the rest of the rejected request - a real, confirmed
        gap: the first version of this fallback always forced a text-free
        image, which meant a content-safety rejection on a LABELLED
        illustration shipped a generic, unlabelled placeholder instead,
        quietly losing essential educational content the course still
        needed. Labels are not what a safety filter rejects (the depicted
        scene/subject is), so asking for them on an otherwise generic,
        abstract depiction is safe; the caller still verifies them with the
        same `_verify_generated_text` mechanism as every other labelled
        path, never trusted on faith."""
        topic = (
            block_topic.strip()[:_KEY_CONCEPT_CHAR_CAP]
            or key_concept.strip()
            or section_title.strip()
            or chapter_title.strip()
            or course_title.strip()
        )
        base = (
            f"A clean, minimal textbook-style line diagram for the general theme of \"{topic}\" "
            f"in a course titled \"{course_title}\". Draw simple labeled geometric shapes or a "
            "plain schematic that abstracts the subject's actual structure (boxes, lines, arrows,"
            " simple curves) - flat colors, no shading or gradients, no decorative stock icons "
            "(no gears, lightbulbs, trophies, books-as-props, generic charts), no mascots, and "
            "nothing that depicts a specific person, scene, brand, weapon, or sensitive subject "
            "matter. Nothing graphic, violent, political, or controversial - purely schematic and "
            "safe for an educational audience of any age."
        )
        labels = [label.strip() for label in (expected_labels or []) if label.strip()]
        if not labels:
            return f"{base}{_NO_TEXT_SUFFIX}"
        return (
            f"{base}\n\nThis illustration must still clearly label these exact parts, spelled "
            f"correctly, in large legible lettering, even though the overall scene is generic/"
            f"abstract: {', '.join(labels)}."
        )

    # --- Wikimedia Commons (first priority, before any AI model) ----------

    @staticmethod
    def _wikimedia_queries(
        *, chapter_title: str, section_title: str, key_concept: str, block: Block,
    ) -> list[str]:
        """Most-specific-first search queries - `_try_wikimedia_image` stops
        at the first one that yields a validated candidate, so this order IS
        what "prefer the more specific match" means in practice. Built from
        the exact same grounding fields `build_prompt` already uses for the
        AI path - never a separate, new notion of "what this block is
        about". Deduplicated case-insensitively (a block with no distinct
        key_concept/purpose would otherwise search the same chapter title
        two or three times over)."""
        purpose = str(block.content.get("purpose") or "").strip()
        queries: list[str] = []
        seen: set[str] = set()
        for text in (key_concept.strip(), purpose, section_title.strip(), chapter_title.strip()):
            if not text:
                continue
            query = text[:200]
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(query)
        return queries[:3]

    @staticmethod
    def _wikimedia_metadata(candidate: WikimediaCandidate) -> dict:
        return {
            "image_source": "wikimedia",
            "source_url": candidate.file_page_url,
            "license_name": candidate.license_short_name,
            "license_url": candidate.license_url,
            "attribution_text": candidate.attribution_text(),
            "wikimedia_title": candidate.title,
            # Carried through to a cache hit too (see storage.
            # save_wikimedia_cache_entry) so _materialize_wikimedia_result
            # can tell an SVG candidate apart from a raster one without
            # re-sniffing the cached bytes.
            "mime": candidate.mime,
        }

    def _materialize_wikimedia_result(
        self, data: bytes, meta: dict, *, course_id: str, visual_qa_findings: list[dict] | None = None,
    ) -> dict:
        """Saves `data` as this COURSE's own asset (same `storage.save_asset`
        every AI-generated image uses - a Wikimedia image is otherwise
        indistinguishable from a generated one to the rest of the pipeline)
        and builds the `block.content` patch. `meta` is either a freshly
        validated candidate's own metadata or a cache hit's stored metadata -
        both carry the exact same keys (see `_wikimedia_metadata`/
        `storage.save_wikimedia_cache_entry`).

        SVG (`meta["mime"] == "image/svg+xml"`) never goes through
        `_detect_extension`/`_dimensions` - both decode via PIL, which
        cannot open SVG at all (confirmed: raises UnidentifiedImageError).
        `wikimedia_provider.svg_dimensions` reads the real intrinsic size
        straight out of the SVG's own markup instead - the SVG-specific
        counterpart to `_dimensions` for every raster format.

        `visual_qa_findings` (Category B hardening pass): a REVIEW-level
        identifier-value ambiguity `_wikimedia_result_for_query` found while
        checking an information-bearing candidate's own transcribed labels
        against this course's source text - a CONFIRMED contradiction is
        never passed here at all (the candidate is rejected outright before
        this is ever called), so this list only ever carries the genuinely
        ambiguous, advisory case, same as the AI-generated illustration
        path's own `source_grounding_findings`."""
        is_svg = meta.get("mime") == "image/svg+xml"
        if is_svg:
            extension = "svg"
            width, height = wikimedia_provider.svg_dimensions(data)
        else:
            extension = self._detect_extension(data)
            width, height = self._dimensions(data)
        relative = self.storage.save_asset(course_id, data, extension=extension)
        return {
            "path": relative,
            "asset_id": relative.rsplit("/", 1)[-1],
            "generated": True,
            "error": None,
            "omitted_after_qa_failure": False,
            "width": width,
            "height": height,
            "generation_mode": "wikimedia",
            "image_source": meta.get("image_source", "wikimedia"),
            "source_url": meta.get("source_url", ""),
            "license_name": meta.get("license_name", ""),
            "license_url": meta.get("license_url", ""),
            "attribution_text": meta.get("attribution_text", ""),
            "wikimedia_title": meta.get("wikimedia_title", ""),
            "visual_qa_findings": visual_qa_findings or [],
        }

    @staticmethod
    def _wikimedia_category_b_findings(
        check: WikimediaCandidateCheck, source_excerpt: str,
    ) -> tuple[bool, list[dict]]:
        """Category A/B hardening pass. Category A (`information_bearing`
        False - an ordinary photograph) makes no checkable factual claims,
        so this is skipped entirely: no new source-grounding call, exactly
        the existing relevance/licensing/metadata gates. Category B gets the
        SAME deterministic numeric/identifier check an AI-generated diagram
        or illustration's own brief gets (app.services.numeric_consistency_qa,
        zero extra AI calls - `check.visible_numeric_labels` was already
        transcribed by the one vision call every candidate gets regardless).

        Returns `(confirmed_contradiction, review_findings)`: a CONFIRMED
        contradiction means the caller must reject this candidate outright
        (never publish an information-bearing image whose own labels
        contradict the course's text); `review_findings` carries only the
        genuinely ambiguous case, for the caller to attach advisory-only,
        mirroring `ImageService._generate_illustration`'s own
        `source_grounding_findings` handling for the AI-generated path."""
        if not check.information_bearing or not check.visible_numeric_labels.strip() or not source_excerpt.strip():
            return False, []
        claim_text = check.visible_numeric_labels
        if find_numeric_contradictions(claim_text, source_excerpt):
            return True, []
        review: list[dict] = []
        for issue in find_identifier_value_issues(claim_text, source_excerpt):
            if issue.reason == IDENTIFIER_VALUE_NEEDS_REVIEW:
                review.extend(
                    visual_qa_finding_entries(
                        [(issue.reason, issue.detail, False)], resolution="source_grounding_unsupported",
                    )
                )
            else:
                return True, []
        return False, review

    async def _wikimedia_result_for_query(
        self, query: str, *, course_id: str, course_title: str, brief: str, source_excerpt: str = "",
    ) -> dict | None:
        """One query's own full pipeline: cache check, then (on a miss)
        search -> deterministic filter -> bounded vision-check -> cache the
        first pass. Returns None on "nothing suitable for this query" -
        the caller tries the next, broader query, if any."""
        cached = self.storage.load_wikimedia_cache_entry(query)
        if cached is not None:
            data, meta = cached
            # Root-cause fix (cross-course cache safety audit): this cache is
            # deliberately COURSE-INDEPENDENT (see `storage.
            # wikimedia_cache_dir`'s own comment - "reused across every
            # later course that asks an equivalent topic") and keyed by an
            # exact normalised query STRING, not by anything about the
            # course that originally validated it. The entry's own bytes
            # already passed `evaluate_candidate`/`candidate_passed` once -
            # but only against WHICHEVER course first populated it; a cache
            # hit used to serve those same bytes to a different, later
            # course with zero re-check against THIS course's own
            # `brief`/`course_title`, on the unstated assumption that an
            # identical search string always means an identical need. A
            # cache hit now gets exactly the same scrutiny a fresh,
            # never-seen candidate already gets below - never trusted on
            # faith just because the bytes were fetched once before.
            is_svg = meta.get("mime") == "image/svg+xml"
            if is_svg:
                vision_bytes = await wikimedia_provider.rasterize_svg_for_vision(data, settings=self.settings)
            else:
                vision_bytes = data
            still_relevant = False
            category_b_findings: list[dict] = []
            if vision_bytes is not None:
                check = await evaluate_candidate(
                    vision_bytes, brief=brief, course_title=course_title, ai=self.ai, settings=self.settings
                )
                still_relevant = candidate_passed(check)
                if still_relevant:
                    confirmed_contradiction, category_b_findings = self._wikimedia_category_b_findings(
                        check, source_excerpt
                    )
                    still_relevant = not confirmed_contradiction
            if still_relevant:
                log.info("Wikimedia cache hit for query %r (%s)", query, meta.get("wikimedia_title"))
                return self._materialize_wikimedia_result(
                    data, meta, course_id=course_id, visual_qa_findings=category_b_findings,
                )
            log.info(
                "Wikimedia cache hit for query %r (%s) failed re-validation against this course's own "
                "brief - falling through to a fresh search instead of serving it",
                query, meta.get("wikimedia_title"),
            )

        candidates = await wikimedia_provider.search_commons(
            query, limit=self.settings.wikimedia_search_candidates, settings=self.settings
        )
        eligible = [
            candidate for candidate in candidates
            if candidate.license_ok
            # SVG is explicitly INCLUDED (not excluded) - a real, confirmed
            # check against the live Commons API found that a large share
            # of its best educational diagrams are published as SVG, not
            # JPEG/PNG; excluding it meant Wikimedia rarely won for exactly
            # the "textbook diagram" topics this feature targets. See
            # wikimedia_provider.svg_dimensions/rasterize_svg_for_vision's
            # own docstrings for how PIL's "cannot open SVG at all" gap
            # (the original reason it was excluded) is actually closed
            # below, rather than worked around by skipping it.
            and (candidate.mime.startswith("image/"))
            and candidate.width >= self.settings.wikimedia_min_width
            and candidate.height >= self.settings.wikimedia_min_height
        ]
        for candidate in eligible[:_WIKIMEDIA_MAX_CANDIDATES_CHECKED]:
            try:
                data = await wikimedia_provider.fetch_image_bytes(candidate.image_url, settings=self.settings)
            except Exception as exc:  # noqa: BLE001 - one bad candidate must not abort the whole lookup
                log.warning("Could not download Wikimedia candidate %r: %s", candidate.title, exc)
                continue
            is_svg = candidate.mime == "image/svg+xml"
            if is_svg:
                # The vision model can only look at real pixels (see
                # rasterize_svg_for_vision's own docstring) - this
                # rasterised copy is used for THIS check only; the
                # original SVG `data` is still what gets saved/served if
                # the candidate passes, a few lines down.
                vision_bytes = await wikimedia_provider.rasterize_svg_for_vision(data, settings=self.settings)
                if vision_bytes is None:
                    log.warning("Could not rasterise SVG Wikimedia candidate %r for review - skipped", candidate.title)
                    continue
            else:
                vision_bytes = data
            check = await evaluate_candidate(
                vision_bytes, brief=brief, course_title=course_title, ai=self.ai, settings=self.settings
            )
            if not candidate_passed(check):
                continue
            confirmed_contradiction, category_b_findings = self._wikimedia_category_b_findings(
                check, source_excerpt
            )
            if confirmed_contradiction:
                log.info(
                    "Wikimedia candidate %r rejected: its own labels contradict this course's source "
                    "text (Category B information-bearing check)", candidate.title,
                )
                continue
            meta = self._wikimedia_metadata(candidate)
            extension = "svg" if is_svg else self._detect_extension(data)
            self.storage.save_wikimedia_cache_entry(query, data, meta, extension=extension)
            return self._materialize_wikimedia_result(
                data, meta, course_id=course_id, visual_qa_findings=category_b_findings,
            )
        return None

    async def _try_wikimedia_image(
        self,
        *,
        course_id: str,
        block: Block,
        course_title: str,
        chapter_title: str,
        section_title: str,
        key_concept: str,
        source_excerpt: str = "",
    ) -> dict | None:
        """The one, correct insertion point for "search Wikimedia Commons
        before ever calling an AI image model" - see this module's own
        docstring and the approved plan. Returns the `block.content` patch
        to apply on a genuine, validated hit, or `None` when nothing
        suitable exists OR Wikimedia itself is unreachable/erroring - both
        `None` cases are indistinguishable to the caller on purpose, which
        simply falls through to the existing, unchanged AI path either way.
        Never raises: a Wikimedia outage must never interrupt course
        generation."""
        if not self.settings.enable_wikimedia_images:
            return None
        style = str(block.content.get("illustration_style") or "").strip().lower()
        if style in _WIKIMEDIA_EXCLUDED_STYLES:
            return None
        brief = "\n".join(
            part for part in (
                key_concept.strip(),
                str(block.content.get("purpose") or "").strip(),
                str(block.content.get("prompt") or "").strip(),
                str(block.content.get("caption") or "").strip(),
            ) if part
        )[:MAX_PROMPT_CHARS]
        if not brief:
            return None  # nothing to search for or validate a candidate against
        queries = self._wikimedia_queries(
            chapter_title=chapter_title, section_title=section_title, key_concept=key_concept, block=block,
        )
        # Root-cause fix: this used to `return None` (give up on Wikimedia
        # entirely for this block, falling through to the AI model) the
        # moment the FIRST query raised - a real, confirmed bug: a single
        # transient/volume-driven failure (see wikimedia_provider.py's own
        # module docstring) on query 1 of N silently discarded every chance
        # the remaining, genuinely different queries had to find a real,
        # valid Wikimedia result. Now every query gets its own real attempt;
        # only after ALL of them have failed/missed does this fall through.
        # Deliberately serial, kept LOW and process-wide (not per-block) via
        # `wikimedia_concurrency` (default 1) - see that setting's own
        # docstring for why a bursty multi-block concurrent search pattern
        # was itself the actual root cause of being blocked, never the HTTP
        # client used.
        limiter = get_limiter("wikimedia", self.settings.concurrency_for("wikimedia"))
        for query in queries:
            try:
                async with limiter.slot():
                    result = await self._wikimedia_result_for_query(
                        query, course_id=course_id, course_title=course_title, brief=brief,
                        source_excerpt=source_excerpt,
                    )
            except Exception as exc:  # noqa: BLE001 - never block course generation on a Wikimedia failure
                log.warning(
                    "Wikimedia lookup failed for query %r (block %s): %s - trying the next query if any",
                    query, block.id, exc,
                )
                continue
            if result is not None:
                return result
        return None

    async def _edit_illustration(
        self,
        *,
        course_id: str,
        block: Block,
    ) -> bool:
        """Replace Image root-cause fix - genuine image-to-image EDIT of an
        illustration block's EXISTING picture, set up by
        `app.course.document.patcher`'s `replace_image` handler: it leaves
        `content["path"]` untouched (never clears it, unlike the old
        behaviour) and sets `content["_pending_edit"] = True` exactly when
        the block already has a real raster image on disk and is staying
        `kind: "illustration"` - see that function's own docstring. This
        reads that EXACT picture's real bytes off disk and sends them to
        the configured provider's own image-to-image edit endpoint
        (`AIClient.image_edit` -> azure_image_provider.edit_image /
        azure_mai_image_provider.edit_image, both verified against the
        official Microsoft docs, never a generic `image()` text-to-image
        call) alongside the correction instruction - a genuine edit, never
        a fresh regeneration with the old image merely described in words.

        Callers (`generate_for_block`) only reach this function at all when
        `settings.enable_image_editing` is also on - that flag's own
        "editing is off, fall through to fresh generation" case is handled
        entirely by the caller BEFORE this function is ever invoked (a
        deliberate operator choice, not a failure), so every failure
        reported from inside this function is a genuine editing failure.

        STRICT failure contract - deliberately different from every other
        path in this module: this NEVER falls back to `self.ai.image()`
        (text-to-image) on any failure. Doing so would silently replace a
        correctly-working picture with an unrelated new one while this
        function's own caller reports the course as "edited" - the exact
        failure mode this whole feature exists to close. On ANY failure
        (no source file, unreadable source, no instructions, a provider/
        API error), `content["path"]`/`width`/`height`/`generated`/
        `asset_id` are never written to at all here, so the block keeps
        showing its last known-good picture; only `content["error"]` (a
        clear, real reason) and `_pending_edit` (cleared, so a later
        unrelated regeneration never silently retries a stale correction)
        change."""
        content = block.content
        source_path = content.get("path")
        if not isinstance(source_path, str) or not source_path:
            # patcher.py only ever sets `_pending_edit=True` alongside a
            # real `path` - still checked defensively so a stale/manually-
            # crafted flag with nothing to edit reports a clear reason
            # rather than crashing the block.
            block.content = merge_content(
                block.type, block.content,
                {"_pending_edit": False, "error": "No existing image to edit"},
            )
            return False
        try:
            source_bytes = self.storage.asset_abs_path(course_id, source_path).read_bytes()
        except OSError as exc:
            log.warning("Could not read the existing image to edit for %s (%s): %s", block.id, source_path, exc)
            block.content = merge_content(
                block.type, block.content,
                {"_pending_edit": False, "error": f"Could not read the existing image to edit: {exc}"},
            )
            return False

        correction = str(content.get("prompt") or "").strip()
        if not correction:
            block.content = merge_content(
                block.type, block.content,
                {"_pending_edit": False, "error": "No correction instructions were given"},
            )
            return False
        # Explicitly distinguishes "what changes" from "what must stay the
        # same" - the whole point of an EDIT instruction versus a fresh
        # generation prompt, which has no existing picture to preserve
        # anything from. `correction` is the editor agent's own already-
        # composed corrective prompt (see EDITOR_SYSTEM's replace_image
        # guidance - built from the block's ORIGINAL prompt plus the
        # user's exact instructions, prioritised over any generic framing).
        edit_prompt = (
            f"Edit the attached image exactly as follows: {correction}\n\n"
            "Preserve everything else about the original image unchanged - its subject, "
            "composition, objects, their positions, and overall visual style - except for "
            "what this instruction explicitly asks to change. Do not redesign the picture "
            "from scratch or replace its subject."
        )
        is_textbook = (content.get("illustration_style") or "").strip().lower() == "textbook"
        provider = self.settings.textbook_image_provider if is_textbook else None
        expected_labels = sanitize_expected_labels(
            [str(label).strip() for label in (content.get("expected_labels") or []) if str(label).strip()]
        )
        log.info(
            "%s image EDIT for %s (provider=%s)",
            "MOCK" if self.ai.is_mock else "REAL", block.id,
            "mock" if self.ai.is_mock else (provider or self.settings.image_provider),
        )
        try:
            data = await self.ai.image_edit(prompt=edit_prompt, image=source_bytes, provider=provider)
            if expected_labels:
                # One bounded retry on a confirmed-wrong label - the same
                # "never trust text on faith" discipline _generate_with_
                # text_check already keeps for a fresh generation, just a
                # single retry here (not four) since an edit that already
                # preserves most of the image correctly needs far less
                # room for the model to get labels wrong than a from-
                # scratch generation does - see this feature's own "avoid
                # unnecessary additional AI calls" requirement.
                missing = await self._verify_generated_text(data, expected_labels)
                if missing:
                    log.info(
                        "Edited image text check failed for %s (missing/misspelled: %s) - "
                        "retrying the edit once", block.id, missing,
                    )
                    retry_prompt = (
                        f"{edit_prompt}\n\nThe previous edit got this text wrong or left it out: "
                        f"{', '.join(missing)}. This exact text must appear, spelled correctly: "
                        f"{', '.join(expected_labels)}."
                    )
                    data = await self.ai.image_edit(prompt=retry_prompt, image=source_bytes, provider=provider)
        except Exception as exc:  # noqa: BLE001 - never silently fall back to a fresh, unrelated generation
            log.warning("Image edit failed for %s - the existing image was kept unchanged: %s", block.id, exc)
            block.content = merge_content(
                block.type, block.content,
                {"_pending_edit": False, "error": f"Image edit failed: {str(exc)[:300]}"},
            )
            return False

        relative = self.storage.save_asset(course_id, data, extension=self._detect_extension(data))
        width, height = self._dimensions(data)
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                "width": width,
                "height": height,
                "_pending_edit": False,
            },
        )
        log.info("Edited illustration %s for block %s", relative, block.id)
        return True

    def _use_generic_section_icon(
        self, *, course_id: str, block: Block, template: CourseTemplate, icon_kind: str,
    ) -> bool:
        """Deterministic, zero-AI-call replacement for a GENERIC pedagogical
        section opener - see `app.render.section_icons`'s own module
        docstring for why this category is special-cased ahead of both
        Wikimedia and the AI illustration path. Can never fail: there is no
        network call, no model call and no QA step, so this always returns
        True."""
        data = render_section_icon_svg(icon_kind, template.theme)
        relative = self.storage.save_asset(course_id, data, extension="svg")
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                "omitted_after_qa_failure": False,
                "width": self._SECTION_INTRO_WIDTH,
                "height": self._SECTION_INTRO_HEIGHT,
                # Nothing was promised to check here - a hand-authored glyph
                # has no baked-in label text to verify, matching
                # `text_verified`'s own "nothing checkable" None contract
                # (see ImageContent.text_verified's docstring).
                "text_verified": None,
                "visual_qa_findings": [],
                # Distinct from both "generated" (an AI model drew this) and
                # "wikimedia" (a licensed Commons photo) - a course-template-
                # themed static glyph is neither. Display code only ever
                # special-cases the "wikimedia" value (for attribution), so
                # this is a safe, additive third value.
                "image_source": "static_icon",
            },
        )
        log.info(
            "Used a deterministic generic icon (%s) for section-opener block %s - no model call made",
            icon_kind, block.id,
        )
        return True

    async def _generate_illustration(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
        chapter_title: str = "",
        section_title: str = "",
        key_concept: str = "",
        force_text_free: bool = False,
        source_excerpt: str = "",
    ) -> bool:
        """`force_text_free` is set when this is a FALLBACK from a failed
        diagram/concept_experience attempt (see generate_for_block) - that
        original request was for a labelled flowchart/architecture-style
        visual, which a raster image model cannot reliably spell (this is
        the confirmed cause of real, observed garbled-text diagrams: e.g.
        "Balid prompt", "eparates prompt text", "Fallute" instead of
        "Failure" - a diffusion model attempting to bake a dozen technical
        labels into pixels). This asks for a text-free picture instead -
        still a real, relevant visual, just never meant to carry labels -
        and, unlike an earlier version of this path, that "text-free"
        request is itself verified with a bounded retry
        (`_generate_text_free_illustration`), not trusted on faith: a real
        image model does not reliably comply with "no text" either, and an
        unverified attempt was exactly how a flowchart-shaped request for
        "list vs generator" ended up as a real image with "coan oicts" and
        "medership" baked into it."""
        # Root-cause fix (production course audit, ThermoDynamics/
        # doc_a2f5625a7fd9): a `section_intro` opener for a GENERIC,
        # subject-agnostic pedagogical section (Quiz/Summary/Best
        # Practices/Common Mistakes/Exercise/Challenge/Learning
        # Objectives - see app.render.section_icons's own module
        # docstring) carries no real information content, so there is
        # nothing an AI model usefully adds - it only adds the one
        # failure mode a "strictly no text" raster prompt cannot be
        # trusted not to hit (see this function's own docstring above).
        # A deterministic, theme-coloured SVG glyph never fails, never
        # calls a model, and is checked BEFORE Wikimedia/AI so this
        # category never spends a provider call or a bounded-retry
        # attempt at all. Every other section_intro (a course's actual
        # subject matter) is untouched.
        if (block.content.get("illustration_style") or "").strip().lower() == "section_intro":
            icon_kind = match_generic_section_icon(section_title)
            if icon_kind is not None:
                return self._use_generic_section_icon(
                    course_id=course_id, block=block, template=template, icon_kind=icon_kind,
                )
        # Wikimedia Commons is checked FIRST, before any AI model, for every
        # path that reaches this function (a genuine illustration request
        # AND the raster fallback from a failed diagram/concept_experience
        # attempt - see this module's own docstring and the approved plan).
        # `None` here means "nothing suitable, or Wikimedia itself failed" -
        # both cases fall straight through to the unchanged AI path below,
        # never raise, never interrupt course generation.
        wikimedia_result = await self._try_wikimedia_image(
            course_id=course_id,
            block=block,
            course_title=course_title,
            chapter_title=chapter_title,
            section_title=section_title,
            key_concept=key_concept,
            source_excerpt=source_excerpt,
        )
        if wikimedia_result is not None:
            block.content = merge_content(block.type, block.content, wikimedia_result)
            log.info("Used a Wikimedia Commons image for block %s (%s)", block.id, wikimedia_result.get("wikimedia_title"))
            return True
        prompt = self.build_prompt(
            block, template, course_title,
            chapter_title=chapter_title, section_title=section_title, key_concept=key_concept,
        )
        if not prompt.strip():
            log.warning("Image block %s has no prompt - skipped", block.id)
            return False
        if force_text_free:
            # Root-cause fix (production course audit, ThermoDynamics/
            # doc_a2f5625a7fd9): `prompt` here is still the ORIGINAL
            # diagram-authoring text (block.content["prompt"], written by
            # the content agent for a structured SVG diagram), which
            # routinely instructs exact quoted labels ('labeled exactly:
            # "Mastery milestones (self‑checks)"...') for a dozen+ nodes -
            # a real, confirmed case shipped exactly that prompt, appended
            # with only the generic `_NO_TEXT_SUFFIX`, and the model tried
            # to honour BOTH the explicit "label it exactly" instructions
            # and the trailing "no text" note, producing a text-heavy image
            # with several labels garbled ("coniJt", "mullivtrabile"). A
            # trailing suffix alone is too easy for a model to under-weight
            # against several much more specific, repeated instructions
            # earlier in the same prompt - this override is prepended
            # instead, directly superseding any such instruction before the
            # model reads it, on top of (not instead of) `_NO_TEXT_SUFFIX`
            # still being appended by `_generate_text_free_illustration`.
            prompt = (
                "OVERRIDE: ignore any instruction below that asks for specific quoted "
                "text, labels, captions or exact wording to be rendered in the image - "
                "this request has been downgraded to a decorative, completely TEXT-FREE "
                "illustration. Use shape, position, grouping and colour alone to convey "
                "the same structure; render zero letters, words or numbers anywhere.\n\n"
                f"{prompt}"
            )
        # Made explicit (never silent) so nobody evaluates visual quality
        # against a deterministic offline placeholder without realising it -
        # `MockAIClient.image()` ignores prompt content entirely. `is_mock`
        # already exists on AIClient for exactly this purpose (course_service
        # already uses it to record which model actually produced a block).
        is_textbook = (block.content.get("illustration_style") or "").strip().lower() == "textbook"
        # A `textbook` illustration's whole job is accurately depicting and
        # LABELLING a real apparatus/structure - text/label correctness
        # matters far more there than for a purely decorative illustration.
        # A real, confirmed side-by-side comparison (see Settings.
        # textbook_image_provider's own docstring) showed the configured
        # default provider missing requested labels or outright garbling one
        # ("Irrlstior" instead of "Indicator") on 2 of 3 real topics, while
        # this override's provider got every label right on all 3 - so this
        # style overrides the configured default rather than inheriting it.
        provider = self.settings.textbook_image_provider if is_textbook else None
        log.info(
            "%s image generation for %s (provider=%s)",
            "MOCK" if self.ai.is_mock else "REAL",
            block.id,
            "mock" if self.ai.is_mock else (provider or self.settings.image_provider),
        )
        expected_labels = sanitize_expected_labels(
            [str(label).strip() for label in (block.content.get("expected_labels") or []) if str(label).strip()]
        )
        # A content-rich textbook illustration (several distinct labelled
        # parts) is routinely a naturally WIDE composition (several icons or
        # components side by side) - forcing it into a square canvas and
        # hoping the model rearranges into a grid on its own is exactly what
        # produced a real, confirmed bug: a 4-label composition drawn wider
        # than the square canvas could hold, slicing the first and last
        # items off at the edges mid-label. Giving it real extra width up
        # front, rather than relying on the framing instruction alone to
        # compensate after the fact, is the more reliable fix - the two
        # layer together (this for genuine breathing room, the framing rule
        # in TEXTBOOK_ILLUSTRATION_GUIDANCE as the safety net for whatever
        # composition the model still chooses).
        #
        # Visual-generation-quality fix: gating landscape on `len(expected_
        # labels) >= 4 or raw_prompt_len > 1200` was itself the bug - the
        # module's own prior comment already admitted this count is "NOT a
        # reliable density signal" (a real, confirmed case: a dense 3-scene
        # bench-wiring schematic with ~15 real labelled parts had only 3
        # actually recovered into `expected_labels`, missed the threshold,
        # and got squeezed into a square canvas anyway). Rather than lean
        # harder on an already-unreliable heuristic, landscape is now the
        # DEFAULT for every `textbook`-style image - matching how a real
        # textbook/Playground-style diagram is actually composed - and only
        # the genuinely simple, single-panel case (no labels requested, no
        # governing formula, and a short writer prompt) falls back to a
        # square canvas, where a single centred subject doesn't need the
        # extra width.
        raw_prompt_len = len(str(block.content.get("prompt") or ""))
        formula_expression = str(block.content.get("formula_expression") or "").strip()
        is_simple_single_panel_textbook = (
            is_textbook and not expected_labels and not formula_expression and raw_prompt_len <= 300
        )
        image_size = (
            self._request_size(block)
            if block.content.get("aspect")
            else (
                "1024x1024"
                if is_simple_single_panel_textbook
                else ("1536x1024" if is_textbook else None)
            )
        )
        # None means "nothing was promised to verify" (no expected labels at
        # all) - genuinely different from False ("checked, and it's still
        # wrong after every bounded attempt"), so a caller can tell "nothing
        # to confirm" from "confirmed wrong" rather than a bare boolean
        # collapsing both into one meaning. A forced text-free fallback DOES
        # get checked now (see _generate_text_free_illustration) - "nothing
        # was promised" never applied to it in the first place, since the
        # whole point of forcing text-free is a real promise ("no text")
        # that's just as checkable as a labelled one.
        text_verified: bool | None = None
        # Best Candidate Image Tracking / multi-error correction fix: real,
        # known-correct ground truth (each entry is one of this block's own
        # `expected_labels`) for whichever specific labels the declared-label
        # check (`_generate_with_text_check`) confirmed still wrong/missing
        # on its own last bounded attempt - `[]` whenever that path wasn't
        # used, or was used and came back clean. Threaded into the
        # relevance-retry loop below so a SECOND, independent correction
        # pass can name every confirmed-wrong label explicitly, not just
        # whatever the holistic vision check's own single `detail` string
        # happens to mention.
        declared_missing_labels: list[str] = []
        # Set True only inside the content-safety except-block below - skips
        # the topic-relevance check for that one, deliberately-generic/
        # abstract emergency image (see `_safety_fallback_prompt`'s own
        # docstring): checking "is this on-topic" against a picture built
        # NOT to be specific, by design, would always fail and waste a
        # retry on something that can never pass.
        is_safety_fallback = False
        try:
            if force_text_free or not is_textbook:
                # Every style that isn't "textbook" (the default/blank style,
                # and "section_intro") carries its own STRICT NO-TEXT RULE
                # (see DEFAULT_ILLUSTRATION_GUIDANCE/
                # SECTION_INTRO_ILLUSTRATION_GUIDANCE) - previously trusted
                # on faith, with zero verification, unlike the force-text-
                # free diagram-fallback case right above it. A real,
                # confirmed case: a set of small section-intro icons shipped
                # with "sourrces", "Fraith magntised field", "toleente",
                # "Teretore", "Mechanician" baked in - this is exactly as
                # checkable (and exactly as capable of silently garbling) as
                # the force-text-free case, so it now gets the identical
                # bounded verify-and-retry treatment, not a free pass just
                # because its trigger is a style name instead of a failed
                # structured-diagram attempt.
                data, text_verified = await self._generate_text_free_illustration(
                    prompt, block_id=block.id, provider=provider, size=image_size
                )
            elif expected_labels:
                data, text_verified, declared_missing_labels = await self._generate_with_text_check(
                    prompt, expected_labels, block_id=block.id, provider=provider, size=image_size
                )
            else:
                data = await self._draw(
                    prompt=prompt, size=image_size or self.settings.image_size, provider=provider
                )
        except Exception as exc:  # noqa: BLE001 - a failed image must not fail the course
            if not _is_content_safety_violation(exc):
                log.warning("Image generation failed for %s: %s", block.id, exc)
                block.content = merge_content(
                    block.type, block.content,
                    # `omitted_after_qa_failure` explicitly cleared: this is
                    # a genuine technical failure (`error` is set, which
                    # already independently makes layout.image_generation_
                    # failed() treat it as a compact placeholder) - a stale
                    # True from an earlier, DIFFERENT (QA) failure on this
                    # same block must not linger with the wrong meaning.
                    {"error": str(exc)[:300], "generated": False, "omitted_after_qa_failure": False},
                )
                return False
            # A content-policy rejection is never retried with the SAME
            # prompt (the provider will reject it again, identically) - the
            # writer's own free-text prompt/purpose/caption is the one part
            # of the request that is both block-specific and outside this
            # codebase's control, so it's the most likely trigger and is
            # dropped entirely for this one bounded retry, in favour of a
            # generic, explicitly policy-conscious depiction of the same
            # topic built only from the course/chapter/section grounding
            # context. A real, confirmed case this fixes: a capstone-project
            # illustration request rejected by Azure's DallEBlockList_Prompt
            # filter, which previously left the block with no image at all
            # (`generated: False`) for the rest of that course's lifetime -
            # see this module's own `_safety_fallback_prompt`.
            block_topic = (
                str(block.content.get("purpose") or "").strip()
                or str(block.content.get("caption") or "").strip()
            )
            safe_prompt = self._safety_fallback_prompt(
                course_title=course_title,
                chapter_title=chapter_title,
                section_title=section_title,
                key_concept=key_concept,
                block_topic=block_topic,
                expected_labels=expected_labels,
            )
            log.warning(
                "Image generation for %s was rejected by the provider's content-safety filter "
                "(%s) - retrying once with a generic, topic-only safe-fallback prompt", block.id, exc,
            )
            try:
                data = await self._draw(
                    prompt=safe_prompt, size=image_size or self.settings.image_size, provider=provider
                )
            except Exception as retry_exc:  # noqa: BLE001 - same reasoning as the outer handler
                log.warning(
                    "Safety-fallback image generation also failed for %s: %s", block.id, retry_exc
                )
                block.content = merge_content(
                    block.type, block.content,
                    {"error": str(retry_exc)[:300], "generated": False, "omitted_after_qa_failure": False},
                )
                return False
            prompt = safe_prompt  # the persisted `prompt` must reflect what was actually sent
            is_safety_fallback = True
            # Essential labels, if any, are checked exactly like every other
            # labelled path - never trusted on faith just because this is an
            # emergency fallback. None (not False) when nothing was promised
            # in the first place, same contract `_generate_illustration`
            # already keeps for the non-fallback paths.
            if expected_labels:
                missing = await self._verify_generated_text(data, expected_labels)
                # `missing == []`, not `not missing` - a vision-check
                # exception (`None`) must never collapse into "verified
                # correct" (production risk follow-up).
                text_verified = missing == []
            else:
                text_verified = None
        if text_verified is False:
            log.warning(
                "Image for %s has confirmed-incorrect text after every bounded attempt - "
                "not silently accepted as correct; see Best Candidate Image Tracking below for "
                "whether it still ships (textbook style) or is withheld (text-free style)", block.id,
            )
        # Topic/educational relevance - the gap behind "a visually
        # attractive but irrelevant image must not pass merely because a
        # file exists": nothing before this checked whether a successfully-
        # generated raster image actually depicts its own brief, only
        # whether its (optional) baked-in TEXT was spelled right. One vision
        # call, one bounded retry with the brief re-emphasised if it fails -
        # safe to retry (unlike a formula mismatch, there's no risk of
        # replacing legitimately-correct content with a wrong "correction":
        # regenerating with a more specific prompt can only make the result
        # more on-topic or leave it equally generic, never introduce a new
        # kind of error). The full retry-and-regenerate version of this
        # check below is skipped for the deliberately-generic safety
        # fallback (regenerating would most likely just reproduce the same
        # provider rejection) - but a single, un-retried `genuinely_
        # educational`/`on_topic` check still runs on it (visual-generation
        # policy fix), in its own separate bucket below, so this emergency
        # image is never silently shipped as if independently confirmed
        # relevant just because it was never checked at all.
        relevance_findings: list[dict] = []
        # Best Candidate Image Tracking / Strategy D (text-preservation
        # fix): a confirmed HOLISTIC text-accuracy defect on the best
        # available candidate is recorded HERE, separately from
        # `relevance_findings` above - the two are never merged into one
        # combined verdict, so a scientifically-sound-but-imperfectly-spelled
        # image can ship (flagged) instead of being withheld outright.
        text_findings: list[dict] = []
        source_grounding_findings: list[dict] = []
        safety_fallback_relevance_findings: list[dict] = []
        if is_safety_fallback:
            safety_relevance = await self._verify_image_relevance(
                data, brief=block_topic or course_title, course_title=course_title,
            )
            if not safety_relevance.errored and not (
                safety_relevance.on_topic and safety_relevance.genuinely_educational
            ):
                log.warning(
                    "Safety-fallback image for %s reads as generic/non-educational "
                    "(on_topic=%s, genuinely_educational=%s) - shipping it anyway (no better "
                    "alternative exists after a content-safety rejection), flagged for human review",
                    block.id, safety_relevance.on_topic, safety_relevance.genuinely_educational,
                )
                safety_fallback_relevance_findings = visual_qa_finding_entries(
                    [(
                        "safety_fallback_relevance_unconfirmed",
                        safety_relevance.on_topic_detail or safety_relevance.educational_detail or (
                            "This emergency, content-safety-constrained image could not be confirmed "
                            "as a specific, genuine teaching aid for this block's subject."
                        ),
                        False,
                    )],
                    resolution="safety_fallback_relevance_unconfirmed",
                )
        if not is_safety_fallback:
            brief = "\n".join(
                part for part in (
                    str(block.content.get("purpose") or ""),
                    str(block.content.get("prompt") or ""),
                    str(block.content.get("caption") or ""),
                ) if part
            )[:MAX_PROMPT_CHARS]
            # Canonical Visual Spec milestone: a `section_intro` illustration
            # is a deliberately generic, decorative scene-setter (see this
            # function's own section_intro short-circuit above) - it carries
            # no specific factual claim about the subject to check against
            # the source text in the first place, so source-grounding is a
            # deliberate no-op here (an empty excerpt), never a forced check
            # on content that was never meant to assert anything checkable.
            grounding_excerpt = (
                "" if (block.content.get("illustration_style") or "").strip().lower() == "section_intro"
                else source_excerpt
            )
            relevance = await self._verify_image_relevance(
                data, brief=brief, course_title=course_title, textbook_style=is_textbook,
                source_excerpt=grounding_excerpt,
            )

            def _scientific_check_failed(check: ImageRelevanceCheck) -> bool:
                """The STRUCTURAL/scientific half of relevance - deliberately
                excludes `text_accurately_spelled` (see `_text_check_failed`
                below, its sibling): Best Candidate Image Tracking fix - text
                correctness and scientific/structural correctness must never
                be merged into one combined verdict, so a candidate's
                spelling can never outrank its own scientific validity (or
                vice versa) when choosing the best of several attempts.
                `errored`/`contradicts_source_text` are checked explicitly,
                alongside on_topic/genuinely_educational/labels_legible - a
                vision-call exception leaves all three at their safe default
                (True), so without this check an errored (never actually
                verified) attempt would silently fall straight through as if
                it had genuinely passed (production risk follow-up: the same
                ERROR-vs-PASS conflation fixed for the text-legibility checks
                above). `labels_legible` closes a separate, real gap:
                `_verify_generated_text` only checks that each expected
                label's text appears somewhere, verbatim - never whether it
                landed legibly, without colliding with another label or the
                image's own title - so a labelled illustration could pass
                that check while its callouts overlapped each other
                (confirmed on a real generated "rigid-body rotation"
                diagram). `style_compliant` is only ever asked about for a
                `textbook`-style generation (see `_verify_image_relevance`'s
                `textbook_style` param) - gated on `is_textbook` here too,
                not just trusted to stay at its True default, since nothing
                stops a model (or a test double) from setting it False on a
                field it was never actually asked to judge."""
                return check.errored or check.contradicts_source_text or not (
                    check.on_topic and check.genuinely_educational and check.labels_legible
                    and (check.style_compliant or not is_textbook)
                )

            def _text_check_failed(check: ImageRelevanceCheck) -> bool:
                """The holistic text-accuracy half of relevance - kept
                entirely separate from `_scientific_check_failed` above (Best
                Candidate Image Tracking fix). Only ever asked about for a
                `textbook`-style generation, same reasoning as
                `style_compliant` above."""
                return is_textbook and not check.text_accurately_spelled

            def _ocr_confirms_text_defect(candidate_data: bytes) -> tuple[bool, str]:
                """OCR phase: a deterministic, LOCAL (zero-API-cost) second
                opinion on text accuracy, additive to - never a replacement
                for - the vision checks above. Only ever returns `True` when
                real, known-correct ground truth (this block's own declared
                `expected_labels`, or words actually present in the course's
                own source text) backs a confident, specific mismatch - see
                `image_text_ocr.find_ground_truth_mismatches`'s own
                docstring for the exact conservative rules (never a generic
                dictionary, never a guess, never confident on formula/Greek-
                symbol content, never confident below a minimum OCR
                confidence). `run_ocr` returning `None` (OCR unavailable,
                failed, or errored) and an empty ground-truth set (a "no
                source documents" course with no declared labels on this
                block) both correctly fall through to `(False, "")` here -
                UNCERTAIN, not a vote either way - so Vision QA's own
                judgment is never second-guessed by an absence of evidence.
                Only ever asked about for `textbook`-style generation, same
                gating as `_text_check_failed` - OCR has nothing meaningful
                to check on a style that is required to carry no text at
                all in the first place."""
                if not is_textbook:
                    return False, ""
                ocr_result = run_ocr(candidate_data)
                if ocr_result is None:
                    return False, ""
                mismatches = find_ground_truth_mismatches(
                    ocr_result, expected_labels=expected_labels, source_text=grounding_excerpt,
                )
                if not mismatches:
                    return False, ""
                return True, "; ".join(m.detail for m in mismatches[:3])

            if _scientific_check_failed(relevance) or _text_check_failed(relevance) or _ocr_confirms_text_defect(data)[0]:
                # Best Candidate Image Tracking (production quality-
                # preservation fix): every attempt generated during this
                # bounded retry loop - the original plus each retry - is now
                # kept as its own candidate (the exact bytes, the exact
                # prompt that produced them, and the relevance check actually
                # run against THOSE bytes), instead of the previous behaviour
                # of simply overwriting `data`/`relevance` with whatever the
                # loop drew most recently. This is the direct fix for a real
                # failure shape: a later retry can fix spelling while
                # accidentally drifting off-topic or out of the required
                # style - "last attempt wins" would ship that regression
                # purely because it happened to be generated last. Bounded at
                # _MAX_RELEVANCE_RETRIES (2), not the single retry every
                # other dimension here uses - a real, confirmed case:
                # `style_compliant` specifically (MAI-Image-2.6, like every
                # current raster provider, has no negative-prompt/style
                # parameter - see `ImageRelevanceCheck.style_compliant`'s own
                # docstring) failed BOTH attempts on most of a chapter's
                # textbook illustrations even after the `expected_labels`
                # overload fix (see `sanitize_expected_labels`) resolved
                # their text-legibility failures - a single retry wasn't
                # enough to overcome this provider's genuine per-attempt
                # style-drift rate. Still bounded (never unbounded) - the
                # loop exits the moment an attempt is fully clean (neither
                # scientific nor text issues), so a check that already
                # reliably recovers on the first retry never spends the
                # second one.
                candidates: list[tuple[bytes, str, ImageRelevanceCheck]] = [(data, prompt, relevance)]
                for attempt in range(_MAX_RELEVANCE_RETRIES):
                    log.info(
                        "Image relevance check failed for %s (on_topic=%s, genuinely_educational=%s, "
                        "labels_legible=%s, style_compliant=%s, text_accurately_spelled=%s, errored=%s) - "
                        "retrying (attempt %s of %s) with the brief re-emphasised",
                        block.id, relevance.on_topic, relevance.genuinely_educational,
                        relevance.labels_legible, relevance.style_compliant, relevance.text_accurately_spelled,
                        relevance.errored, attempt + 1, _MAX_RELEVANCE_RETRIES,
                    )
                    legibility_note = (
                        f" The previous attempt also had labels/callouts overlapping or colliding with "
                        f"each other or with other elements ({relevance.labels_legible_detail or 'unreadable'}) "
                        "- give every label clear, non-overlapping space with its own leader line."
                        if not relevance.labels_legible else ""
                    )
                    style_note = (
                        f" The previous attempt also did not follow the required flat 2D vector textbook "
                        f"style ({relevance.style_compliant_detail or 'drifted photorealistic/3D or was cropped'}) "
                        "- redraw as crisp flat-colour line art with no photorealistic shading, no 3D render, "
                        "no glow/gradient background, and every element fully inside the frame with margin."
                        if is_textbook and not relevance.style_compliant else ""
                    )
                    # Multi-error correction fix: names EVERY confirmed-wrong
                    # piece of text this attempt is aware of, not just the
                    # holistic vision check's own single `detail` string -
                    # `declared_missing_labels` is real, known ground truth
                    # (each entry is one of this block's own `expected_
                    # labels`) from the earlier, independent declared-label
                    # check, folded in here so a correction pass addresses
                    # every confirmed defect at once, never just the
                    # first/most obvious one.
                    declared_label_note = (
                        f" The following required labels were also confirmed wrong or missing and must "
                        f"now appear, spelled correctly, exactly as given: {', '.join(declared_missing_labels)}."
                        if declared_missing_labels else ""
                    )
                    # OCR phase (Strategy A integration): a deterministic,
                    # ground-truth-backed mismatch this attempt's own bytes
                    # confirmed - fed into the SAME correction prompt, never
                    # a separate retry mechanism.
                    ocr_confirmed_for_retry, ocr_detail_for_retry = _ocr_confirms_text_defect(data)
                    ocr_note = (
                        f" OCR also confirmed this specific text defect: {ocr_detail_for_retry}."
                        if ocr_confirmed_for_retry else ""
                    )
                    spelling_note = (
                        (
                            f" The previous attempt also had misspelled or garbled text "
                            f"({relevance.text_accurately_spelled_detail or 'a garbled word'}) - spell every "
                            "piece of text correctly, letter-by-letter; simplify or shorten any label that "
                            "keeps coming out wrong rather than repeating the same wording."
                            if is_textbook and not relevance.text_accurately_spelled else ""
                        )
                        + declared_label_note
                        + ocr_note
                    )
                    contradiction_note = (
                        f" The previous attempt also contradicted the course's own text "
                        f"({relevance.contradicts_source_text_detail or 'a stated fact or relationship'}) - "
                        "correct this specific conflict; depict only what is consistent with the course's "
                        "own material."
                        if relevance.contradicts_source_text else ""
                    )
                    retry_prompt = (
                        f"{prompt}\n\nIMPORTANT: the previous attempt did not clearly depict the actual "
                        f"subject. This image MUST specifically, unmistakably depict: {brief or course_title}. "
                        f"Do not produce a generic or decorative image unrelated to this exact subject."
                        f"{legibility_note}{style_note}{spelling_note}{contradiction_note}"
                    )
                    try:
                        retried_data = await self._draw(
                            prompt=retry_prompt, size=image_size or self.settings.image_size, provider=provider
                        )
                        retried_relevance = await self._verify_image_relevance(
                            retried_data, brief=brief, course_title=course_title, textbook_style=is_textbook,
                            source_excerpt=grounding_excerpt,
                        )
                        candidates.append((retried_data, retry_prompt, retried_relevance))
                        data, relevance, prompt = retried_data, retried_relevance, retry_prompt
                        # OCR phase: re-run OCR on the new candidate before
                        # deciding whether to stop retrying - a candidate
                        # vision calls "clean" must still keep retrying if
                        # OCR confirms a real, ground-truth-backed defect
                        # vision missed.
                        if not (
                            _scientific_check_failed(relevance) or _text_check_failed(relevance)
                            or _ocr_confirms_text_defect(data)[0]
                        ):
                            break
                    except Exception as exc:  # noqa: BLE001 - keep every candidate generated so far, don't fail the block
                        log.warning("Image relevance retry failed for %s: %s", block.id, exc)
                        break

                def _candidate_rank(candidate: tuple[bytes, str, ImageRelevanceCheck]) -> tuple[int, int]:
                    """Scientific correctness strictly outranks text
                    correctness: the first tuple element always dominates
                    the second, so a candidate with a confirmed scientific/
                    structural defect can never be selected as best purely
                    because its text happens to be cleaner. The ranking
                    SHAPE and priority order are unchanged by the OCR phase
                    - OCR only enriches what counts as a text defect on the
                    second axis (same as the holistic vision check already
                    does), never adds a third axis and never touches the
                    scientific axis at all."""
                    candidate_data, _, check = candidate
                    text_failed = _text_check_failed(check) or _ocr_confirms_text_defect(candidate_data)[0]
                    return (1 if _scientific_check_failed(check) else 0, 1 if text_failed else 0)

                # Best Candidate Image Tracking: explicitly compare every
                # candidate generated above - never assume the most recently
                # drawn one is the best. `min()` over an `enumerate()`d list
                # is stable: on an exact tie it returns the FIRST (lowest-
                # index) candidate, so the original image is preferred over
                # an equally-good-or-worse later retry, while a genuinely
                # later improvement (a strictly lower rank) still correctly
                # wins - no recency bias in either direction.
                best_index, (data, prompt, relevance) = min(
                    enumerate(candidates), key=lambda item: _candidate_rank(item[1])
                )
                log.info(
                    "Best Candidate Image Tracking selected candidate %s of %s for %s "
                    "(scientific_failed=%s, text_failed=%s)",
                    best_index + 1, len(candidates),
                    block.id, _scientific_check_failed(relevance), _text_check_failed(relevance),
                )

            # Scientific/structural findings - deliberately never includes a
            # text-accuracy issue (Best Candidate Image Tracking fix: that is
            # its own separate list below, `text_findings`, with its own
            # resolution/severity - the two must never be merged into one
            # combined verdict). A confirmed defect here still withholds the
            # image (unchanged from before this fix) - preserving a
            # scientifically-wrong-or-off-topic image would never be a
            # genuine improvement, unlike a spelling defect.
            if _scientific_check_failed(relevance):
                log.warning(
                    "Image for %s still failed its scientific/structural relevance check after retry - "
                    "will be omitted rather than published, not silently accepted as correct", block.id,
                )
                issues: list[tuple[str, str, bool]] = []
                if relevance.errored:
                    issues.append((
                        "image_relevance_check_error",
                        "The image relevance check could not be completed (vision call failed) "
                        "after every bounded attempt - never treated as a confirmed pass.",
                        False,
                    ))
                if not relevance.on_topic:
                    issues.append(("off_topic_image", relevance.on_topic_detail or "Image does not depict the requested topic.", False))
                if not relevance.labels_legible:
                    issues.append((
                        "overlapping_or_illegible_labels",
                        relevance.labels_legible_detail
                        or "Text labels/callouts overlap each other or other elements, unreadable.",
                        False,
                    ))
                if not relevance.genuinely_educational:
                    issues.append(("decorative_not_educational", relevance.educational_detail or "Image reads as decorative, not educational.", False))
                if is_textbook and not relevance.style_compliant:
                    issues.append((
                        "textbook_style_not_followed",
                        relevance.style_compliant_detail
                        or "Image does not follow the required flat 2D vector textbook style.",
                        False,
                    ))
                if relevance.contradicts_source_text:
                    issues.append((
                        "contradicts_source_text",
                        relevance.contradicts_source_text_detail
                        or "Image depicts something that contradicts the course's own text.",
                        False,
                    ))
                relevance_findings = visual_qa_finding_entries(issues, resolution="image_relevance_unverified")

            # Strategy D (text-preservation fix): a confirmed text defect on
            # the best available candidate - from the HOLISTIC vision check
            # (`text_accurately_spelled=False`) and/or the OCR phase's own
            # deterministic, ground-truth-backed confirmation - as opposed
            # to the separate declared-label `text_verified` signal (handled
            # entirely via the pre-existing `image_text_unverified` quality_
            # framework finding, keyed off `content["text_verified"]`
            # directly) - no longer feeds `relevance_findings` above, so it
            # can no longer trigger withholding on its own. It gets its own
            # separate finding/resolution instead, carrying the best
            # correction information actually available - a scientifically-
            # sound diagram is shipped, flagged for a human, rather than
            # discarded over a spelling defect.
            #
            # `deterministic` here is OCR's one real effect on the finding
            # itself (never on the ship/withhold DECISION, which stays
            # governed purely by scientific soundness): when OCR confirms a
            # mismatch against real, known-correct ground truth, the finding
            # is marked `deterministic=True` -> `verification_status=
            # "independently_verified"` (quality_framework.py), correctly
            # distinguishing "an LLM thought this might be wrong" from "a
            # deterministic check confirmed this specific word against
            # known-correct text" - a real severity/confidence upgrade, not
            # a new decision path. Vision-only (no OCR backing, or OCR
            # unavailable/uncertain) stays `deterministic=False`, byte-for-
            # byte the same as before the OCR phase.
            ocr_confirmed, ocr_detail = _ocr_confirms_text_defect(data)
            if is_textbook and (_text_check_failed(relevance) or ocr_confirmed):
                combined_detail = " ".join(
                    part for part in (
                        relevance.text_accurately_spelled_detail if _text_check_failed(relevance) else "",
                        ocr_detail,
                    ) if part
                ) or "Text baked into the image is misspelled or garbled."
                log.warning(
                    "Image for %s still has a text-accuracy defect after retry (vision=%s, ocr_confirmed=%s, "
                    "%s) - shipping the best available candidate anyway (scientifically/structurally sound), "
                    "flagged for human review with the correction detail attached, rather than withheld",
                    block.id, _text_check_failed(relevance), ocr_confirmed, combined_detail,
                )
                text_findings = visual_qa_finding_entries(
                    [("text_misspelled_or_garbled", combined_detail, ocr_confirmed)],
                    resolution="text_defect_preserved_with_mapping",
                )
            # Canonical Visual Spec milestone: a claim the source excerpt
            # simply doesn't ADDRESS (neither confirms nor contradicts) is a
            # genuinely different, less severe case than the confirmed
            # relevance failures above - never withheld (this must NOT feed
            # `confirmed_relevance_failure`, hence its own separate list,
            # only ever merged into the SUCCESS path's `visual_qa_findings`
            # below), but never silently invisible either. Routes to the
            # REVIEW outcome (a `"blocker"`-severity, `deterministic=False`
            # finding - see quality_framework.py's `review_findings`), the
            # same "flag for a human, don't auto-approve, don't auto-reject"
            # treatment the matching `diagram_content_qa` field gets. Still
            # inside `if not is_safety_fallback` (where `relevance` is
            # actually defined) but OUTSIDE `if _relevance_failed(...)` -
            # an otherwise-clean image can still carry an unsupported claim.
            if relevance.unsupported_claims:
                source_grounding_findings = visual_qa_finding_entries(
                    [(
                        "source_grounding_unsupported",
                        "The course's own text doesn't address: " + "; ".join(relevance.unsupported_claims[:5]),
                        False,
                    )],
                    resolution="source_grounding_unsupported",
                )
            # Phase 2/5 (Canonical Visual Spec milestone): the same
            # deterministic, zero-API-call numeric check `diagram_content_qa.
            # evaluate_content` runs for a structured diagram, applied here
            # to the raster path's own BRIEF (what the image was ASKED to
            # depict) against the grounding excerpt - see
            # app.services.numeric_consistency_qa's own docstring on why
            # this is a source-CONSISTENCY check, never a truth check. A
            # confirmed, high-confidence mismatch is at least as
            # disqualifying as a confirmed LLM-judged contradiction above -
            # folded into the SAME `relevance_findings` list (resolution
            # "image_relevance_unverified"), which already drives
            # `confirmed_relevance_failure` and withholding below,
            # unchanged mechanism, never a new one.
            numeric_issues = find_numeric_contradictions(brief, grounding_excerpt)
            if numeric_issues:
                relevance_findings = relevance_findings + visual_qa_finding_entries(
                    [(issue.reason, issue.detail, True) for issue in numeric_issues],
                    resolution="image_relevance_unverified",
                )
            # Short-identifier hardening (Canonical Visual Spec milestone):
            # same split as `diagram_content_qa._numeric_contradiction_
            # issues` - a CONFIRMED identifier mismatch (Si=28 vs the
            # source's own Si=14) joins the same hard-block path as the
            # plain numeric case just above; an AMBIGUOUS one (identifier
            # never resolves confidently) is NOT a block - it folds into
            # `source_grounding_findings` instead, the same "flag for
            # review, ship anyway" treatment `unsupported_claims` already
            # gets a few lines up, never `confirmed_relevance_failure`.
            for issue in find_identifier_value_issues(brief, grounding_excerpt):
                if issue.reason == IDENTIFIER_VALUE_NEEDS_REVIEW:
                    source_grounding_findings = source_grounding_findings + visual_qa_finding_entries(
                        [(issue.reason, issue.detail, False)],
                        resolution="source_grounding_unsupported",
                    )
                else:
                    relevance_findings = relevance_findings + visual_qa_finding_entries(
                        [(issue.reason, issue.detail, True)],
                        resolution="image_relevance_unverified",
                    )
        if (block.content.get("illustration_style") or "").strip().lower() == "section_intro":
            data = self._resize_section_intro(data)
        # Mathematical/scientific correctness of a depicted governing
        # equation - deterministic, AST-based, never an OCR read or the
        # generating model's own self-certification (see formula_qa's own
        # docstring on why). `formula_expression` is blank for every
        # illustration that isn't a formula block (the overwhelming
        # majority), so this is a no-op then - `formula_verified` stays
        # `None`, never a false claim of having checked something that
        # wasn't there to check. Computed BEFORE the publication-eligibility
        # decision below (moved up from its previous position, after the now-
        # conditional `save_asset`) so both branches below can record it.
        formula_expression = str(block.content.get("formula_expression") or "").strip()
        formula_verified: bool | None = None
        formula_findings: list[dict] = []
        if formula_expression:
            formula_result = evaluate_formula(formula_expression)
            formula_verified = formula_result.verifiable and formula_result.passed
            if not formula_result.passed:
                log.warning(
                    "Formula verification failed for %s (%s) - flagged, not silently shipped as correct",
                    block.id, [issue.reason for issue in formula_result.issues],
                )
                formula_findings = visual_qa_finding_entries(
                    [(issue.reason, issue.detail, True) for issue in formula_result.issues],
                    resolution="formula_verification_failed",
                )
        # merge_content is a shallow merge - `visual_qa_findings` would
        # otherwise be REPLACED wholesale, silently erasing a finding
        # app.services.diagram_service just persisted on this exact block a
        # moment earlier (a real, confirmed risk: a failed diagram falls
        # back to THIS SAME illustration call with its "fell_back_to_
        # illustration" finding already sitting in block.content). Keep
        # every existing entry except a STALE formula/relevance/fallback one
        # from an earlier attempt on this same block (never double-stack
        # across re-renders) - `generic_fallback_published`/`image_withheld_
        # after_qa_failure` belong in this set too (a real, confirmed bug: a
        # block that fell back once kept that finding forever, even after a
        # LATER regeneration succeeded cleanly with the real, on-topic image
        # - misreporting a defect that no longer existed). `generic_
        # fallback_published` is kept here even though nothing persists it
        # any more (fallback-cascade root-cause fix) so a course saved
        # before that fix still gets its stale finding cleared on its next
        # successful regeneration.
        _stale_resolutions = {
            "formula_verification_failed", "image_relevance_unverified",
            "generic_fallback_published", "image_withheld_after_qa_failure",
            "safety_fallback_relevance_unconfirmed", "text_defect_preserved_with_mapping",
        }
        existing_findings = [
            entry for entry in (block.content.get("visual_qa_findings") or [])
            if isinstance(entry, dict) and entry.get("resolution") not in _stale_resolutions
        ]
        # Fail-closed publication gate (production risk follow-up): a
        # CONFIRMED raster defect - required/attempted text that still reads
        # wrong after EVERY bounded attempt (`text_verified is False`, never
        # `None` - "nothing was promised" never reaches here), or an image
        # confirmed off-topic/non-educational after its own bounded retry
        # (`relevance_findings` non-empty) - must never be persisted as a
        # successful, publishable asset merely because the provider call
        # itself returned bytes. Reuses the exact same "not yet generated"
        # state `document/layout.py.image_generation_failed` and this same
        # method's own content-safety except-handler already use for a real
        # generation failure (`path=None`, `generated=False`) - deliberately
        # `error=None` here (NOT a technical failure, and must NOT trip
        # quality_framework's separate, always-"blocker" `image_generation_
        # failed` finding; the real reason is already recorded, at its own
        # already-established severity, via `text_verified`/
        # `visual_qa_findings` below - unchanged by this branch). Formula
        # correctness is deliberately NOT included here - see formula_qa's
        # own module docstring on why a wrong equation is never silently
        # "fixed" or withdrawn, only ever flagged; this is a materially
        # different defect class (bad raster text/relevance has no
        # "could silently replace correct content with a NEW wrong one"
        # risk the way an auto-corrected formula would).
        # Best Candidate Image Tracking / Strategy D (text-preservation
        # fix): a purely TEXT defect on an otherwise scientifically/
        # structurally sound, NORMALLY-generated `textbook`-style image no
        # longer withholds the image - see the branch below. This still
        # withholds (unchanged original behaviour) for every case where
        # Strategy D's reasoning does not apply:
        #   - force-text-free/non-"textbook" styles promise NO text at all
        #     (see DEFAULT_ILLUSTRATION_GUIDANCE's STRICT NO-TEXT RULE) -
        #     there is no "correct" text to map to or preserve when none was
        #     ever supposed to exist.
        #   - the content-safety emergency fallback (`is_safety_fallback`) is
        #     a single-shot generation with no candidate comparison to prefer
        #     a "scientifically sound" option from - shipping CONFIRMED-WRONG
        #     required text here (e.g. a safety-critical label like "Pressure
        #     Valve"/"Safety Interlock" on a dangerous-machinery diagram) is
        #     never an improvement over no image at all, so this path keeps
        #     its original, stricter contract unchanged.
        confirmed_unexpected_text = (
            (not is_textbook or force_text_free or is_safety_fallback) and text_verified is False
        )
        # Scientific/structural only (Best Candidate Image Tracking fix) -
        # `relevance_findings` no longer contains a text-accuracy issue, see
        # `_scientific_check_failed`/`_text_check_failed` above.
        confirmed_relevance_failure = bool(relevance_findings)
        # Used only for logging and to keep the overlay-label-suggestion
        # gate below correctly suppressed for a Strategy-D ship (a NEW
        # overlay label should never be suggested for an image already
        # known to carry a text defect) - never used to trigger withholding
        # on its own; see `confirmed_unexpected_text` above for the one
        # text-failure case that still does.
        confirmed_text_failure = (text_verified is False) or bool(text_findings)
        if confirmed_unexpected_text or confirmed_relevance_failure:
            # Fallback-cascade root-cause fix: every real attempt at the
            # actual brief failed its own bounded QA, and Wikimedia (tried
            # BEFORE any of this, at the very top of this method - see
            # `_try_wikimedia_image`) already had nothing suitable either.
            # This used to draw one more, deliberately generic/abstract
            # picture here and publish it as if it were a genuine answer to
            # the brief - exactly the "generic decorative placeholder...as a
            # substitute for educational content" this pipeline must never
            # ship: a reader cannot tell a published image apart from a
            # real, verified one, so an unrelated generic picture is
            # actively MISLEADING, not merely decorative. Withholding the
            # image instead (same `omitted_after_qa_failure` state a
            # technical generation failure already uses, so the page gets
            # the same compact placeholder either way) is the honest
            # outcome - the course's text content is completely unaffected,
            # and exactly why is recorded in `visual_qa_findings` below for
            # a human reviewer, never papered over with a picture that
            # doesn't actually depict anything real. A scientific/structural
            # defect reaches here regardless of text status (Best Candidate
            # Image Tracking never ships a scientifically-wrong diagram just
            # because its text is clean); `confirmed_unexpected_text` is the
            # one remaining text-only path that still withholds (see its own
            # docstring above).
            log.warning(
                "Image for %s failed confirmed QA after every bounded attempt (unexpected_text=%s, "
                "relevance_failed=%s) and no Wikimedia match was available - withholding the image "
                "rather than publishing an unrelated generic picture in its place",
                block.id, confirmed_unexpected_text, confirmed_relevance_failure,
            )
            withheld_findings = visual_qa_finding_entries(
                [(
                    "image_withheld_after_qa_failure",
                    "Every attempt failed quality checks (garbled text or off-topic/decorative) and no "
                    "suitable Wikimedia image was found - withheld rather than replaced with an unrelated "
                    "generic illustration. Regenerate, or replace with a specific image.",
                    # True (production-readiness audit fix), not False: the
                    # REASON this image was withheld is an AI judgment call
                    # (already recorded, as such, via the preserved
                    # `relevance_findings`/`text_verified` below) - but the
                    # fact THIS finding reports is simply "no image exists
                    # for this block", a directly-checkable fact
                    # (`content["path"] is None`), not a judgment. Mirrors
                    # the genuine-technical-failure finding's own reasoning
                    # (quality_framework.py's hardcoded `image_generation_
                    # failed`, `verification_status="independently_
                    # verified"`): without this, `deterministic=False` ->
                    # `verification_status="unverified"` -> quality_
                    # framework.QualityReport.approval_blocking_findings'
                    # `severity == "blocker" and verification_status !=
                    # "unverified"` gate would silently EXCLUDE this
                    # "blocker" finding, letting a course with a completely
                    # missing image still pass approval - exactly the
                    # "silently treated as fully quality-approved" failure
                    # mode this field must prevent.
                    True,
                )],
                resolution="image_withheld_after_qa_failure",
            )
            block.content = merge_content(
                block.type,
                block.content,
                {
                    "path": None,
                    "asset_id": None,
                    "generated": False,
                    "error": None,
                    # Narrow, explicit state (layout-defect follow-up) - see
                    # ImageContent.omitted_after_qa_failure's own docstring.
                    # This is what lets app.course.document.layout.image_
                    # generation_failed tell this apart from "simply not
                    # generated yet" and give it the same compact
                    # placeholder a technical failure gets, WITHOUT setting
                    # `error` (which would incorrectly trip quality_
                    # framework's separate, always-"blocker" image_
                    # generation_failed finding - unchanged by this field,
                    # which that function never reads; `image_withheld_
                    # after_qa_failure` is its own equally-"blocker"
                    # resolution instead, see quality_framework.py).
                    "omitted_after_qa_failure": True,
                    "prompt": prompt,
                    "width": None,
                    "height": None,
                    # None, not the confirmed-False value - no image is
                    # being published at all, so there is nothing left to
                    # make a "this image's text" claim about; the
                    # `image_withheld_after_qa_failure` finding below is
                    # the single, authoritative record of why (and already
                    # names "garbled text" as one real cause). Without
                    # this reset, quality_framework's separate `text_
                    # verified is False` check would also fire its own
                    # `image_text_unverified` finding describing a picture
                    # that was never actually shipped.
                    "text_verified": None,
                    "formula_verified": formula_verified,
                    "visual_qa_findings": (
                        existing_findings + formula_findings + relevance_findings
                        + safety_fallback_relevance_findings + text_findings + withheld_findings
                    ),
                    "generation_mode": "mock" if self.ai.is_mock else "real",
                },
            )
            return False
        relative = self.storage.save_asset(course_id, data, extension=self._detect_extension(data))
        width, height = self._dimensions(data)
        # Edit Visuals Part 2 - suggested overlay labels, only for a
        # genuine, on-brief ship: never for the deliberately generic/
        # content-safety/confirmed-QA-failure fallback paths above (each
        # produces an image that no longer depicts the original, specific
        # brief, so labels drawn from that brief would name parts the
        # picture doesn't actually show). Gated the same way diagram/
        # concept_experience generation already is (a settings flag), and
        # skipped entirely for `section_intro` - those are deliberately
        # simple, single-subject, no-label-wanted images by design.
        overlay_labels: list[dict] = []
        illustration_style = (block.content.get("illustration_style") or "").strip().lower()
        if (
            self.settings.enable_image_overlay_suggestions
            and not is_safety_fallback
            and not (confirmed_text_failure or confirmed_relevance_failure)
            and illustration_style != "section_intro"
        ):
            overlay_brief = "\n".join(
                part for part in (
                    str(block.content.get("purpose") or ""),
                    str(block.content.get("prompt") or ""),
                    str(block.content.get("caption") or ""),
                ) if part
            )[:MAX_PROMPT_CHARS]
            suggestions = await self._suggest_overlay_labels(
                data, brief=overlay_brief, course_title=course_title
            )
            overlay_labels = [
                {"id": f"ol{i}", "text": label.text, "x_pct": label.x_pct, "y_pct": label.y_pct, "style": label.style, "color": ""}
                for i, label in enumerate(suggestions.labels)
                if label.text.strip()
            ]
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                # A genuinely successful ship - any earlier QA-omission flag
                # from a previous, now-superseded render of this same block
                # must not linger and wrongly shrink THIS valid image's
                # reserved layout space.
                "omitted_after_qa_failure": False,
                "prompt": prompt,
                "width": width,
                "height": height,
                "text_verified": text_verified,
                "formula_verified": formula_verified,
                "overlay_labels": overlay_labels,
                "visual_qa_findings": (
                    existing_findings + formula_findings + relevance_findings
                    + safety_fallback_relevance_findings + source_grounding_findings + text_findings
                ),
                "generation_mode": "mock" if self.ai.is_mock else "real",
            },
        )
        if confirmed_text_failure:
            # Strategy D (text-preservation fix): shipped despite a known
            # text defect - the course's own `review_findings`/review-brief
            # surface (quality_framework.py) now carries exactly why, via
            # `image_text_unverified` (declared-label case, reads
            # `expected_labels` directly) and/or the `text_findings` entry
            # above (holistic case) - never silently indistinguishable from
            # a genuinely clean ship.
            log.info(
                "Generated %s for block %s with a known, flagged text defect preserved "
                "(scientifically/structurally sound) rather than withheld", relative, block.id,
            )
        else:
            log.info("Generated %s for block %s", relative, block.id)
        return True

    @staticmethod
    def missing_image_blocks(
        document: CourseDocument, *, only_block_ids: list[str] | None = None, force: bool = False
    ) -> list[Block]:
        """Every IMAGE block `generate_missing` would (re)generate - exposed
        so a caller can get an accurate total (for progress reporting)
        before the batch starts, without duplicating this filter."""
        wanted = set(only_block_ids) if only_block_ids else None
        targets: list[Block] = []
        for _, block in document.iter_blocks():
            if block.type is not BlockType.IMAGE:
                continue
            if wanted is not None and block.id not in wanted:
                continue
            if block.content.get("path") and not force:
                continue
            targets.append(block)
        return targets

    async def generate_missing(
        self,
        *,
        document: CourseDocument,
        template: CourseTemplate,
        only_block_ids: list[str] | None = None,
        force: bool = False,
        on_progress: Callable[[Block, bool], Awaitable[None]] | None = None,
        job_id: str = "",
    ) -> int:
        """Generate every image the document is still missing. Returns the count.

        `on_progress`, when given, fires once per image AS IT FINISHES
        (success or failure) rather than only after the whole batch - the
        caller (`CourseService._finalise`) uses this to persist each image's
        result immediately and keep a real "N of M" counter, instead of
        losing every completed image in the batch if a later one crashes.

        `job_id` tags every freshly-generated block with the id of the run
        that validated it (`content["_qa_job_id"]`) - `VisualReviewService.
        review_document`, called later in the SAME run, uses this to skip
        re-spending a vision call re-checking an image that was just
        checked moments ago. Defaults to a fresh id when the caller doesn't
        have one (e.g. an ad-hoc "regenerate missing images" call).
        """
        targets = self.missing_image_blocks(document, only_block_ids=only_block_ids, force=force)
        if not targets:
            return 0

        # Computed once over the whole document (see chapter_section_context's
        # own docstring) rather than per-block, so a big course doesn't repeat
        # the same linear walk once per image block.
        context_map = chapter_section_context(document)
        limiter = get_limiter("image", self.settings.concurrency_for("image"))
        job_id = job_id or str(uuid.uuid4())

        async def worker(block: Block) -> bool:
            chapter_title, section_title, key_concept, source_excerpt = context_map.get(
                block.id, ("", "", "", "")
            )
            async with limiter.slot():
                ok = await self.generate_for_block(
                    course_id=document.course_id,
                    block=block,
                    template=template,
                    course_title=document.course_title,
                    chapter_title=chapter_title,
                    section_title=section_title,
                    key_concept=key_concept,
                    source_excerpt=source_excerpt,
                )
                if ok:
                    # Marks this block as validated during THIS job - see
                    # VisualReviewService.review_document's own skip logic,
                    # which avoids re-spending a vision call re-checking an
                    # image that was just checked moments ago in the same run.
                    block.content["_qa_job_id"] = job_id
                return ok

        async def tracked(block: Block) -> bool:
            ok = await worker(block)
            if on_progress is not None:
                await on_progress(block, ok)
            return ok

        with metrics_phase("images"):
            results = await asyncio.gather(*(tracked(block) for block in targets))
        generated = sum(1 for ok in results if ok)
        log.info("Generated %s/%s images for %s", generated, len(targets), document.course_id)
        return generated
