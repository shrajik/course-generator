"""Check a finished course's actual pictures and diagrams the way a teacher would.

Runs after images are generated (app.services.course_service._finalise) and on
demand for an existing document. For every image block it gathers what a
reader sees next to it - the chapter, the section heading, the text before and
after, the caption - and asks:

* Generated pictures (raster): a vision model looks at the ACTUAL image file
  and says what it shows, whether that is relevant to the topic, whether it
  shows what the text explains, whether it could mislead, whether it adds
  anything, and whether any text drawn in it is right.
* Diagrams (SVG from app.render.diagram_renderer): the outline of what was
  drawn (parts and arrows, recorded at render time - see
  diagram_service.diagram_outline) plus the labels actually present in the SVG
  are compared, by meaning, with the text: right concept, right labels, arrows
  in the right direction. Readability is checked on the real SVG with the
  existing render QA (overlaps, clipping, tiny text).
* Duplicates are found from the images themselves (a perceptual hash), not
  from their prompts.

A visual that fails is regenerated once through the existing ImageService,
with the specific correction fed into its brief, then checked again. If the
new one is no better, the original is kept and flagged. Only the affected
block changes. Nothing is ever reported as passed without an explicit answer:
a missing judgement leaves the visual "unchecked".
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.core.metrics import phase as metrics_phase
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, BlockType
from app.schemas.document import Block, CourseDocument
from app.schemas.template import CourseTemplate
from app.schemas.visual_review import DiagramAssessment, ImageAssessment, VisualCheck, VisualReviewReport
from app.services.openai_service import AIClient, get_ai_client
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

_RASTER = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_MAX_VISION_SIDE = 768
_DUPLICATE_DISTANCE = 6  # of 64 bits
_COLOUR_TOLERANCE = 30  # per channel, of 255
_CONTEXT_CHARS = 700

IMAGE_SYSTEM = """\
You are a teacher checking one picture in a course before students see it. You
are shown the picture itself and the text it sits beside. Judge what the
picture ACTUALLY shows - not what its caption or the brief claims it shows -
by meaning and teaching value.

Answer every field honestly:
- `depicts`: what the picture really shows, in one plain sentence.
- `relevant_to_topic`: does it belong in this course and section at all?
- `matches_text`: does it show the specific thing the neighbouring text explains
  (the same object, process or idea - not merely the same general subject)?
- `misleading`: true only if a student would come away believing something
  WRONG about what this section teaches - the wrong process, parts doing the
  wrong job, a reversed direction or order, impossible science, a different
  concept. Ordinary textbook simplification, stylisation and missing finer
  detail are NOT misleading: list them in `problems` and answer false.
- `necessary`: does it help a student understand beyond what the text says
  alone? False for filler or a generic stock-like scene.
- `text_in_image_correct`: if any words or labels are drawn in it, are they
  spelled correctly and correct for the subject? Null if there is no text.
- `labels_and_arrows_correct`: only when the picture is meant to be a diagram -
  are its labels, arrows and relationships correct? Null otherwise.
- `style_compliant`: ONLY when told below this picture must be a flat 2D
  vector textbook illustration - answer false if it instead looks
  photorealistic, 3D-rendered, has soft studio lighting/a glow/gradient
  background, or has any panel/icon/label/text cropped at or crowding the
  canvas edge. A clean flat-colour illustration with simple shading is
  compliant even if not perfectly flat; judge the OVERALL look, not every
  pixel. Null when you were not told this picture must be that style.
- `problems`: each concrete problem you found.
- `better_prompt`: if anything is wrong, a precise brief (under 80 words) for a
  replacement picture that shows exactly what the text explains.
Never answer true for something you did not actually check in the picture.
"""

DIAGRAM_SYSTEM = """\
You are a teacher checking one diagram in a course before students see it. You
are given exactly what the diagram draws - its parts and its arrows in the
direction they point - and the text it sits beside. Judge by meaning, as a
subject expert would, not by matching words:
- `represents_text`: does the diagram show the same concept, structure or
  process the text explains, at the right level? A simplified overview suited
  to the audience is fine - finer detail it leaves out belongs in `missing`,
  not here.
- `labels_correct`: are the parts named correctly for this subject and
  consistently with the text?
- `arrows_correct`: does every arrow point the right way and stand for a real
  relationship or step the text describes, in the right order? Null if the
  arrows were not given to you.
- `missing`: important parts or steps the text explains that the diagram lacks.
- `wrong`: each wrong label, arrow, relationship or order.
- `problems`: anything else that would confuse a student.
- `correction`: if anything is wrong, exactly what the diagram must show instead.
Never answer true for something you did not actually check.
"""


@dataclass
class _Context:
    chapter_id: str
    page_number: int
    course_title: str
    chapter: str
    section: str
    before: str
    after: str


def _block_text(block: Block) -> str:
    content = block.content
    return " ".join(str(content.get(key) or "") for key in ("title", "text", "intro") if content.get(key)).strip()


def _contexts(document: CourseDocument) -> dict[str, _Context]:
    """What a reader sees around every image block, in reading order."""
    flat = [(page.page_number, block) for page, block in document.iter_blocks()]
    contexts: dict[str, _Context] = {}
    chapter_titles: dict[str, str] = {}
    section = ""
    current_chapter = ""
    for index, (page_number, block) in enumerate(flat):
        chapter_id = block.meta.chapter_id or ""
        if chapter_id != current_chapter:
            current_chapter, section = chapter_id, ""
        if block.type is BlockType.HEADING:
            text = str(block.content.get("text") or "")
            if int(block.content.get("level") or 2) <= 1 and chapter_id and chapter_id not in chapter_titles:
                chapter_titles[chapter_id] = text
            section = text
            continue
        if block.type is not BlockType.IMAGE:
            continue
        before: list[str] = []
        for _, earlier in reversed(flat[:index]):
            if (earlier.meta.chapter_id or "") != chapter_id or earlier.type is BlockType.HEADING:
                break
            if earlier.type in TEXTUAL_BLOCK_TYPES:
                before.insert(0, _block_text(earlier))
                if sum(len(part) for part in before) > _CONTEXT_CHARS:
                    break
        after = ""
        for _, later in flat[index + 1 :]:
            if (later.meta.chapter_id or "") != chapter_id or later.type is BlockType.HEADING:
                break
            if later.type in TEXTUAL_BLOCK_TYPES:
                after = _block_text(later)
                break
        contexts[block.id] = _Context(
            chapter_id=chapter_id,
            page_number=page_number,
            course_title=document.course_title,
            chapter=chapter_titles.get(chapter_id, ""),
            section=section,
            before=" ".join(before)[-_CONTEXT_CHARS:],
            after=after[:_CONTEXT_CHARS],
        )
    return contexts


def _context_lines(block: Block, context: _Context) -> str:
    content = block.content
    return (
        f"COURSE: {context.course_title}\n"
        f"CHAPTER: {context.chapter or '(untitled)'}\n"
        f"SECTION HEADING: {context.section or '(none)'}\n"
        f"TEXT BEFORE THE VISUAL: {context.before or '(none)'}\n"
        f"TEXT AFTER THE VISUAL: {context.after or '(none)'}\n"
        f"CAPTION: {content.get('caption') or '(none)'}\n"
        f"WHAT THE VISUAL WAS MEANT TO SHOW: {content.get('purpose') or '(not stated)'}\n"
    )


def _vision_png(data: bytes) -> bytes | None:
    """The picture as a modest PNG for the vision model (cost and payload)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((_MAX_VISION_SIDE, _MAX_VISION_SIDE))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - an unreadable file is reported, not raised
        return None


Fingerprint = tuple[int, tuple[int, ...]]


def image_fingerprint(data: bytes) -> Fingerprint | None:
    """What the picture itself looks like: a 64-bit difference hash of its
    structure plus its average colour. Near-identical images get
    near-identical fingerprints whatever their prompts said; two pictures
    with the same layout but different colours are not the same picture."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
            small = rgb.convert("L").resize((9, 8))
            pixels = list(small.getdata())
            channels = list(zip(*rgb.resize((16, 16)).getdata()))
            mean = tuple(int(sum(channel) / len(channel)) for channel in channels)
    except Exception:  # noqa: BLE001
        return None
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return value, mean


def _distance(a: Fingerprint, b: Fingerprint) -> int:
    """Structural bit difference; a clearly different colour counts as different."""
    if max(abs(x - y) for x, y in zip(a[1], b[1])) > _COLOUR_TOLERANCE:
        return 64
    return bin(a[0] ^ b[0]).count("1")


def _svg_words(svg: bytes) -> set[str]:
    text = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", svg.decode("utf-8", "ignore"), flags=re.DOTALL))
    text = re.sub(r"<[^>]+>", " ", text)
    return set(re.findall(r"[a-z0-9]+", text.lower()))


@dataclass
class _DuplicateIndex:
    """Precomputed duplicate relationships for one `review_document` pass -
    see `_precompute_duplicates`'s own docstring for why this exists and
    the ordering guarantee it preserves.

    `order` fixes the DOCUMENT order every lookup here scans in, so a match
    always resolves to "the earliest block in the course that shows this",
    identical to what a fully sequential, one-block-at-a-time review would
    have found - whether the lookup happens during the precompute pass
    itself or later, during a `_fix` recheck of a freshly regenerated
    image's NEW fingerprint (see `find_duplicate`).

    `fingerprints`/`outline_keys` hold every eligible block's OWN
    identity-on-disk data, read once at precompute time from the file as it
    existed before any concurrent AI/regeneration work started - immutable
    for the rest of the pass. Every duplicate lookup, whether for a
    block's first check or for `_fix`'s post-regeneration recheck of a
    freshly changed fingerprint, goes through `find_duplicate` - a fresh,
    cheap, local (zero-AI) scan each time, always against this same fixed
    `order` and these same frozen "others"' identities, which is what keeps
    "who is a duplicate of whom" identical to a fully sequential review
    regardless of WHEN during the concurrent phase a given block's own
    check happens to run. `depicts` starts empty and is filled in as each
    block's own check completes (each coroutine only ever writes its OWN
    key, so this is safe to mutate concurrently) - it lets a LATER block's
    `_fix` cite what an EARLIER duplicate "really shows" even though that
    earlier block's own vision call may have finished only moments
    before."""

    order: list[str] = field(default_factory=list)
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    outline_keys: dict[str, frozenset[str]] = field(default_factory=dict)
    depicts: dict[str, str] = field(default_factory=dict)

    def find_duplicate(self, self_id: str, *, fingerprint: Fingerprint | None = None, outline_key: frozenset[str] | None = None) -> str:
        """The earliest OTHER block (by fixed document `order`, self
        excluded) whose precomputed identity matches `fingerprint`/
        `outline_key` - "" when nothing matches. Always a fresh scan,
        never a cached answer - see this class's own docstring for why
        that is exactly what keeps this correct and cheap under
        concurrency."""
        for other_id in self.order:
            if other_id == self_id:
                continue
            if fingerprint is not None and other_id in self.fingerprints:
                if _distance(fingerprint, self.fingerprints[other_id]) <= _DUPLICATE_DISTANCE:
                    return other_id
            if outline_key is not None and len(outline_key) >= 2 and self.outline_keys.get(other_id) == outline_key:
                return other_id
        return ""


def _precompute_duplicates(storage: StorageService, course_id: str, eligible: list[tuple[Block, "_Context", VisualCheck]]) -> _DuplicateIndex:
    """Reads every eligible block's own saved file and computes its
    identity (a perceptual hash for a picture, a label-set key for a
    diagram outline) - pure local computation, zero AI calls, so doing
    this for every block up front costs milliseconds even for a large
    course. Walks `eligible` in DOCUMENT order and, for each block,
    compares only against the blocks already added to the index (the
    PRECEDING ones) - identical to the original fully-sequential review's
    own duplicate-detection order: a later near-identical visual is always
    "duplicate_of" the EARLIEST one that matches it, never the reverse, and
    never a later one. This is what lets the actual AI-calling checks run
    concurrently afterward (see `review_document`) without changing which
    block gets flagged as whose duplicate - that relationship is already
    fully and deterministically resolved before any concurrency starts."""
    index = _DuplicateIndex()
    for block, _context, check in eligible:
        index.order.append(block.id)
        content = block.content
        path = str(content.get("path") or "")
        if not path:
            continue
        file = storage.asset_abs_path(course_id, path)
        if not file.is_file():
            continue
        data = file.read_bytes()
        if path.lower().endswith(".svg"):
            outline = content.get("diagram_outline") if isinstance(content.get("diagram_outline"), dict) else None
            parts = [str(part.get("label") or "") for part in (outline or {}).get("parts", []) if part.get("label")]
            key = frozenset(label.lower() for label in parts)
            if len(key) >= 2:
                index.outline_keys[block.id] = key
        elif path.lower().endswith(_RASTER):
            fingerprint = image_fingerprint(data)
            if fingerprint is not None:
                index.fingerprints[block.id] = fingerprint
    return index


def image_verdict(
    assessment: ImageAssessment, *, decorative_opener: bool, meant_as_diagram: bool, textbook_style: bool = False,
) -> tuple[str, list[str], bool]:
    """(status, problems, worth regenerating) from a vision assessment.

    Passed only when every judgement that matters is explicitly good; any
    explicit failure is failed; anything left unanswered is unchecked.
    """
    problems = list(assessment.problems)
    failures: list[str] = []
    if assessment.relevant_to_topic is False:
        failures.append("not relevant to the topic")
    if assessment.matches_text is False:
        failures.append("does not show what the text explains")
    if assessment.misleading is True:
        failures.append("could mislead a student")
    if assessment.text_in_image_correct is False:
        failures.append("text drawn in the picture is wrong")
    if meant_as_diagram and assessment.labels_and_arrows_correct is False:
        failures.append("labels or arrows are wrong")
    if textbook_style and assessment.style_compliant is False:
        failures.append("does not follow the required flat textbook illustration style")
    unnecessary = assessment.necessary is False and not decorative_opener
    if unnecessary:
        failures.append("adds nothing the text does not already say")

    if failures:
        detail = [f"Picture {item}." for item in failures]
        return "failed", detail + [p for p in problems if p not in detail], bool(set(failures) - {"adds nothing the text does not already say"})
    required = [assessment.relevant_to_topic, assessment.matches_text, assessment.misleading]
    if not decorative_opener:
        required.append(assessment.necessary)
    if meant_as_diagram:
        required.append(assessment.labels_and_arrows_correct)
    if textbook_style:
        required.append(assessment.style_compliant)
    if any(value is None for value in required):
        return "unchecked", problems or ["The check returned no verdict for this picture."], False
    return "passed", problems, False


def diagram_verdict(assessment: DiagramAssessment, *, arrows_known: bool, readability: list[str], missing_labels: list[str]) -> tuple[str, list[str]]:
    problems: list[str] = []
    if assessment.represents_text is False:
        problems.append("Diagram does not show what the text explains.")
    if assessment.labels_correct is False:
        problems.append("Diagram labels are wrong.")
    if assessment.arrows_correct is False:
        problems.append("Diagram arrows or relationships are wrong.")
    problems += [f"Missing: {item}" for item in assessment.missing]
    problems += [f"Wrong: {item}" for item in assessment.wrong]
    problems += [f"Hard to read: {item}" for item in readability]
    problems += [f'Label "{label}" is not actually drawn in the diagram.' for label in missing_labels]
    explicit_fail = (
        assessment.represents_text is False
        or assessment.labels_correct is False
        or assessment.arrows_correct is False
        or bool(assessment.wrong)
        or bool(readability)
        or bool(missing_labels)
    )
    if explicit_fail:
        return "failed", problems + assessment.problems
    required = [assessment.represents_text, assessment.labels_correct]
    if arrows_known:
        required.append(assessment.arrows_correct)
    if any(value is None for value in required):
        return "unchecked", problems + (assessment.problems or ["The check returned no verdict for this diagram."])
    # Missing parts alone are worth reporting but are not a wrong diagram.
    return "passed", problems + assessment.problems


def core_failures(assessment, check: VisualCheck) -> int:
    """How seriously a visual fails, weighted: showing the wrong subject or
    not what the text explains (3) outweighs teaching something wrong (2),
    which outweighs a slip in drawn text, labels or readability (1). Remarks
    about finer detail don't count. Used to decide whether a regenerated
    visual is better than the one it replaces."""
    score = 2 if check.duplicate_of else 0
    if isinstance(assessment, ImageAssessment):
        score += 3 * (assessment.relevant_to_topic is False) + 3 * (assessment.matches_text is False)
        score += 2 * (assessment.misleading is True) + (assessment.text_in_image_correct is False)
        score += assessment.style_compliant is False
        if check.kind == "diagram":  # labels and arrows only matter for a picture meant as a diagram
            score += assessment.labels_and_arrows_correct is False
    elif isinstance(assessment, DiagramAssessment):
        score += 3 * (assessment.represents_text is False)
        score += 2 * (assessment.arrows_correct is False) + 2 * (assessment.labels_correct is False)
        score += sum(
            1 for problem in check.problems
            if problem.startswith("Hard to read:") or problem.endswith("is not actually drawn in the diagram.")
        )
    return score


class VisualReviewService:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        storage: StorageService | None = None,
        images=None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()
        self._images = images

    @property
    def images(self):
        if self._images is None:
            from app.services.image_service import ImageService

            self._images = ImageService(ai=self.ai, settings=self.settings, storage=self.storage)
        return self._images

    # --- entry point ------------------------------------------------------------
    async def review_document(
        self,
        document: CourseDocument,
        template: CourseTemplate,
        *,
        fix: bool = True,
        job_id: str = "",
        on_progress: Callable[[Block, int, int], Awaitable[None]] | None = None,
    ) -> VisualReviewReport:
        """Checks every real visual in `document` against its own text, and
        regenerates the ones that fail, up to the configured per-course
        caps. Runs the actual AI-calling work CONCURRENTLY, bounded by a
        dedicated adaptive limiter (`get_limiter("visual_review", ...)` -
        the same pattern `ImageService.generate_missing` already uses for
        the main image-generation batch) - a real, confirmed cost: this
        loop used to run one block at a time with no concurrency at all,
        while the batch that generates the images in the first place is
        already parallelized, so a course's "Building Document" stage could
        sit for many minutes re-checking (and regenerating) visuals one by
        one with zero progress visible, long after the "Generating Images"
        counter already showed 100%.

        Three things make this safe to parallelize without changing WHICH
        visuals get flagged, regenerated or reported, or in what numbers:
        1. Duplicate detection (`_precompute_duplicates`) is resolved in one
           fast, strictly sequential, zero-AI-call pass BEFORE any
           concurrent work starts - see its own docstring for why this
           keeps "who is a duplicate of whom" identical to a fully
           sequential review.
        2. The per-course fix budget (`visual_review_max_fixes`) is
           allocated in a SEPARATE sequential pass, in document order,
           after every concurrent check has returned - so the first N
           genuinely-failing visuals IN DOCUMENT ORDER still get the
           limited regeneration slots, exactly as before; concurrency only
           changes how fast the checks run, never which ones get fixed.
        3. Every block's own `VisualCheck`/`block.content` mutation is
           confined to that block alone - no two concurrent tasks ever
           write the same block, so there is nothing to lock there.

        `on_progress`, when given, fires once per CHECKED block (not the
        skipped/capped ones) as it finishes, `(block, done, total)` - the
        real "N of M visuals reviewed" counter `CourseService._finalise`
        surfaces to the UI, the same progress-reporting contract
        `ImageService.generate_missing`'s `on_progress` already keeps.
        """
        contexts = _contexts(document)
        report = VisualReviewReport(
            document_id=document.document_id, course_id=document.course_id, checked_at=utc_now_iso()
        )

        # Selection pass - identical order/semantics to before, just
        # separated from the (now concurrent) AI-calling work below.
        candidates: list[tuple[Block, _Context, VisualCheck]] = []
        for page, block in document.iter_blocks():
            if block.type is not BlockType.IMAGE or block.content.get("kind") == "toc":
                continue
            context = contexts.get(block.id) or _Context("", page.page_number, document.course_title, "", "", "", "")
            check = VisualCheck(
                block_id=block.id,
                chapter_id=context.chapter_id,
                page_number=context.page_number,
                section=context.section,
                kind=str(block.content.get("kind") or "illustration"),
            )
            report.checks.append(check)
            candidates.append((block, context, check))

        eligible: list[tuple[Block, _Context, VisualCheck]] = []
        for block, context, check in candidates:
            if len(eligible) >= self.settings.visual_review_max_images:
                check.status, check.note = "unchecked", "Over the per-course review limit."
                continue
            # This exact image was already generated and fully validated
            # moments ago, in THIS same run (see ImageService.generate_missing's
            # `_qa_job_id` marker) - spending another vision call re-checking
            # it here is pure duplicate work. An image reused from an earlier
            # run (no marker, or a stale one from a different job) still gets
            # the full check below - this only skips what's provably fresh.
            if job_id and block.content.get("_qa_job_id") == job_id:
                check.status, check.note = "skipped", "Already validated during generation."
                continue
            eligible.append((block, context, check))

        with metrics_phase("visual_review"):
            dupes = _precompute_duplicates(self.storage, document.course_id, eligible)

            total = len(eligible)
            done = 0
            done_lock = asyncio.Lock()

            async def _note_progress(block: Block) -> None:
                nonlocal done
                if on_progress is None:
                    return
                async with done_lock:
                    done += 1
                    current = done
                await on_progress(block, current, total)

            limiter = get_limiter("visual_review", self.settings.concurrency_for("visual_review"))

            async def _check_and_confirm(block: Block, context: _Context, check: VisualCheck):
                async with limiter.slot():
                    outcome, assessment = await self._check(document.course_id, block, context, check, dupes)
                    if outcome and fix and not check.duplicate_of:
                        outcome, assessment = await self._confirm(
                            document.course_id, block, context, check, outcome, assessment, dupes
                        )
                await _note_progress(block)
                return outcome, assessment

            outcomes = await asyncio.gather(
                *(_check_and_confirm(block, context, check) for block, context, check in eligible)
            )

            # Fix-budget allocation - sequential and deterministic, in
            # document order (see this method's own docstring, point 2).
            fixes_left = self.settings.visual_review_max_fixes if fix else 0
            to_fix: list[tuple[Block, _Context, VisualCheck, str, object]] = []
            for (block, context, check), (outcome, assessment) in zip(eligible, outcomes):
                if outcome == "flag":
                    check.action = "flagged"
                elif outcome and fixes_left > 0:
                    fixes_left -= 1
                    to_fix.append((block, context, check, outcome, assessment))
                elif outcome:
                    check.action, check.note = "flagged", "Not regenerated: the per-course fix limit was reached."

            async def _run_fix(block: Block, context: _Context, check: VisualCheck, outcome: str, assessment) -> None:
                async with limiter.slot():
                    await self._fix(block, context, check, template, document, outcome, assessment, dupes)

            if to_fix:
                await asyncio.gather(*(_run_fix(*item) for item in to_fix))

            for block, _context, check in eligible:
                self._record(block, check)

        self.storage.write_json(
            self.storage.course_dir(document.course_id) / "visual_review.json", report.model_dump(mode="json")
        )
        skipped_fresh = sum(1 for _, _, check in candidates if check.note == "Already validated during generation.")
        log.info(
            "Visual review of %s: %s (%s/%s candidates checked, %s skipped as already-fresh, concurrency=%s)",
            document.document_id, report.counts(), total, len(candidates), skipped_fresh, limiter.effective_limit,
        )
        return report

    # --- one visual -------------------------------------------------------------
    async def _check(self, course_id, block, context, check, dupes: _DuplicateIndex):
        """Fill `check`; return (what to fix - "" for nothing, the assessment)."""
        content = block.content
        if check.kind == "concept_experience":
            check.status, check.note = "skipped", "Interactive concept visuals are checked by their own QA when generated."
            return "", None
        path = str(content.get("path") or "")
        if not path:
            check.status, check.note = "skipped", "No image was generated for this block."
            return "", None
        file = self.storage.asset_abs_path(course_id, path)
        if not file.is_file():
            check.status, check.problems = "unchecked", ["The image file is missing."]
            return "", None
        data = file.read_bytes()
        if path.lower().endswith(".svg"):
            return await self._check_diagram(block, context, check, data, dupes)
        if path.lower().endswith(_RASTER):
            return await self._check_picture(block, context, check, data, dupes)
        check.status, check.note = "skipped", f"Unsupported image format: {path.rsplit('.', 1)[-1]}"
        return "", None

    async def _check_picture(self, block, context, check, data, dupes: _DuplicateIndex) -> str:
        content = block.content
        decorative_opener = (content.get("illustration_style") or "") == "section_intro"
        textbook_style = (content.get("illustration_style") or "") == "textbook"
        meant_as_diagram = check.kind == "diagram"  # a diagram that fell back to a picture
        fingerprint = image_fingerprint(data)
        duplicate = dupes.find_duplicate(block.id, fingerprint=fingerprint) if fingerprint is not None else ""
        png = _vision_png(data)
        if png is None:
            check.status, check.problems = "unchecked", ["The image file could not be read."]
            return "", None
        try:
            assessment = await self.ai.structured(
                schema=ImageAssessment,
                system=IMAGE_SYSTEM,
                user=_context_lines(block, context)
                + ("THIS PICTURE IS MEANT TO BE A DIAGRAM: check its labels and arrows too.\n" if meant_as_diagram else "")
                + ("This is a small picture that opens its section; it need not add new information, but it must fit the section.\n" if decorative_opener else "")
                + (
                    "THIS PICTURE MUST BE A FLAT 2D VECTOR TEXTBOOK ILLUSTRATION: check `style_compliant` too - "
                    "fail it if it looks photorealistic or 3D-rendered, has soft studio lighting or a glow/gradient "
                    "background, or has anything cropped at or crowding the canvas edge.\n"
                    if textbook_style else ""
                )
                + "Check the attached picture now.",
                model=self.settings.visual_review_model or self.settings.diagram_model,
                purpose=f"visual_review:{block.id}",
                phase="image",
                image=png,
            )
        except Exception as exc:  # noqa: BLE001 - a failed check is unchecked, never passed
            log.warning("Visual review of %s failed: %s", block.id, exc)
            check.status, check.problems = "unchecked", [f"The check could not run ({type(exc).__name__})."]
            return "", None
        status, problems, regenerate = image_verdict(
            assessment, decorative_opener=decorative_opener, meant_as_diagram=meant_as_diagram,
            textbook_style=textbook_style,
        )
        check.status, check.problems, check.depicts = status, problems, assessment.depicts
        dupes.depicts[block.id] = assessment.depicts
        if duplicate:
            check.status = "failed"
            check.duplicate_of = duplicate
            check.problems.insert(0, f"Looks the same as the picture in block {duplicate}.")
            regenerate = True
        if check.status != "failed":
            return "", assessment
        return ("picture" if regenerate else "flag"), assessment

    async def _check_diagram(self, block, context, check, svg, dupes: _DuplicateIndex) -> str:
        from app.services.diagram_render_qa import validate_rendered_svg

        content = block.content
        outline = content.get("diagram_outline") if isinstance(content.get("diagram_outline"), dict) else None
        drawn_words = _svg_words(svg)
        kind = (outline or {}).get("kind") or str(content.get("diagram_kind") or "")
        try:
            readability = [issue.detail for issue in validate_rendered_svg(svg, kind=kind)] if kind and kind != "schematic" else []
        except Exception:  # noqa: BLE001 - render QA is a helper, not a gate on reviewing
            readability = []
        parts = [str(part.get("label") or "") for part in (outline or {}).get("parts", []) if part.get("label")]
        missing_labels = [
            label for label in parts
            if not set(re.findall(r"[a-z0-9]+", label.lower())) <= drawn_words
        ] if drawn_words else []
        arrows = (outline or {}).get("arrows") or []
        if outline:
            drawn = "PARTS:\n" + "\n".join(
                f"- {part.get('label')}" + (f": {part.get('detail')}" if part.get("detail") else "")
                for part in outline.get("parts", [])
            ) + "\nARROWS (from -> to, as drawn):\n" + (
                "\n".join(f"- {a.get('from')} -> {a.get('to')}" + (f" ({a.get('label')})" if a.get("label") else "") for a in arrows)
                or "(none)"
            )
        else:
            drawn = "LABELS DRAWN (arrows not recorded for this older diagram):\n" + ", ".join(sorted(drawn_words))[:1500]
        key = frozenset(label.lower() for label in parts)
        duplicate = dupes.find_duplicate(block.id, outline_key=key) if len(key) >= 2 else ""
        try:
            assessment = await self.ai.structured(
                schema=DiagramAssessment,
                system=DIAGRAM_SYSTEM,
                user=_context_lines(block, context)
                + f"DIAGRAM KIND: {kind or 'unknown'}\nWHAT THE DIAGRAM DRAWS:\n{drawn}\n\nCheck this diagram now.",
                model=self.settings.visual_review_model or self.settings.diagram_model,
                purpose=f"visual_review:{block.id}",
                phase="image",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Diagram review of %s failed: %s", block.id, exc)
            check.status, check.problems = "unchecked", [f"The check could not run ({type(exc).__name__})."]
            return "", None
        status, problems = diagram_verdict(
            assessment, arrows_known=bool(outline), readability=readability, missing_labels=missing_labels
        )
        check.status, check.problems = status, problems
        check.depicts = ", ".join(parts[:8])
        if duplicate:
            check.status, check.duplicate_of = "failed", duplicate
            check.problems.insert(0, f"Shows the same parts as the diagram in block {duplicate}.")
        return ("diagram" if check.status == "failed" else ""), assessment

    async def _confirm(self, course_id, block, context, check, outcome, assessment, dupes: _DuplicateIndex):
        """Ask a second, independent time before changing anything.

        Model judgements of the same picture are not fully consistent; one
        stray "misleading" must not get a good visual replaced. Only when
        both checks find a core failure is the visual fixed. When they
        disagree it is left as it is and reported for a person to look at.
        Duplicates are found from pixels and need no confirmation.
        """
        second = VisualCheck(block_id=block.id, kind=check.kind, page_number=check.page_number, section=check.section)
        _, second_assessment = await self._check(course_id, block, context, second, dupes)
        if second.status == "failed" and core_failures(second_assessment, second) > 0:
            return outcome, assessment
        check.status = "unchecked"
        check.note = (
            "Two independent checks disagreed (one found: " + "; ".join(check.problems[:2]) + "), so it was "
            "left unchanged for a person to look at."
        )
        return "", assessment

    # --- fixing -------------------------------------------------------------------
    async def _fix(self, block, context, check, template, document, outcome, assessment, dupes: _DuplicateIndex) -> None:
        content = block.content
        original = dict(content)
        brief = str(content.get("original_prompt") or content.get("prompt") or content.get("purpose") or "")
        if outcome == "picture":
            better = getattr(assessment, "better_prompt", "").strip()
            corrected = better or brief
            if check.duplicate_of:
                other = dupes.depicts.get(check.duplicate_of, "")
                corrected += f"\nIt must look clearly different from another picture in this course{f' (which shows: {other})' if other else ''}."
            if check.problems:
                corrected += "\nAvoid these problems of the previous picture: " + "; ".join(check.problems[:4])
        else:
            correction = getattr(assessment, "correction", "").strip()
            notes = [*getattr(assessment, "wrong", []), *getattr(assessment, "missing", [])]
            corrected = (
                f"{brief}\n\nCORRECTION FROM REVIEW - the previous diagram was wrong: "
                + (correction or "; ".join(check.problems[:5]))
                + (f" Fix: {'; '.join(notes[:6])}." if notes else "")
            )
        updates = {"prompt": corrected[:4000], "original_prompt": brief}
        block.content = {**content, **updates}
        try:
            ok = await self.images.generate_for_block(
                course_id=document.course_id, block=block, template=template, course_title=document.course_title
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Regenerating %s after review failed: %s", block.id, exc)
            ok = False
        if not ok or not block.content.get("path"):
            block.content = original
            check.action, check.fixed = "flagged", False
            check.note = "Regeneration failed; the original visual was kept."
            return
        check.action = "regenerated"
        recheck = VisualCheck(block_id=block.id, kind=check.kind, page_number=check.page_number, section=check.section)
        # `dupes` is frozen from precompute and was never mutated with this
        # block's OWN (now-replaced) identity in the first place - see
        # _DuplicateIndex's own docstring - so this recheck's `find_duplicate`
        # calls (inside `_check`) naturally compare only against every OTHER
        # block's original identity, never against this image's own stale,
        # pre-regeneration fingerprint/outline.
        _, reassessment = await self._check(document.course_id, block, context, recheck, dupes)
        before = core_failures(assessment, check)
        after = core_failures(reassessment, recheck)
        # Judged by what is seriously wrong, not by how many remarks were made:
        # a picture of the right subject with a few quibbles beats an
        # unrelated one with none listed.
        if recheck.status == "failed" and after >= before:
            block.content = original
            check.fixed = False
            check.note = "The regenerated visual was no better, so the original was kept: " + "; ".join(recheck.problems[:2])
            return
        check.fixed = recheck.status == "passed"
        check.status, check.problems, check.depicts = recheck.status, recheck.problems, recheck.depicts
        check.duplicate_of = recheck.duplicate_of
        check.note = "Regenerated after review."

    @staticmethod
    def _record(block: Block, check: VisualCheck) -> None:
        """Keep the verdict on the block, for the editor and later reviews."""
        block.content = {
            **block.content,
            "quality_review": {
                "status": check.status,
                "problems": check.problems[:6],
                "action": check.action,
                "checked_at": utc_now_iso(),
            },
        }



def load_visual_review(storage: StorageService, course_id: str) -> VisualReviewReport | None:
    path = storage.course_dir(course_id) / "visual_review.json"
    if not path.exists():
        return None
    return VisualReviewReport.model_validate_json(path.read_text(encoding="utf-8"))
