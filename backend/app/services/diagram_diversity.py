"""Course-level diagram-kind diversity.

`DiagramService.generate_for_block` (app.services.diagram_service) renders
whatever `diagram_kind` the writer already chose per block, with zero
visibility into what any OTHER block in the same course picked - `kind_hint`
is decided once, per block, at write time. Parallel chapter writing (the
default - `Settings.writing_mode == "parallel"`) naturally converges on
whichever few kinds feel "safest" for a topic, which is the actual root
cause of "the generator keeps producing the same flowchart/SmartArt types."

This is an ACTIVE fix, not another warning: `CourseService._continuity_warnings`
already reviews a finished course and reports prose repetition, but never
corrects it. This module instead RELABELS a bounded number of genuinely
over-used `diagram_kind` blocks to a better-fitting alternative BEFORE
diagram/image generation ever runs for them - the corrected kind is what
actually gets generated, not a note about what happened after the fact.

Mirrors `app.course.document.diagram_compaction`'s own proven shape: a pure,
standalone module function - "mutates `document` in place; does not save it;
the caller's own existing save step persists the result" - not a new
service class, and not a duplicate pipeline.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.blocks import BlockType, merge_content
from app.schemas.diagram import DIAGRAM_KINDS
from app.schemas.document import Block, CourseDocument
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

# A kind used this many times or fewer is never worth reconsidering - most
# courses legitimately reuse flow_chart/process/smart_art a handful of
# times, and relabelling an already-fine choice just to "look different"
# would trade correctness for novelty (the request's own "allow repetition
# when it's genuinely the most appropriate educational choice").
_OVERUSE_THRESHOLD = 3
# At most this many blocks get relabelled per course, however many are
# technically over threshold - bounds how much one AI judgment call can
# ever change in a single pass, the same "never let one AI call silently
# rewrite half a course" discipline diagram_content_qa's own
# missing_concepts list already keeps to a handful.
_MAX_REASSIGNMENTS = 6
# Below this many diagram blocks total, there's nothing meaningfully
# repetitive to fix - a 2-3-diagram course reusing one kind twice is
# completely normal, not the "keeps generating the same type" problem this
# module exists for.
_MIN_DIAGRAM_BLOCKS = 4

_BRIEF_CHAR_CAP = 220


class _KindReassignment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    block_id: str = ""
    new_kind: str = ""
    reason: str = ""


class _DiversityPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reassignments: list[_KindReassignment] = Field(default_factory=list)


DIVERSITY_SYSTEM = """\
You are a visual-design editor reviewing a whole course's diagrams for \
REPETITION. You'll see every diagram block whose own kind is used more \
than a few times across the course, each with a short brief of what it's \
actually about.

For each one, decide whether a DIFFERENT, still-genuinely-accurate kind \
from the full palette given would communicate that SPECIFIC block's own \
content better than its current kind. Only propose a change when you are \
genuinely confident the new kind fits that block's actual content at \
least as well as the old one - it is completely fine, and expected, to \
propose NO change for most blocks, even ones sharing an over-used kind, \
when that kind really is the right choice for them. Propose at most a \
handful of the clearest, highest-confidence improvements, never a change \
just to make the course look more varied for its own sake."""


def _brief(content: dict) -> str:
    parts = [
        str(content.get("purpose") or "").strip(),
        str(content.get("prompt") or "").strip(),
        str(content.get("caption") or "").strip(),
    ]
    return " / ".join(part for part in parts if part)[:_BRIEF_CHAR_CAP]


def _diagram_blocks(document: CourseDocument) -> list[Block]:
    # Only ever considers a block that hasn't been rendered yet (no `path`)
    # - the exact same gate `ImageService.missing_image_blocks` already
    # uses for "still needs generating". A block that already has a `path`
    # was rendered in an earlier run; relabelling its `diagram_kind` now
    # would desync the stored kind from the image that's already on disk
    # without ever regenerating it, and would silently reach into a
    # resume's already-completed work - this module must never do either.
    return [
        block
        for _page, block in document.iter_blocks()
        if block.type is BlockType.IMAGE
        and block.content.get("kind") == "diagram"
        and str(block.content.get("diagram_kind") or "").strip()
        and not block.content.get("path")
    ]


async def balance_diagram_kinds(
    document: CourseDocument,
    *,
    ai: AIClient | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Relabels a bounded number of over-used `diagram_kind` blocks to a
    better-fitting alternative. Must run BEFORE diagram/image generation
    for those blocks (see `CourseService._finalise`'s own call site) -
    relabelling an already-rendered image would do nothing. Returns
    human-readable notes for the generation warnings channel, the same
    shape `CourseService._continuity_warnings` already returns. Never
    raises: a failure here just leaves every block with whatever kind the
    writer already picked, exactly as if this pass didn't exist."""
    settings = settings or get_settings()
    if not settings.enable_diagram_diversity_pass:
        return []
    blocks = _diagram_blocks(document)
    if len(blocks) < _MIN_DIAGRAM_BLOCKS:
        return []

    counts: dict[str, int] = {}
    for block in blocks:
        kind = str(block.content.get("diagram_kind") or "").strip()
        counts[kind] = counts.get(kind, 0) + 1
    overused = {kind for kind, n in counts.items() if n > _OVERUSE_THRESHOLD}
    if not overused:
        return []  # zero AI calls - nothing is actually overused, per MIN_DIAGRAM_BLOCKS's own docstring

    candidates = [block for block in blocks if str(block.content.get("diagram_kind") or "").strip() in overused]
    histogram = ", ".join(f"{kind} x{n}" for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines = [
        f"- block {block.id}: currently '{block.content.get('diagram_kind')}' - "
        f"{_brief(block.content) or '(no brief available)'}"
        for block in candidates
    ]

    try:
        plan = await (ai or get_ai_client()).structured(
            schema=_DiversityPlan,
            system=DIVERSITY_SYSTEM,
            user=(
                f"Kind usage across this course: {histogram}\n\n"
                f"Available kinds: {', '.join(DIAGRAM_KINDS)}\n\n"
                "Over-used blocks:\n" + "\n".join(lines)
            ),
            model=settings.diagram_model,
            purpose="diagram_diversity_pass",
            phase="image",
        )
    except Exception as exc:  # noqa: BLE001 - never block course generation on this advisory pass
        log.warning("Diagram diversity pass failed: %s - every block keeps its original kind", exc)
        return []

    by_id = {block.id: block for block in blocks}
    notes: list[str] = []
    for reassignment in plan.reassignments[:_MAX_REASSIGNMENTS]:
        block = by_id.get(reassignment.block_id)
        new_kind = reassignment.new_kind.strip()
        if block is None or not new_kind or new_kind not in DIAGRAM_KINDS:
            continue
        old_kind = str(block.content.get("diagram_kind") or "")
        if new_kind == old_kind:
            continue
        block.content = merge_content(block.type, block.content, {"diagram_kind": new_kind})
        notes.append(f"Block {block.id}: {old_kind} -> {new_kind} for more visual variety.")
    return notes
