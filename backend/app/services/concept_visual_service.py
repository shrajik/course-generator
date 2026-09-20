"""Orchestrates a `concept_experience` visual end to end - the sibling of
`app.services.diagram_service.DiagramService` for the new concept-teaching
pipeline (see the approved plan at
C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md).

Flow: plan (VisualPlanner) -> blueprint defaults (concept_visual_blueprints)
-> QA (concept_qa) -> ONE targeted retry -> render (concept_experience_renderer)
-> save asset. Reuse-check (VisualKnowledgeService.find_approved_for_concept)
and auto-registration land in Phase 5 - until then every call plans/renders
fresh, exactly like DiagramService did before its own reuse layer existed.

Any failure here (unusable spec, rendering error) returns False so the
caller (ImageService) falls back to the raster illustration path - a
concept_experience visual is a *preference*, not a requirement, matching the
exact safety contract DiagramService already has for `schematic`.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.render.concept_experience_renderer import estimate_pixel_size, render_concept_experience_html
from app.render.concept_visual_blueprints import apply_concept_blueprint_defaults
from app.schemas.blocks import merge_content
from app.schemas.diagram import DiagramSpec
from app.schemas.document import Block
from app.schemas.template import CourseTemplate
from app.services.concept_qa import DiagramQAResult, evaluate_concept_experience
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client
from app.services.storage_service import StorageService, get_storage
from app.services.visual_knowledge_service import VisualKnowledgeService, get_visual_knowledge_service
from app.services.visual_planner import VisualPlanner

log = get_logger(__name__)



class ConceptVisualService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
        visual_knowledge: VisualKnowledgeService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.storage = storage or get_storage()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()
        self.visual_knowledge = visual_knowledge or get_visual_knowledge_service()
        self.planner = VisualPlanner(self.ai, self.settings, self.memory)

    async def _check(self, spec: DiagramSpec) -> DiagramQAResult:
        return await evaluate_concept_experience(spec, ai=self.ai, settings=self.settings)

    async def _plan_and_prepare(
        self, *, purpose: str, prompt: str, caption: str, course_title: str, block_id: str, qa_feedback: str = "",
    ) -> DiagramSpec:
        spec = await self.planner.plan(
            purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
            block_id=block_id, qa_feedback=qa_feedback,
        )
        return apply_concept_blueprint_defaults(spec)

    async def generate_for_block(
        self,
        *,
        course_id: str,
        block: Block,
        template: CourseTemplate,
        course_title: str,
    ) -> bool:
        content = block.content
        purpose = str(content.get("purpose") or "")
        prompt = str(content.get("prompt") or "")
        caption = str(content.get("caption") or "")
        if not (purpose.strip() or prompt.strip()):
            log.warning("Concept visual block %s has no purpose/prompt - skipped", block.id)
            return False

        try:
            spec = await self._plan_and_prepare(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title, block_id=block.id,
            )
        except Exception as exc:  # noqa: BLE001 - caller falls back to illustration
            log.warning("Concept visual planning failed for %s: %s", block.id, exc)
            return False

        if not spec.is_usable():
            log.info(
                "Concept visual spec for %s (concept=%s) is not usable (%s entities/%s steps/%s states) - "
                "falling back",
                block.id, spec.concept, len(spec.entities), len(spec.steps), len(spec.concept_states),
            )
            return False

        result = await self._check(spec)
        if not result.passed:
            log.info(
                "Concept QA failed for %s (%s) - retrying with targeted feedback",
                block.id, [issue.reason for issue in result.issues],
            )
            try:
                retried = await self._plan_and_prepare(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    block_id=block.id, qa_feedback=result.feedback(),
                )
            except Exception as exc:  # noqa: BLE001 - keep the first attempt, don't fail the block
                log.warning("Concept visual QA retry failed for %s: %s", block.id, exc)
                retried = None
            if retried is not None and retried.is_usable():
                retried_result = await self._check(retried)
                if len(retried_result.issues) < len(result.issues):
                    spec, result = retried, retried_result

        try:
            html_bytes = render_concept_experience_html(spec, template.theme)
        except Exception as exc:  # noqa: BLE001 - rendering must not fail the block
            log.warning("Concept visual rendering failed for %s: %s", block.id, exc)
            return False

        relative = self.storage.save_asset(course_id, html_bytes, extension="html")
        # A real, spec-specific estimate - not a fixed placeholder - so the
        # page layout engine reserves the actual space this content needs.
        # Getting this wrong doesn't just make the block look odd; the next
        # block on the page is positioned from it, so an undersized estimate
        # makes the NEXT block overlap this one (see estimate_pixel_size's
        # own docstring).
        width, height = estimate_pixel_size(spec)
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
            },
        )
        log.info(
            "Generated concept_experience visual %s (concept=%s, representation=%s) for block %s",
            relative, spec.concept, spec.representation, block.id,
        )
        return True
