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

from pydantic import BaseModel, Field

from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import phase as metrics_phase
from app.schemas.blocks import BlockType, merge_content
from app.schemas.document import Block, CourseDocument
from app.schemas.template import CourseTemplate
from app.services.concept_visual_service import ConceptVisualService
from app.services.diagram_service import DiagramService
from app.services.formula_qa import evaluate_formula
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client
from app.services.quality_framework import visual_qa_finding_entries
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

MAX_PROMPT_CHARS = 4500

_CONTEXT_FIELD_CHAR_CAP = 120  # a title is always short; this only guards against a pathological outlier


_KEY_CONCEPT_CHAR_CAP = 200


def chapter_section_context(document: CourseDocument) -> dict[str, tuple[str, str, str]]:
    """Maps every block id to (chapter_title, section_title, key_concept).

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
    return {
        block_id: (ch, se, chapter_key_concept.get(ch) or chapter_fallback_concept.get(ch, ""))
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
    errored: bool = False

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
    "MULTI-PANEL COMPOSITIONS: when the brief covers 2-3 closely related "
    "sub-concepts that build on each other (e.g. a field around a source, "
    "then the effect that field produces, then the governing law), compose "
    "them as separate bordered panels arranged in a clean grid within the "
    "one image, each panel with its own short, bold section title in a "
    "colored header strip at its top (e.g. \"Magnetic Field\", \"Induced "
    "Current\") and its own pastel background tint - this in-panel section "
    "title is a structural label for that panel's own sub-topic, not the "
    "image's overall course/chapter caption, so it's expected and welcome "
    "here even though the rule below still applies to the course/chapter "
    "name itself.\n"
    "FORMULA BLOCK: if the concept has one key governing equation, you may "
    "include it once, in its own clearly bordered box (distinct from the "
    "illustration panels), set in large clean mathematical notation, with "
    "each symbol it uses defined immediately below in short \"symbol = "
    "meaning\" lines (exactly like a textbook's own equation callout) - "
    "spell every defined term correctly and completely, nothing truncated "
    "or cut off.\n"
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
            [str(label).strip() for label in (content.get("expected_labels") or []) if str(label).strip()]
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
        parts = [
            context_line,
            content.get("prompt") or content.get("purpose") or content.get("caption") or "",
            labels_line,
            style_guidance,
            "Do not include watermarks, signatures or lorem ipsum text.",
        ]
        return "\n".join(part for part in parts if part)[:MAX_PROMPT_CHARS]

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
        self, image: bytes, *, brief: str, course_title: str
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
        docstring, production risk follow-up)."""
        if not brief.strip():
            return ImageRelevanceCheck()
        try:
            return await self.ai.structured(
                schema=ImageRelevanceCheck,
                system=(
                    "You are a strict reviewer judging whether a generated educational image "
                    "actually depicts what it was asked to depict. Look at the attached image "
                    "and judge two things: (1) `on_topic` - does the image's actual visual "
                    "content genuinely, specifically depict the stated brief, not a generic, "
                    "unrelated, or merely thematically-adjacent picture? (2) `genuinely_"
                    "educational` - does this read as a real teaching aid for the brief, not a "
                    "purely decorative/stock-photo-style image that happens not to contradict "
                    "it? Be concrete in every detail field: name exactly what the image actually "
                    "shows and why that does or doesn't match, never a bare \"could be better\"."
                ),
                user=f"COURSE: {course_title}\n\nBRIEF (what this image is supposed to depict):\n{brief}\n\n"
                "Judge the attached image against this brief.",
                model=self.settings.diagram_model,
                purpose="image_relevance_check",
                phase="image",
                image=image,
            )
        except Exception as exc:  # noqa: BLE001 - never block on a QA-step failure
            log.warning("Image relevance check failed (inconclusive, never treated as a pass): %s", exc)
            return ImageRelevanceCheck(errored=True)

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
        path."""
        size = size or self.settings.image_size
        data = await self.ai.image(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size, provider=provider)
        found = await self._verify_image_has_no_text(data)
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
        data = await self.ai.image(prompt=final_prompt, size=size, provider=provider)
        found = await self._verify_image_has_no_text(data)
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
    ) -> bool:
        """Diagram/concept_experience blocks try their structured path first
        and fall back to the raster illustration path on any failure - a
        block must never end up with nothing just because a specialised path
        had trouble."""
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
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
        elif kind == "concept_experience" and self.settings.enable_concept_experience_visuals:
            ok = await self.concept_visuals.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
        return await self._generate_illustration(
            course_id=course_id,
            block=block,
            template=template,
            course_title=course_title,
            chapter_title=chapter_title,
            section_title=section_title,
            key_concept=key_concept,
            force_text_free=structured_fallback,
        )

    async def _generate_with_text_check(
        self,
        prompt: str,
        expected_labels: list[str],
        *,
        block_id: str,
        provider: str | None = None,
        size: str | None = None,
    ) -> tuple[bytes, bool]:
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
        Returns (image bytes, text_verified). `text_verified` is True only
        when some attempt's check GENUINELY completed and found every label
        correct (`missing == []`) - a vision-check exception (`missing is
        None`, see `_verify_generated_text`) is never treated the same as a
        genuine pass, at any of the four bounded attempts below; it falls
        through to the next attempt exactly like a confirmed wrong answer
        would, and the final, honest `text_verified` is False either way."""
        size = size or self.settings.image_size
        data = await self.ai.image(prompt=prompt, size=size, provider=provider)
        missing = await self._verify_generated_text(data, expected_labels)
        if missing == []:
            return data, True

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
        data = await self.ai.image(prompt=retry_prompt, size=size, provider=provider)
        missing = await self._verify_generated_text(data, expected_labels)
        if missing == []:
            return data, True

        log.info(
            "Image text check failed again for %s (%s) - attempting a text-free image", block_id,
            missing if missing is not None else "vision check inconclusive",
        )
        data = await self.ai.image(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size, provider=provider)
        # Trust nothing on faith: a "text-free" request can still come back
        # with partial, wrong text (the confirmed "Oxgeen" case) - checked
        # against the exact same expected labels, since any of them
        # appearing wrong is exactly as bad as it was on a labelled attempt.
        missing = await self._verify_generated_text(data, expected_labels)
        if missing == []:
            return data, True

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
        data = await self.ai.image(prompt=final_prompt, size=size, provider=provider)
        missing = await self._verify_generated_text(data, expected_labels)
        # Whatever this final, bounded attempt produced is what ships - but
        # the caller is told the honest truth about whether it's clean.
        # `missing == []` is deliberately NOT `not missing`: that would also
        # be True for `None` (a vision-check exception, Python's own
        # falsiness rule for None), silently treating "could not verify" as
        # "verified correct" - the exact conflation this fix exists to close.
        return data, missing == []

    @staticmethod
    def _safety_fallback_prompt(
        *,
        course_title: str,
        chapter_title: str,
        section_title: str,
        key_concept: str,
        expected_labels: list[str] | None = None,
    ) -> str:
        """A generic, policy-conscious prompt for the ONE bounded retry after
        a content-safety rejection (see `_is_content_safety_violation`'s own
        docstring on why this drops the writer's free-text prompt entirely).
        Deliberately abstract/symbolic rather than depicting anything
        specific enough to risk the same or a different policy rule.

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
        topic = key_concept.strip() or section_title.strip() or chapter_title.strip() or course_title.strip()
        base = (
            f"A clean, abstract, symbolic educational illustration representing the general "
            f"theme of \"{topic}\" for a course titled \"{course_title}\". Use simple geometric "
            "shapes, soft colors and generic iconography (e.g. gears, books, charts, arrows, "
            "lightbulbs) rather than depicting any specific person, scene, brand, weapon, or "
            "sensitive subject matter. Nothing graphic, violent, political, or controversial - "
            "purely decorative and safe for an educational audience of any age."
        )
        labels = [label.strip() for label in (expected_labels or []) if label.strip()]
        if not labels:
            return f"{base}{_NO_TEXT_SUFFIX}"
        return (
            f"{base}\n\nThis illustration must still clearly label these exact parts, spelled "
            f"correctly, in large legible lettering, even though the overall scene is generic/"
            f"abstract: {', '.join(labels)}."
        )

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
        prompt = self.build_prompt(
            block, template, course_title,
            chapter_title=chapter_title, section_title=section_title, key_concept=key_concept,
        )
        if not prompt.strip():
            log.warning("Image block %s has no prompt - skipped", block.id)
            return False
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
        expected_labels = [
            str(label).strip() for label in (block.content.get("expected_labels") or []) if str(label).strip()
        ]
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
        image_size = (
            "1536x1024" if is_textbook and len(expected_labels) >= 4 else None
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
                data, text_verified = await self._generate_with_text_check(
                    prompt, expected_labels, block_id=block.id, provider=provider, size=image_size
                )
            else:
                data = await self.ai.image(
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
            safe_prompt = self._safety_fallback_prompt(
                course_title=course_title,
                chapter_title=chapter_title,
                section_title=section_title,
                key_concept=key_concept,
                expected_labels=expected_labels,
            )
            log.warning(
                "Image generation for %s was rejected by the provider's content-safety filter "
                "(%s) - retrying once with a generic, topic-only safe-fallback prompt", block.id, exc,
            )
            try:
                data = await self.ai.image(
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
                "will be omitted rather than published, not silently accepted as correct", block.id,
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
        # kind of error). Skipped for the deliberately-generic safety
        # fallback (see `is_safety_fallback`'s own comment above).
        relevance_findings: list[dict] = []
        if not is_safety_fallback:
            brief = "\n".join(
                part for part in (
                    str(block.content.get("purpose") or ""),
                    str(block.content.get("prompt") or ""),
                    str(block.content.get("caption") or ""),
                ) if part
            )[:MAX_PROMPT_CHARS]
            relevance = await self._verify_image_relevance(data, brief=brief, course_title=course_title)
            # `relevance.errored` is checked explicitly, alongside on_topic/
            # genuinely_educational - a vision-call exception leaves BOTH of
            # those at their safe default (True), so without this check an
            # errored (never actually verified) attempt would silently fall
            # straight through as if it had genuinely passed (production
            # risk follow-up: the same ERROR-vs-PASS conflation fixed for
            # the text-legibility checks above).
            if relevance.errored or not (relevance.on_topic and relevance.genuinely_educational):
                log.info(
                    "Image relevance check failed for %s (on_topic=%s, genuinely_educational=%s, "
                    "errored=%s) - retrying once with the brief re-emphasised",
                    block.id, relevance.on_topic, relevance.genuinely_educational, relevance.errored,
                )
                retry_prompt = (
                    f"{prompt}\n\nIMPORTANT: the previous attempt did not clearly depict the actual "
                    f"subject. This image MUST specifically, unmistakably depict: {brief or course_title}. "
                    "Do not produce a generic or decorative image unrelated to this exact subject."
                )
                try:
                    retried_data = await self.ai.image(
                        prompt=retry_prompt, size=image_size or self.settings.image_size, provider=provider
                    )
                    retried_relevance = await self._verify_image_relevance(
                        retried_data, brief=brief, course_title=course_title
                    )
                    if (
                        not retried_relevance.errored
                        and retried_relevance.on_topic
                        and retried_relevance.genuinely_educational
                    ):
                        data, relevance, prompt = retried_data, retried_relevance, retry_prompt
                except Exception as exc:  # noqa: BLE001 - keep the first image, don't fail the block
                    log.warning("Image relevance retry failed for %s: %s", block.id, exc)
                if relevance.errored or not (relevance.on_topic and relevance.genuinely_educational):
                    log.warning(
                        "Image for %s still failed relevance check after retry - will be omitted "
                        "rather than published, not silently accepted as correct", block.id,
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
                    if not relevance.genuinely_educational:
                        issues.append(("decorative_not_educational", relevance.educational_detail or "Image reads as decorative, not educational.", False))
                    relevance_findings = visual_qa_finding_entries(issues, resolution="image_relevance_unverified")
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
        # every existing entry except a STALE formula/relevance one from an
        # earlier attempt on this same block (never double-stack across
        # re-renders), then add this attempt's own findings, if any.
        _stale_resolutions = {"formula_verification_failed", "image_relevance_unverified"}
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
        confirmed_text_failure = text_verified is False
        confirmed_relevance_failure = bool(relevance_findings)
        if confirmed_text_failure or confirmed_relevance_failure:
            # Every real attempt at the actual brief failed its own QA - but
            # a blank "Image not generated yet" placeholder is a worse
            # outcome for every page than a clean, generic, still
            # chapter-themed illustration, so this is never the end of the
            # road: one final attempt drops exactly what broke the last few
            # tries - any specific labels (the source of every confirmed
            # text failure above) and any claim of depicting the precise
            # brief (the source of every confirmed relevance failure) -
            # reusing the same generic/abstract/policy-safe prompt builder
            # the content-safety path already trusts enough to ship
            # unverified (see `is_safety_fallback` below). This can only
            # ever be reached after a genuine, confirmed QA failure, never
            # in place of a normal successful generation.
            log.warning(
                "Image for %s failed confirmed QA after every bounded attempt (text_failed=%s, "
                "relevance_failed=%s) - trying one generic, label-free fallback before giving up "
                "on this block entirely", block.id, confirmed_text_failure, confirmed_relevance_failure,
            )
            fallback_prompt = self._safety_fallback_prompt(
                course_title=course_title,
                chapter_title=chapter_title,
                section_title=section_title,
                key_concept=key_concept,
                expected_labels=None,
            )
            try:
                data = await self.ai.image(
                    prompt=fallback_prompt, size=self.settings.image_size, provider=provider
                )
            except Exception as exc:  # noqa: BLE001 - genuinely nothing left to try
                log.warning(
                    "Generic last-resort fallback also failed for %s: %s - omitting the image "
                    "rather than publishing a known-bad visual; the course's text content is "
                    "unaffected", block.id, exc,
                )
                block.content = merge_content(
                    block.type,
                    block.content,
                    {
                        "path": None,
                        "asset_id": None,
                        "generated": False,
                        "error": None,
                        # Narrow, explicit state (layout-defect follow-up) -
                        # see ImageContent.omitted_after_qa_failure's own
                        # docstring. This is what lets app.course.document.
                        # layout.image_generation_failed tell this apart from
                        # "simply not generated yet" and give it the same
                        # compact placeholder a technical failure gets,
                        # WITHOUT setting `error` (which would incorrectly
                        # trip quality_framework's separate, always-"blocker"
                        # `image_generation_failed` finding - unchanged by
                        # this field, which that function never reads).
                        "omitted_after_qa_failure": True,
                        "prompt": prompt,
                        "width": None,
                        "height": None,
                        "text_verified": text_verified,
                        "formula_verified": formula_verified,
                        "visual_qa_findings": existing_findings + formula_findings + relevance_findings,
                        "generation_mode": "mock" if self.ai.is_mock else "real",
                    },
                )
                return False
            prompt = fallback_prompt
            text_verified = None
            # The ORIGINAL image's relevance findings described a picture
            # that is no longer what's being shipped - keeping them would
            # misreport a defect on an image nobody will ever see. The
            # generic fallback is deliberately NOT topic-specific (same
            # reasoning `is_safety_fallback` already uses to skip this same
            # check above), so it never makes sense to re-run the relevance
            # check against it either.
            relevance_findings = visual_qa_finding_entries(
                [(
                    "generic_fallback_used",
                    "Every attempt at the actual brief failed quality checks (garbled text or "
                    "off-topic/decorative) - a clean, generic, chapter-themed illustration was "
                    "published instead so this page is never left without an image.",
                    False,
                )],
                resolution="generic_fallback_published",
            )
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
                "visual_qa_findings": existing_findings + formula_findings + relevance_findings,
                "generation_mode": "mock" if self.ai.is_mock else "real",
            },
        )
        log.info("Generated %s for block %s", relative, block.id)
        return True

    async def generate_missing(
        self,
        *,
        document: CourseDocument,
        template: CourseTemplate,
        only_block_ids: list[str] | None = None,
        force: bool = False,
    ) -> int:
        """Generate every image the document is still missing. Returns the count."""
        targets: list[Block] = []
        wanted = set(only_block_ids) if only_block_ids else None
        for _, block in document.iter_blocks():
            if block.type is not BlockType.IMAGE:
                continue
            if wanted is not None and block.id not in wanted:
                continue
            if block.content.get("path") and not force:
                continue
            targets.append(block)

        if not targets:
            return 0

        # Computed once over the whole document (see chapter_section_context's
        # own docstring) rather than per-block, so a big course doesn't repeat
        # the same linear walk once per image block.
        context_map = chapter_section_context(document)
        limiter = get_limiter("image", self.settings.concurrency_for("image"))

        async def worker(block: Block) -> bool:
            chapter_title, section_title, key_concept = context_map.get(block.id, ("", "", ""))
            async with limiter.slot():
                return await self.generate_for_block(
                    course_id=document.course_id,
                    block=block,
                    template=template,
                    course_title=document.course_title,
                    chapter_title=chapter_title,
                    section_title=section_title,
                    key_concept=key_concept,
                )

        with metrics_phase("images"):
            results = await asyncio.gather(*(worker(block) for block in targets))
        generated = sum(1 for ok in results if ok)
        log.info("Generated %s/%s images for %s", generated, len(targets), document.course_id)
        return generated
