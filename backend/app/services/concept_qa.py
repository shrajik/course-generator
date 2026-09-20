"""Quality gate for a `concept_experience` visual spec - the concept-level
counterpart to `app.services.diagram_qa`'s geometry/blueprint checks for
`schematic`.

Two layers, run in order (see the approved plan at
C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md for the full design):

1. Deterministic structural QA (Python, no LLM call, always runs first) -
   entity/step/state presence appropriate to the declared `representation`
   (see `_representation_plausible` - never blindly trusts the planner's
   representation choice), instance-value distinctness, a single-line
   `technical_signature`, non-empty `learning_objectives`/`core_message`/
   `validation_criteria`, label length caps, and the "default state is
   already complete" check that structurally guarantees no-click-required
   rendering. `representation`-aware throughout: every check names which of
   the 11 REPRESENTATION_TYPES families it applies to, never hardcoded to
   Java or any other single subject.
2. Semantic QA (one GPT-5-mini structured call, only runs once layer 1
   passes - a Layer-1 failure never spends a Layer-2 call) - grades the
   spec's own `learning_objectives`, `core_message` and `validation_criteria`
   against the structured spec itself, never rendered pixels - no vision
   model, matching `diagram_qa`'s existing no-vision-model constraint.
   Supplements layer 1, never replaces it.

Reuses `DiagramQAIssue`/`DiagramQAResult` from `diagram_qa` rather than
duplicating them - both are already generic (reason/detail strings), not
schematic-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.schemas.diagram import DiagramSpec
from app.services.diagram_qa import DiagramQAIssue, DiagramQAResult
from app.services.openai_service import AIClient, get_ai_client

# --- structural (layer 1) failure reasons -----------------------------------
MISSING_ENTITY = "missing_entity"
NO_INSTANCE_VARIATION = "no_instance_variation"
MULTILINE_TECHNICAL_SIGNATURE = "multiline_technical_signature"
INCOMPLETE_DEFAULT_STATE = "incomplete_default_state"
MISSING_LEARNING_OBJECTIVES = "missing_learning_objectives"
TOO_DENSE = "too_dense"
GENERIC_VISUAL = "generic_visual"
DANGLING_TRANSITION = "dangling_transition"

# --- semantic (layer 2) failure reasons -------------------------------------
CONCEPT_NOT_CLEAR = "concept_not_clear"
CORE_MESSAGE_NOT_DELIVERED = "core_message_not_delivered"
WRONG_RELATIONSHIP = "wrong_relationship"

_LABEL_LENGTH_CAP = 40  # characters - "short, a few words", not a sentence
# Representations where a learner comparing concrete instances is central to
# the whole point - the only ones instance-count/variation checks apply to.
_INSTANCE_DRIVEN_REPRESENTATIONS = ("object", "comparison", "data_structure")
_METAPHOR_FRIENDLY_REPRESENTATIONS = ("object", "spatial")
_SEQUENCE_REPRESENTATIONS = ("process", "sequence", "pipeline")
_ENTITY_BASED_REPRESENTATIONS = ("object", "data_structure", "comparison", "code_visualization", "spatial")


# ---------------------------------------------------------------------------
# Layer 1: deterministic structural QA
# ---------------------------------------------------------------------------


def _structural_issues(spec: DiagramSpec) -> list[DiagramQAIssue]:
    issues: list[DiagramQAIssue] = []
    representation = spec.representation.strip() or "object"

    if not spec.learning_objectives:
        issues.append(DiagramQAIssue(
            MISSING_LEARNING_OBJECTIVES,
            "Set at least one `learning_objectives` entry - what should a learner be able to "
            "explain after seeing this visual?",
        ))
    if not spec.core_message.strip():
        issues.append(DiagramQAIssue(
            MISSING_LEARNING_OBJECTIVES,
            "Set `core_message` - the one short, quotable sentence this visual should deliver.",
        ))
    if not spec.validation_criteria:
        issues.append(DiagramQAIssue(
            MISSING_LEARNING_OBJECTIVES,
            "Set at least one `validation_criteria` entry - a checkable assertion for what this "
            "visual must show.",
        ))

    if "\n" in spec.technical_signature:
        issues.append(DiagramQAIssue(
            MULTILINE_TECHNICAL_SIGNATURE,
            "`technical_signature` must be a single line - never a multi-line code block or "
            "anything resembling a code screenshot.",
        ))

    issues.extend(_representation_plausible(spec, representation))

    if representation in _METAPHOR_FRIENDLY_REPRESENTATIONS and not spec.visual_metaphor.strip():
        issues.append(DiagramQAIssue(
            GENERIC_VISUAL,
            f"A {representation} visual usually needs a concrete, everyday `visual_metaphor` - "
            "none is set, so this risks reading as a generic labelled diagram instead of a "
            "beginner-friendly one.",
        ))

    if representation in _INSTANCE_DRIVEN_REPRESENTATIONS:
        issues.extend(_instance_issues(spec, representation))

    if representation == "state_machine":
        issues.extend(_state_machine_issues(spec))

    long_labels = [
        e.label for e in spec.entities if len(e.label) > _LABEL_LENGTH_CAP
    ] + [
        s.label for s in spec.steps if len(s.label) > _LABEL_LENGTH_CAP
    ] + [
        st.label for st in spec.concept_states if len(st.label) > _LABEL_LENGTH_CAP
    ]
    if long_labels:
        issues.append(DiagramQAIssue(
            TOO_DENSE,
            f"Some labels are too long (over {_LABEL_LENGTH_CAP} characters, e.g. "
            f"'{long_labels[0][:60]}...') - keep every label/property/action short, this is a "
            "visual, not a passage of prose.",
        ))

    return issues


def _representation_plausible(spec: DiagramSpec, representation: str) -> list[DiagramQAIssue]:
    """Point 8 of the approved plan - never blindly trust the planner's
    representation choice. If the declared `representation` structurally
    requires content the spec doesn't have, that's a real, retryable issue,
    not a silent mismatch discovered only at render time."""
    if representation in _SEQUENCE_REPRESENTATIONS:
        if not spec.steps and not spec.concept_states:
            return [DiagramQAIssue(
                MISSING_ENTITY,
                f'representation="{representation}" needs at least one `steps` entry (an ordered '
                "sequence) - none is set.",
            )]
        return []
    if representation == "state_machine":
        if not spec.concept_states:
            return [DiagramQAIssue(
                MISSING_ENTITY,
                'representation="state_machine" needs at least one `concept_states` entry - none is set.',
            )]
        return []
    if representation in ("relationship", "hierarchy"):
        if len(spec.entities) < 2 and not spec.relationships:
            return [DiagramQAIssue(
                MISSING_ENTITY,
                f'representation="{representation}" needs at least 2 entities or at least 1 declared '
                f"relationship - found {len(spec.entities)} entities and {len(spec.relationships)} relationships.",
            )]
        return []
    if representation in _ENTITY_BASED_REPRESENTATIONS:
        issues: list[DiagramQAIssue] = []
        if not spec.entities:
            issues.append(DiagramQAIssue(
                MISSING_ENTITY, f'representation="{representation}" needs at least one entity - none is set.',
            ))
        if representation == "code_visualization" and not spec.technical_signature.strip():
            issues.append(DiagramQAIssue(
                MISSING_ENTITY, 'representation="code_visualization" needs a non-blank `technical_signature`.',
            ))
        return issues
    # A genuinely unrecognised representation value - fall back to the same
    # blanket "some structure must exist" safety net every kind of spec
    # needs, matching the renderer's own "falls back to object, never a
    # crash" contract.
    if not (spec.entities or spec.steps or spec.concept_states):
        return [DiagramQAIssue(
            MISSING_ENTITY,
            f'The visual has no entities, steps or states for representation="{representation}" - add '
            "whichever structure this representation actually needs.",
        )]
    return []


def _instance_issues(spec: DiagramSpec, representation: str) -> list[DiagramQAIssue]:
    issues: list[DiagramQAIssue] = []
    templates = [e for e in spec.entities if e.role == "template"]
    instances = [e for e in spec.entities if e.role != "template"]

    if representation == "object" and not templates:
        issues.append(DiagramQAIssue(
            MISSING_ENTITY,
            'An object visual needs one entity with role="template" (the blueprint/class) - use '
            'representation="comparison" instead for a template-less row of peer entities.',
        ))
    if len(instances) < 2:
        issues.append(DiagramQAIssue(
            MISSING_ENTITY,
            f"Add at least 2 instance-role entities (found {len(instances)}) so a learner can "
            "compare real examples.",
        ))
    else:
        signatures = {tuple(sorted(e.properties.items())) for e in instances}
        if len(signatures) < len(instances):
            issues.append(DiagramQAIssue(
                NO_INSTANCE_VARIATION,
                "Two or more instance entities have identical property values - give each a "
                "genuinely different value so the distinction between them is actually visible.",
            ))
        empty_props = [e.id for e in instances if e.properties and not any(v.strip() for v in e.properties.values())]
        if empty_props:
            issues.append(DiagramQAIssue(
                INCOMPLETE_DEFAULT_STATE,
                f"These instance entities have no populated property values yet: "
                f"{', '.join(empty_props)} - the default state must already be a complete "
                "picture, not filled in only after an interaction.",
            ))
    return issues


def _state_machine_issues(spec: DiagramSpec) -> list[DiagramQAIssue]:
    issues: list[DiagramQAIssue] = []
    state_ids = {s.id for s in spec.concept_states if s.id}
    if not state_ids:
        issues.append(DiagramQAIssue(
            MISSING_ENTITY, "A state_machine visual needs at least one `concept_states` entry.",
        ))
        return issues
    dangling = [t for t in spec.transitions if t.from_state not in state_ids or t.to_state not in state_ids]
    if dangling:
        names = ", ".join(f"{t.from_state}->{t.to_state}" for t in dangling[:3])
        issues.append(DiagramQAIssue(
            DANGLING_TRANSITION,
            f"These transitions reference a state that doesn't exist among concept_states: {names}.",
        ))
    return issues


# ---------------------------------------------------------------------------
# Layer 2: semantic QA (one GPT-5-mini structured call)
# ---------------------------------------------------------------------------


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    criterion: str = ""
    passed: bool = True
    detail: str = ""


class ConceptCritique(BaseModel):
    """Structured grading of a concept_experience spec against its own
    stated objectives/message/criteria - never against rendered pixels."""

    model_config = ConfigDict(extra="ignore")

    objective_verdicts: list[CriterionVerdict] = Field(default_factory=list)
    core_message_delivered: bool = True
    core_message_detail: str = ""
    criteria_verdicts: list[CriterionVerdict] = Field(default_factory=list)
    structure_correct: bool = True
    structure_detail: str = ""


CRITIQUE_SYSTEM = """\
You are a rigorous learning-experience reviewer grading a small static
teaching visual's SPECIFICATION (structure only - entities, steps, states,
labels) against the goals it declares for itself. You never see a rendered
image; judge only whether the given structure, as described, would actually
achieve each stated objective for a learner at the stated level. This
visual is one motionless picture - there is no interaction to grade.

For EACH entry in `learning_objectives`, decide whether the spec's own
entities/steps/states actually support achieving it - not whether the
objective is well-written, whether the STRUCTURE delivers it. Do the same
for EACH entry in `validation_criteria`.

Also judge:
- `core_message_delivered`: does the structure (not just the text of
  core_message itself) actually make that message obvious?
- `structure_correct`: are the entities/relationships/steps/states an
  accurate, sensible representation of the concept (not factually wrong,
  not missing something essential a beginner would need)?

Be specific and concrete in every `detail` - name exactly what's missing or
wrong, in one sentence, never a generic "could be better."
"""


def _critique_user_prompt(spec: DiagramSpec) -> str:
    objectives = "\n".join(f"- {o}" for o in spec.learning_objectives) or "(none)"
    criteria = "\n".join(f"- {c}" for c in spec.validation_criteria) or "(none)"
    entities = "\n".join(
        f"- {e.id} (role={e.role}): properties={e.properties}, actions={e.actions}" for e in spec.entities
    ) or "(none)"
    steps = "\n".join(f"- {s.order}. {s.label}: {s.description}" for s in spec.steps) or "(none)"
    return f"""\
CONCEPT: {spec.concept}
REPRESENTATION: {spec.representation}
CORE MESSAGE: {spec.core_message}
VISUAL METAPHOR: {spec.visual_metaphor or "(none)"}

LEARNING OBJECTIVES:
{objectives}

VALIDATION CRITERIA:
{criteria}

ENTITIES:
{entities}

STEPS:
{steps}

Grade this specification.
"""


async def _semantic_issues(spec: DiagramSpec, *, ai: AIClient, settings: Settings) -> list[DiagramQAIssue]:
    critique = await ai.structured(
        schema=ConceptCritique,
        system=CRITIQUE_SYSTEM,
        user=_critique_user_prompt(spec),
        model=settings.concept_visual_model,
        purpose=f"concept_qa:{spec.concept}",
        phase="image",
    )
    issues: list[DiagramQAIssue] = []
    for verdict in critique.objective_verdicts:
        if not verdict.passed:
            issues.append(DiagramQAIssue(
                CONCEPT_NOT_CLEAR,
                f"Learning objective not achievable from this spec: \"{verdict.criterion}\" - {verdict.detail}",
            ))
    if not critique.core_message_delivered:
        issues.append(DiagramQAIssue(
            CORE_MESSAGE_NOT_DELIVERED,
            critique.core_message_detail or "The structure doesn't actually deliver `core_message`.",
        ))
    for verdict in critique.criteria_verdicts:
        if not verdict.passed:
            issues.append(DiagramQAIssue(
                CONCEPT_NOT_CLEAR,
                f"Validation criterion failed: \"{verdict.criterion}\" - {verdict.detail}",
            ))
    if not critique.structure_correct:
        issues.append(DiagramQAIssue(
            WRONG_RELATIONSHIP,
            critique.structure_detail or "The entity/relationship structure doesn't correctly represent the concept.",
        ))
    return issues


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def evaluate_concept_experience(
    spec: DiagramSpec, *, ai: AIClient | None = None, settings: Settings | None = None,
) -> DiagramQAResult:
    """A no-op pass for every kind except `concept_experience`. Layer 1
    always runs; Layer 2 only runs (spending one LLM call) if Layer 1 found
    nothing wrong - a structurally broken spec is never worth grading
    semantically, and this keeps the common "obviously incomplete" retry
    case free."""
    if spec.normalised_kind() != "concept_experience":
        return DiagramQAResult()

    structural = DiagramQAResult(issues=_structural_issues(spec))
    if not structural.passed:
        return structural

    semantic = await _semantic_issues(spec, ai=ai or get_ai_client(), settings=settings or get_settings())
    return DiagramQAResult(issues=[*structural.issues, *semantic])
