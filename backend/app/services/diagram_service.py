"""Diagram generation for `image` blocks with `kind == "diagram"`.

Unlike a raster illustration, a diagram is never drawn by a generative model:
the model returns *structure* (a `DiagramSpec` - nodes, edges, kind) via the
same `ai.structured()` call every other agent in this codebase uses, which
already gives us JSON-schema validation, a repair pass and retries for free.
`app.render.diagram_renderer` then turns that structure into an on-theme SVG
deterministically, so labels are always exactly what was asked for.

The writer may optionally pin `content["diagram_kind"]` (e.g. "concept_map")
when it already knows what shape the visual needs - see
`app.agents.prompts.WRITER_SYSTEM`. When it does, a returned spec whose kind
doesn't actually match gets one corrective retry before being accepted, so a
requested conceptual/relationship diagram never silently turns into a
flowchart just because that's what the model defaulted to.

Any failure here (bad/unusable spec, rendering error) is left to the caller
(`ImageService`) to fall back to the raster illustration path - a diagram is a
*preference*, not a requirement, and a block must never end up with nothing.
"""

from __future__ import annotations

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.render.diagram_renderer import render_diagram_svg
from app.render.schematic_layout import resolve_schematic_layout
from app.render.visual_blueprints import apply_blueprint_defaults
from app.schemas.blocks import merge_content
from app.schemas.diagram import RELATIONSHIP_KINDS, SEQUENTIAL_KINDS, DiagramSpec, max_nodes_for
from app.schemas.document import Block
from app.schemas.template import CourseTemplate
from app.services.diagram_qa import DiagramQAResult, evaluate_blueprint, evaluate_schematic
from app.services.diagram_render_qa import validate_rendered_svg
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)

MAX_BRIEF_CHARS = 2000

DIAGRAM_SYSTEM = """\
You are two experts working as one: a professor who is a genuine
subject-matter authority in whatever field this brief belongs to, and a
professional 2D textbook illustrator who has spent a career drawing clean,
precise diagrams for that field's textbooks. Think like the professor when
deciding what the diagram must show and whether every label and fact is
correct; think like the illustrator when deciding how to structure, space and
label it. The result must read like a diagram pulled straight out of a
well-produced textbook: perfectly labelled, clearly structured, generously
spaced, and with nothing crowded, crossed or overlapping.

You turn a visual request into a structured diagram specification. You never
draw anything yourself - the specification is rendered deterministically
afterwards - so the only things that matter are (1) choosing the structure a
textbook illustrator would choose for this exact concept, and (2) writing
precise, short, unambiguous labels a professor would stand behind.

First decide what the brief actually needs, then pick exactly one `kind`:

- The brief describes STEPS that happen in order (a procedure, a sequence of
  stages, a decision path) -> flow_chart or process. Order `nodes` the way
  the steps happen and add one `edge` per consecutive pair. Add an edge
  `label` only when the transition needs one (e.g. "if valid").
- The steps repeat with no fixed end -> cycle. Order `nodes` around the loop;
  the renderer closes it back to the first node automatically.
- The brief describes a THING made of parts, or a phenomenon/mechanism whose
  components relate to or act on each other, and a labelled-boxes-and-arrows
  diagram communicates it well enough (an organisation, a reaction pathway,
  an abstract system of parts) -> concept_map. Put the central subject as the
  node with `level: 0`; every other node is a component or factor around it,
  `level: 1`. Every edge must carry a short, precise relationship `label`
  naming what happens between the two nodes - a direction, a force, a cause,
  a flow (e.g. "moving toward", "induces", "opposes", "flows into").
- The brief describes something with a real, recognisable physical shape and
  arrangement - not just abstract relationships between ideas - where seeing
  that arrangement is what actually teaches the concept -> schematic. This
  applies just as much to a chemistry reaction (two reactants combining into
  a product), a biology structure (a cell and its organelles, an organ), an
  astronomy scene (a planet and its orbit) or a mechanical device as it does
  to an electrical apparatus - use whichever domain the brief is actually
  about, never default to an electrical/physics scene just because that's a
  common example. Often pairs with a "before/after", "at rest/in motion" or
  "reactant/product" comparison. Use the small illustrated-shape vocabulary
  below instead of nodes/edges - this is the right choice whenever the brief
  describes a drawing with real parts and positions, not just a relationship
  between labelled ideas; never substitute concept_map for a brief that
  clearly wants an illustration, regardless of subject.
- The brief describes a strict parent/child breakdown (an org chart, a
  taxonomy, a category tree) -> hierarchy. Set every node's `level` (0 =
  root, 1 = its children, ...) and add one edge per parent -> child link.
- The brief compares two or more things side by side -> comparison. Each
  node is one thing being compared; leave `edges` empty.
- The brief is specifically about how DATA moves through a system - a
  request/response pipeline, a pipeline of processing stages with an
  external caller and a data store, a system-design walkthrough -> data_flow_diagram.
  This is still a SEQUENCE (order `nodes` the way data actually flows,
  one edge per consecutive step, labelled with what's being sent - "Login
  request", "Query result"), but each node also gets a `shape_role`:
  the outside actor that starts/receives the flow is `"entity"`, a place
  data is written to or read from is `"store"`, everything else (the
  actual processing) is left blank (a plain process step). Only a strictly
  linear chain is supported - never describe a step that loops back to an
  earlier one.
- The brief is specifically about a DATABASE's entities and how they
  relate (tables, records, a schema) -> er_diagram. Each real-world entity
  (a table) is a node with `shape_role` left blank; each relationship
  between two entities (e.g. "enrolled in") is its OWN node with
  `shape_role: "relationship"`, connected to both entities it relates via
  two edges (entity -> relationship -> entity), each edge's `label` giving
  that side's cardinality ("1", "N", "0..1"). A field/column belonging to
  one entity (or to a relationship, e.g. a join table's own column) is a
  node with `shape_role: "attribute"` and `parent_id` set to that entity's
  or relationship's `id` - do not connect an attribute via `edges`, only
  `parent_id`. Keep attribute labels to just the field name.
- The brief is specifically about a process that crosses roles/actors/
  systems, and which one does WHICH step actually matters (a support
  ticket moving between a customer, a support agent and a billing system;
  a request crossing a frontend, a backend and a database) -> swimlane.
  Every node needs `lane` set to the role/actor/system it belongs to
  (short, consistent names - the same lane name across nodes groups them
  into one column, in the order lanes first appear). Order `nodes` the way
  the work actually flows and add one edge per handoff, whether it stays
  in the same lane or crosses into another - a crossing edge is normal and
  expected, that's the whole point of this shape. Do NOT use swimlane just
  because a process has multiple steps - only when WHO/WHAT SYSTEM does
  each step is itself part of what the brief needs to teach.
- Anything else - a short list of steps, pillars, principles or ideas with
  no real relationships between them -> smart_art. Leave `edges` empty.

For `flow_chart`/`process` specifically: if the sequence has a genuine
bounded start and end (a bounded procedure, not an ongoing pipeline), add
explicit nodes literally labelled "Start" and "End" as the first and last
node - the renderer recognises those exact words and draws them as
coloured start/end markers (green/red), the standard flowchart convention.
Leave them out for a sequence with no natural bookends (an ongoing data
pipeline, a cycle). A node with 2 outgoing edges (e.g. a Yes/No decision) is
automatically drawn as a decision diamond - just add both edges, each with
its own `label` ("Yes"/"No" or whatever the two outcomes are called); a
loop is a branch whose edge points back at the decision node's own `id`.

Rules for flow_chart/process/cycle/concept_map/hierarchy/comparison/smart_art/
data_flow_diagram/er_diagram/swimlane:
- Produce between 2 and 8 `nodes` for concept_map/hierarchy/comparison/
  smart_art - those are meant to stay a small, scannable set of
  relationships. For flow_chart/process/cycle/data_flow_diagram/swimlane
  you may go up to 14 nodes when the brief genuinely describes that many
  distinct steps (e.g. a real request pipeline with its error branches, an
  agent's decide/call-tool/observe loop) - never pad a simple sequence with
  extra steps just to look more thorough, but never compress a genuinely
  multi-stage technical process down to 8 boxes either; a rejected,
  too-small diagram falls back to a raster illustration that cannot
  reliably render that many text labels, which is worse than a few extra
  boxes. Give each node a short, unique `id`, a
  `label` (2-6 words - the concept/action/decision itself, e.g. "Check
  saturation current", never "Screen hard limits (Isat, SRF)" with the
  reasoning folded in) and, only when it adds real information, a `detail`
  of AT MOST 6-8 words - a short qualifying phrase, never a full sentence
  and never the label's own reasoning restated. If what you'd put in
  `detail` doesn't fit in well under 10 words, it belongs in the
  surrounding paragraph text instead, not crammed into the node - a reader
  can hover any node for its full label+detail in a tooltip, so the box
  itself only ever needs to name the thing, not explain it. Keep labels and
  details this concise on purpose - the renderer gives every node a fixed,
  often narrow box, and a textbook-clean diagram never has text
  overflowing, wrapping badly or crowding its neighbours.
- Base every label and detail only on the brief below. Do not invent facts,
  numbers or names that were not given to you. No markdown, no quotes.

Rules for `schematic` - fill `shapes` (or `states` for a before/after
comparison) instead of `nodes`/`edges`. This is a small, generic drawing kit,
not physics-specific - it is exactly as appropriate for a chemistry reaction,
a biology structure or a mechanical device as it is for an electrical one:
- Each shape has a `type`:
  * "block" - a labelled rectangle: a magnet, a battery, a beaker/flask, a
    reactant/product, a machine part - any rectangular object. Set both
    `label` and `sublabel` for a two-part object (a magnet's N/S poles, a
    battery's +/- terminals, a reactant turning into a product).
  * "circle" - a labelled round object: a cell, an atom, a seed, a planet,
    a droplet - any round object. Set `sublabel` to draw a smaller labelled
    circle inside it (a cell's nucleus, an atom's core, an embryo).
  * "coil" - a wound coil, spring, or coiled tube (an induction coil, an
    intestine, a spring).
  * "gauge" - a dial/meter reading (`rotation` degrees = needle angle, 0 =
    resting/vertical; `sublabel` = its caption, e.g. "Ammeter", "pH meter").
  * "arrow" - a straight motion/direction/reaction-progress arrow
    (`rotation` degrees = the direction it points, 0 = east, 90 = south,
    180 = west, 270 = north, only used if `target_id` is unset).
  * "label" - a standalone text annotation for anything the other shapes
    don't cover.
  Pick whichever combination fits the actual subject - e.g. two "block"
  reactants connected by an "arrow" for a chemical reaction; a "circle" cell
  with a "circle" nucleus inside plus "label" shapes for other organelles for
  a biology diagram; "block"/"coil"/"gauge" for an electrical or mechanical
  apparatus. There is deliberately no shape for field lines, current flow,
  airflow or any other multi-line "fan" of movement radiating from a point -
  that geometry has no single correct position for its own label once
  anything else sits nearby, so it reads as accurate for exactly one layout
  and overlapping/garbled for every other. A field/flux/current-direction/
  wave visualization belongs to `image_kind: illustration` with
  `illustration_style: textbook` instead (an actual picture, not a labelled
  shape) - describe the exact field/flow pattern in `image_prompt`.
- Do NOT set `x`/`y`/`width`/`height` yourself - a layout engine positions
  every shape automatically from the semantic fields below, the same way you
  never compute pixel coordinates for any other diagram kind. Describe the
  diagram's *structure*, not its geometry:
  * `role`: exactly one shape per state is `"primary"` - the single focal
    object the whole diagram is about (the rotor, the cell, the main
    reactant). Every other shape is `"secondary"`.
  * `anchor`: where a shape sits *relative to another shape*, using its
    `id`. One of `"orbit:<id>"` (arranged around that shape - the default
    for anything without a stronger spatial reason), `"above:<id>"`,
    `"below:<id>"`, `"left_of:<id>"`, `"right_of:<id>"` (a clear directional
    relationship - current flowing left-to-right, a label sitting above what
    it names), or `"inside:<id>"` (nested content - a nucleus inside a cell,
    a core inside an atom; use `sublabel` on the parent shape instead
    whenever that's simpler). Leave `anchor` blank to mean "orbit the
    primary shape" - the default for most secondary objects.
  * `priority`: `"critical"` (must always be visible - the primary object
    always counts as critical), `"important"` (show if the diagram has
    room), or `"optional"` (the first thing to drop if there are too many
    annotations). Be honest about this - not every labelled detail is
    equally important to the learning objective.
  * `size`: `"small"`, `"medium"` (default), or `"large"` - relative to
    other shapes, not a measurement. The primary object is usually
    `"large"` or `"medium"`; a minor annotation is usually `"small"`.
  * `target_id`: for an "arrow" shape, the `id` of the shape it points
    at - this both draws the connection and tells the layout engine these
    two shapes are related, so keep using it exactly as before.
- One learning objective per diagram. Set `learning_objective` to the single
  sentence a student should take away. If the brief genuinely contains
  several independent things to learn with no shared primary object, cover
  only the most important one here well (the writer can request a second,
  separate diagram for the rest) rather than cramming every idea into one
  overloaded panel.
- Set `max_annotations` (default 5) to how many secondary shapes this
  diagram can clearly show at once - lower it (e.g. 3-4) for a simple
  concept, raise it only when the subject genuinely has that many
  co-equally important parts. Shapes beyond the budget are dropped by
  priority automatically, so mark the ones that must survive as `critical`.
- Produce 2 to 8 shapes per state (or in `shapes` for a single static
  illustration). Every shape needs a `label` naming exactly what it is - no
  unlabelled decoration.
- Use `states` (2 or more) only when the brief genuinely describes a
  before/after, at-rest/in-motion or cause/effect comparison - each state is
  one snapshot with its own `caption` (e.g. "No current" / "Current flows")
  and its own `shapes` (repeat the shapes that don't change, move/adjust the
  ones that do - keep the same `id`s and `role`/`anchor` across states so
  the layout stays consistent). Leave `states` empty and use the top-level
  `shapes` for a single static illustration.
- If the subject is a well-known textbook visual (an electric motor,
  electromagnetic induction, a fixed pulley, the human heart, an animal or
  plant cell, a nephron, a convex/concave lens ray diagram, a transformer, a
  simple electric circuit, or similar), set `visual_type` to its canonical
  snake_case name (e.g. "electric_motor", "animal_cell"). This lets a
  canonical blueprint fill in any component you omit and validates your
  structure against it - prefer matching the real textbook structure for
  that visual over inventing your own arrangement. Leave `visual_type` blank
  for anything else; the diagram still renders normally.
- Use the *minimum* set of components a textbook diagram of this subject
  actually needs to teach the learning objective - do not add extra shapes
  just to fill space or make the diagram look busier. Never invent a
  decorative component (a random gear, an unrelated label, a made-up part)
  that isn't a real part of the thing being depicted.
- Set `color_role` on a shape to one of the semantic color roles ("primary",
  "secondary", "accent", "structure", "current", "magnetic_field",
  "positive", "negative", "fluid", "highlight", "annotation", "neutral") to
  give it deliberate, consistent educational color instead of the plain
  default - e.g. current-carrying parts get "current", a magnet/field gets
  "magnetic_field", the single focal/primary object usually gets "primary"
  or "accent". Leave it blank when no particular color meaning applies. This
  is always a semantic role name, never a hex code. Reuse the same role for
  every shape that shares the same meaning so the color language stays
  consistent across the diagram - don't scatter many different roles just to
  look colorful.
- Optionally set `relationships` (each with `source`, `type`, `target` shape
  ids) to state real structural facts using exactly one of: inside,
  contains, connected_to, attached_to, above, below, left_of, right_of,
  passes_through, surrounds, contacts, points_to, flows_into, rotates_around,
  between. This documents *what is true* for validation - it does not affect
  layout (that's still `anchor`'s job), so a relationship and its matching
  anchor are independent and both may be set.
- Base every label, relationship and state only on the brief below. Do not
  invent facts, numbers or components that were not implied by it.
"""

_KIND_PIN_INSTRUCTION = """

The brief above already specifies the required `kind`: "{hint}". Use exactly
this kind. Do not substitute a sequential kind (flow_chart/process/cycle)
when a structural/relationship kind was requested, or vice versa - if the
content seems sequential but a relationship kind was requested, express the
sequence as an edge label (e.g. "then", "causes") between concept_map nodes
instead of switching kind.
"""


def _diagram_user_prompt(
    *,
    purpose: str,
    prompt: str,
    caption: str,
    course_title: str,
    memory: str = "",
    kind_hint: str = "",
    pin_kind: bool = False,
    qa_feedback: str = "",
) -> str:
    brief = "\n".join(part for part in (purpose, prompt, caption) if part)[:MAX_BRIEF_CHARS]
    section = prompts.memory_section(memory)
    memory_block = f"\n{section}\n" if section else ""
    requested = f"\nREQUESTED DIAGRAM TYPE: {kind_hint}\n" if kind_hint else ""
    pin = _KIND_PIN_INSTRUCTION.format(hint=kind_hint) if pin_kind and kind_hint else ""
    feedback_block = f"\n{qa_feedback}\n" if qa_feedback else ""
    return f"""\
COURSE: {course_title}

VISUAL BRIEF:
{brief or "(not specified)"}
{requested}{memory_block}{feedback_block}
Produce the diagram specification for this visual.{pin}
"""


def _kind_conflicts(hint: str, actual: str) -> bool:
    """True only for a conflict worth correcting - e.g. a relationship kind
    was requested but the model reached for a sequential one (the exact bug
    this hint mechanism exists to catch). Different-but-compatible choices
    within the same family (flow_chart vs process, concept_map vs hierarchy)
    are left alone - the model is allowed some judgement there."""
    hint = (hint or "").strip().lower().replace(" ", "_")
    if not hint or hint == actual:
        return False
    if hint in SEQUENTIAL_KINDS:
        return actual not in SEQUENTIAL_KINDS
    if hint in RELATIONSHIP_KINDS:
        return actual in SEQUENTIAL_KINDS
    if hint in ("comparison", "smart_art"):
        return actual in SEQUENTIAL_KINDS or (hint == "comparison" and actual != "comparison")
    return False


def _flow_chart_connectivity_issues(spec: DiagramSpec) -> list[str]:
    """A bounded procedure ("flow_chart" specifically - "process"/"cycle"
    legitimately have no bookends for an ongoing pipeline, so this doesn't
    apply to them) must have exactly one node with no incoming edge (the
    Start) and exactly one with no outgoing edge (the End) - every other
    node needs at least one real edge in and out. Checked on the SPEC
    itself, before rendering ever gets a chance to paper over it: the
    renderer's own connectivity fixes (see diagram_renderer.py's
    `connect_real`/final sweep) make sure no real edge is ever silently
    dropped, but they can't invent an edge the model never wrote, and they
    can't merge two genuinely separate terminal nodes into the single
    shared End a bounded procedure needs. Returns human-readable issues
    for the retry-feedback channel; empty means the spec is well-formed."""
    if spec.normalised_kind() != "flow_chart":
        return []
    node_ids = {n.id for n in spec.nodes if n.id}
    if len(node_ids) < 2:
        return []
    out_degree = {nid: 0 for nid in node_ids}
    in_degree = {nid: 0 for nid in node_ids}
    for edge in spec.edges:
        if edge.source in node_ids and edge.target in node_ids:
            out_degree[edge.source] += 1
            in_degree[edge.target] += 1
    by_id = {n.id: n for n in spec.nodes if n.id}
    sources = [nid for nid in node_ids if in_degree[nid] == 0]
    sinks = [nid for nid in node_ids if out_degree[nid] == 0]
    issues: list[str] = []
    if len(sources) > 1:
        labels = ", ".join(by_id[nid].label or nid for nid in sources)
        issues.append(f"{len(sources)} nodes have no incoming edge at all ({labels}) - there must be exactly one Start")
    elif len(sources) == 0:
        issues.append("every node has an incoming edge - there is no valid Start (a bounded procedure needs one)")
    if len(sinks) > 1:
        labels = ", ".join(by_id[nid].label or nid for nid in sinks)
        issues.append(
            f"{len(sinks)} different nodes have no outgoing edge ({labels}) - every terminal path (Close, "
            "Escalate, Keep, etc.) must lead to the SAME single End node, or loop back to an earlier step"
        )
    elif len(sinks) == 0:
        issues.append("every node has an outgoing edge - there is no valid End (a bounded procedure needs one)")
    return issues


class DiagramService:
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

    @staticmethod
    def _normalise(spec: DiagramSpec) -> DiagramSpec:
        """Guarantee unique node ids and resolve edges that reference a label
        instead of an id (models occasionally do this despite the schema)."""
        seen_ids: set[str] = set()
        by_label: dict[str, str] = {}
        for index, node in enumerate(spec.nodes):
            node_id = node.id.strip() or f"n{index}"
            if node_id in seen_ids:
                node_id = f"{node_id}_{index}"
            node.id = node_id
            seen_ids.add(node_id)
            if node.label.strip():
                by_label.setdefault(node.label.strip().lower(), node_id)

        def resolve(ref: str) -> str:
            ref = (ref or "").strip()
            if ref in seen_ids:
                return ref
            return by_label.get(ref.lower(), ref)

        for edge in spec.edges:
            edge.source = resolve(edge.source)
            edge.target = resolve(edge.target)
        spec.edges = [edge for edge in spec.edges if edge.source in seen_ids and edge.target in seen_ids]
        return spec

    async def _request_spec(
        self,
        *,
        purpose: str,
        prompt: str,
        caption: str,
        course_title: str,
        memory: str,
        kind_hint: str,
        pin_kind: bool,
        block_id: str,
        qa_feedback: str = "",
    ) -> DiagramSpec:
        return await self.ai.structured(
            schema=DiagramSpec,
            system=DIAGRAM_SYSTEM,
            user=_diagram_user_prompt(
                purpose=purpose,
                prompt=prompt,
                caption=caption,
                course_title=course_title,
                memory=memory,
                kind_hint=kind_hint,
                pin_kind=pin_kind,
                qa_feedback=qa_feedback,
            ),
            model=self.settings.diagram_model,
            purpose=f"diagram:{block_id}",
            phase="image",
        )

    @staticmethod
    def _check_schematic(spec: DiagramSpec) -> tuple[DiagramSpec, DiagramQAResult]:
        """Apply canonical blueprint defaults, validate the pre-layout
        semantic structure (missing/unknown components, invalid
        relationships, color roles), resolve layout, then run the geometry/
        composition checks on the result - one combined result so a single
        retry can address both a structural problem and a layout problem at
        once. `apply_blueprint_defaults` is a no-op for an unset/unrecognised
        `visual_type`, so this is exactly today's behaviour for every
        schematic that doesn't opt into the blueprint system."""
        spec = apply_blueprint_defaults(spec)
        blueprint_result = evaluate_blueprint(spec)
        resolved = resolve_schematic_layout(spec)
        geometry_result = evaluate_schematic(resolved)
        combined = DiagramQAResult(issues=[*blueprint_result.issues, *geometry_result.issues])
        return resolved, combined

    async def _resolve_and_check_schematic(
        self,
        spec: DiagramSpec,
        *,
        purpose: str,
        prompt: str,
        caption: str,
        course_title: str,
        memory_text: str,
        kind_hint: str,
        block_id: str,
    ) -> DiagramSpec:
        """Turn semantic shapes into real coordinates and run the combined
        blueprint/geometry/composition quality gate; on failure, ONE retry
        with the exact reasons fed back to the model (never a bare "try
        again"), keeping whichever attempt is actually better. A schematic
        that still has issues after the retry is still rendered - quality
        review sharpens the diagram, it isn't a second reason (beyond
        is_usable()) to fall back to a raster illustration."""
        resolved, result = self._check_schematic(spec)
        if result.passed:
            return resolved

        log.info(
            "Schematic QA failed for %s (%s) - retrying with targeted feedback",
            block_id, [issue.reason for issue in result.issues],
        )
        try:
            retried = await self._request_spec(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory=memory_text, kind_hint=kind_hint or "schematic", pin_kind=True,
                block_id=block_id, qa_feedback=result.feedback(),
            )
            retried = self._normalise(retried)
        except Exception as exc:  # noqa: BLE001 - keep the first attempt, don't fail the block
            log.warning("Schematic QA retry failed for %s: %s", block_id, exc)
            return resolved

        if retried.normalised_kind() != "schematic" or not retried.is_usable():
            return resolved

        retried_resolved, retried_result = self._check_schematic(retried)
        if len(retried_result.issues) < len(result.issues):
            return retried_resolved
        return resolved

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
        kind_hint = str(content.get("diagram_kind") or "").strip().lower().replace(" ", "_")
        if not (purpose.strip() or prompt.strip()):
            log.warning("Diagram block %s has no purpose/prompt - skipped", block.id)
            return False

        memory = await self.memory.build_context(
            stage="visual", course_title=course_title, chapter_title=purpose or prompt, template=template
        )
        memory_text = memory.render()

        try:
            spec = await self._request_spec(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory=memory_text, kind_hint=kind_hint, pin_kind=False, block_id=block.id,
            )
        except Exception as exc:  # noqa: BLE001 - caller falls back to illustration
            log.warning("Diagram spec generation failed for %s: %s", block.id, exc)
            return False

        spec = self._normalise(spec)

        # A genuinely complex technical topic can legitimately need more
        # nodes than this diagram kind's cap allows (see max_nodes_for) -
        # rather than immediately giving up on the structured path (which
        # falls back to an unreliable raster illustration - see
        # ImageService._generate_illustration), ask the model to consolidate
        # closely related steps into the budget it actually has, once.
        labelled_count = len([n for n in spec.nodes if n.label.strip()])
        cap = max_nodes_for(spec.normalised_kind())
        if labelled_count > cap:
            log.info(
                "Diagram for %s has %s nodes (max %s for %s) - retrying with a consolidation request",
                block.id, labelled_count, cap, spec.normalised_kind(),
            )
            try:
                consolidated = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint=spec.normalised_kind(), pin_kind=True, block_id=block.id,
                    qa_feedback=(
                        f"Your previous attempt had {labelled_count} nodes, more than a "
                        f"{spec.normalised_kind()} diagram can clearly show (maximum {cap}). "
                        "Consolidate closely related steps so the total is at most "
                        f"{cap} nodes, while keeping the sequence faithful to the brief - "
                        "merge an action with its immediate follow-up rather than dropping "
                        "any genuinely distinct stage."
                    ),
                )
                consolidated = self._normalise(consolidated)
                consolidated_count = len([n for n in consolidated.nodes if n.label.strip()])
                if consolidated.normalised_kind() == spec.normalised_kind() and consolidated_count <= cap:
                    spec = consolidated
                # Still over budget after asking nicely - is_usable() below
                # will reject it and the caller falls back, same as before.
            except Exception as exc:  # noqa: BLE001 - keep the first spec, don't fail the block
                log.warning("Diagram consolidation retry failed for %s: %s", block.id, exc)

        # The writer asked for a specific shape (e.g. a relationship diagram)
        # but the model reached for a sequential one anyway - one corrective
        # retry with the kind pinned explicitly, never a silent flowchart.
        if _kind_conflicts(kind_hint, spec.normalised_kind()):
            log.info(
                "Diagram for %s requested '%s' but got '%s' - retrying with the kind pinned",
                block.id, kind_hint, spec.normalised_kind(),
            )
            try:
                corrected = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint=kind_hint, pin_kind=True, block_id=block.id,
                )
                corrected = self._normalise(corrected)
                if not _kind_conflicts(kind_hint, corrected.normalised_kind()):
                    spec = corrected
                # A persistent mismatch after pinning means the brief itself
                # doesn't fit the requested kind - keep the original rather
                # than looping; is_usable() below still gets the final say.
            except Exception as exc:  # noqa: BLE001 - keep the first spec, don't fail the block
                log.warning("Diagram kind-correction retry failed for %s: %s", block.id, exc)

        # A bounded procedure needs exactly one Start and one shared End -
        # a real, confirmed case: a spec with three separate terminal nodes
        # (Close, Escalate, Keep Task) none of which pointed at a single
        # shared End left one path with nothing to connect to. One retry,
        # feeding back exactly which nodes are disconnected, before
        # rendering ever sees it - render_diagram_svg's own connectivity
        # repairs (connect_real / the final sweep) still guarantee no REAL
        # edge is ever silently dropped even if this retry doesn't fully
        # fix the spec, so a persistent issue is logged and rendered
        # anyway rather than discarded for the unreliable raster fallback.
        connectivity_issues = _flow_chart_connectivity_issues(spec)
        if connectivity_issues:
            log.info(
                "Diagram for %s has connectivity problems (%s) - retrying with targeted feedback",
                block.id, connectivity_issues,
            )
            try:
                reconnected = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint="flow_chart", pin_kind=True, block_id=block.id,
                    qa_feedback=(
                        "The rendered flowchart had structural problems: " + "; ".join(connectivity_issues) + ". "
                        "Every node except the single Start must have at least one real incoming edge; every "
                        "node except the single End must have at least one real outgoing edge (or loop back to "
                        "an earlier step). Route every terminal outcome (closing, escalating, keeping, etc.) "
                        "to the SAME single End node rather than leaving it as its own separate dead end."
                    ),
                )
                reconnected = self._normalise(reconnected)
                if reconnected.normalised_kind() == "flow_chart" and not _flow_chart_connectivity_issues(reconnected):
                    spec = reconnected
                else:
                    log.warning(
                        "Diagram for %s still has connectivity problems after retry (%s) - rendering anyway; "
                        "real edges are still guaranteed to reach the page, just not necessarily a single End",
                        block.id, connectivity_issues,
                    )
            except Exception as exc:  # noqa: BLE001 - keep the first spec, don't fail the block
                log.warning("Diagram connectivity retry failed for %s: %s", block.id, exc)

        if not spec.is_usable():
            log.info(
                "Diagram spec for %s (kind=%s) is not usable (%s nodes, %s edges) - falling back",
                block.id, spec.normalised_kind(), len(spec.nodes), len(spec.edges),
            )
            return False

        if spec.normalised_kind() == "schematic":
            spec = await self._resolve_and_check_schematic(
                spec, purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory_text=memory_text, kind_hint=kind_hint, block_id=block.id,
            )

        try:
            svg_bytes, width, height = render_diagram_svg(spec, template.theme)
        except Exception as exc:  # noqa: BLE001 - rendering must not fail the block
            log.warning("Diagram rendering failed for %s: %s", block.id, exc)
            return False

        # Validate the ACTUAL rendered geometry - node overlap, an edge
        # crossing through an unrelated node's text, text overflowing its
        # own node, anything outside the canvas. This renderer's placement
        # is sequential/systematic by construction, so a real collision is
        # rare, but "rare" isn't "never" (a pathological label length, an
        # edge case in a kind's own layout math) - never silently ship one
        # when caught. One retry, feeding back exactly what collided, same
        # shape as every other QA retry in this module; still rendered on a
        # persistent failure would ship a known-bad diagram, so this falls
        # back to illustration instead, same as an unusable spec does.
        #
        # Schematic is exempt: it already went through its own dedicated,
        # purpose-built geometry QA above (`_resolve_and_check_schematic` /
        # `evaluate_schematic`), and its shapes are text-fitted by
        # `_fit_boxed_text`/`_caption_text` - different padding/centering
        # conventions than `_node_block`'s, which this generic check
        # assumes. Running it on schematic produced real false positives
        # (confirmed: several passing electric-motor/pulley tests started
        # failing) rather than catching anything real.
        issues = [] if spec.normalised_kind() == "schematic" else validate_rendered_svg(svg_bytes, kind=spec.normalised_kind())
        if issues:
            log.info(
                "Rendered diagram QA failed for %s (%s) - retrying with targeted feedback",
                block.id, [issue.kind for issue in issues],
            )
            try:
                retried = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint=spec.normalised_kind(), pin_kind=True, block_id=block.id,
                    qa_feedback=(
                        "The rendered diagram had layout problems: "
                        + "; ".join(issue.detail for issue in issues[:5])
                        + ". Use noticeably shorter labels and fewer nodes so everything has room to lay "
                        "out cleanly."
                    ),
                )
                retried = self._normalise(retried)
                if retried.normalised_kind() == spec.normalised_kind() and retried.is_usable():
                    retried_svg, retried_w, retried_h = render_diagram_svg(retried, template.theme)
                    retried_issues = validate_rendered_svg(retried_svg, kind=retried.normalised_kind())
                    if len(retried_issues) < len(issues):
                        spec, svg_bytes, width, height, issues = retried, retried_svg, retried_w, retried_h, retried_issues
            except Exception as exc:  # noqa: BLE001 - keep the first render, don't fail the block
                log.warning("Diagram render-QA retry failed for %s: %s", block.id, exc)

        if issues:
            log.info(
                "Diagram for %s still has %s rendered layout issue(s) after retry - falling back",
                block.id, len(issues),
            )
            return False

        relative = self.storage.save_asset(course_id, svg_bytes, extension="svg")
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
            "Generated %s diagram %s (%s nodes) for block %s",
            spec.normalised_kind(),
            relative,
            len(spec.nodes),
            block.id,
        )
        return True
