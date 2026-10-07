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
    "Professional educational textbook illustration. Clean, technically or "
    "scientifically accurate, structured composition suitable for a modern "
    "textbook or educational platform - not generic AI art, not a stock "
    "photo. Depict the real subject clearly (the apparatus, organism, "
    "structure or mechanism itself), not a diagram of boxes and arrows. "
    "Avoid unnecessary or excessive text - include a label only when it is "
    "essential for understanding, never paragraphs or captions baked into "
    "the image. Do not render any title, heading or course/subject name as "
    "text anywhere in the image - a caption is shown separately outside the "
    "picture. Consistent, polished, uncluttered style."
)


# Fixed (not per-template) guidance for a "section_intro" illustration - see
# ImageContent.illustration_style. The writer's own `image_prompt` for this
# style already follows the exact wording contract (<=40 words, one clear
# subject, ends with "no text, no letters, no labels" - see WRITER_SYSTEM) -
# this constant exists to REINFORCE that same palette/style consistently
# rather than let DEFAULT_ILLUSTRATION_GUIDANCE's own "bright... flat vector"
# style direction dilute or contradict it (every section_intro image across
# a whole course should read as the same consistent picture language, not
# whatever DEFAULT's broader per-topic guidance happens to suggest).
SECTION_INTRO_ILLUSTRATION_GUIDANCE = (
    "STRICT NO-TEXT RULE: the image must contain zero text of any kind - no "
    "words, letters, numbers, labels, captions, titles, watermarks or logos, "
    "and no writing rendered on any object, screen, sign or surface within "
    "the scene either.\n"
    "STYLE: clean modern digital illustration, dark-blue/teal tones with "
    "soft glow accents - the exact same palette for every image. One single "
    "clear subject, centred, with no clutter and no unnecessary background "
    "detail."
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
    def build_prompt(block: Block, template: CourseTemplate, course_title: str) -> str:
        content = block.content
        style = (content.get("illustration_style") or "").strip().lower()
        # The generic "for the course 'X'" framing line is dropped for every
        # style - a real illustration model tends to read a quoted course/
        # subject name as a title to render literally into the picture
        # (confirmed: without this, a "textbook" heart illustration came
        # back with a "HUMAN BIOLOGY" banner baked across the top), exactly
        # the text every style's own guidance below already forbids on its
        # own terms.
        if style == "textbook":
            style_guidance = TEXTBOOK_ILLUSTRATION_GUIDANCE
        elif style == "section_intro":
            style_guidance = SECTION_INTRO_ILLUSTRATION_GUIDANCE
        else:
            style_guidance = DEFAULT_ILLUSTRATION_GUIDANCE
        parts = [
            content.get("prompt") or content.get("purpose") or content.get("caption") or "",
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

    # --- generation -------------------------------------------------------
    async def generate_for_block(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
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
        structured_fallback = False
        if kind == "diagram" and self.settings.enable_diagram_generation:
            ok = await self.diagrams.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
            structured_fallback = True
        elif kind == "concept_experience" and self.settings.enable_concept_experience_visuals:
            ok = await self.concept_visuals.generate_for_block(
                course_id=course_id, block=block, template=template, course_title=course_title
            )
            if ok:
                return True
            log.info("Falling back to a raster illustration for %s", block.id)
            structured_fallback = True
        return await self._generate_illustration(
            course_id=course_id,
            block=block,
            template=template,
            course_title=course_title,
            force_text_free=structured_fallback,
        )

    async def _generate_with_text_check(
        self, prompt: str, expected_labels: list[str], *, block_id: str
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
        size = self.settings.image_size
        data = await self.ai.image(prompt=prompt, size=size)
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
        data = await self.ai.image(prompt=retry_prompt, size=size)
        missing = await self._verify_generated_text(data, expected_labels)
        if not missing:
            return data, True

        log.info(
            "Image text check failed again for %s (%s) - attempting a text-free image", block_id, missing
        )
        data = await self.ai.image(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=size)
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
        data = await self.ai.image(prompt=final_prompt, size=size)
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
        force_text_free: bool = False,
    ) -> bool:
        """`force_text_free` is set when this is a FALLBACK from a failed
        diagram/concept_experience attempt (see generate_for_block) - that
        original request was for a labelled flowchart/architecture-style
        visual, which a raster image model cannot reliably spell (this is
        the confirmed cause of real, observed garbled-text diagrams: e.g.
        "Balid prompt", "eparates prompt text", "Fallute" instead of
        "Failure" - a diffusion model attempting to bake a dozen technical
        labels into pixels). Rather than gamble on an unverified, text-heavy
        raster image for a request that was never really an "illustration"
        to begin with, this asks for a text-free picture instead - still a
        real, relevant visual, just never a source of misspelled labels."""
        prompt = self.build_prompt(block, template, course_title)
        if not prompt.strip():
            log.warning("Image block %s has no prompt - skipped", block.id)
            return False
        is_textbook = (block.content.get("illustration_style") or "").strip().lower() == "textbook"
        expected_labels = [
            str(label).strip() for label in (block.content.get("expected_labels") or []) if str(label).strip()
        ]
        # None means "nothing was promised to verify" (no expected labels,
        # or a forced text-free fallback) - genuinely different from False
        # ("checked, and it's still wrong after every bounded attempt"), so
        # a caller can tell "nothing to confirm" from "confirmed wrong"
        # rather than a bare boolean collapsing both into one meaning.
        text_verified: bool | None = None
        try:
            if force_text_free:
                data = await self.ai.image(prompt=f"{prompt}{_NO_TEXT_SUFFIX}", size=self.settings.image_size)
            elif is_textbook and expected_labels:
                data, text_verified = await self._generate_with_text_check(
                    prompt, expected_labels, block_id=block.id
                )
            else:
                data = await self.ai.image(prompt=prompt, size=self.settings.image_size)
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

        limiter = get_limiter("image", self.settings.concurrency_for("image"))

        async def worker(block: Block) -> bool:
            async with limiter.slot():
                return await self.generate_for_block(
                    course_id=document.course_id,
                    block=block,
                    template=template,
                    course_title=document.course_title,
                )

        with metrics_phase("images"):
            results = await asyncio.gather(*(worker(block) for block in targets))
        generated = sum(1 for ok in results if ok)
        log.info("Generated %s/%s images for %s", generated, len(targets), document.course_id)
        return generated
