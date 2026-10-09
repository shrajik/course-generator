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
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client
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


class ImageTextCheck(BaseModel):
    """What a vision-capable model reports actually seeing rendered in a
    generated image - see ImageService._verify_generated_text. Deliberately
    just a flat list of whatever text is visible, exactly as it appears
    (typos included) - the ground truth to compare against is
    `expected_labels` on the block itself, not anything this schema knows
    about the topic."""

    detected_text: list[str] = Field(default_factory=list)

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
    async def _verify_generated_text(self, image: bytes, expected_labels: list[str]) -> list[str]:
        """Asks a vision-capable model to read back every piece of text it
        can actually see in `image`, then reports which of `expected_labels`
        never showed up verbatim (case/whitespace-insensitive) - a missing
        label means either it's absent or - just as likely, since image
        models garble baked-in text - misspelled into something that no
        longer matches. Returns the empty list when everything expected was
        found; never raises (a vision-check failure isn't treated as a text
        error - a genuinely garbled image gets a real chance to be flagged
        the NEXT time this runs, not silently blocked on this call alone).

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
            log.warning("Image text check failed: %s", exc)
            return []
        detected = {normalize(text) for text in result.detected_text}
        return [label for label in expected_labels if normalize(label) not in detected]

    async def _verify_image_has_no_text(self, image: bytes) -> list[str]:
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
        (empty list = genuinely text-free); never raises (a vision-check
        failure isn't treated as "it has text" - that would needlessly burn
        through the caller's own bounded retry budget on a QA-step problem,
        not an image problem)."""
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
            log.warning("Image no-text check failed: %s", exc)
            return []
        return [text for text in result.detected_text if text.strip()]

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
        if not found:
            return data, True

        log.info(
            "Text-free image for %s still rendered text (%s) - one final, more emphatic attempt",
            block_id, found,
        )
        final_prompt = (
            f"{prompt}\n\nABSOLUTELY CRITICAL: this image must contain ZERO text of any kind - "
            "no letters, no words, no numbers, no labels, no captions, nothing resembling "
            "writing anywhere in the image, not even small or decorative text. A previous "
            "attempt still rendered some text - this time, render no text at all, full stop."
        )
        data = await self.ai.image(prompt=final_prompt, size=size, provider=provider)
        found = await self._verify_image_has_no_text(data)
        if found:
            log.warning(
                "Text-free image for %s still has text after every bounded attempt (%s) - "
                "flagged, not silently accepted as correct", block_id, found,
            )
        return data, not found

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
        Returns (image bytes, text_verified)."""
        size = size or self.settings.image_size
        data = await self.ai.image(prompt=prompt, size=size, provider=provider)
        missing = await self._verify_generated_text(data, expected_labels)
        if not missing:
            return data, True

        log.info("Image text check failed for %s (missing/misspelled: %s) - retrying", block_id, missing)
        retry_prompt = (
            f"{prompt}\n\nThe previous attempt got this text wrong or left it out: "
            f"{', '.join(missing)}. This exact text must appear, spelled correctly, "
            "letter by letter, in large clearly legible lettering: "
            f"{', '.join(expected_labels)}. Double-check every letter before finalizing - "
            "a near-miss spelling (an extra, missing, or swapped letter) is still wrong."
        )
        data = await self.ai.image(prompt=retry_prompt, size=size, provider=provider)
        missing = await self._verify_generated_text(data, expected_labels)
        if not missing:
            return data, True

        log.info(
            "Image text check failed again for %s (%s) - attempting a text-free image", block_id, missing
        )
        data = await self.ai.image(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size, provider=provider)
        # Trust nothing on faith: a "text-free" request can still come back
        # with partial, wrong text (the confirmed "Oxgeen" case) - checked
        # against the exact same expected labels, since any of them
        # appearing wrong is exactly as bad as it was on a labelled attempt.
        missing = await self._verify_generated_text(data, expected_labels)
        if not missing:
            return data, True

        log.info(
            "Text-free attempt for %s still rendered incorrect text (%s) - one final, more "
            "emphatic attempt", block_id, missing,
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
        return data, not missing

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
            log.warning("Image generation failed for %s: %s", block.id, exc)
            block.content = merge_content(
                block.type, block.content, {"error": str(exc)[:300], "generated": False}
            )
            return False
        if text_verified is False:
            log.warning(
                "Image for %s shipped with unverified/incorrect text after every bounded "
                "attempt - flagged, not silently accepted as correct", block.id,
            )
        if (block.content.get("illustration_style") or "").strip().lower() == "section_intro":
            data = self._resize_section_intro(data)
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
                "prompt": prompt,
                "width": width,
                "height": height,
                "text_verified": text_verified,
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
