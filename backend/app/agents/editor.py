"""AI editing agent - the backend foundation for the future visual editor.

Returns a validated patch. It never returns a document, and it may only touch the
blocks the user selected (plus blocks it inserts next to them).
"""

from __future__ import annotations

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.course.templates.registry import ensure_template_loaded, load_template
from app.schemas.document import Block, CourseDocument
from app.schemas.patch import DocumentPatch
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

CONTEXT_RADIUS = 2

# Conservative, zero-extra-LLM-call signal for an explicit "I want a real
# AI-generated picture, not a structured diagram" request. Real, confirmed
# gap: EDITOR_SYSTEM's shape classification (comparison/flow_chart/hierarchy/
# ...) applies even to a generic "convert this into an image" instruction, so
# prose that merely describes two contrasting things (e.g. a rolling suitcase
# vs. a sliding box) can get routed to a structured comparison diagram
# instead of the raster illustration the user actually asked for - no AI
# image model ever gets called. The bare, generic "convert this into an
# image"/"turn this into a diagram" phrasing is deliberately left alone here
# (it's the documented ambiguous case that should keep running the full shape
# check) - this only fires on wording that goes further and explicitly asks
# for a real/actual/photographic/AI picture, or explicitly rules out a
# diagram/chart. False-negative biased on purpose: missing an edge-case
# phrasing just falls back to today's existing classification, never to a
# misfire that overrides a genuinely diagram-shaped request.
_EXPLICIT_ILLUSTRATION_PHRASES = (
    "real image", "real picture", "real photo", "actual image", "actual picture",
    "actual photo", "actual illustration", "realistic image", "realistic picture",
    "realistic illustration", "photorealistic", "photo-realistic", "ai image",
    "ai-generated image", "ai generated image", "ai generated picture",
    "not a diagram", "not a chart", "not a flowchart", "no diagram", "no chart",
    "instead of a diagram", "instead of a chart", "draw a picture", "draw an image",
    "draw a photo", "paint a picture", "a photo of", "a photograph of",
)


def requests_real_illustration_over_diagram(instruction: str) -> bool:
    lowered = instruction.lower()
    return any(phrase in lowered for phrase in _EXPLICIT_ILLUSTRATION_PHRASES)


_ALLOWED_STYLE_KEYS = {
    "font_family",
    "font_size",
    "font_weight",
    "line_height",
    "color",
    "background",
    "border_color",
    "border_radius",
    "border_width",
    "padding",
    "align",
    "italic",
    "letter_spacing",
    "accent_color",
}


class EditorAgent:
    def __init__(self, ai: AIClient | None = None, settings: Settings | None = None) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()

    async def edit(
        self,
        *,
        document: CourseDocument,
        selected_block_ids: list[str],
        instruction: str,
    ) -> DocumentPatch:
        selected = self._resolve_selection(document, selected_block_ids)
        await ensure_template_loaded(document.template_id)
        template = load_template(document.template_id)
        patch = await self.ai.structured(
            schema=DocumentPatch,
            system=prompts.EDITOR_SYSTEM,
            user=prompts.editor_user(
                document.course_title,
                template,
                selected,
                self._context_blocks(document, selected_block_ids),
                instruction,
                audience=document.meta.audience,
                force_illustration_override=requests_real_illustration_over_diagram(instruction),
            ),
            model=self.settings.editor_model,
            purpose="editor",
        )
        patch.operations = self._sanitise(patch, document, set(selected_block_ids))
        if not patch.operations:
            raise ValidationFailedError(
                "The editor returned no applicable operations for this instruction",
                details={"reasoning": patch.reasoning},
            )
        return patch

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _resolve_selection(document: CourseDocument, block_ids: list[str]) -> list[Block]:
        blocks: list[Block] = []
        missing: list[str] = []
        for block_id in block_ids:
            found = document.find_block(block_id)
            if found is None:
                missing.append(block_id)
            else:
                blocks.append(found[1])
        if missing:
            raise ValidationFailedError(
                f"Unknown block ids: {', '.join(missing)}",
                details={"missing_block_ids": missing},
            )
        return blocks

    @staticmethod
    def _context_blocks(document: CourseDocument, block_ids: list[str]) -> list[Block]:
        all_blocks = [block for _, block in document.iter_blocks()]
        index_of = {block.id: i for i, block in enumerate(all_blocks)}
        wanted: set[int] = set()
        for block_id in block_ids:
            centre = index_of.get(block_id)
            if centre is None:
                continue
            for offset in range(-CONTEXT_RADIUS, CONTEXT_RADIUS + 1):
                position = centre + offset
                if 0 <= position < len(all_blocks) and all_blocks[position].id != block_id:
                    wanted.add(position)
        return [all_blocks[i] for i in sorted(wanted)]

    @staticmethod
    def _sanitise(
        patch: DocumentPatch, document: CourseDocument, selected: set[str]
    ) -> list:
        """Drop operations that target blocks outside the selection or don't exist."""
        known = set(document.block_ids())
        kept = []
        for operation in patch.operations:
            target = getattr(operation, "block_id", None)
            anchor = getattr(operation, "after_block_id", None) or getattr(
                operation, "before_block_id", None
            )
            reference = target or anchor
            if reference is not None:
                if reference not in known:
                    log.warning("Editor referenced unknown block %s - dropped", reference)
                    continue
                if reference not in selected:
                    log.warning(
                        "Editor tried to touch unselected block %s - dropped", reference
                    )
                    continue
            if operation.type == "update_style":
                operation.style = {
                    k: v for k, v in operation.style.items() if k in _ALLOWED_STYLE_KEYS
                }
                if not operation.style:
                    continue
            if operation.type == "update_block" and operation.style is not None:
                operation.style = {
                    k: v for k, v in operation.style.items() if k in _ALLOWED_STYLE_KEYS
                }
            kept.append(operation)
        return kept
