"""Structured diagram specification.

A diagram is authored by the model as *structure* (nodes/edges), never as
pixels or markup - `app.render.diagram_renderer` turns it into SVG
deterministically. This is what makes flow charts, SmartArt-style lists,
hierarchies and conceptual diagrams reliable: labels can never be garbled the
way a diffusion image model garbles text.
"""

from __future__ import annotations

import ast

from pydantic import BaseModel, ConfigDict, Field

_AI = ConfigDict(extra="ignore")


def _parses(expression: str) -> bool:
    """Cheap, dependency-free syntax check (stdlib `ast` only - this module
    stays free of any app.render/app.services import, same as every other
    schema module) used only to reject an obviously-malformed function
    expression at is_usable() time, before the raster-illustration fallback
    would otherwise be reached. The actual safe, whitelisted evaluation at
    render time is app.render.graph_math.safe_eval - this check is a cheaper
    pre-filter, not a substitute for it."""
    try:
        ast.parse(expression, mode="eval")
        return True
    except (SyntaxError, ValueError):
        return False

DIAGRAM_KINDS = (
    "flow_chart",
    "process",
    "cycle",
    "hierarchy",
    "comparison",
    "smart_art",
    "conceptual",
    "concept_map",
    "schematic",
    "concept_experience",
    "data_flow_diagram",
    "er_diagram",
    "swimlane",
    "graph",
    "pyramid",
    "matrix",
    "radial_mindmap",
    "venn",
    "chevron_process",
    "timeline",
    "staircase_process",
    "balance_comparison",
    "nested_circles",
    "network_mesh",
    "decision_tree",
    "fishbone",
    "layered_diagram",
    "gantt_chart",
    "funnel",
    "honeycomb",
    "puzzle_pieces",
    "gears_process",
    "donut_segments",
    "pie_wedge",
    "wavy_journey_path",
    "progress_rings",
    "orbit_diagram",
    "bridge_process",
    "chain_links",
    "flower_petal",
    "hourglass_diagram",
    "org_chart",
    "spectrum",
    "iceberg",
    "feedback_loop_process",
    "causal_loop",
    "input_process_output",
    "ranked_bars",
    # circuit_schematic: real wire/node electrical topology - a STRUCTURE
    # (which terminals are actually tied to which node), not a sequence. See
    # its own comment below RELATIONSHIP_KINDS.
    "circuit_schematic",
    # periodic_table: the real, fixed 118-element grid (app.render.
    # periodic_table_data) - a REFERENCE, not a structure or a sequence the
    # model authors at all (same reasoning `toc` is never AI-authored -
    # app.course.document.builder's TOC page - applied here: a periodic
    # table has exactly one scientifically correct layout, so the model
    # only ever supplies WHICH cells to highlight/annotate via `nodes`,
    # never the grid itself). Root-cause fix (production course audit): two
    # real courses generated a periodic table as a raster `illustration`
    # instead, and the image model fabricated plausible-looking but
    # chemically WRONG pictures - duplicated element symbols, duplicated/
    # wrong group numbers - which no existing check caught (text-legibility
    # and topical-relevance QA, not scientific-correctness QA). Joins
    # neither SEQUENTIAL_KINDS nor RELATIONSHIP_KINDS below, same reasoning
    # "graph" already documents - it's a third family, not a sequence or an
    # arbitrary labelled-relationship network.
    "periodic_table",
)

# Root-cause fix (kind-selection drift, confirmed twice this session: a
# newly-added kind silently missing its own mention in one of the two
# model-facing prompts that need to know about it - `app.agents.prompts.
# WRITER_SYSTEM`, which decides WHICH kind to request, and
# `app.services.diagram_service.DIAGRAM_SYSTEM`, which tells the
# spec-authoring model HOW to actually build one). A real, confirmed case
# when this registry was introduced: 7 kinds (`conceptual`, `spectrum`,
# `iceberg`, `feedback_loop_process`, `causal_loop`, `input_process_output`,
# `ranked_bars`) had a full authoring bullet in `DIAGRAM_SYSTEM` but were
# NEVER mentioned in `WRITER_SYSTEM` at all - meaning the model that decides
# which kind to request could never have picked them, regardless of how
# well `DIAGRAM_SYSTEM` would have handled one. `test_diagram_service.py`'s
# drift-guard test asserts every kind in `DIAGRAM_KINDS` is mentioned by
# name in BOTH prompt strings, with exactly the two deliberate, documented
# exceptions below - not a loophole, a reviewed list, so a future exception
# has to be added here explicitly (and explain why) rather than the test
# just silently stop checking a kind.
DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS = {
    # A pre-existing alias for `concept_map` (identical renderer/dispatch -
    # see `_layout_concept_wheel`'s own `kind in ("concept_map",
    # "conceptual")` dispatch) kept only so a document saved under the old
    # name still renders - never something new content should be authored
    # as, so neither prompt ever needs to tell a model to pick it.
    "conceptual",
    # Not authored through this file's `diagram_kind` pipeline at all -
    # `image_kind: concept_experience` is a separate, purpose-built system
    # (`app.services.concept_visual_service`) for one nameable technical
    # concept (a class/object, a data structure, a protocol handshake);
    # `WRITER_SYSTEM` tells the model to pick it explicitly BEFORE it would
    # ever reach `diagram_kind`/`DIAGRAM_SYSTEM`, so `DIAGRAM_SYSTEM` itself
    # never needs its own mention of it.
    "concept_experience",
}

# "graph" is its own, third family - neither a sequence nor a labelled-box
# relationship, so it deliberately joins neither SEQUENTIAL_KINDS nor
# RELATIONSHIP_KINDS below. _kind_conflicts (app.services.diagram_service)
# only ever flags a conflict for a hint that IS in one of those two tuples,
# so leaving "graph" out of both is what keeps it safely inert there.
# periodic_table joins neither for the same reason - see its own comment in
# DIAGRAM_KINDS above.

# Kinds that show a SEQUENCE (steps happen in this order) - as opposed to
# concept_map, which shows a STRUCTURE (things and how they relate, with no
# implied order). Used to detect when the model picked a sequential kind for
# what was actually requested as a relationship diagram - see
# DiagramService._kind_family_matches. data_flow_diagram and swimlane are
# both sequential (data/work moves from node to node, across lanes for
# swimlane); er_diagram is a structure (entities and their relationships
# have no inherent order), so it joins RELATIONSHIP_KINDS instead.
SEQUENTIAL_KINDS = (
    "flow_chart", "process", "cycle", "data_flow_diagram", "swimlane", "chevron_process", "timeline",
    "staircase_process", "gantt_chart",
    # funnel: stages narrow top-to-bottom in a fixed order (the top of the
    # funnel happens before the bottom) - a real sequence, not just a
    # pyramid-style size ordering (see "pyramid"'s own comment below on
    # that distinction). gears_process: numbered, left-to-right steps of
    # one mechanism, same ordered-sequence reasoning as chevron_process.
    "funnel", "gears_process",
    # wavy_journey_path: numbered milestones along a roadmap, in order.
    # bridge_process: numbered steps crossing the bridge, in order.
    # chain_links: each link depends on/follows the one before it (a chain
    # of causation/dependency, not a static set) - same reasoning as
    # funnel above, not a size/importance ordering.
    "wavy_journey_path", "bridge_process", "chain_links",
    # feedback_loop_process: a linear sequence of steps, same ordered
    # reasoning as "process" - it just ALSO draws one explicit "revise and
    # go back" return edge partway through, unlike "cycle" (which loops
    # back to the very first step every time by definition).
    # input_process_output: input happens, then processing, then output -
    # a genuine three-stage time sequence, not a static structure.
    "feedback_loop_process", "input_process_output",
)
RELATIONSHIP_KINDS = (
    "concept_map", "conceptual", "hierarchy", "schematic", "er_diagram", "matrix", "radial_mindmap", "venn",
    "balance_comparison", "nested_circles", "network_mesh", "decision_tree", "fishbone",
    # honeycomb/puzzle_pieces: a set of related components with no inherent
    # before/after order - the same reasoning "concept_map" already
    # documents (a STRUCTURE, not a SEQUENCE).
    "honeycomb", "puzzle_pieces",
    # donut_segments/pie_wedge: categories that together make up one whole,
    # with no inherent order. progress_rings: a set of independent metrics,
    # not a sequence. orbit_diagram/flower_petal: a central theme and its
    # surrounding related sub-topics, same "structure, not sequence"
    # reasoning as concept_map/radial_mindmap. hourglass_diagram: two
    # opposing framings (cost/value, before/after as CATEGORIES, not a time
    # sequence between them) - same reasoning balance_comparison already
    # documents. org_chart: a role tree with depth, same reasoning
    # hierarchy already documents.
    "donut_segments", "pie_wedge", "progress_rings", "orbit_diagram", "flower_petal",
    "hourglass_diagram", "org_chart",
    # spectrum: positions along a continuum, with no inherent before/after
    # order between the labelled positions themselves (a political
    # spectrum, a pH scale) - a STRUCTURE, same reasoning as matrix.
    # iceberg: visible/hidden layers of ONE idea, not a time sequence -
    # same reasoning hourglass_diagram's "categories, not a sequence"
    # already documents. causal_loop: cause-and-effect relationships
    # between factors (which can themselves cycle back on each other) -
    # a STRUCTURE of causal links, same reasoning network_mesh already
    # documents, not an ordered procedure. ranked_bars: a set of factors
    # ordered by MAGNITUDE (importance/frequency/score), not by when they
    # happen - same "ordered but not a sequence of steps" reasoning
    # "pyramid" documents below, close enough to this family to join it
    # rather than stand alone a third time.
    "spectrum", "iceberg", "causal_loop", "ranked_bars",
    # circuit_schematic: which terminals are actually wired to which shared
    # node is a STRUCTURE (a parallel pair ties two components to the SAME
    # two nodes; a series pair does not) - there's no time order between
    # "component A" and "component B" sitting on the same circuit, the same
    # reasoning network_mesh/concept_map already document above.
    "circuit_schematic",
)
# "pyramid" deliberately joins neither tuple, the same reasoning "graph"
# already documents above: its layers have a top-to-bottom ORDER (narrowest
# to widest, or the reverse for a funnel) but aren't a sequence of STEPS that
# happen one after another in time - so a sequential-vs-relationship hint
# conflict check (_kind_conflicts, app.services.diagram_service) shouldn't
# flag it against either family.


def max_nodes_for(kind: str) -> int:
    """The node-count ceiling `is_usable()` enforces for a given
    `normalised_kind()` - shared with `DiagramService` (which needs the same
    number to tell "too few nodes to be a real diagram" apart from "too many
    nodes, worth a consolidation retry" - see
    DiagramService._exceeds_node_cap) so the two never drift apart."""
    if kind == "er_diagram":
        return 24
    if kind == "puzzle_pieces":
        # app.render.diagram_renderer._layout_puzzle_pieces draws a FIXED
        # ~130px-wide piece per node in a single growing-wider row (like
        # chevron_process) - unlike honeycomb (more nodes only add rows,
        # never width), more pieces here directly shrinks the effective
        # on-page font once the canvas is displayed at CONTENT_WIDTH. 6 is
        # the largest count that still clears MIN_READABLE_PX
        # (app.services.diagram_render_qa) at that kind's own label size -
        # confirmed by the same math RING_LABEL_SIZE's own comment uses.
        return 6
    if kind == "gears_process":
        # Same reasoning as puzzle_pieces just above - a single row that
        # grows wider per node (app.render.diagram_renderer._layout_gears_
        # process). 5 is the largest count that still clears
        # MIN_READABLE_PX at this kind's own label size.
        return 5
    if kind in ("progress_rings", "chain_links", "donut_segments"):
        # A single row (progress_rings/chain_links, same "grows wider per
        # node" reasoning as puzzle_pieces/gears_process above) or a ring
        # whose per-segment label space shrinks as segments are added
        # (donut_segments) - 6 is the largest count that still clears
        # MIN_READABLE_PX for each of these at their own label size.
        return 6
    if kind == "pie_wedge":
        return 7
    if kind == "orbit_diagram":
        # One node per concentric ring - more than a handful of rings
        # around one hub reads as clutter, not clarity.
        return 5
    if kind == "bridge_process":
        # A fixed-width arch (app.render.diagram_renderer._layout_bridge_
        # process) - more steps only cram the same arc tighter, never add
        # width the way a straight row would.
        return 5
    if kind == "wavy_journey_path":
        # A long wave with many milestones reads as clutter, not a clean
        # roadmap - 8 is the largest count this kind stays legible at.
        return 8
    if kind == "flower_petal":
        return 7
    if kind == "hourglass_diagram":
        # Split across two halves (see _layout_hourglass_diagram's own
        # docstring) - 6 total keeps each half at a readable 3-ish layers.
        return 6
    if kind == "spectrum":
        # A single horizontal line with labels alternating above/below it
        # (see _layout_spectrum) - more than a handful of positions
        # crowds the one line they all share.
        return 8
    if kind == "causal_loop":
        # A ring of factors with curved, polarity-labelled arrows between
        # them (see _layout_causal_loop) - the same crowding reasoning
        # network_mesh/orbit_diagram already apply to a circular layout,
        # tightened further since every edge here also carries a visible
        # +/- badge the ring must leave room for. 6, not the 7 tried
        # first - a real, confirmed case: many long-span edges (crossing
        # several other factors' own positions on the ring, not just
        # adjacent ones) on a dense 7-factor loop could still clip a node
        # the curve had to route near, however much the curve's own bow
        # grows with chord length. VisualPlanner's own guidance keeps the
        # EDGE COUNT sparse too (favour a single loop plus a small number
        # of real cross-links over a dense arbitrary graph).
        return 6
    if kind == "input_process_output":
        # Three zones (input/process/output, see _layout_ipo) sharing one
        # canvas width - the generic SEQUENTIAL_KINDS ceiling of 14 would
        # cram far more steps into the narrower "process" zone than it
        # can stay legible at.
        return 9
    if kind == "circuit_schematic":
        # Unlike every other RELATIONSHIP_KINDS member, this one routinely
        # draws SEVERAL independent circuit panels at once (e.g. "valid
        # series" / "valid parallel" / two invalid variants side by side -
        # see DiagramNode.lane's circuit_schematic meaning) - the generic 9-
        # node ceiling was sized for ONE structure, not a small multiple of
        # 3-5 junction nodes each. 30 comfortably covers a handful of small
        # (<=6-node) panels without opening the door to one sprawling,
        # illegible single circuit.
        return 30
    if kind in SEQUENTIAL_KINDS:
        return 14
    return 9

# The small, topic-agnostic vocabulary of drawable primitives a "schematic"
# diagram is built from - see SchematicShape. Generic on purpose, and NOT
# physics-specific: "block" is a magnet, a battery, a beaker, a cell wall or
# any two-part rectangular object; "circle" is a cell, an atom/nucleus, a
# planet, a seed or any round object (with an optional smaller labelled
# circle inside it - a nucleus, an embryo, a core); "coil" is a wound coil, a
# spring or a coiled tube/intestine; "gauge" is any dial/meter reading;
# "arrow" is a motion/direction/reaction-progress indicator; "label" is a
# standalone annotation.
#
# Deliberately no "flow" (field lines/current/airflow as a fan of curved
# lines) - retired after it kept producing overlapping/garbled diagrams in
# real courses: its label is positioned purely from its own rotation/length
# with zero awareness of any sibling shape (see the old _draw_flow), so two
# nearby flow shapes (or a flow shape near a gauge/label) reliably collide,
# and the QA geometry check (app.services.diagram_qa) only ever validated
# each shape's own small declared box, never that derived label position -
# so this failure mode shipped straight through the one bounded retry
# without ever being caught. A field/flux/current-direction/wave
# visualization is exactly what image_kind: illustration (illustration_style:
# "textbook") is for instead - a real picture, not a labelled shape - see
# app.services.diagram_service/app.agents.prompts. Any shape already
# persisted with type "flow" from before this change still renders safely:
# _draw_shape falls back to "block" for anything outside SHAPE_TYPES.
SHAPE_TYPES = ("block", "circle", "coil", "gauge", "arrow", "label")


class DiagramNode(BaseModel):
    model_config = _AI

    id: str = ""
    label: str = ""
    detail: str = ""
    # hierarchy: depth (0 = root). concept_map: 0 = the central/focal thing
    # being explained, >=1 = a labelled component/relationship around it.
    # radial_mindmap: same convention as concept_map - 0 = the one central
    # topic (the hub), >=1 = a branch at that depth from the centre (a
    # branch's own children, if any, connect to it via `edges`, same as
    # hierarchy). pyramid: this node's own layer order top-to-bottom as
    # given in `nodes` is what's actually drawn (`level` itself is ignored)
    # - listed here only so a model that sets it anyway does no harm.
    # nested_circles: same as pyramid - `nodes` order IS the nesting order
    # (nodes[0] = outermost/broadest ring, nodes[-1] = innermost/most
    # specific), `level` itself ignored.
    # iceberg: 0 = above the waterline (visible), >=1 = below it (hidden) -
    # a deeper level draws progressively WIDER, the classic "the visible
    # part is only the tip" shape. Nodes sharing a level stack within it in
    # `nodes` order.
    # Ignored by other kinds.
    level: int = 0
    # Which shape this node draws as - meaning is kind-specific, ignored by
    # every kind that doesn't mention it below:
    # data_flow_diagram: "" (default) = process (rounded box), "entity" =
    #   external entity (sharp-cornered box), "store" = data store (open-
    #   ended box, top/bottom border only - the standard DFD notation for
    #   each).
    # er_diagram: "" (default) = entity (sharp-cornered box), "relationship"
    #   = a relationship between two entities (diamond), "attribute" = a
    #   single field belonging to one entity or relationship (oval) - see
    #   `parent_id`.
    # balance_comparison: "left" or "right" - which column this row
    #   belongs to (e.g. "left" = cost/con, "right" = value/pro). ""
    #   (default) falls back to position: the first half of `nodes` is the
    #   left column, the rest is the right column.
    # input_process_output: "input"/"process"/"output" - which of the
    #   three zones this node belongs to; "" (default) falls back to
    #   "process", so a model that only labels the real input(s) and
    #   output(s) still places everything else sensibly in the middle.
    # circuit_schematic: "" (default) = a real electrical junction, drawn as
    #   a small labelled dot that wires/components (see DiagramEdge.label)
    #   actually connect to. "caption" = NOT an electrical node at all - a
    #   verdict/explanation node for its own panel (see `lane`), drawn as
    #   text under that panel instead of a dot; never touched by an edge.
    #   `label` holds its short verdict ("Valid", "Invalid - extra branch"),
    #   `detail` the one-sentence reason why. At most one caption node per
    #   lane is drawn; declare it anywhere in `nodes` - position doesn't
    #   matter, only `lane` grouping does.
    shape_role: str = ""
    # er_diagram only, and only meaningful when shape_role == "attribute":
    # the id of the entity/relationship node this attribute belongs to -
    # positions it orbiting that node instead of taking a place in the main
    # entity/relationship row. Ignored by every other kind and shape_role.
    parent_id: str = ""
    # swimlane only: which lane/column (a role, actor or system - e.g.
    # "User", "System", "Database") this node belongs to. Lanes are drawn
    # left to right in the order they're first seen across `nodes`. Every
    # node needs one for a sensible layout; a blank lane groups with any
    # other blank-lane nodes into its own column.
    # circuit_schematic: which independent circuit PANEL this junction (or
    # "caption", see `shape_role`) belongs to - e.g. "valid_series" vs
    # "invalid_parallel" for a multi-scenario comparison. Panels are drawn
    # left to right, wrapping into new rows, in the order first seen across
    # `nodes` - the SAME convention swimlane already uses for its own lanes,
    # just one circuit per "lane" instead of one actor's column. A blank
    # lane on every node (the common single-circuit case - nothing being
    # compared) draws one plain, unpanelled circuit with no header.
    lane: str = ""
    # Edit Visuals (flowchart editor) - flow_chart/process only. "" (default)
    # keeps diagram_renderer.role_of()'s existing in/out-degree + label-text
    # auto-detection exactly as it was before this field existed; one of
    # "start"/"process"/"decision"/"end" overrides it explicitly, letting a
    # person re-type a node's role from the editor without renaming it to a
    # magic word like "Start"/"End".
    role: str = ""
    # Optional "#rrggbb"/"#rgb" hex override of the role's default fill/
    # stroke pair - "" (default, or anything that doesn't parse as a real
    # hex colour) keeps the existing fixed _FLOW_ROLE_COLORS palette exactly
    # as it was before this field existed.
    # periodic_table: an optional "#rrggbb"/"#rgb" highlight fill for ONE
    # real element's cell, identified by `id` (its symbol, e.g. "Na" -
    # case-insensitive, matched against app.render.periodic_table_data's
    # real dataset; an id that isn't a real element symbol is simply
    # ignored, never an error). `label` is unused (the cell already shows
    # the real symbol); `detail`, if set, is this highlight's one-line
    # reason, shown in a small legend below the grid. A periodic_table spec
    # with NO nodes at all still draws the complete, correct, unhighlighted
    # grid - highlighting is purely optional annotation, never what makes
    # the table usable (see DiagramSpec.is_usable's own periodic_table
    # case).
    color: str = ""
    # gantt_chart only: this task's own bar, in whatever time unit the
    # brief uses (day/week/sprint number - never a literal calendar date,
    # this is a relative schedule chart, not a date-aware one).
    # gantt_start: how many units after the project's own start this task
    # begins. gantt_duration: how many units it spans. Both ignored by
    # every other kind.
    gantt_start: float = 0.0
    gantt_duration: float = 1.0
    # smart_art/chevron_process/funnel/honeycomb/puzzle_pieces only: an
    # optional small pictogram drawn inside this node's own shape, the same
    # "icon badge" look real presentation-software SmartArt uses (see
    # app.render.diagram_renderer._ICON_DRAWERS for the exact supported
    # keywords - any other value, or blank, simply draws no icon; never an
    # error). Ignored by every other kind.
    icon: str = ""
    # progress_rings: this node's own stat, 0-100. A percentage has no
    # honest place in `label` (which stays the short metric NAME) or
    # `detail` (free text, not a number this renderer can trust to parse) -
    # a dedicated, explicitly-numeric field, same reasoning `gantt_start`/
    # `gantt_duration` already use for their own kind-specific numbers.
    # spectrum: this node's own POSITION along the continuum, 0 (the low
    # end) to 100 (the high end) - same "a number has no honest place in
    # `label`" reasoning. ranked_bars: this bar's own magnitude, 0-100 (a
    # score/frequency/importance - whatever the brief's ranking is actually
    # by), drawn as the bar's length; bars sort automatically by this value,
    # highest first, so the model never has to pre-sort `nodes` itself.
    # None (not 0.0) when the model didn't set one, so the renderer can
    # tell "genuinely 0%"/0-valued from "nothing given" and fall back
    # sensibly (an empty progress ring; an even spread along the spectrum;
    # a ranked_bars node sorted last) rather than silently drawing a
    # confident-looking 0. Ignored by
    # every other kind.
    value_pct: float | None = None


class DiagramEdge(BaseModel):
    model_config = _AI

    source: str = ""
    target: str = ""
    # causal_loop only: starts with "+" for a reinforcing link (more of the
    # source causes more of the target, e.g. "+ drives up") or "-" for a
    # balancing one (more of the source causes LESS of the target, e.g.
    # "- reduces"); the leading sign is pulled out and drawn as its own
    # bold +/- badge on the arrow, the rest of the string (if any) still
    # renders as the edge's own explanatory text. A label with neither
    # leading sign draws no polarity badge, never a crash - the same
    # "missing content degrades gracefully" contract every other optional
    # field in this module already keeps.
    # circuit_schematic only: a leading component tag, same "parse a prefix
    # off the front of a generic field" convention causal_loop's +/- just
    # above already uses, so this wire/component distinction needed no new
    # schema field at all. "R:<value>" draws a resistor zigzag ("R:220Ω"),
    # "C:<value>" a capacitor ("C:100nF"), "L:<value>" an inductor coil,
    # "V:<value>" or "B:<value>" a battery/voltage source ("V:9V"),
    # "SW"/"SW:<value>" a switch (open gap). The text after the colon (if
    # any) is drawn as the component's own value label. Anything else -
    # blank, or no recognised tag - draws a plain wire with that full string
    # (if non-blank) as a small label beside it; never a crash on an
    # unrecognised tag. See app.render.diagram_renderer._parse_circuit_
    # component for the exact parsing rule.
    label: str = ""


# --- graph/chart (kind == "graph") -------------------------------------------


class GraphFunction(BaseModel):
    """One plotted curve, only meaningful when DiagramSpec.graph_type ==
    "function". `expression` is a Python-style expression of `x` (e.g.
    "x**2", "sin(x)") evaluated safely by app.render.graph_math.safe_eval -
    never passed to eval()/exec()."""

    model_config = _AI

    expression: str = ""
    label: str = ""


class GraphSeries(BaseModel):
    """One bar/line series, only meaningful when DiagramSpec.graph_type is
    "bar" or "line". `values` must have exactly one entry per
    DiagramSpec.categories, in the same order."""

    model_config = _AI

    label: str = ""
    values: list[float] = Field(default_factory=list)


# The semantic-relationship vocabulary a schematic's `DiagramSpec.relationships`
# entries use - purely for validation (see app.services.diagram_qa) and for
# matching a canonical blueprint's expected structure (see
# app.render.visual_blueprints). Layout is unaffected by these: a shape's
# actual position still comes only from its own `anchor` field. Several of
# these read like anchor keywords (inside/above/below/left_of/right_of)
# because the same spatial relationship is often worth stating twice - once
# as *where* a shape sits (anchor, used by the layout engine) and once as
# *what it means* (relationships, used for validation) - but a relationship
# never has to have a matching anchor, and vice versa.
RELATIONSHIP_TYPES = (
    "inside",
    "contains",
    "connected_to",
    "attached_to",
    "above",
    "below",
    "left_of",
    "right_of",
    "passes_through",
    "surrounds",
    "contacts",
    "points_to",
    "flows_into",
    "rotates_around",
    "between",
)


class SchematicRelationship(BaseModel):
    """One semantic relationship between two schematic components, e.g.
    `brush CONTACTS commutator`. Declarative only - it documents *what is
    true*, not where anything is drawn; see RELATIONSHIP_TYPES."""

    model_config = _AI

    source: str = ""
    type: str = ""
    target: str = ""


ANNOTATION_PRIORITIES = ("critical", "important", "optional")
# Ordering used to decide what survives DiagramSpec.max_annotations when a
# semantically-authored schematic proposes more secondary objects than fit -
# see app.render.schematic_layout. Lower index = kept first.
_PRIORITY_RANK = {name: rank for rank, name in enumerate(ANNOTATION_PRIORITIES)}

SIZE_CLASSES = ("small", "medium", "large")

# The anchor grammar a semantically-authored shape's `anchor` field uses -
# see app.render.schematic_layout.resolve_schematic_layout for how each is
# turned into an actual x/y. "<id>" refers to another shape's `id` in the
# same state.
ANCHOR_KEYWORDS = ("center", "orbit", "left_of", "right_of", "above", "below", "inside")


class SchematicShape(BaseModel):
    """One drawable primitive in a `schematic` diagram - a real illustrated
    component (a coil, a magnet, a meter, field lines) rather than a labelled
    box.

    Two ways to position a shape, kept simultaneously for backward
    compatibility (see app.render.schematic_layout):

    1. Legacy / coordinate-authored: set `x`/`y`/`width`/`height` directly
       (0..1 fraction of the panel). This is what hand-built specs (tests,
       the pre-blueprint mock) already do, and continues to render exactly
       as before - nothing here changes that path.
    2. Semantic / blueprint-authored: leave `x`/`y` at their defaults and set
       `role`/`anchor`/`priority`/`size` instead. A shape is treated as
       semantically-authored the moment ANY shape in the same state sets
       `anchor` or `role` - `resolve_schematic_layout` then computes real
       x/y/width/height for the whole state deterministically, the same way
       the model authors nodes/edges (not pixels) for every other diagram
       kind. This is the path new LLM output uses; see DIAGRAM_SYSTEM.
    """

    model_config = _AI

    type: str = "block"  # one of SHAPE_TYPES
    id: str = ""
    x: float = 0.5  # 0..1, centre position within the panel (legacy path)
    y: float = 0.5
    width: float = 0.22  # 0..1, meaning depends on type (legacy path)
    height: float = 0.22
    label: str = ""
    # block: the second half's label (e.g. a magnet's other pole). circle: a
    # smaller labelled circle drawn inside it (e.g. a cell's nucleus, an
    # atom's core). gauge: the reading shown under the dial. Unused by other
    # types.
    sublabel: str = ""
    rotation: float = 0.0  # degrees: arrow direction / gauge needle angle
    intensity: float = 0.5  # 0..1: gauge deflection magnitude. Unused by other types.
    # arrow: id of another shape in the same state to point toward - takes
    # priority over `rotation` when it resolves.
    target_id: str = ""

    # --- semantic / blueprint-authored fields (all optional; see docstring) ---
    # "primary" = the one focal subject of the diagram (there should be
    # exactly one per state); "secondary" = everything else. Leave "" on a
    # legacy/coordinate-authored shape.
    role: str = ""
    # One of ANCHOR_KEYWORDS, optionally with ":<other shape's id>" - e.g.
    # "center", "orbit:magnet", "left_of:coil", "above:ammeter". Empty means
    # "not semantically placed" (legacy path) unless another shape in the
    # same state has already opted the state into the semantic path, in
    # which case empty defaults to orbiting the primary shape.
    anchor: str = ""
    # Annotation budget priority - see ANNOTATION_PRIORITIES and
    # DiagramSpec.max_annotations. Only consulted on the semantic path.
    priority: str = "important"
    # Relative size class mapped to a concrete width/height by
    # schematic_layout - see SIZE_CLASSES. Empty means "use width/height as
    # given" (legacy path).
    size: str = ""
    # One of app.render.textbook_palette.COLOR_ROLES (e.g. "current",
    # "magnetic_field", "positive") - a semantic role, never a raw hex value;
    # the renderer resolves it against the centralized palette. Empty means
    # "use the theme's default schematic colors", which is exactly today's
    # rendering - this is what keeps every legacy/coordinate-authored shape
    # visually unchanged.
    color_role: str = ""
    # The color_role a nested "inside:<this shape's id>" child shape carried
    # before app.render.schematic_layout._merge_inside_anchors folded it into
    # this shape's own `sublabel` (see that field above) - lets the inner
    # nested circle (a nucleus inside a cell, a core inside an atom) keep its
    # own distinct semantic color instead of inheriting the parent's. Set
    # automatically by the merge step; a model should never set this
    # directly - a directly-authored `sublabel` (e.g. a gauge's caption or a
    # block's second half) has no separate nested shape to carry a color
    # from, so this stays blank and the parent's own `color_role` is used
    # for both, exactly as before this field existed.
    sublabel_color_role: str = ""


class SchematicState(BaseModel):
    """One snapshot of a schematic - e.g. "before" / "after", "at rest" /
    "in motion". A single-state schematic just uses DiagramSpec.shapes and
    never needs this; use `states` (2 or more) for a before/after comparison,
    rendered as stacked, captioned panels."""

    model_config = _AI

    caption: str = ""
    shapes: list[SchematicShape] = Field(default_factory=list)


# --- concept_experience: a beginner-oriented, static visual for a single
# named concept (e.g. "java_class_and_object"), distinct from schematic's
# illustrated-apparatus vocabulary above. See
# app.render.concept_visual_blueprints for the canonical registry and
# app.render.concept_experience_renderer for how a spec becomes markup. Only
# meaningful for kind == "concept_experience"; every field here defaults to
# empty/None, so nothing above this line is affected by its existence.

# How a concept_experience spec gets turned into markup - proposed by the
# planner, but overridden deterministically by a known concept_visual_blueprints
# entry when one exists (same "model proposes, blueprint constrains" pattern
# visual_type/BLUEPRINT_REGISTRY already uses for schematic). Named for what
# each strategy actually produces, not for a specific graphics technology
# assumption: "deterministic_interactive_html" is self-contained HTML+CSS
# (entity cards, connectors) - not SVG shapes; despite the name it renders a
# static picture, no script and no buttons (see
# app.render.concept_experience_renderer's module docstring).
GENERATION_STRATEGIES = (
    "deterministic_svg",
    "deterministic_interactive_html",
    "gpt_image",
    "hybrid",
)

# Which *shape* of visual best teaches this concept - a domain-agnostic
# dispatch key `app.render.concept_experience_renderer` uses to pick a
# renderer function, chosen from what the concept itself needs (an object
# with instances, an ordered structure, a step sequence, a relationship
# graph, ...), never from which subject it belongs to. Distinct from
# `GENERATION_STRATEGIES` (a *technology* choice - svg/html/image/hybrid)
# and from `concept` (an *identity* key - which registered blueprint, if
# any). See app.render.concept_visual_blueprints and the approved plan for
# the full rationale. Three families have a real, dedicated renderer
# ("object", "data_structure", "process") - every other value renders
# through a documented compatibility fallback onto one of those three (see
# `_REPRESENTATION_RENDERERS` in app.render.concept_experience_renderer),
# never a failure. Four more have their own dedicated renderer too -
# "comparison" (a real table, not a fallback), "timeline", "before_after"
# and "cycle" (a repeating "process" that visibly loops back to its first
# step) - listed separately below for that reason, though they follow the
# exact same "never a failure" contract. "decision_tree" is a documented
# alias onto "hierarchy"/"object", not a new renderer - a branching
# decision is exactly that shape (entities + labelled relationship edges).
REPRESENTATION_TYPES = (
    "object",              # a template/blueprint entity + real instances (a class and its objects,
                            # a microservice) - or, for relationship-style content, a template-less
                            # row of entities (optionally connected by `relationships`)
    "data_structure",      # ordered/structured entities + operations (a stack, a queue, a linked list)
    "process",             # ordered steps, optionally played through (a CI/CD pipeline, an auth flow)
    "sequence",             # a strict linear walkthrough (an algorithm's execution order, a protocol
                            # handshake) - renders the same way as "process"
    "state_machine",       # named states + transitions (TCP states, an order's lifecycle)
    "relationship",        # entities + relationships, no strong sequence (a SQL join, a microservice
                            # topology, an ER diagram) - renders via "object" + relationship connector lines
    "hierarchy",           # parent/child structure (OSI layers, class inheritance, an org chart) -
                            # renders via "object" + relationship connector lines
    "comparison",          # 2+ entities compared side by side as a real table (stack vs queue, SQL vs
                            # NoSQL) - rows are the union of every entity's `properties` keys, so give
                            # every compared entity the SAME property keys (the criteria)
    "timeline",            # a chronological sequence of milestones/eras (a technology's history, a
                            # project's phases) - uses `steps`, same grammar as "process"/"sequence":
                            # put the date/era in `label` (e.g. "2015 - Docker 1.0"), the detail in
                            # `description`. Only for content that is genuinely dated/chronological -
                            # a plain ordered procedure with no time dimension is still "process"
    "before_after",        # a state transformation, not a sequence or a many-way comparison (a
                            # variable's value, a system's architecture, a metric) shown as exactly
                            # two entities: the FIRST entity in `entities` is "before", the SECOND is
                            # "after" - only set when there is a genuine, single before/after contrast
    "pipeline",            # staged data transformation (RAG, ETL, a compiler pipeline) - renders like a process
    # "spatial" is RETIRED (see VisualPlanner's CONCEPT_SYSTEM prompt for the
    # full reasoning) - it never had a real renderer, only a generic
    # "object" fallback that dumps every entity's `properties` as literal
    # "key: value" text instead of an actual structural layout. The writer
    # is steered away from ever choosing it; the renderer dispatch table
    # still maps the string to that same fallback purely so a stray/older
    # spec with this value already on it still renders instead of crashing.
    "code_visualization",  # a short technical_signature tied directly to the entity it describes
    "cycle",               # a REPEATING sequence (an ML training loop, a release cycle, a review
                            # cadence) - uses `steps`, same grammar as "process", but the last step
                            # visibly loops back to the first instead of just ending. Only for content
                            # that genuinely repeats; a one-shot procedure is still "process"
    "decision_tree",       # a branching decision path (if/else logic, a diagnostic flow) - entities
                            # are the decision points/outcomes, `relationships` are the branches, each
                            # one's `type` naming the condition (e.g. "yes"/"no", "if cache hit") -
                            # same grammar as "hierarchy", just branch-labelled
)

# A VisualEntity's role - "template" is the one class/blueprint object,
# "instance" is a real object created from it. Kept as a free string (like
# SchematicShape.role) rather than a strict enum so a future concept (e.g. an
# interface, an abstract class) can introduce its own role without a schema
# change.
ENTITY_ROLES = ("template", "instance")


class VisualEntity(BaseModel):
    """One thing shown in a concept_experience visual - generic enough to
    cover a Java class/object, a stack's frames, two things being compared,
    or a labelled part of an annotated diagram; never subject-specific.
    `properties` holds the actual instance values a learner can inspect
    (e.g. {"color": "Red"}); `actions` is a short, display-only list of
    capabilities/behaviours (e.g. "start()" for a Java method, "Divides" for
    a cell, "Repairs DNA" for an enzyme) - neither is executed, this is a
    visual spec, not code, and `actions` is deliberately not named "methods"
    so it reads naturally for any domain, not just OOP."""

    model_config = _AI

    id: str = ""
    label: str = ""
    role: str = ""  # one of ENTITY_ROLES, informally - see role docstring above
    properties: dict[str, str] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)
    icon: str = ""
    color_role: str = ""


class VisualStep(BaseModel):
    """One step in a `representation="process"/"sequence"/"pipeline"`
    concept_experience visual (e.g. one stage of a CI/CD pipeline, one
    message in a TCP handshake) - see DiagramSpec.steps. `order` is the
    step's position (0-based); ties are broken by list order, so it's only
    needed when steps might be reordered independently of the list
    itself."""

    model_config = _AI

    id: str = ""
    label: str = ""
    description: str = ""
    order: int = 0
    icon: str = ""
    color_role: str = ""


class VisualState(BaseModel):
    """One named state in a `representation="state_machine"`
    concept_experience visual (e.g. "No current" / "Current flows", a TCP
    connection state) - see DiagramSpec.concept_states. `entity_ids` names
    which entities are involved in/visible during this state; empty means
    "not entity-specific"."""

    model_config = _AI

    id: str = ""
    label: str = ""
    description: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    icon: str = ""
    color_role: str = ""


class VisualTransition(BaseModel):
    """One edge between two `VisualState`s - see DiagramSpec.transitions."""

    model_config = _AI

    from_state: str = ""
    to_state: str = ""
    label: str = ""


class InteractionSpec(BaseModel):
    """One interaction a concept_experience visual's rendered markup offers -
    e.g. clicking the class entity, or a "Create Object"/"Reset" control.
    Declarative only: this describes *what interaction exists and on what*,
    never how it's implemented - app.render.concept_experience_renderer owns
    the actual click/hover/reset JS. `target_entity_ids` empty means "applies
    broadly" (e.g. every instance for a click-to-inspect interaction, or the
    whole visual for reset) rather than one specific entity."""

    model_config = _AI

    type: str = ""
    target_entity_ids: list[str] = Field(default_factory=list)
    trigger_label: str = ""


class DiagramSpec(BaseModel):
    model_config = _AI

    kind: str = "flow_chart"
    title: str = ""
    # Editor-only "increase text size" control (Edit Diagram panel) - never
    # set by the AI writer/planner, always 1.0 for a freshly generated
    # diagram. A real, confirmed complaint: even a diagram that clears the
    # renderer's own readability floor can still read as cramped/small to
    # an actual reader, and the only way to get bigger text before this was
    # to edit every node label by hand. 1.0 is today's existing, unchanged
    # size; clamped to [1.0, 1.5] by the renderer - this control only ever
    # makes text BIGGER, never smaller (which could push a diagram back
    # under the readability floor). Applied uniformly by every kind that
    # shares this module's common label-drawing/box-sizing primitives
    # (`_node_block`, `_circle_node`/`_circle_node_size`, `_draw_diamond`,
    # `_draw_process_box`, `_fit_boxed_text`, `_key_table_rows`,
    # `_edge_label`, `_anchored_label`) - see render_diagram_svg's own
    # comment for exactly which kinds that covers today.
    label_scale: float = 1.0
    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)
    # Only used when kind == "schematic": either a flat `shapes` list (one
    # static illustration) or 2+ `states` (a before/after comparison) - never
    # both populated at once.
    shapes: list[SchematicShape] = Field(default_factory=list)
    states: list[SchematicState] = Field(default_factory=list)
    # Only meaningful for a semantically-authored schematic (see
    # SchematicShape) - what a student should take away from this specific
    # visual, and how many secondary shapes it may show before annotation
    # priority starts trimming (see app.render.schematic_layout). Ignored by
    # every other kind and by a legacy/coordinate-authored schematic.
    learning_objective: str = ""
    max_annotations: int = 5
    # Names a canonical textbook visual (e.g. "electric_motor", "animal_cell")
    # - see app.render.visual_blueprints.BLUEPRINT_REGISTRY. Purely additive:
    # an unset or unrecognised visual_type is always valid and simply skips
    # blueprint defaults/validation, falling back to the generic semantic
    # schematic system exactly as before this field existed. Only meaningful
    # for kind == "schematic".
    visual_type: str = ""
    # Declarative semantic relationships between shape ids - see
    # SchematicRelationship. Only used for validation/blueprint-matching,
    # never for layout (that's `SchematicShape.anchor`'s job). Only
    # meaningful for kind == "schematic".
    relationships: list[SchematicRelationship] = Field(default_factory=list)

    # --- concept_experience fields (all optional; see VisualEntity/
    # InteractionSpec docstrings above) - only meaningful for
    # kind == "concept_experience"; every existing kind is unaffected by
    # their presence. ---
    # Names a canonical concept (e.g. "java_class_and_object") - see
    # app.render.concept_visual_blueprints.CONCEPT_BLUEPRINT_REGISTRY.
    # Purely additive, same fallback contract as `visual_type` above: an
    # unset or unrecognised concept is always valid and just skips blueprint
    # defaults, never a hard failure.
    concept: str = ""
    # Free-text/snake_case subject label (e.g. "java", "physics", "sql",
    # "biology"). METADATA ONLY - used for storage/reuse-grouping and human
    # browsing of the blueprint registry; deliberately never read by any
    # branching logic (VisualPlanner's prompt, apply_concept_blueprint_defaults,
    # concept_qa's checks) - representation/entities/steps decide behaviour,
    # not the subject name. See the approved plan's "universal architecture"
    # rationale.
    domain: str = ""
    # One of REPRESENTATION_TYPES - which shape of visual best teaches this
    # concept. See REPRESENTATION_TYPES' docstring above.
    representation: str = ""
    learner_level: str = ""
    # Planner-generated, specific objectives derived from the source content
    # - deliberately a *separate*, plural field from `learning_objective`
    # above (which stays exactly what it was: a schematic's single takeaway
    # sentence). Concept-level QA (app.services.concept_qa) grades the spec
    # against each entry here.
    learning_objectives: list[str] = Field(default_factory=list)
    # The one-sentence, punchy takeaway (e.g. "A class is a blueprint; an
    # object is a real thing built from it.") - shorter and more quotable
    # than any single `learning_objectives` entry; rendered as an on-visual
    # caption/banner.
    core_message: str = ""
    visual_metaphor: str = ""
    # A short, single-line code-like mapping (e.g. "class Car { color;
    # model; speed; start(); stop(); drive(); }") shown beside the
    # template/blueprint entity - never a multi-line code block or
    # screenshot. concept_qa's structural layer rejects anything containing
    # a newline here.
    technical_signature: str = ""
    # One of GENERATION_STRATEGIES. Empty means "not yet decided" - the
    # planner proposes a value, a known concept_visual_blueprints entry may
    # override it deterministically.
    generation_strategy: str = ""
    entities: list[VisualEntity] = Field(default_factory=list)
    # Only meaningful for representation == "process"/"sequence"/"pipeline".
    steps: list[VisualStep] = Field(default_factory=list)
    # Only meaningful for representation == "state_machine". Named
    # `concept_states` (not `states`) to avoid colliding with the existing
    # `states` field above, which is schematic's own before/after panel list
    # - the two are unrelated and must stay independently addressable.
    concept_states: list[VisualState] = Field(default_factory=list)
    transitions: list[VisualTransition] = Field(default_factory=list)
    interactions: list[InteractionSpec] = Field(default_factory=list)
    # Free-text, planner-written assertions concept-level QA's semantic layer
    # grades the spec against - generated from the concept itself, not a
    # hardcoded per-concept ruleset (mirrors how `relationships` above is
    # declarative-only content, never layout).
    validation_criteria: list[str] = Field(default_factory=list)

    # --- graph/chart fields (only meaningful for kind == "graph") -----------
    graph_type: str = ""  # "function" | "bar" | "line"
    x_label: str = ""
    y_label: str = ""
    # Domain for graph_type == "function". A rendering-range default
    # (-10..10) applies when unset - see is_usable() - never an invented
    # data value, the same category as DEFAULT_IMAGE_ASPECT elsewhere in
    # this codebase.
    x_min: float | None = None
    x_max: float | None = None
    functions: list[GraphFunction] = Field(default_factory=list)
    # Shared by graph_type "bar"/"line": the x-axis groups, and one or more
    # named series with exactly one value per category.
    categories: list[str] = Field(default_factory=list)
    series: list[GraphSeries] = Field(default_factory=list)

    # --- pyramid (only meaningful for kind == "pyramid") --------------------
    # False (default): a classic pyramid - nodes[0] is the narrow APEX (top
    # layer), nodes[-1] is the widest BASE (bottom layer) - "levels of
    # importance/foundation" content (e.g. Maslow's hierarchy). True: a
    # funnel - nodes[0] is the widest TOP, nodes[-1] the narrow bottom -
    # "narrowing stages" content (e.g. a sales/conversion funnel). Same
    # renderer, same node list, just which end tapers.
    pyramid_inverted: bool = False

    # --- matrix (only meaningful for kind == "matrix") -----------------------
    # Axis titles for a 2-axis quadrant matrix (e.g. "Effort" / "Impact") -
    # only drawn when exactly 4 labelled nodes are given (one per quadrant,
    # in reading order: top-left, top-right, bottom-left, bottom-right); a
    # different node count renders as a plain labelled grid instead, with no
    # axis cross. All four blank is a plain grid even at 4 nodes - the axis
    # cross is opt-in, not inferred.
    matrix_x_label: str = ""
    matrix_y_label: str = ""
    # Endpoint labels for each axis (e.g. "Low" / "High") - purely cosmetic,
    # blank draws no endpoint tick label.
    # Also reused by "spectrum" (only meaningful for kind == "spectrum"):
    # `matrix_x_low`/`matrix_x_high` are the continuum's own two end labels
    # (e.g. "Introvert" / "Extrovert", "Acidic" / "Basic") - `matrix_x_label`
    # doubles as the spectrum's own short title drawn above the bar when
    # set. `matrix_y_*` fields are unused by spectrum.
    matrix_x_low: str = ""
    matrix_x_high: str = ""
    matrix_y_low: str = ""
    matrix_y_high: str = ""

    # --- balance_comparison (only meaningful for kind == "balance_comparison") --
    # Headers for the two columns (e.g. "Cost" / "Value", "Cons" / "Pros") -
    # drawn above each column; blank draws no header for that side.
    balance_left_label: str = ""
    balance_right_label: str = ""

    def normalised_kind(self) -> str:
        value = (self.kind or "").strip().lower().replace(" ", "_")
        return value if value in DIAGRAM_KINDS else "smart_art"

    def is_usable(self) -> bool:
        """A diagram needs at least two labelled things to relate to each
        other, and a relationship-shaped diagram needs at least one labelled
        relationship - a bare list of boxes isn't a concept map.

        `schematic` is permanently retired - never usable, regardless of
        content. Real, confirmed failure modes: the anchor-based shape
        layout produced genuinely overlapping, unreadable results (a label
        shape landing directly on top of its own parent box's text), and a
        multi-state "before/after" comparison doubled that risk across two
        stacked panels. Rather than trust prompt guidance alone to stop the
        model from ever picking this kind, `is_usable() == False` here makes
        the EXISTING "unusable spec -> fall back to a raster illustration"
        path (see DiagramService.generate_for_block) catch it unconditionally
        - every apparatus/structure topic that used to go through schematic
        (physics, chemistry, mechanical AND biology/life-science alike) now
        reliably lands on `kind: "illustration"` with `illustration_style:
        "textbook"` instead, a real photographic-style depiction rather than
        a labelled-box schematic, regardless of what any future prompt or
        model happens to request."""
        kind = self.normalised_kind()
        if kind == "schematic":
            return False

        if kind == "concept_experience":
            # Not every representation needs entities (a pure step sequence
            # or state machine may have none) - usable as long as SOME
            # content structure is present, whichever the representation
            # actually needs.
            return bool(self.concept.strip()) and (
                len(self.entities) + len(self.steps) + len(self.concept_states) >= 1
            )

        if kind == "graph":
            if self.graph_type == "function":
                valid_functions = [f for f in self.functions if f.expression.strip() and _parses(f.expression)]
                if not valid_functions:
                    return False
                x_min = self.x_min if self.x_min is not None else -10.0
                x_max = self.x_max if self.x_max is not None else 10.0
                return x_min < x_max
            if self.graph_type in ("bar", "line"):
                if not self.categories or not self.series:
                    return False
                return all(len(s.values) == len(self.categories) for s in self.series)
            return False

        if kind == "periodic_table":
            # The grid itself is the real, fixed 118-element dataset (see
            # DIAGRAM_KINDS' own comment above) - never authored by the
            # model - so unlike every other kind checked below, an empty
            # `nodes` list (no highlights/annotations requested) is still a
            # perfectly usable, complete request, not a degenerate one.
            return True

        labelled = [n for n in self.nodes if n.label.strip()]
        # See max_nodes_for: er_diagram's node count includes attributes,
        # which multiply fast; a SEQUENTIAL kind earns a higher ceiling too
        # (a real technical pipeline can legitimately need more than 9
        # distinct steps to stay faithful to the brief - rejecting it here
        # was pushing genuinely complex-but-valid technical content onto the
        # raster illustration fallback, which has no reliable way to render
        # that many text labels); a relationship/structural kind keeps the
        # original, tighter cap.
        max_nodes = max_nodes_for(kind)
        if not (2 <= len(labelled) <= max_nodes):
            return False
        if kind == "concept_map":
            has_labelled_edge = any(e.label.strip() for e in self.edges)
            return bool(self.edges) and (has_labelled_edge or len(labelled) >= 3)
        if kind in ("er_diagram", "data_flow_diagram", "circuit_schematic"):
            # A relationship/data-flow/circuit diagram is exactly its
            # connections - a bare list of entities, processes or junctions
            # with nothing wiring them together isn't one, the same
            # reasoning concept_map already uses above.
            return bool(self.edges)
        if kind == "swimlane":
            # The whole point is showing work crossing roles/actors - at
            # least 2 distinct lanes, connected by something.
            lanes = {n.lane.strip() for n in self.nodes if n.lane.strip()}
            return bool(self.edges) and len(lanes) >= 2
        if kind == "venn":
            # Venn geometry only reads cleanly at 2 or 3 overlapping circles -
            # the generic 2..9 node-count check above is too permissive here.
            return 2 <= len(labelled) <= 3
        if kind == "iceberg":
            # The whole point is a visible/hidden split - at least one node
            # above the waterline (level 0) and at least one below it
            # (level >= 1), the same "don't fake a shape the data doesn't
            # have" reasoning concept_map/swimlane already apply above.
            return any(n.level == 0 for n in self.nodes) and any(n.level >= 1 for n in self.nodes)
        if kind in ("feedback_loop_process", "causal_loop"):
            # Both kinds are defined by their edges (the loop-back step for
            # feedback_loop_process, the causal links for causal_loop) -
            # the same "a relationship IS its connections" reasoning
            # concept_map/er_diagram/data_flow_diagram already use above.
            return bool(self.edges)
        return True
