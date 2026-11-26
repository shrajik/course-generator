"""Orchestrates a `concept_experience` visual end to end - the sibling of
`app.services.diagram_service.DiagramService` for the new concept-teaching
pipeline (see the approved plan at
C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md).

Flow: plan (VisualPlanner) -> blueprint defaults (concept_visual_blueprints)
-> QA (concept_qa) -> ONE targeted retry -> render (concept_experience_renderer)
-> save asset. Every call plans and renders fresh.

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
from app.services.quality_framework import visual_qa_finding_entries
from app.services.storage_service import StorageService, get_storage
from app.services.visual_consistency import apply_concept_consistency
from app.services.visual_planner import VisualPlanner

log = get_logger(__name__)

_CORRECTION_DRIFT_MIN_LABELS = 2
_CORRECTION_DRIFT_RATIO_FLOOR = 0.4


def _spec_labels(spec: DiagramSpec) -> set[str]:
    return {
        item.label.strip().lower()
        for item in (*spec.entities, *spec.steps, *spec.concept_states)
        if item.label.strip()
    }


def _correction_drift_issue(existing_spec: dict | None, spec: DiagramSpec) -> str | None:
    """Mirrors `DiagramService._correction_drift_issue` exactly (see its own
    docstring for the full reasoning) for the concept_experience pipeline:
    a deterministic, zero-AI-call check of how much of the ORIGINAL
    visual's labelled content (entities/steps/states) survived a
    replace_image correction. `None` when there's nothing to compare (a
    fresh generation) or too few labels to measure meaningfully."""
    if not existing_spec:
        return None
    try:
        old_labels = _spec_labels(DiagramSpec.model_validate(existing_spec))
    except Exception:  # noqa: BLE001 - a malformed stored spec just can't be compared
        return None
    if len(old_labels) < _CORRECTION_DRIFT_MIN_LABELS:
        return None
    new_labels = _spec_labels(spec)
    kept = old_labels & new_labels
    ratio = len(kept) / len(old_labels)
    if ratio >= _CORRECTION_DRIFT_RATIO_FLOOR:
        return None
    return (
        f"This regenerated a visual that was meant to be CORRECTED, but only "
        f"{len(kept)}/{len(old_labels)} of its original entity/step/state labels survived - "
        f"it may be a different visual rather than the same one with the requested fix."
    )


_UNDER_CORRECTION_MIN_PROMPT_CHARS = 15  # long enough to be a real instruction, not a stray word


def _under_correction_issue(existing_spec: dict | None, prompt: str, spec: DiagramSpec) -> str | None:
    """Mirrors `DiagramService._under_correction_issue` exactly (see its own
    docstring for the full reasoning) for the concept_experience pipeline -
    the OTHER direction of `_correction_drift_issue` just above: a
    correction asking for a SUBSTANTIAL, specific change but coming back
    byte-identical to the spec it was meant to correct is a measurable
    signal the instruction was effectively ignored, not applied. A visible,
    inspectable finding only, never a retry, never a reason to reject the
    result. `existing_spec` is already the FULL original spec dict here
    (unlike the diagram pipeline's compact `existing_outline`), so this can
    compare it directly - no separate "raw" spec needed."""
    if not existing_spec or len(prompt.strip()) < _UNDER_CORRECTION_MIN_PROMPT_CHARS:
        return None
    try:
        old_dump = DiagramSpec.model_validate(existing_spec).model_dump(mode="json")
    except Exception:  # noqa: BLE001 - an unparseable old spec just can't be compared
        return None
    if old_dump != spec.model_dump(mode="json"):
        return None
    shown_prompt = prompt.strip()[:80] + ("..." if len(prompt.strip()) > 80 else "")
    return (
        f"This regenerated a visual in response to a specific correction request "
        f"('{shown_prompt}'), but the result is identical to the one before it - the "
        f"request may not have actually been applied."
    )


# Pre-generation planning follow-up (diagram quality audit, Phase 2) for the
# concept_experience pipeline - mirrors DiagramService's own
# `_SIMPLE_COMPLEXITY_MAX_NODES`/`_complexity_mismatch_issue` exactly (see
# their docstrings for the full reasoning), measuring "how much structure
# did this actually produce" as entities+steps+concept_states combined,
# since a concept_experience spec can use any one of those three lists
# depending on `representation`.
_SIMPLE_COMPLEXITY_MAX_ITEMS = 6


def _complexity_mismatch_issue(spec: DiagramSpec) -> str | None:
    """Mirrors `DiagramService._complexity_mismatch_issue` exactly, for the
    concept_experience pipeline. Unlike the diagram pipeline (which folds
    this into a combined structural retry), this pipeline has no such
    retry loop - its only retry is the existing concept_qa pass above - so
    this is wired as a QA finding only, same as `_correction_drift_issue`/
    `_under_correction_issue` just above: visible and inspectable, never a
    reason to re-plan or withhold the result."""
    if spec.complexity_estimate.strip().lower() != "simple":
        return None
    item_count = len(spec.entities) + len(spec.steps) + len(spec.concept_states)
    if item_count <= _SIMPLE_COMPLEXITY_MAX_ITEMS:
        return None
    return (
        f"This visual was estimated as 'simple' but has {item_count} entities/steps/states "
        f"combined - more than a genuinely simple visual needs (roughly "
        f"{_SIMPLE_COMPLEXITY_MAX_ITEMS} or fewer). Either consolidate down to match a "
        "genuinely simple shape, or this content actually needs a 'moderate'/'complex' "
        "structure - don't just relabel the estimate without changing the content."
    )


class ConceptVisualService:
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
        self.planner = VisualPlanner(self.ai, self.settings, self.memory)

    async def _check(self, spec: DiagramSpec) -> DiagramQAResult:
        return await evaluate_concept_experience(spec, ai=self.ai, settings=self.settings)

    async def _plan_and_prepare(
        self, *, purpose: str, prompt: str, caption: str, course_title: str, block_id: str, qa_feedback: str = "",
        existing_spec: dict | None = None,
    ) -> DiagramSpec:
        spec = await self.planner.plan(
            purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
            block_id=block_id, qa_feedback=qa_feedback, existing_spec=existing_spec,
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

        # A real, confirmed bug this closes: `replace_image` (the editor's
        # "fix this visual" workflow) clears `content["path"]` so this
        # block regenerates, but never cleared `content["spec"]` - the
        # existing entities/steps/metaphor just sat there unused while
        # this call planned from `purpose`/`prompt`/`caption` alone, with
        # zero awareness the old visual ever existed, so a "fix the
        # overlapping label" correction came back as a different visual
        # instead of the same one with that one thing fixed. See
        # DiagramService.generate_for_block's identical fix/comment for the
        # sibling `diagram` pipeline.
        existing_spec = content.get("spec")
        existing_spec = existing_spec if isinstance(existing_spec, dict) and existing_spec else None

        try:
            spec = await self._plan_and_prepare(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title, block_id=block.id,
                existing_spec=existing_spec,
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
                    block_id=block.id, qa_feedback=result.feedback(), existing_spec=existing_spec,
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

        # Course-wide recurring-concept consistency (diagram quality audit
        # follow-up, Phase 8) - see app.services.visual_consistency's own
        # module docstring. Only re-renders (cheap, deterministic, zero
        # extra AI call) when an icon genuinely needed overriding.
        if apply_concept_consistency(course_id, spec):
            try:
                html_bytes = render_concept_experience_html(spec, template.theme)
            except Exception as exc:  # noqa: BLE001 - keep the pre-override render rather than fail the block
                log.warning("Concept visual consistency re-render failed for %s: %s", block.id, exc)

        relative = self.storage.save_asset(course_id, html_bytes, extension="html")
        # See _correction_drift_issue's own docstring - a no-op (None)
        # whenever this generation wasn't a replace_image correction at
        # all (existing_spec unset), so a fresh first-time generation
        # never gets a finding here.
        drift_issue = _correction_drift_issue(existing_spec, spec)
        under_correction_issue = _under_correction_issue(existing_spec, prompt, spec)
        # Same "measured fresh against whatever actually shipped" contract
        # as the other two checks just above.
        complexity_mismatch_issue = _complexity_mismatch_issue(spec)
        # A real, spec-specific estimate - not a fixed placeholder - so the
        # page layout engine reserves the actual space this content needs.
        # Getting this wrong doesn't just make the block look odd; the next
        # block on the page is positioned from it, so an undersized estimate
        # makes the NEXT block overlap this one (see estimate_pixel_size's
        # own docstring).
        width, height = estimate_pixel_size(spec)
        # TASK 3 (quality audit follow-up) - unlike DiagramService, this
        # pipeline has no fallback-on-QA-failure: it renders `spec` either
        # way, keeping the better of the original/retried attempt. So when
        # `result.issues` is still non-empty here, that is the FINAL,
        # SHIPPED state, not a problem a fallback already absorbed -
        # recorded explicitly (including the clean "[]" case) so a re-render
        # never leaves a stale list from an earlier, worse attempt.
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
                # The structured spec this rendering came from - kept (not
                # just the rendered picture) so the editor can later let a
                # user fix a label/description and re-render deterministically
                # without a new AI planning call. See
                # DocumentService.update_visual_spec.
                "spec": spec.model_dump(mode="json"),
                "visual_qa_findings": visual_qa_finding_entries(
                    # Carries each issue's own real provenance (quality-
                    # governance follow-up, Phase 3.7) - Layer 1 (structural)
                    # issues are deterministic=True, Layer 2 (concept_qa's
                    # one LLM critique call) issues are deterministic=False;
                    # never collapsed to one blanket value here, since a
                    # single result can (by evaluate_concept_experience's
                    # own short-circuit contract) only ever be ALL layer-1 or
                    # ALL layer-2 - but reading each issue's own flag keeps
                    # this correct even if that contract ever changes.
                    [
                        (issue.reason, issue.detail, issue.deterministic) for issue in result.issues
                    ] + ([("replace_image_correction_drift", drift_issue, True)] if drift_issue else [])
                    + ([("replace_image_correction_not_applied", under_correction_issue, True)] if under_correction_issue else [])
                    + ([("concept_complexity_mismatch", complexity_mismatch_issue, True)] if complexity_mismatch_issue else []),
                    resolution="rendered_with_unresolved_issues",
                ),
            },
        )
        log.info(
            "Generated concept_experience visual %s (concept=%s, representation=%s) for block %s",
            relative, spec.concept, spec.representation, block.id,
        )
        return True
