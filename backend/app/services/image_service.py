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

MAX_PROMPT_CHARS = 3800

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
        is_textbook = (content.get("illustration_style") or "").strip().lower() == "textbook"
        # The generic "for the course 'X'" framing line is harmless for the
        # default decorative/flat-vector illustration, but a real accurate
        # illustration model tends to read a quoted course/subject name as a
        # title to render literally into the picture (confirmed: without
        # this, a "textbook" heart illustration came back with a
        # "HUMAN BIOLOGY" banner baked across the top) - exactly the
        # unnecessary in-image text this style is meant to avoid, so it's
        # dropped here; TEXTBOOK_ILLUSTRATION_GUIDANCE already establishes
        # the "educational textbook" framing on its own.
        course_line = "" if is_textbook else f"Educational illustration for the course '{course_title}'."
        style_guidance = TEXTBOOK_ILLUSTRATION_GUIDANCE if is_textbook else template.image_guidance
        parts = [
            content.get("prompt") or content.get("purpose") or content.get("caption") or "",
            course_line,
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
        relative = self.storage.save_asset(course_id, data)
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
