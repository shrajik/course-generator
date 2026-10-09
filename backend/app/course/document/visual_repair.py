"""Post-pagination visual audit and repair.

`page_visual_fraction` (app.course.document.layout) measures a page's real
visual/text balance from its actual rendered heights, and `flow_blocks`'s
own keep-together rule stops a visual from being separated from its lead-in
text - both already existed before this module. What was still missing:
nothing ever ACTED on a page that measured as deficient - the number was
only ever logged. This module is that action: for every content page that's
still thin on visuals after normal generation, plan a visual specifically
for THAT page's own text, generate it through the exact same
DiagramService/ConceptVisualService/ImageService pipeline every other
visual in this codebase already goes through (never a separate, parallel
generation system), insert it, and re-paginate - bounded to a few passes,
every page that still can't be repaired reported by name and reason rather
than silently left as-is.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.course.document.builder import reflow_document
from app.course.document.layout import page_visual_fraction
from app.course.document.layout_validator import validate_document
from app.core.ids import block_id as new_block_id
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, BlockType
from app.schemas.document import Block, BlockLayout, BlockMeta, BlockStyle, CourseDocument
from app.schemas.template import CourseTemplate
from app.services.image_service import ImageService, chapter_section_context
from app.services.openai_service import AIClient

log = get_logger(__name__)

# A page below this visual-area fraction is "deficient". Deliberately well
# under the 40% target, not equal to it - the tolerance the brief asks for:
# a page at 30-40% is a reasonable, natural outcome of real content and
# must not trigger regeneration just to chase an exact number, but a page
# at 0-10% (no visual at all, or a token one) is a genuine gap.
MIN_VISUAL_FRACTION = 0.20

# Below this many words, a page's own text isn't substantial enough to plan
# a genuinely relevant visual for - forcing one on here is exactly the
# "irrelevant image inserted just to fill space" the brief says not to do.
MIN_WORDS_TO_ILLUSTRATE = 60

# A page made up ONLY of these has nothing a visual would explain, however
# long it is - a quiz's questions, a summary's bullet list, a divider.
_NON_ILLUSTRABLE_TYPES = {
    BlockType.QUIZ,
    BlockType.SUMMARY,
    BlockType.DIVIDER,
    BlockType.EXERCISE,
    BlockType.CHALLENGE,
    BlockType.REFLECTION,
    BlockType.LEARNING_OBJECTIVES,
}

MAX_REPAIR_PASSES = 2


def _block_word_count(block: Block) -> int:
    if block.type not in TEXTUAL_BLOCK_TYPES:
        return 0
    return len(str(block.content.get("text", "")).split())


def page_needs_visual(page_blocks: list[Block]) -> bool:
    """True when `page_blocks` (one page's worth) is both visually thin AND
    has real text a visual could actually explain - the two independent
    conditions that keep this from ever firing on a legitimately text-only
    page (a quiz, a short summary) just because its fraction happens to be
    low."""
    if not page_blocks:
        return False
    if page_visual_fraction(page_blocks) >= MIN_VISUAL_FRACTION:
        return False
    if sum(_block_word_count(b) for b in page_blocks) < MIN_WORDS_TO_ILLUSTRATE:
        return False
    illustrable_types = {b.type for b in page_blocks} - _NON_ILLUSTRABLE_TYPES
    return bool(illustrable_types)


def _page_text(page_blocks: list[Block]) -> str:
    parts = []
    for block in page_blocks:
        if block.type is BlockType.HEADING:
            parts.append(str(block.content.get("text", "")))
        elif block.type in TEXTUAL_BLOCK_TYPES:
            parts.append(str(block.content.get("text", "")))
    return "\n\n".join(p for p in parts if p.strip())


class PageVisualPlan(BaseModel):
    """What the repair planner decides this one page's missing visual
    should be - the same shape of decision WRITER_SYSTEM makes for a whole
    chapter, scoped down to a single already-written page's own text."""

    needs_visual: bool = True
    reason_if_not: str = ""
    purpose: str = ""
    prompt: str = ""
    caption: str = ""
    kind: str = "illustration"  # "illustration" | "diagram" | "concept_experience"
    diagram_kind: str = ""
    illustration_style: str = ""
    expected_labels: list[str] = Field(default_factory=list)


REPAIR_PLANNER_SYSTEM = """\
You look at ONE already-written page from an educational course and decide \
whether it needs a visual, and if so, exactly what kind - the same kind of \
call a course's own writer already makes for a whole chapter, just scoped \
to this one page's own text. Never invent facts beyond what the page text \
says.

First decide: does this page's content have anything a visual could \
actually explain? Say `needs_visual: false` (with a one-line \
`reason_if_not`) for a page that's pure narrative, motivation, opinion, or \
otherwise has no concept/process/structure/scene worth depicting - do not \
force an image onto text that has nothing to show.

When it does need one, pick exactly one `kind`:
- A programming/DSA/databases/networks/OS/system-design/cloud/AI-ML/
  software-engineering topic centred on ONE nameable technical concept with
  real internal structure (a class and its objects, a data structure, a
  protocol, a pipeline, a state machine) -> "concept_experience". Leave
  `diagram_kind` blank.
- Steps that happen in order, a procedure, a decision point -> "diagram"
  with `diagram_kind: flow_chart` (or `process`/`cycle` for a repeating
  sequence, `swimlane` if which role/system does each step matters,
  `data_flow_diagram` for a system/pipeline walkthrough with external
  callers and data stores).
- A thing made of parts, or things related to each other, for a
  NON-technical or biology/life-science subject -> "diagram" with
  `diagram_kind: schematic` (a real physical arrangement), `concept_map`
  (abstract relationships), `hierarchy` (parent/child), `comparison` (two+
  things side by side), or `er_diagram` (a genuine database schema).
- Physics, chemistry or math needing to show the real apparatus, molecule,
  scene or graph, OR any subject needing an actual illustrative picture
  (anatomy, a historical scene, a geographic/geological scene, a
  well-known technical-architecture illustration) -> "illustration" with
  `illustration_style: "textbook"`. If the picture needs specific words
  rendered in it, list each one exactly spelled in `expected_labels` -
  otherwise leave it empty and let the picture be text-free.
- Nothing else fits, or you're unsure -> "illustration", `illustration_style`
  left blank.

Never diagram/illustrate the course or page itself, only the subject
matter. `purpose` is one line on why this visual helps; `prompt` is the
precise brief the actual generator will use (every component/step/scene
detail it needs, not just a style description); `caption` is a short
reader-facing line shown under the visual.
"""


def _build_repair_block(plan: PageVisualPlan, template: CourseTemplate, reference: Block) -> Block:
    kind = plan.kind if plan.kind in ("diagram", "concept_experience") else "illustration"
    content: dict = {
        "purpose": plan.purpose,
        "prompt": plan.prompt,
        "caption": plan.caption,
        "kind": kind,
        "diagram_kind": plan.diagram_kind.strip().lower().replace(" ", "_") if kind == "diagram" else "",
        "illustration_style": plan.illustration_style.strip().lower() if kind == "illustration" else "",
        "expected_labels": [label.strip() for label in plan.expected_labels if label.strip()],
    }
    return Block(
        id=new_block_id(),
        type=BlockType.IMAGE,
        content=content,
        style=BlockStyle.model_validate(template.style_for(BlockType.IMAGE)),
        layout=BlockLayout(),
        meta=BlockMeta(
            chapter_id=reference.meta.chapter_id,
            chapter_number=reference.meta.chapter_number,
            section_key=reference.meta.section_key,
            origin="inserted",
        ),
    )


def _insertion_index(page_blocks: list[Block]) -> int:
    """Right after the last textual block on the page - a visual added
    during repair pairs with whatever explanation is already there, the
    same "visual follows the text it illustrates" convention every other
    visual in this codebase already follows. Falls back to the very end of
    the page when there's no textual block to anchor to (a page of only
    headings/callouts, say)."""
    last_text_index = -1
    for index, block in enumerate(page_blocks):
        if block.type in TEXTUAL_BLOCK_TYPES:
            last_text_index = index
    return last_text_index + 1 if last_text_index >= 0 else len(page_blocks)


class RepairReport(BaseModel):
    passes_run: int = 0
    repaired: list[dict] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    failed: list[dict] = Field(default_factory=list)
    still_deficient: list[dict] = Field(default_factory=list)
    # Structural findings (overflow/overlap/orphan-page) from validating the
    # FINAL, already-reflowed document - see app.course.document.layout_validator.
    # Empty means the rendered output actually passed validation, not just
    # that the repair loop believes it did.
    validation_findings: dict[int, list[str]] = Field(default_factory=dict)


async def audit_and_repair_visuals(
    document: CourseDocument,
    template: CourseTemplate,
    *,
    images: ImageService,
    ai: AIClient,
    max_passes: int = MAX_REPAIR_PASSES,
) -> RepairReport:
    """Audits every content page, plans and generates a relevant visual for
    each deficient one, inserts it, and re-paginates - bounded to
    `max_passes` full passes (inserting a visual can shift page boundaries,
    which can occasionally leave a *different* page thin, so more than one
    pass is worth attempting, but never unboundedly - see the module
    docstring). Mutates `document` in place; does not save it.

    A subtlety worth being explicit about: a page whose existing text
    already nearly fills it can't gain a substantial visual and KEEP it on
    that same page - pagination will push the new (tall) visual, and
    whatever text immediately precedes it, onto a fresh page instead (the
    same keep-together rule that already stops a visual from being
    stranded without its lead-in - see flow_blocks). That's the correct,
    intended pagination outcome, not a bug to route around by cramming an
    oversized visual into too little room - but it does mean the ORIGINAL
    page can still measure as deficient after the repair, even though a
    real, relevant visual was generated for it. `_anchor_ids` tracks which
    page (by its own last block's id, before reflow moves anything) has
    already had a repair attempt, so a page in that situation is reported
    once as `still_deficient` with the real reason, never retried into a
    second, redundant visual stacking up wherever the first one landed."""
    report = RepairReport()
    attempted_anchor_ids: set[str] = set()

    for pass_number in range(1, max_passes + 1):
        report.passes_run = pass_number
        content_pages = [p for p in document.pages if p.kind == "content" and p.blocks]
        deficient = [
            p for p in content_pages
            if page_needs_visual(p.blocks) and p.blocks[-1].id not in attempted_anchor_ids
        ]
        if not deficient:
            break

        # Computed once per pass (not once per page) over the document's
        # CURRENT structure - reflow_document at the end of the previous
        # pass can move blocks between pages/chapters, so this must be
        # recomputed every pass, never cached across the whole repair run.
        # Reuses the exact same production mechanism generate_missing()
        # already uses (see image_service.py) - never a second,
        # independent context-resolution implementation.
        context_map = chapter_section_context(document)

        any_inserted = False
        for page in deficient:
            fraction_before = page_visual_fraction(page.blocks)
            attempted_anchor_ids.add(page.blocks[-1].id)
            try:
                plan = await ai.structured(
                    schema=PageVisualPlan,
                    system=REPAIR_PLANNER_SYSTEM,
                    user=(
                        f"COURSE TITLE: {document.course_title}\n\nPAGE {page.page_number} TEXT:\n"
                        f"{_page_text(page.blocks)[:4000]}"
                    ),
                    model=images.settings.diagram_model,
                    purpose=f"visual_repair:{document.document_id}:p{page.page_number}",
                    phase="image",
                )
            except Exception as exc:  # noqa: BLE001 - one page's planning failure must not abort the pass
                log.warning("Visual repair planning failed for page %s: %s", page.page_number, exc)
                report.failed.append({"page": page.page_number, "reason": f"planning failed: {exc}"})
                continue

            if not plan.needs_visual or not (plan.prompt.strip() or plan.purpose.strip()):
                report.skipped.append({
                    "page": page.page_number,
                    "reason": plan.reason_if_not or "no relevant visual for this page's content",
                })
                continue

            reference = page.blocks[-1]
            new_block = _build_repair_block(plan, template, reference)
            # `new_block` isn't in `document` yet (inserted further below,
            # only once generation succeeds), so it has no entry of its own
            # in `context_map` - the reference block it's about to be
            # inserted next to already does, and is exactly the chapter/
            # section this repair visual belongs to.
            chapter_title, section_title, key_concept = context_map.get(reference.id, ("", "", ""))
            ok = await images.generate_for_block(
                course_id=document.course_id,
                block=new_block,
                template=template,
                course_title=document.course_title,
                chapter_title=chapter_title,
                section_title=section_title,
                key_concept=key_concept,
            )
            if not ok:
                report.failed.append({
                    "page": page.page_number,
                    "reason": new_block.content.get("error") or "visual generation failed",
                })
                continue

            page.blocks.insert(_insertion_index(page.blocks), new_block)
            any_inserted = True
            report.repaired.append({
                "page": page.page_number,
                "block_id": new_block.id,
                "kind": new_block.content.get("kind"),
                "fraction_before": round(fraction_before, 2),
            })

        if not any_inserted:
            break
        reflow_document(document, template)

    # Final accounting: anything still deficient after the bounded passes is
    # reported explicitly, never silently accepted as done (the brief's own
    # requirement - a page repair either worked or is named as not having).
    for page in document.pages:
        if page.kind == "content" and page.blocks and page_needs_visual(page.blocks):
            report.still_deficient.append({
                "page": page.page_number,
                "fraction": round(page_visual_fraction(page.blocks), 2),
            })

    # Never call a page "repaired" on faith - validate what will actually be
    # rendered. A page inserted into `report.repaired` earlier can still end
    # up here if reflow pushed its content somewhere that overflows or
    # overlaps; this is the honest, final word on that.
    report.validation_findings = validate_document(document)
    if report.validation_findings:
        log.warning(
            "Visual repair for %s left %s page(s) failing layout validation: %s",
            document.document_id,
            len(report.validation_findings),
            report.validation_findings,
        )

    return report
