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

Visual-generation policy fix: a block authored with a structured diagram
kind (`diagram_kind` set to flow_chart/decision_tree/process/cycle/
hierarchy/tree/concept_map/etc.) NEVER falls back to the raster AI
illustration path, regardless of why the structured attempt had trouble -
confirmed, real production cases showed that fallback routinely producing
a vague, textless, unrelated picture standing in for what should have been
a labelled diagram (e.g. a flowchart's own content-safety-filtered
fallback image sharing one generic, chapter-wide prompt across several
structurally-unrelated blocks). Two outcomes only:

1. The spec is genuinely unusable (no usable structure, a generation/
   rendering exception, confirmed garbled/misspelled text) OR the rendered
   geometry has a CRITICAL defect that could mislead about what connects
   to what, or renders part of the diagram invisible/unverifiable (see
   `_CRITICAL_RENDER_ISSUE_KINDS`) -> the image is WITHHELD entirely (see
   `_withhold_structured_diagram`), the same
   `omitted_after_qa_failure`/blocker-severity state `ImageService`'s own
   illustration path already uses for its equivalent case. Never a
   fabricated replacement picture just to avoid shipping nothing.
2. The spec is structurally sound and legible, but a QA check (rendered
   geometry, content-relevance, a confirmed source-text contradiction)
   still has an unresolved issue after its bounded retry -> it is rendered
   and shipped anyway, flagged `major`/`review_findings` for a human, per
   `generate_for_block`'s own comments on each check. Discarding a real,
   topic-grounded, legible diagram would be a worse outcome than shipping
   it flagged: an AI illustration has no structured content of its own to
   fall back on, and is exactly the "generic decorative placeholder" this
   pipeline must never use as a substitute for real educational content.

`schematic` is the one exception: it is a permanently-retired kind that is
rerouted to the illustration path BEFORE any structured attempt is ever
made (see `generate_for_block`'s own "Phase 2 card-grid audit" comment) -
that is a deliberate classification decision, not a QA-failure fallback,
so it is unaffected by the policy above. Mirrors
`concept_visual_service.py`'s own already-established "no fallback on
content-QA failure" contract, extended here to cover every failure mode
this module can hit, not just content-QA.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.course.blocks.normalizer import _reads_as_young_learner_audience
from app.core.errors import AIServiceError
from app.core.logging import get_logger
from app.render.diagram_renderer import render_diagram_svg
from app.render.schematic_layout import resolve_schematic_layout
from app.render.visual_blueprints import apply_blueprint_defaults
from app.schemas.blocks import merge_content
from app.schemas.diagram import (
    DIAGRAM_KINDS,
    RELATIONSHIP_KINDS,
    SEQUENTIAL_KINDS,
    DiagramSpec,
    max_edges_for,
    max_nodes_for,
)
from app.schemas.document import Block
from app.schemas.template import CourseTemplate
from app.services.correction_intent import CORRECTION_INTENT_POLICY, classify_correction_intent
from app.services.diagram_content_qa import (
    CITATION_LEAKED_INTO_LABEL,
    MISSPELLED_OR_GARBLED_TEXT,
    SOURCE_GROUNDING_UNSUPPORTED,
    STRUCTURALLY_GARBLED_TEXT,
    evaluate_content,
)
from app.services.diagram_qa import DiagramQAResult, evaluate_blueprint, evaluate_schematic
from app.services.diagram_render_qa import validate_rendered_svg
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client
from app.services.quality_framework import visual_qa_finding_entries
from app.services.storage_service import StorageService, get_storage
from app.services.visual_consistency import apply_diagram_consistency

log = get_logger(__name__)

MAX_BRIEF_CHARS = 2000

# How many corrective attempts `evaluate_content`'s own content-relevance
# check gets before `generate_for_block` falls back to the raster
# illustration path - see that call site's own comment on why one retry
# wasn't always enough. Bounded, not unlimited: a persistently-failing spec
# still falls back after this many tries, same as before.
_MAX_CONTENT_QA_RETRIES = 2

# Conditional-shipping policy fix (structured-diagram parity with the
# illustration path's same-day fix): most of `validate_rendered_svg`'s
# issue kinds are genuine but COSMETIC - a reader can still correctly tell
# which label belongs to which node/edge, just with imperfect visual
# polish (`shared_edge_segment`, `label_overlaps_node`/`label_overlaps_
# label`, `duplicate_arrowhead_side`, `text_overflows_node`, `font_below_
# minimum`, `text_too_small_after_scaling`, `excessive_whitespace`,
# `edge_collinear_with_node_border`, `node_overlap`) - these keep the
# existing, unchanged "ship anyway, flagged `rendered_with_unresolved_
# issues`" treatment. `edge_crosses_node` is different in kind, not just
# degree: an edge visually passing THROUGH an unrelated node's box is
# exactly the "edges pointing to the wrong things" case that can make a
# reader genuinely misread which nodes a connection is actually between -
# a correctness problem, not a layout imperfection. `out_of_canvas` means
# part of the diagram is literally invisible (missing information, not
# merely untidy). `unparseable_svg` means this check could not even
# verify the rendered output at all - never silently trusted either way,
# same "never ship something unverified" principle `ImageService.
# _scientific_check_failed`'s own `errored` case already applies. These
# three force withholding after the existing corrective retry, the same
# way `is_usable()`/confirmed garbled text already do - never downgraded
# to a quality flag.
_CRITICAL_RENDER_ISSUE_KINDS = frozenset({"edge_crosses_node", "out_of_canvas", "unparseable_svg"})

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

PLAN BEFORE YOU FILL IN ANY NODE OR EDGE: set `complexity_estimate` first,
honestly, to one of "simple" (a handful of nodes, one clear idea - most
briefs are this), "moderate", or "complex" (genuinely many interconnected
parts the brief itself describes) - then let that honest estimate actually
constrain what you produce. If you find yourself about to write a "simple"
diagram with a dozen nodes, that is a sign to consolidate or re-plan, not a
sign to relabel the estimate as "complex" after the fact to match what you
already wrote.

When the brief includes a "CURRENT DIAGRAM" section, this is a CORRECTION of
an existing diagram, not a fresh one - the reader already approved its
overall subject and structure and is asking you to fix specific, named
problems with it (garbled symbols, overlapping/cut-off text, a wrong
connection, cramped spacing, missing content), not to redesign it. The text
under "YOUR CORRECTION REQUEST" (when present) is the user's own current,
explicit instruction for THIS regeneration - treat it as the authoritative
statement of what to change, and apply it precisely and visibly; a
"CORRECTION REQUEST" that clearly asks for something specific is never
something to water down or only partially apply just because the default
below is to preserve everything else. Reuse
the exact same `kind`, the exact same set of nodes/edges with the exact
same ids/labels/detail, and the exact same structure, UNLESS the brief
explicitly asks to add, remove, rename or reconnect something - copy every
node and edge you are not asked to change verbatim rather than
paraphrasing or reinventing it, even if your own phrasing would read
slightly better. A brief that only describes a rendering problem (spacing,
overlap, a cramped label, a wrong symbol) needs ZERO structural change at
all - correct only the specific label/symbol/detail text named, nothing
else; this specification is rendered by the SAME layout engine either way,
so a spacing/overlap complaint is about what the text needs to fit well
(a label genuinely too long to lay out cleanly may need shortening), never
a reason to drop or rewrite content the brief didn't flag. Only when the
brief clearly asks for a broader redesign (a different kind, a
reorganised structure, substantially different content) should you depart
from the current diagram more freely - and even then, preserve every fact
it got right.

First decide what the brief actually needs, then pick exactly one `kind`:

- The brief describes STEPS that happen in order (a procedure, a sequence of
  stages, a decision path) -> flow_chart or process. Order `nodes` the way
  the steps happen and add one `edge` per consecutive pair. Add an edge
  `label` only when the transition needs one (e.g. "if valid").
- The steps repeat with no fixed end -> cycle. Order `nodes` around the loop;
  the renderer closes it back to the first node automatically.
- The brief describes a procedure with one explicit "revise and go back"
  step partway through (the scientific method, an Agile sprint, an
  editorial review cycle) rather than a clean full loop back to the very
  first step -> feedback_loop_process. Order `nodes` the way the steps
  happen (same as flow_chart/process) and add one edge per consecutive
  forward pair PLUS one extra edge for the return step itself, from the
  later node back to the earlier one it revises, with a short `label`
  naming the condition (e.g. "if it fails", "if rejected").
- The brief is about factors that drive each other in a feedback system -
  more of one thing causing more (or less) of another, which can itself
  loop back - a population/resource dynamic, a market feedback effect, an
  ecosystem interaction -> causal_loop. Each node is one factor; each edge
  is a causal link, its `label` starting with "+" (more of the source
  causes MORE of the target - a reinforcing link) or "-" (more of the
  source causes LESS of it - a balancing link), optionally followed by a
  short phrase (e.g. "+ drives up", "- reduces"). Keep this one SPARSE - a
  single loop around the ring plus at most one or two real cross-links,
  never a dense web connecting most factors to most others (the ring
  layout only has room to route a handful of curved edges cleanly).
- The brief describes positions along a single continuum or scale with no
  real structure beyond where each one sits (a political spectrum, a pH
  scale, an introvert-extrovert scale) -> spectrum. Leave `edges` empty;
  set `matrix_x_low`/`matrix_x_high` to the two end labels (e.g. "Acidic"/
  "Basic") and optionally `matrix_x_label` for a short title above the
  line. Give every node a `value_pct` (0-100) for its own position along
  the line - never leave it unset when the brief states or implies a real
  position.
- The brief is specifically a "visible vs. hidden" structure - the small
  part everyone notices vs. the much larger set of root causes/drivers
  underneath it (an iceberg of conflict, a competency iceberg, an
  incident's visible symptom vs. its underlying causes) -> iceberg. The
  visible item(s) get `level: 0`; each hidden layer gets `level: 1, 2, 3...`
  from least to most fundamental - leave `edges` empty.
- The brief is specifically the classic "black box" systems view - what
  goes IN, what processing happens, what comes OUT (an algorithm's
  input/processing/output, a function's signature, a system boundary) ->
  input_process_output. Give every node a `shape_role` of "input",
  "process" or "output" (processing steps default to "process" when left
  blank); leave `edges` empty, the renderer connects each zone
  automatically.
- The brief ranks or sizes a set of factors by magnitude - importance,
  frequency, a score, a survey result (top causes of X, most common
  reasons for Y) - rather than describing a sequence or structure between
  them -> ranked_bars. Leave `edges` empty; give every node a `value_pct`
  (0-100, whatever scale the brief's own ranking uses) - the renderer
  sorts and sizes the bars automatically, never pre-sort `nodes` yourself.
- The brief describes a THING made of parts, or a phenomenon/mechanism whose
  components relate to or act on each other, and a labelled-boxes-and-arrows
  diagram communicates it well enough (an organisation, a reaction pathway,
  an abstract system of parts) -> concept_map. Put the central subject as the
  node with `level: 0`; every other node is a component or factor around it,
  `level: 1`. Every edge must carry a short, precise relationship `label`
  naming what happens between the two nodes - a direction, a force, a cause,
  a flow (e.g. "moving toward", "induces", "opposes", "flows into").
- The brief is specifically about a REAL PERIODIC TABLE OF THE ELEMENTS
  (groups/periods/blocks, trends across the table, which elements belong to
  a family) -> periodic_table. The grid itself is NEVER authored here - it
  is a fixed, always scientifically correct 118-element dataset the
  renderer draws directly every time, regardless of what `nodes` contains.
  `nodes` is ONLY an optional, short list of cells to highlight/annotate:
  one `DiagramNode` per highlighted element, `id` set to that element's
  real symbol (e.g. "Na", "Cl" - an id that isn't a real element symbol is
  simply ignored), an optional `color` ("#rrggbb") as that cell's highlight
  fill, and `detail` as the one-line reason it's highlighted (shown in a
  small legend below the grid). Leave `nodes` completely empty for a plain,
  complete, unannotated table - never try to construct the grid's own 118
  cells as nodes, and leave `edges` empty always (nothing is ever wired
  between elements here).
- The brief is specifically about REAL ELECTRICAL/WIRING TOPOLOGY - which
  components connect to which shared nodes, a circuit's actual wiring (a
  series/parallel reduction, a bench measurement setup, a sensor's wiring) ->
  circuit_schematic. Each node is a real electrical JUNCTION, drawn as a
  small labelled dot (e.g. "A", "B") that components wire to - leave `label`
  short (a node letter/name), never a sentence. An edge's `label` names the
  component ON that wire via a leading tag: "R:<value>" draws a resistor
  ("R:220Ω"), "C:<value>" a capacitor, "L:<value>" an inductor, "V:<value>"
  or "B:<value>" a battery/voltage source, "SW" or "SW:<value>" a switch -
  anything else (blank, or no recognised tag) draws a plain wire. Comparing
  several scenarios (valid vs. invalid wiring) in one figure: give every
  node a `lane` naming which panel/scenario it belongs to (panels draw left
  to right, wrapping into rows, in the order first seen) - node `id`s must
  stay globally unique ACROSS panels, never reused between two different
  scenarios' own "A"/"B" even if they represent analogous points, or wires
  will cross between panels. Add one `shape_role: "caption"` node per panel
  (not a real junction, never touched by an edge) with `label` its short
  verdict ("Valid", "Invalid - extra branch") and `detail` the one-sentence
  reason why.
- `schematic` is PERMANENTLY RETIRED - never produce it, regardless of what
  the brief describes. A real, recognisable physical shape/arrangement (a
  chemistry reaction, a biology structure, an astronomy scene, a mechanical
  device or an electrical apparatus) is never diagrammed here at all - that
  whole category belongs to `image_kind: illustration` with
  `illustration_style: "textbook"` instead (a different pipeline, decided by
  the writer before this call), a real depiction rather than a labelled-box
  drawing. If a brief like this still reaches you, the caller already
  rejects a `schematic` answer unconditionally and falls back to that
  illustration path anyway - so treat this prompt's remaining kinds
  (__DIAGRAM_KIND_ENUM__) as the complete
  list; none of them is
  "physical shape and arrangement," and that's fine - this call was
  reached in error for that brief.
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
  `parent_id`. Keep attribute labels to just the field name. The
  relationship node's own `label` stays a 1-4 word relationship name ONLY
  ("enrolled in", "placed by", "assigned to") - never append provenance,
  confidence or other metadata to it (see the relationship-label rule
  above); if that metadata matters for this entity pair, give it its own
  `shape_role: "attribute"` child node instead, the same way any other
  column would be modelled.
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
- The brief describes LEVELS of importance, priority or foundation (a
  base/foundation that supports what's above it, a hierarchy of needs, a
  "most fundamental at the bottom" structure) -> pyramid. Order `nodes`
  narrowest-first (the apex/top layer) to widest-last (the base/bottom
  layer); leave `pyramid_inverted` false. Leave `edges` empty; add a short
  `detail` per layer only when it adds real information.
- The brief instead describes a NARROWING STAGE process (a sales/
  conversion funnel, a filtering/elimination process where each stage has
  fewer items than the last, a "top of funnel to bottom of funnel"
  pipeline) -> funnel, its own dedicated kind (the SAME wide-top/narrow-
  bottom shape `pyramid`+`pyramid_inverted` used to require - prefer this
  simpler, more discoverable name). Order `nodes` widest-first (the top/
  entry stage) to narrowest-last (the final stage). Leave `edges` empty;
  add a short `detail` per stage only when it adds real information.
- The brief is a SET of related categories, pillars or components with no
  inherent order and no deeper relationship worth naming as an edge (the
  brief would otherwise be smart_art, but reads well as a connected
  cluster rather than a plain list - "the 5 pillars of X", "core
  components of Y") -> honeycomb. Each node is one category; leave
  `edges` empty. Produce 3-9 nodes - fewer reads as a sparse single row,
  which is fine; more starts to feel cramped.
- The brief describes components that only make sense TOGETHER, as
  interlocking parts of one whole strategy/solution/framework (content
  that would naturally be illustrated as literal jigsaw-puzzle pieces
  fitting together) -> puzzle_pieces. Each node is one component; leave
  `edges` empty. Produce at most 6 nodes - this kind draws as a single
  growing-wider row (like chevron_process), so it must stay short to stay
  legible.
- The brief describes a short MECHANISM whose steps genuinely work
  together as one system, each step driving the next the way gears
  literally mesh (not just an ordinary sequence - the brief itself should
  frame it as interdependent/interlocking parts of one machine, e.g. "the
  moving parts of X", a short automated pipeline where each stage directly
  drives the next) -> gears_process. Order `nodes` the way the steps
  drive each other; leave `edges` empty. Produce at most 5 nodes - like
  puzzle_pieces, this draws as a single growing-wider row.
- The brief classifies or compares things across exactly two independent
  axes/dimensions (impact vs. effort, urgency vs. importance, a SWOT-style
  strengths/weaknesses/opportunities/threats grid, a priority matrix)
  -> matrix. Produce exactly 4 nodes, one per quadrant, in reading order
  (top-left, top-right, bottom-left, bottom-right). Set `matrix_x_label`/
  `matrix_y_label` to what each axis measures (e.g. "Effort" / "Impact")
  and `matrix_x_low`/`matrix_x_high`/`matrix_y_low`/`matrix_y_high` to each
  axis's own endpoint words (e.g. "Low" / "High") - leave all six blank
  only for a SWOT-style grid with no real continuous axes, which still
  renders as a clean 2x2 grid. A classification into more than 4 groups
  with no real 2-axis structure is still `matrix` - just more nodes (up to
  9), which renders as a plain grid instead of an axis cross. Leave `edges`
  empty.
- The brief has ONE clear central topic with several distinct branching
  sub-ideas (a brainstorm, a topic broken into its main themes, each
  theme standing on its own rather than being parts/properties of a single
  whole) -> radial_mindmap. Put the central topic as the node with
  `level: 0`; every branch is another node, `level: 1`. This differs from
  concept_map: concept_map's edges each carry a precise relationship label
  (a force, a cause, a flow) between the centre and ITS OWN surrounding
  parts; radial_mindmap's branches are independent sub-topics in their own
  right, connected to the centre with no relationship label needed - leave
  `edges` empty unless a branch genuinely has its own child idea (then add
  one edge, branch -> child, no label).
- The brief is specifically about what two or three things SHARE versus
  what's unique to each of them (overlapping categories, a Venn-style
  "things common to both X and Y" comparison) -> venn. Produce exactly 2 or
  3 nodes, one per set, each a short name; `detail` is what's UNIQUE to
  that set. For what two sets share, add an edge between them with `label`
  set to the shared idea (e.g. source="Mammals", target="Egg-laying
  animals", label="Warm-blooded"); only add an edge between two sets that
  truly do share something named in the brief - omit it otherwise.
- The brief describes a SHORT linear sequence (3-6 steps, no branches or
  decisions) that reads well as a horizontal strip rather than a tall
  column (a short onboarding flow, a product-launch phase strip, "the 4
  stages of X") -> chevron_process. Order `nodes` left to right the way
  the steps happen; leave `edges` empty (the chevron shape itself already
  shows direction). For a LONGER sequence, or one with any decision/
  branch, still use flow_chart/process instead - chevron_process has no
  decision shape.
- The brief is a chronological/historical sequence of DATED events, eras
  or milestones (a country's history, a technology's evolution, a
  project's roadmap) -> timeline, not flow_chart. Each node's `label`
  should carry the date/era (e.g. "1947 - Independence"), `detail` the
  event itself; order `nodes` chronologically. Leave `edges` empty (the
  timeline's own axis already shows order). Only for genuinely dated
  content - an undated step-by-step procedure is still flow_chart/process/
  chevron_process, not timeline.
- The brief describes something that genuinely GROWS or BUILDS in scope,
  scale or difficulty at each step (a skill-building progression, a
  maturity model, a staged rollout where each stage is bigger than the
  last) -> staircase_process. Order `nodes` smallest/earliest first - the
  renderer draws each step's own bar visibly longer than the one before
  it, so the ordering itself IS the growth being shown. Leave `edges`
  empty. Do not use this just because a sequence has several steps - only
  when the brief itself frames it as growing/building, not merely
  happening in order (an ordinary multi-step procedure with no growth
  framing is still flow_chart/process/chevron_process).
- The brief weighs TWO SIDES of one decision or tradeoff against each
  other (cost vs. value, pros vs. cons, risks vs. benefits) ->
  balance_comparison, not comparison (that's for N things compared flatly
  side by side, not a two-sided judgement). List every LEFT-side (cost/
  con/risk) node first, then every RIGHT-side (value/pro/benefit) node -
  the renderer splits `nodes` by position into the two columns, unless you
  set `shape_role` to exactly "left" or "right" on each node for an
  uneven split. Set `balance_left_label`/`balance_right_label` to what
  each column actually is (e.g. "Cost" / "Value", "Risks" / "Benefits").
  Each node's `label` is the short point itself; `detail`, when it adds
  real information, is a brief elaboration - both are shown directly on
  the row (this is the one kind in this list where `detail` is NOT
  limited to a short qualifying phrase the way the general node rule
  below says - write a real sentence or two here, since this kind's rows
  ARE the main content, not a label needing a tooltip).
- The brief describes a SCOPE or CONTAINMENT hierarchy - each thing fully
  CONTAINS the next, narrower one (Vision > Mission > Strategy > Tactics;
  Global > Regional > Local; an atom's nucleus inside the atom inside the
  element) -> nested_circles. Order `nodes` broadest/outermost first,
  narrowest/innermost last. Leave `edges` empty. Different from hierarchy
  (a branching parent/child TREE, not a single nested sequence) and from
  pyramid (levels of importance stacked, not literal containment).
- The brief is genuinely about MANY things connecting to MANY others, not
  one central hub or a strict hierarchy (a microservice topology, an
  organisational network, a social/relationship network) -> network_mesh.
  Add one edge per real connection - unlike every other node/edge kind,
  network_mesh edges carry no label (leave `edge.label` blank; the
  connection existing IS the information). Do not use this for a simple
  hub-and-spoke (one central thing, others only connect to IT, never to
  each other) - that's radial_mindmap instead.
- The brief describes a CASCADE of decisions/questions, each branch
  leading to a further decision or a final outcome (a troubleshooting
  guide, "which plan is right for you", eligibility logic) ->
  decision_tree. Not the same as a single flow_chart decision diamond (one
  branch point inside an otherwise linear procedure) - this is a WHOLE
  TREE of them. Give every decision node `level` = its depth from the
  root (0 = root) and one edge per branch, `edge.label` the condition
  ("Yes"/"No", a threshold) - a node with outgoing edges is automatically
  drawn as a decision diamond, a node with none as a rounded outcome box;
  only set `role` to "decision"/"outcome" explicitly if that auto-
  detection would ever be wrong for a leaf that still happens to need an
  edge for some other reason.
- The brief is a root-cause analysis - what are the different CATEGORIES
  of causes behind one effect/problem (a fishbone/Ishikawa diagram - "why
  did X happen", a postmortem, a quality/defect investigation) ->
  fishbone. `nodes[0]` is the effect/problem itself; every other node is
  one CAUSE CATEGORY (e.g. "People", "Process", "Technology",
  "Environment" - the classic "4M/6M" categories are a good default when
  the brief doesn't name its own). Leave `edges` empty. A category's own
  `detail` lists its real sub-causes from the brief (a short comma- or
  semicolon-separated list reads well) - never invent causes the brief
  didn't give you. Keep this to 3-5 categories - a real fishbone stays
  scannable; split a genuinely bigger investigation into more than one
  diagram instead of cramming extra categories into one.
- The brief describes a STACK of distinct LAYERS/TIERS that sit on top of
  each other with no containment and no importance ranking - each layer is
  simply a separate level in a technical stack (a network protocol stack,
  an architecture's tiers, rock strata, atmosphere layers) -> layered_diagram.
  Order `nodes` top-to-bottom exactly as the brief describes the stack
  (e.g. application layer first, physical layer last, for a networking
  stack). Leave `edges` empty. Different from pyramid (importance/priority
  narrowing to a point) and nested_circles (literal containment, one fully
  inside the next) - a layered stack is neither; it's simply distinct,
  equal-width bands stacked in a fixed technical order.
- The brief is specifically about a SCHEDULE over time - which tasks run
  when, how long each takes, and which overlap (a project plan, a sprint/
  launch schedule, a Gantt chart) -> gantt_chart. Each node is one task:
  `label` the task name, `gantt_start` the task's start offset (a plain
  number - days/weeks/whatever unit the brief uses, 0 = the schedule's own
  start) and `gantt_duration` how long it runs in that same unit. Order
  `nodes` the way they first start. Leave `edges` empty (overlap/sequencing
  is shown by each bar's own position, not connectors). Only use this when
  the brief gives real relative timing for each task - when it only lists
  steps with no timing at all, that's flow_chart/process/chevron_process
  instead.
- The brief is specifically about a mathematical function, a quantitative
  trend, or a comparison of real numeric values across categories -> graph.
  This kind does NOT use `nodes`/`edges` at all - leave both empty. Set
  `graph_type` to exactly one of:
  * `"function"` - plot one or more curves. Each entry in `functions` is a
    Python-style expression of `x` (e.g. `"x**2"`, `"sin(x)"`, `"2*x + 1"`)
    with a short `label` (e.g. "y = x^2"); set `x_min`/`x_max` to a sensible
    domain for the concept.
  * `"bar"` or `"line"` - set `categories` (the x-axis groups, e.g. a list
    of approaches, time periods or options) and one or more `series`, each
    a `label` and exactly one numeric value per category, in the same order
    as `categories`. Use `"bar"` to compare discrete categories, `"line"`
    for a trend over an ordered sequence (time, steps).
  Always set `x_label`/`y_label` to what each axis actually represents.
  Base every function/value only on the brief - a genuine illustrative or
  pedagogical example (e.g. "plot y = x^2 to show a parabola", "three
  example speed/risk scores to illustrate a tradeoff") is fine; a fabricated
  real-world statistic, measurement or dataset presented as fact is not -
  when the brief gives no real numbers and none would be honestly
  illustrative, choose a different kind instead (or `image_kind:
  illustration`, decided by the writer before this call).
- The brief is a set of CATEGORIES that together make up one whole - a
  budget's spending categories, a framework's pillars shown as PROPORTIONS
  of a total rather than just a list (if there's no real "parts of 100%"
  framing, that's honeycomb instead) -> donut_segments. Each node is one
  category; leave `edges` empty. Produces equal-width bands by default (this
  kind doesn't carry real percentage DATA - for that, use `graph` with
  `graph_type: "bar"` instead).
- The brief is the same "parts of a whole" idea as donut_segments but reads
  better as a classic filled pie (no open centre) - typically when there are
  fewer, larger categories -> pie_wedge. Same node/edge rules as
  donut_segments.
- The brief describes a sequential NARRATIVE journey or roadmap with real
  milestones, told as a story arc rather than a plain procedure (a customer
  journey, a product roadmap, "the path from awareness to advocacy") ->
  wavy_journey_path, not timeline (timeline is for DATED historical events)
  or flow_chart (which is for a procedure, not a narrative arc). Order
  `nodes` the way the journey happens; leave `edges` empty.
- The brief gives a real PERCENTAGE/STAT for each of several named items (a
  satisfaction score, a completion rate, an adoption percentage) ->
  progress_rings. Set each node's `value_pct` to that exact number from the
  brief (0-100) - never invent one; leave a node's `value_pct` unset only
  when the brief genuinely gives no number for that specific item. Leave
  `edges` empty.
- The brief has ONE central concept with items at DIFFERENT, MEANINGFUL
  DISTANCES or tiers from it (layers of the atmosphere around a planet,
  concentric rings of influence, "closer" vs "further" related ideas from
  one core) -> orbit_diagram. This differs from radial_mindmap, where every
  branch is an equally-independent sub-topic with no distance meaning -
  orbit_diagram is specifically for when WHICH RING/TIER a node sits in is
  itself part of what the brief teaches. Put nodes in the order they sit
  from innermost tier to outermost. Leave `edges` empty.
- The brief explicitly frames a short, bounded sequence as CROSSING from one
  state to another - "bridging the gap", a migration, a transformation
  journey with a clear before/after framing -> bridge_process. Order `nodes`
  the way the steps happen; leave `edges` empty. Produce at most 5 nodes.
- The brief describes items that form a DEPENDENCY CHAIN - each one follows
  from and depends on the one before it (a supply chain, a chain of custody,
  a chain of causation) -> chain_links. This differs from honeycomb/
  puzzle_pieces (no inherent order) - chain_links IS ordered, like funnel.
  Order `nodes` the way the chain runs; leave `edges` empty.
- The brief has ONE central theme with several related sub-topics orbiting
  it, and a softer, more decorative presentation suits the content better
  than radial_mindmap's plain spokes (core values around a central mission,
  themes radiating from one idea) -> flower_petal. Put the central theme's
  own node first if it needs its own label (otherwise leave it implicit);
  every other node is one petal. Leave `edges` empty. Produce 4-7 nodes.
- The brief weighs TWO OPPOSING FRAMINGS against each other where each side
  itself has multiple layers (cost broken into several cost types vs. value
  broken into several value types; a detailed before broken into stages vs.
  a detailed after broken into stages) -> hourglass_diagram. This differs
  from balance_comparison (a flat list of points on each side) - use
  hourglass_diagram specifically when each side has its own internal
  LAYERS/LEVELS worth showing as a stack, not just a bullet list. List every
  node for the FIRST (narrowing/top) framing before every node for the
  SECOND (widening/bottom) one. Leave `edges` empty.
- The brief is literally about an ORGANISATION'S reporting structure - real
  roles/titles and who reports to whom, not a generic category tree ->
  org_chart, not hierarchy (hierarchy is for a generic parent/child
  breakdown - a taxonomy, a category tree - where the nodes aren't people/
  roles in an org). Each node's `label` is the role/title, `detail` the
  person's name or a one-line scope note. Set every node's `level` (0 =
  top of the org) and add one edge per reporting line.
- The brief is about a SYSTEM/CLOUD/NETWORK architecture whose components
  are grouped INSIDE bigger boxes - services inside a VPC inside an
  account, tiers inside an application, modules inside a package - and
  that grouping/nesting is itself part of what's being taught, not just a
  flat list of parts -> architecture_diagram, not hierarchy/org_chart
  (neither one draws a box's own children visually INSIDE a bordered
  parent box - they only connect boxes with lines) and not network_mesh
  (no containment at all, every node sits directly on the ring). Give
  each grouping box `shape_role: "container"` and a short `label` (e.g.
  "AWS Account", "VPC", "Web tier"); every node - container or leaf
  component - that belongs inside one sets `parent_id` to that container's
  own `id` (a container can itself have `parent_id` set to an OUTER
  container, nesting as deep as the brief actually needs - rarely more
  than 2-3 levels before it stops being readable). A leaf component
  (`shape_role` left blank) is one real part - `label` its name, `icon` an
  optional pictogram (see the icon keyword list elsewhere in this prompt),
  `detail` a short one-line note if useful. `edges` connect two LEAF
  component ids only (never a container id - a container is a grouping,
  not a thing a connection wires to), each with an optional `label`
  naming the connection ("HTTPS", "writes to", "publishes event").
  Produce at most ~20 nodes total across every level (containers and
  leaves combined) - a diagram with more than that stops being legible no
  matter how it's nested.

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
data_flow_diagram/er_diagram/swimlane/pyramid/funnel/honeycomb/puzzle_pieces/
gears_process/matrix/radial_mindmap/venn/chevron_process/timeline/
staircase_process/balance_comparison/nested_circles/network_mesh/
decision_tree/fishbone/layered_diagram/gantt_chart/donut_segments/
pie_wedge/wavy_journey_path/progress_rings/orbit_diagram/bridge_process/
chain_links/flower_petal/hourglass_diagram/org_chart:
- Produce between 2 and 8 `nodes` for concept_map/hierarchy/comparison/
  smart_art/pyramid/funnel/honeycomb/matrix/radial_mindmap/
  balance_comparison/nested_circles/network_mesh/decision_tree/
  layered_diagram/gantt_chart/donut_segments/pie_wedge/wavy_journey_path/
  progress_rings/chain_links/hourglass_diagram/org_chart
  (venn is always exactly 2 or 3 - see its own section above; fishbone's
  own 3-5-category guidance, and flower_petal's own 4-7, are in their own
  sections above too) - those are meant to stay a small, scannable set of
  relationships (nested_circles especially - realistically 3-5 levels,
  never more than a reader can visually nest; layered_diagram similarly -
  realistically 3-7 layers).
  chevron_process/staircase_process are meant to stay even smaller - 3-6
  nodes, a short strip, not a long column (use flow_chart/process instead
  for anything longer); puzzle_pieces is capped even tighter - at most 6,
  gears_process/orbit_diagram/bridge_process at most 5, see their own
  sections above. For
  flow_chart/process/cycle/data_flow_diagram/
  swimlane/timeline you may go up to 14 nodes when the
  brief genuinely describes that many distinct steps/milestones (e.g. a
  real request pipeline with its error branches, an agent's decide/call-
  tool/observe loop, a long historical timeline) - never pad a simple
  sequence with extra steps just to look more thorough, but never compress
  a genuinely multi-stage technical process down to 8 boxes either; a
  rejected, too-small diagram falls back to a raster illustration that
  cannot reliably render that many text labels, which is worse than a few
  extra boxes. Give each node a short, unique `id`, a
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
- Every EDGE `label` (and, for `er_diagram`, every relationship node's own
  `label`/`detail` - see that kind's own section below) must read as a
  natural short phrase answering "what does this connection mean", never a
  mechanically concatenated cluster of field names, abbreviations or
  metadata. Prefer a short verb/phrase: "created by", "belongs to",
  "contains", "references", "resolves", "depends on", "generates",
  "validates", "triggers", "retrieves", "updates" - 1-3 words is the target,
  4 words the hard maximum. A real, confirmed failure mode: a concept_map
  edge labelled "raised by · prov · conf" - three ideas (who raised it, that
  it carries provenance, that it carries a confidence score) dot-joined into
  one label instead of written as one phrase. If a relationship genuinely
  carries provenance, confidence, timestamps or other metadata, that detail
  belongs on its own `shape_role: "attribute"` child node (exactly like a
  database column) or in the surrounding course text - it must NEVER be
  repeated inside the relationship label itself, and never joined onto it
  with a delimiter (·, |, /, ::) or a "field: value" tag. If a relationship
  truly cannot be said in 4 words without losing its meaning, that is a sign
  to redesign it - split it into two simpler relationships, rename it, or
  reconsider whether this is the right diagram shape for it - never truncate
  a meaningful label blindly to hit the word count.
- Not every relationship in the brief deserves its own visible edge. Prefer
  fewer, clearer relationships over drawing every connection you can infer -
  keep only the ones most essential to what this diagram is teaching; group,
  merge or simply omit the rest.
- Base every label and detail only on the brief below. Do not invent facts,
  numbers or names that were not given to you. No markdown, no quotes.
  This still applies when you personally know the standard formula, number
  or procedure from general domain knowledge and the brief's own wording
  merely implies or gestures at it - a diagram label is read as a direct
  claim about THIS course's content, not a textbook fact in general, so
  "filling in" a correct-but-ungrounded specific (a formula the brief never
  wrote out, a numeric value, a named method like "secant" or "Newton-
  Raphson" the brief never named, an integral or convergence criterion the
  brief never stated) fails this course's own source-grounding check even
  when the added detail is true - state only what the brief itself already
  says, in your own words, never a textbook-standard elaboration on top of
  it.
- Never write dense inline mathematical notation as a label or detail -
  subscripts/superscripts, Greek letters, LaTeX-style markup, or a packed
  symbolic expression (e.g. "P_l^m(cosθ)", "Y_{lm}(θ,φ)=N·P_l^m(cosθ)·
  e^{imφ}", "Δℓ=±1,Δm=0,±1"). This renderer draws labels as plain flat SVG
  text with no real typesetting, so notation like this does not render as
  math at all - it renders as a cramped run of underscores, carets and
  symbols that reads as corrupted text, not an equation, and fails this
  pipeline's own garbled-text check. Say the same thing in plain words
  instead (e.g. "Associated Legendre function", "spherical harmonic",
  "angular momentum selection rule: Δℓ = ±1"), and if a real rendered
  formula is genuinely essential, that belongs in the surrounding prose as
  a proper formula block, never baked into a diagram node's label/detail.
- Never make a node or edge label ABOUT a source: a document's filename,
  title, heading, author, or a bracketed citation reference tag like
  "[S1.3]" or "[G2.5]". The brief may hand you research built from the
  course's own uploaded documents or the knowledge library, each passage
  marked with exactly this kind of reference so the WRITER can cite it in
  prose - that marking is for prose citation, never diagram content. A
  diagram's nodes and edges represent the subject matter itself (a
  mechanism, a process, a relationship between real concepts), never "which
  document says this" or "what resources exist on this topic" - a reading
  list belongs in the course's own Sources section, never as a concept_map
  or any other diagram kind.
- For smart_art/chevron_process/funnel/honeycomb/puzzle_pieces specifically,
  optionally set a node's `icon` to ONE of these exact keywords when it
  genuinely fits that node's own concept - a small pictogram badge draws
  next to the label: person, people, gear, lock, shield, chart, chart_line,
  idea, check, warning, clock, target, arrow, document, database, cloud,
  globe, mail, search, flag, star, money, calendar, link. Leave `icon`
  blank rather than forcing a poor match - a blank icon still draws a
  normal numbered badge instead, never a broken or missing one. Never
  invent a keyword outside this exact list; an unrecognised one silently
  draws no icon at all. Ignored by every other kind.

Rules for spectrum/iceberg/feedback_loop_process/causal_loop/
input_process_output/ranked_bars (see each kind's own section above for its
exact field usage):
- Keep labels just as short as every other kind (2-6 words) - these kinds
  are NOT an exemption, and some are tighter still: `spectrum`'s cards and
  `causal_loop`'s ring circles have real, measured geometry limits that a
  long label pushes past (a too-wide spectrum card gets nudged/shrunk to
  fit more positions; a too-big causal_loop circle can crowd a neighbour
  the curved edges have to route around) - short wins over descriptive
  every time here.
- Node counts: spectrum up to 8, causal_loop up to 6 (and keep its EDGE
  count sparse too - a single loop around the ring plus at most one or two
  real cross-links, see its own section above), input_process_output up to
  9, iceberg/ranked_bars up to 9, feedback_loop_process up to 14 (same
  sequential-kind allowance as flow_chart/process).
- `edges` stays EMPTY for spectrum/iceberg/input_process_output/
  ranked_bars - none of them draw connectors between nodes at all (spectrum
  positions by `value_pct`, iceberg by `level`, input_process_output by
  `shape_role`, ranked_bars by `value_pct`); only feedback_loop_process and
  causal_loop use `edges`, exactly as their own sections above describe.

"""

# Root-cause fix (kind-selection drift, confirmed twice this session: a
# model-facing prompt silently missing a newly-added kind - `periodic_table`
# was initially missing from this very enumeration, and `circuit_schematic`
# had NO authoring bullet at all here for a while after it shipped). The
# bullet-by-bullet authoring guidance above still has to stay hand-written
# (each kind's real field conventions are too bespoke to generate), but this
# one enumeration - "every kind other than the retired `schematic`" - has no
# bespoke content of its own, so it's computed from `DIAGRAM_KINDS` (the
# actual source of truth) instead of hand-typed, making it structurally
# impossible for a newly-added kind to go missing from THIS list again. See
# `test_diagram_service.py`'s drift-guard test for the complementary check
# that every kind still has its OWN authoring bullet somewhere above.
DIAGRAM_SYSTEM = DIAGRAM_SYSTEM.replace(
    "__DIAGRAM_KIND_ENUM__", ", ".join(k for k in DIAGRAM_KINDS if k != "schematic")
)

# ---------------------------------------------------------------------------
# Per-kind trimmed DIAGRAM_SYSTEM - cost optimisation (MOM cost-audit,
# 2026-11-23), built ONLY for a call that already knows its exact kind
# (`pin_kind=True` - see _request_spec's call sites). A pinned call never
# needs the other ~44 kinds' own authoring bullets or their classification
# framing ("is this X or Y?") - it already has its answer. Measured: the
# full ~9.6K-token DIAGRAM_SYSTEM was ~94% of a real diagram call's average
# input tokens; a pinned call only genuinely needs its own kind's bullet
# plus whatever shared rules actually apply to it.
#
# This is NOT a 2-bucket "shared vs per-kind" split - DIAGRAM_SYSTEM nests
# three different kinds of shared content, and getting this wrong would
# silently drop a real rule for some kinds (confirmed concretely below, see
# test_diagram_service.py's exhaustive per-kind completeness test):
#   1. Two explicit "Rules for <kind>/<kind>/...:" / "For <kind> specifically"
#      blocks, each scoped to its OWN named kind-list (parsed from the
#      header text itself via regex - never hand-copied, so a future edit
#      to either header automatically changes which kinds the block applies
#      to, with no separate list to keep in sync).
#   2. A genuinely kind-agnostic span (no-invented-facts, no-dense-math-
#      notation) that happens to sit TEXTUALLY inside block 1's span for
#      human readability, but is not scoped to block 1's kind-list at all -
#      e.g. circuit_schematic and periodic_table are NOT in block 1's list,
#      yet obviously still must never invent facts. Carved out explicitly by
#      its own exact marker text (searched for, not line-numbered, so it
#      survives future edits) rather than inferred from its physical
#      position - this is the one place a human judgement call about scope
#      was unavoidable; the exhaustive test below verifies the result
#      against the real source text for every kind, not just this reasoning.
#   3. Each kind's own dispatch bullet, decided by searching the ENTIRE
#      bullet text for a `-> kind_name` marker (not requiring the arrow and
#      name to be on the same visual line - DIAGRAM_SYSTEM line-wraps at
#      ~79 chars, so e.g. "-> \n  wavy_journey_path" is one real "-> X"
#      marker split across a wrap; `\s+` already spans that newline).
#
# A kind this can't locate a bullet for raises loudly (never silently
# skipped) - see _kind_bullets()'s own assertion.

_DIAGRAM_PREAMBLE_END_MARKER = (
    "First decide what the brief actually needs, then pick exactly one `kind`:"
)
_DISPATCH_END_MARKER = "For `flow_chart`/`process` specifically:"
_FLOWCHART_MINIBLOCK_END_MARKER = "\nRules for flow_chart/process/cycle/concept_map/"
_UNIVERSAL_START_MARKER = "- Base every label and detail only on the brief below."
_UNIVERSAL_END_MARKER = (
    "- For smart_art/chevron_process/funnel/honeycomb/puzzle_pieces specifically,"
)

_HEADER_RE = re.compile(
    r"(?:Rules for|For)\s+((?:`?[a-zA-Z_][a-zA-Z0-9_]*`?(?:/`?[a-zA-Z_][a-zA-Z0-9_]*`?)*))"
    r"\s*(?:\([^)]*\))?\s*(?:specifically)?\s*[,:]"
)
_ARROW_RE = re.compile(r"->\s*(.{0,80})", re.DOTALL)
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_ARROW_CONNECTORS = frozenset({"or", "and"})

# Kinds that never reach this prompt at all, so they're excluded from the
# "every kind must have a bullet" requirement, not silently missed:
# `schematic` is permanently retired (see its own paragraph in
# DIAGRAM_SYSTEM - the caller already reroutes away from it before any call
# reaches here); `concept_experience` is authored by a completely separate,
# purpose-built pipeline (app.render.concept_experience_renderer) that never
# calls _request_spec/DIAGRAM_SYSTEM at all. `conceptual` has no bullet of
# its own by design - it's rendered identically to concept_map (see
# _RING_OR_WHEEL_KINDS grouping them together), so it reuses concept_map's
# bullet rather than needing a duplicate.
_NO_OWN_BULLET_EXPECTED = frozenset({"schematic", "concept_experience"})
_KIND_ALIASES = {"conceptual": "concept_map"}


def _find_universal_span(system_text: str) -> str:
    start = system_text.index(_UNIVERSAL_START_MARKER)
    end = system_text.index(_UNIVERSAL_END_MARKER, start)
    return system_text[start:end].strip()


def _find_header_blocks(system_text: str) -> list[tuple[frozenset[str], str]]:
    """Every "Rules for X/Y/Z:" / "For X specifically" block, as
    (parsed kind-set, body text up to the next such header or end of
    string) - parsed from the header's own text, not a hand-copied list."""
    matches = list(_HEADER_RE.finditer(system_text))
    blocks: list[tuple[frozenset[str], str]] = []
    for i, m in enumerate(matches):
        kinds = frozenset(k.strip("`") for k in m.group(1).split("/"))
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(system_text)
        blocks.append((kinds, system_text[body_start:body_end].strip()))
    return blocks


def _kinds_after_arrow(text_after_arrow: str, valid_kinds: frozenset[str]) -> list[str]:
    """Every kind named immediately after a `->` marker, consuming a chain
    like "flow_chart or process" (flow_chart/process share one bullet, one
    arrow, two real outcomes - confirmed by the exhaustive completeness test
    below, which is exactly what caught this case) - stops at the first
    word that isn't a recognised kind and isn't a connector ("or"/"and"),
    so trailing explanatory prose never gets misread as another kind."""
    found: list[str] = []
    for word in _WORD_RE.finditer(text_after_arrow):
        token = word.group(0)
        if token in valid_kinds:
            found.append(token)
            continue
        if token in _ARROW_CONNECTORS:
            continue
        break
    return found


def _kind_bullets(system_text: str) -> dict[str, str]:
    """Every dispatch bullet, keyed by each kind it decides (via its own
    `-> kind_name[ or kind_name...]` marker, found anywhere in the bullet's
    text - never assumed to be the bullet's last line, and a single arrow
    can name more than one real outcome kind)."""
    dispatch_start = system_text.index(_DIAGRAM_PREAMBLE_END_MARKER) + len(
        _DIAGRAM_PREAMBLE_END_MARKER
    )
    dispatch_end = system_text.index(_DISPATCH_END_MARKER)
    dispatch_text = system_text[dispatch_start:dispatch_end]
    bullets = re.split(r"\n(?=- )", dispatch_text)
    valid_kinds = frozenset(DIAGRAM_KINDS)
    result: dict[str, str] = {}
    for bullet in bullets:
        bullet = bullet.strip()
        if not bullet.startswith("-"):
            continue
        arrow_match = _ARROW_RE.search(bullet)
        if arrow_match is None:
            continue
        # Only the FIRST "->" in a bullet is the real decision - a bullet's
        # own explanatory prose may name other kinds later via "not X"/
        # "differs from Y" without ever using another arrow (confirmed by
        # reading every bullet during this change).
        for kind in _kinds_after_arrow(arrow_match.group(1), valid_kinds):
            result[kind] = bullet
    return result


def _build_trimmed_diagram_system(kind: str) -> str:
    """A DIAGRAM_SYSTEM variant containing only what `kind` (already decided
    - see `pin_kind=True` call sites) genuinely needs: the universal framing/
    correction-mode preamble, this kind's own dispatch bullet, the always-
    universal label/fact/notation rules, and every named-list rules block
    that actually names this kind (never guessed - see module-level
    docstring above). Raises if `kind` has no locatable bullet and isn't one
    of the documented exceptions - a silently-empty trim would be worse than
    an error here."""
    kind = _KIND_ALIASES.get(kind, kind)
    preamble = DIAGRAM_SYSTEM[: DIAGRAM_SYSTEM.index(_DIAGRAM_PREAMBLE_END_MARKER)].strip()
    bullets = _kind_bullets(DIAGRAM_SYSTEM)
    if kind not in bullets:
        raise AIServiceError(
            f"_build_trimmed_diagram_system: no dispatch bullet found for kind={kind!r} - "
            "refusing to silently ship an incomplete trimmed prompt."
        )
    own_bullet = bullets[kind]
    universal = _find_universal_span(DIAGRAM_SYSTEM)
    rules_region = DIAGRAM_SYSTEM[DIAGRAM_SYSTEM.index(_DISPATCH_END_MARKER) :]
    applicable_blocks = [
        body for kinds, body in _find_header_blocks(rules_region) if kind in kinds
    ]
    parts = [preamble, "", own_bullet, "", universal]
    parts.extend(["", block] for block in applicable_blocks)
    flat: list[str] = []
    for p in parts:
        if isinstance(p, list):
            flat.extend(p)
        else:
            flat.append(p)
    return "\n".join(flat)


_KIND_PIN_INSTRUCTION = """

The brief above already specifies the required `kind`: "{hint}". Use exactly
this kind. Do not substitute a sequential kind (flow_chart/process/cycle)
when a structural/relationship kind was requested, or vice versa - if the
content seems sequential but a relationship kind was requested, express the
sequence as an edge label (e.g. "then", "causes") between concept_map nodes
instead of switching kind.
"""


_DENSITY_GUIDANCE_SHORT_WORDS = 25
_DENSITY_GUIDANCE_LONG_WORDS = 90


def _density_guidance_line(brief_text: str) -> str:
    """A deterministic, PRE-generation density nudge (diagram quality audit
    follow-up, Phase 2/4) - computed from the brief's own word count before
    the first generation call ever runs, not just a post-hoc cap applied
    only after the model has already overshot (see `needs_consolidation`/
    `max_nodes_for` below, which stays the real, hard backstop regardless -
    this is advisory steering on top of it, never a replacement for it). A
    short brief describes one or two ideas - nudging toward a small diagram
    UP FRONT means fewer corrective retries are needed later; a long,
    detailed brief may genuinely need more nodes, so this never suggests
    shrinking content a longer brief actually calls for - it only steers a
    SHORT brief away from needlessly padding itself out to look more
    thorough, and reminds a LONG brief to still keep individual labels
    short regardless of how much it covers. Returns "" for an ordinary
    brief in between - no signal strong enough to add prompt noise over."""
    word_count = len(brief_text.split())
    if word_count == 0:
        return ""
    if word_count <= _DENSITY_GUIDANCE_SHORT_WORDS:
        return (
            "\nThis brief is short and focused - a compact diagram (roughly 3-6 labelled "
            "nodes) that scans in a few seconds is almost always the right size here; don't "
            "pad it out with extra nodes or invented sub-detail the brief didn't ask for just "
            "to look more thorough.\n"
        )
    if word_count >= _DENSITY_GUIDANCE_LONG_WORDS:
        return (
            "\nThis brief covers real breadth - a larger diagram may genuinely be warranted, "
            "but keep every individual node/label SHORT regardless (a few words, never a "
            "sentence) and favour grouping related ideas over listing every last one "
            "individually; scannability matters more than exhaustiveness.\n"
        )
    return ""


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
    existing_outline: dict | None = None,
    template_audience: str = "",
    template_style: str = "",
) -> str:
    # Prompt-fidelity fix (replace_image correction reliability): `purpose`/
    # `caption` are leftovers from the ORIGINAL generation (DiagramService.
    # generate_for_block never touches them on a correction - only `prompt`
    # gets overwritten with the user's live instruction, see patcher.py's
    # own `replace_image` handling), while `prompt` is what the user is
    # asking THIS call to change. Joining all three with a bare newline into
    # one undifferentiated "VISUAL BRIEF" - the old behaviour, still used
    # below for a fresh, non-correction generation - left the model to GUESS
    # which sentence was the live instruction, and the flat `[:MAX_BRIEF_
    # CHARS]` slice could silently truncate `prompt` away entirely if a long
    # leftover `purpose` ate the budget first. On a correction (an
    # `existing_outline` is only ever set then), reserve the user's own
    # `prompt` text FIRST and label it distinctly from the old context - the
    # same "earlier text gets first claim on the budget" principle app.
    # services.image_service.py's own `_assemble_prompt_with_priority`
    # already applies for the raster-illustration path, just without
    # needing that whole priority-tier machinery here.
    if existing_outline and prompt.strip():
        reserved_prompt = prompt.strip()[:MAX_BRIEF_CHARS]
        remaining_budget = max(MAX_BRIEF_CHARS - len(reserved_prompt) - 1, 0)
        context_text = "\n".join(part for part in (purpose, caption) if part)[:remaining_budget]
        # Correction-intent classification (Phase 7) - a sharpened, intent-
        # specific policy line alongside the general correction-mode rule
        # already in DIAGRAM_SYSTEM, so e.g. "fix the overlapping arrow
        # text" (geometry) and "make this suitable for children" (audience)
        # - both genuine corrections - get the DIFFERENT handling each one
        # actually needs, instead of one blanket "preserve everything"
        # default that's right for the first case and wrong for the second.
        intent = classify_correction_intent(prompt)
        intent_block = f"\nCORRECTION TYPE: {CORRECTION_INTENT_POLICY[intent]}\n" if intent else ""
        brief_block = (
            "YOUR CORRECTION REQUEST (the user's own instruction for THIS "
            f"regeneration - apply it precisely and visibly):\n{reserved_prompt}\n"
            f"{intent_block}"
            + (f"\nORIGINAL BRIEF (context from the first generation):\n{context_text}\n" if context_text else "")
        )
    else:
        brief = "\n".join(part for part in (purpose, prompt, caption) if part)[:MAX_BRIEF_CHARS]
        brief_block = f"VISUAL BRIEF:\n{brief or '(not specified)'}\n"
    section = prompts.memory_section(memory)
    memory_block = f"\n{section}\n" if section else ""
    requested = f"\nREQUESTED DIAGRAM TYPE: {kind_hint}\n" if kind_hint else ""
    pin = _KIND_PIN_INSTRUCTION.format(hint=kind_hint) if pin_kind and kind_hint else ""
    feedback_block = f"\n{qa_feedback}\n" if qa_feedback else ""
    # Set only on a correction (replace_image on a block that already has a
    # rendered spec - see DiagramService.generate_for_block) - see
    # DIAGRAM_SYSTEM's own "CURRENT DIAGRAM" instructions for what this
    # means. `diagram_outline`'s own compact kind/parts/arrows shape (also
    # what app.services.visual_review already shows a reviewer) is reused
    # here rather than the full DiagramSpec JSON - everything a correction
    # needs to reproduce verbatim, without the geometry/style fields that
    # would only bloat the prompt and are re-derived by the renderer anyway.
    current_block = (
        f"\nCURRENT DIAGRAM (this is a correction - see the system prompt's "
        f"own instructions on what to preserve):\n{json.dumps(existing_outline, ensure_ascii=False)}\n"
        if existing_outline
        else ""
    )
    # Audience/style propagation fix: same uploaded-template free text
    # already threaded into image_service.py's build_prompt - never a
    # hardcoded audience branch, passed through verbatim so label
    # verbosity/terminology level follows whatever THIS template's own
    # author actually wrote.
    #
    # Follow-up strengthening fix (same reasoning as build_prompt's own
    # template_guidance_line): a mild, passive restatement produced only a
    # moderate real difference between a young-learner and a senior-
    # professional template. This is now a firm, directive instruction to
    # actively derive concrete labelling decisions from the template's own
    # text - the two example sentences describe HOW to translate an
    # audience into labels in general terms, never naming a specific
    # audience category, so it still generalises to any uploaded template.
    audience_bits = []
    if template_audience.strip():
        audience_bits.append(f"audience = \"{template_audience.strip()[:300]}\"")
    if template_style.strip():
        audience_bits.append(f"style requirement = \"{template_style.strip()[:400]}\"")
    audience_block = (
        "\nFIRM REQUIREMENT, not a minor suggestion - this course's own template specifies "
        + " and ".join(audience_bits) + ". You must deliberately adapt label wording, "
        "terminology level and node/connector detail to match this exact audience and style - "
        "a generic, one-size-fits-all diagram that ignores it is a failure of this brief. "
        "A younger or less technical audience needs noticeably simpler, plainer labels and "
        "fewer/coarser-grained nodes; a more advanced or technical audience needs noticeably "
        "more precise terminology and finer-grained, denser labelling - apply whichever "
        "direction this template's own text above actually calls for.\n"
        if audience_bits
        else ""
    )
    density_block = _density_guidance_line(" ".join(part for part in (purpose, prompt, caption) if part))
    return f"""\
COURSE: {course_title}

{brief_block}{requested}{memory_block}{feedback_block}{current_block}{audience_block}{density_block}
Produce the diagram specification for this visual.{pin}
"""


def diagram_outline(spec: DiagramSpec) -> dict:
    """A compact record of what a rendered diagram shows: its kind, the
    labelled parts, and each arrow as "from -> to (label)" in drawn
    direction. Stored on the block for app.services.visual_review."""
    labels = {node.id: node.label for node in spec.nodes if node.id}
    outline: dict = {
        "kind": spec.normalised_kind(),
        "title": spec.title,
        "parts": [
            {"label": node.label, "detail": node.detail} for node in spec.nodes if node.label.strip()
        ][:40],
        "arrows": [
            {"from": labels.get(edge.source, edge.source), "to": labels.get(edge.target, edge.target), "label": edge.label}
            for edge in spec.edges
        ][:60],
    }
    shapes = list(spec.shapes) + [shape for state in spec.states for shape in state.shapes]
    if shapes:
        names = {shape.id: shape.label for shape in shapes if shape.id}
        outline["parts"] += [{"label": shape.label, "detail": shape.sublabel} for shape in shapes if shape.label.strip()][:40]
        outline["arrows"] += [
            {"from": names.get(rel.source, rel.source), "to": names.get(rel.target, rel.target), "label": rel.type}
            for rel in spec.relationships
        ][:40]
    return outline


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
        # smart_art's own renderer deliberately never draws edges ("numbered
        # list, no connecting arrows" - see diagram_renderer.py) - it can
        # never satisfy a relationship brief by construction, exactly like a
        # sequential answer can't, confirmed via a real generated course
        # sample where a "concept_map" hint came back as "smart_art" and
        # this check let it through silently.
        #
        # "comparison" is the SAME kind of silent escape hatch, and a real,
        # confirmed production case shows it: a block pinned to a specific
        # relationship kind (e.g. a wiring/apparatus request hinted
        # "schematic") came back as a flat "comparison" card grid instead -
        # structurally valid (comparison is a real DIAGRAM_KINDS member, so
        # is_usable() never caught it) but a strictly WORSE answer than the
        # relationship kind actually asked for, which this check previously
        # let through for every one of the 20+ RELATIONSHIP_KINDS members
        # (concept_map, hierarchy, er_diagram, decision_tree, fishbone,
        # network_mesh, matrix, radial_mindmap, org_chart, ...) - "comparison"
        # was the one substitution this branch never flagged as a conflict,
        # even though the sibling `hint in ("comparison", "smart_art")`
        # branch right below already treats a "comparison"-hint request
        # being answered with something else as exactly this kind of
        # conflict. A hint that IS itself "comparison" never reaches this
        # branch (RELATIONSHIP_KINDS doesn't contain "comparison"), so this
        # can't fire a false "conflict" when comparison genuinely was asked
        # for and delivered.
        return actual in SEQUENTIAL_KINDS or actual == "smart_art" or actual == "comparison"
    if hint in ("comparison", "smart_art"):
        return actual in SEQUENTIAL_KINDS or (hint == "comparison" and actual != "comparison")
    return False


def _withhold_structured_diagram(
    block: Block, *, detail: str, extra_findings: list[dict] | None = None
) -> None:
    """Skip this block's image entirely rather than let `generate_for_block`
    return `False` and have `ImageService` silently convert it into an
    unrelated AI-generated illustration (visual-generation policy fix: a
    block authored as a structured diagram kind - flow_chart, decision_tree,
    process, etc. - must never become raster art just because the
    structured renderer had trouble with THIS attempt). Reuses the exact
    same withheld-image state/finding shape `ImageService.generate_for_block`
    already uses for its own "every attempt failed, no Wikimedia match
    either" case (`omitted_after_qa_failure=True`, `path=None`,
    `image_withheld_after_qa_failure` finding, `deterministic=True` since
    "no image exists for this block" is a directly-checkable fact, not a
    judgment call) - one consistent "image missing" signal for a reviewer
    regardless of which path produced it, and the same `severity="blocker"`
    + `verification_status="independently_verified"` treatment (see
    quality_framework.py), so a course with a genuinely missing diagram
    cannot silently pass approval.

    `extra_findings` (already-built via `visual_qa_finding_entries`)
    preserves the SPECIFIC defect that caused the withholding - e.g. the
    exact `misspelled_or_garbled_text` issue(s) - alongside the generic
    "image withheld" summary, same two-entry shape `ImageService`'s own
    withholding already uses (its specific `relevance_findings`/
    `formula_findings` plus its own separate `withheld_findings` marker)."""
    block.content = merge_content(
        block.type,
        block.content,
        {
            "path": None,
            "asset_id": None,
            "generated": False,
            "error": None,
            "omitted_after_qa_failure": True,
            "width": None,
            "height": None,
            "visual_qa_findings": (extra_findings or []) + visual_qa_finding_entries(
                [("image_withheld_after_qa_failure", detail, True)],
                resolution="image_withheld_after_qa_failure",
            ),
        },
    )


# Kinds the new textbook-style ring/wheel renderer draws (see
# app.render.diagram_renderer's dispatch in render_diagram_svg) - these are
# the only ones that get the "Fig. N: " caption prefix below; every other
# kind's title is left exactly as the model wrote it, unchanged.
_RING_OR_WHEEL_KINDS = ("flow_chart", "process", "cycle", "concept_map", "conceptual")


def _with_figure_caption(spec: DiagramSpec, block: Block) -> DiagramSpec:
    """Prefixes a ring/wheel diagram's own title with "Fig. {chapter
    number}: " - the textbook-style caption the new circular rendering
    draws as its big heading above the diagram (see render_diagram_svg's
    existing title handling; no renderer change needed, it already draws
    whatever `spec.title` says). `block.meta.chapter_number` is already
    set by the time any diagram generates (app.course.document.builder
    assigns it during the initial document build, well before
    ImageService.generate_missing runs) - no new parameter needed on this
    method. A true "Fig. {chapter}.{sequence within chapter}" numbering
    would need a document-wide view this per-block method doesn't have
    (and calls can run concurrently - see ImageService.generate_missing's
    own asyncio.gather) - deliberately simplified to the chapter number
    alone rather than risk an inconsistent/racy sub-number. Idempotent:
    does nothing if the title already starts with "Fig. " (the connectivity
    retry path can call this twice on the same spec)."""
    if spec.normalised_kind() not in _RING_OR_WHEEL_KINDS:
        return spec
    chapter_number = block.meta.chapter_number
    if not chapter_number or spec.title.strip().startswith("Fig. "):
        return spec
    spec.title = f"Fig. {chapter_number}: {spec.title.strip()}" if spec.title.strip() else f"Fig. {chapter_number}"
    return spec


_CORRECTION_DRIFT_MIN_LABELS = 2
_CORRECTION_DRIFT_RATIO_FLOOR = 0.4


def _correction_drift_issue(existing_outline: dict | None, spec: DiagramSpec) -> str | None:
    """Deterministic, zero-AI-call sanity check for a `replace_image`
    correction (see `generate_for_block`'s own `existing_outline` - this
    whole fix's root-cause closure): when the model was given the existing
    diagram as context, how much of its labelled content survived into the
    regenerated spec? A correction asked to fix one small thing but coming
    back with almost none of the original labels is a measurable signal
    the instruction was followed as a redesign, not a correction - the
    exact "generates a completely different image" failure this feature
    exists to close. Surfaced as a visible, inspectable finding (never a
    retry, never an extra AI call, never a reason to reject the result - a
    human reviewer decides what to do with it, same as every other finding
    this module already records, e.g. `_flow_chart_connectivity_issues`).
    `None` when there's nothing to compare (a fresh generation) or too few
    labels to measure meaningfully without one coincidental match skewing
    the ratio."""
    if not existing_outline:
        return None
    old_labels = {
        str(part.get("label", "")).strip().lower()
        for part in existing_outline.get("parts", []) or []
        if str(part.get("label", "")).strip()
    }
    if len(old_labels) < _CORRECTION_DRIFT_MIN_LABELS:
        return None
    new_labels = {node.label.strip().lower() for node in spec.nodes if node.label.strip()}
    kept = old_labels & new_labels
    ratio = len(kept) / len(old_labels)
    if ratio >= _CORRECTION_DRIFT_RATIO_FLOOR:
        return None
    return (
        f"This regenerated a diagram that was meant to be CORRECTED, but only "
        f"{len(kept)}/{len(old_labels)} of its original labels survived - it may be a "
        f"different diagram rather than the same one with the requested fix."
    )


_UNDER_CORRECTION_MIN_PROMPT_CHARS = 15  # long enough to be a real instruction, not a stray word


def _under_correction_issue(raw_existing_spec: dict | None, prompt: str, spec: DiagramSpec) -> str | None:
    """The OTHER direction of `_correction_drift_issue` just above: a
    correction asking for a SUBSTANTIAL, specific change but coming back
    byte-identical to the spec it was meant to correct is a measurable
    signal the instruction was effectively ignored, not applied - the "I
    typed a prompt and nothing changed" failure replace_image's own prompt-
    fidelity fix (see `_diagram_user_prompt`'s "YOUR CORRECTION REQUEST"
    block) exists to close. Same house rule as `_correction_drift_issue` -
    a visible, inspectable finding only, never a retry, never a reason to
    reject the result; a human reviewer decides what to do with it.

    Compares the FULL spec (via `raw_existing_spec`, the original content
    dict this block's prior `content["spec"]` held - not
    `_correction_drift_issue`'s own compact `existing_outline`, which only
    has labels) because "nothing at all changed" is a much stronger, simpler
    signal than a label-overlap ratio, and correction mode's whole point is
    preserving everything NOT asked to change - true full-spec equality
    after a genuine correction request is specifically what should be rare.
    `None` when there's nothing to compare (a fresh generation, or an
    unparseable stored spec) or the prompt is too short to reliably judge as
    "a real ask that should have changed something" (a bare word, a stray
    click-through)."""
    if not raw_existing_spec or len(prompt.strip()) < _UNDER_CORRECTION_MIN_PROMPT_CHARS:
        return None
    try:
        old_dump = DiagramSpec.model_validate(raw_existing_spec).model_dump(mode="json")
    except Exception:  # noqa: BLE001 - an unparseable old spec just can't be compared
        return None
    if old_dump != spec.model_dump(mode="json"):
        return None
    shown_prompt = prompt.strip()[:80] + ("..." if len(prompt.strip()) > 80 else "")
    return (
        f"This regenerated a diagram in response to a specific correction request "
        f"('{shown_prompt}'), but the result is identical to the one before it - the "
        f"request may not have actually been applied."
    )


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


# Delimiter characters a relationship label gets flagged for joining two or
# more segments with - the exact shape of a real, confirmed failure mode: a
# concept_map edge labelled "raised by · prov · conf" (relationship name +
# provenance marker + confidence marker, mechanically concatenated) instead
# of a single natural phrase. None of these ever belongs INSIDE a legitimate
# short relationship phrase ("depends on", "flows into") - a label that
# contains one is reliably a metadata-tag cluster, not a sentence a human
# would write.
_LABEL_DELIMITER_RE = re.compile(r"[·•|]|::|\s/\s")
# A bare "word: value" or "word=value" tag - the OTHER shape the same
# failure mode takes (a field name and its value, rather than a relationship
# phrase). Requires a letter on both sides of the separator so it doesn't
# false-positive on a genuine ratio/time/cardinality label ("1:N", "3:00").
_LABEL_FIELD_TAG_RE = re.compile(r"[a-zA-Z][a-zA-Z_ ]*\s*[:=]\s*[a-zA-Z]")
_MAX_RELATIONSHIP_LABEL_WORDS = 4
# Diagram-rendering follow-up: `_wrap` (app.render.diagram_renderer) force-
# breaks any single word longer than one whole line, and `_edge_label`'s own
# `allow_shrink` search is bounded (it tries smaller fonts and a few extra
# lines, never unlimited - see that function's own docstring) - a single
# word long enough to still need MORE lines than that bounded search allows,
# even at the smallest readable font, still truncates with an ellipsis no
# matter how much room the search is given. Confirmed this session via
# direct rendering: no amount of shrinking or extra-line budget rescues an
# unbroken term past a certain length, because the word itself - not the
# label as a whole - is the thing that doesn't fit. The right fix is
# upstream (never let a label reach rendering with a word this long in the
# first place), not a deeper rendering patch - the same "fix the root cause,
# don't add a generic patcher" discipline `edge_label_verbosity_issues`
# itself already follows for the metadata-cluster/word-count cases above. 22
# characters is generous enough for a real single technical term
# ("multivariable", "nanopublication", "authentication" are all well under
# it) while still catching a genuinely unbroken compound/identifier/URL.
_MAX_LABEL_WORD_CHARS = 22


def _relationship_label_issue(label: str, *, owner: str, max_words: int = _MAX_RELATIONSHIP_LABEL_WORDS) -> str | None:
    """None when `label` reads as a short, natural relationship phrase;
    otherwise one human-readable issue for the retry-feedback channel. See
    `edge_label_verbosity_issues`'s own docstring for why this check exists
    and what it does/doesn't catch. `max_words` lets a younger/beginner
    audience get a tighter ceiling (see `audience_multiplier`) - never
    looser than the default, since conciseness is the right default for
    every audience, not something an advanced one should be exempt from."""
    text = label.strip()
    if not text:
        return None
    if _LABEL_DELIMITER_RE.search(text):
        return f'{owner} "{text}" joins multiple segments together with a delimiter - write ONE natural phrase instead'
    if _LABEL_FIELD_TAG_RE.search(text):
        return f'{owner} "{text}" reads as a field-name/value tag, not a relationship phrase'
    longest_word = max(text.split(), key=len, default="")
    if len(longest_word) > _MAX_LABEL_WORD_CHARS:
        return (
            f'{owner} "{text}" contains "{longest_word}" ({len(longest_word)} characters, no spaces) - too '
            "long to wrap cleanly at any readable size; shorten it, hyphenate it, or use a standard "
            "abbreviation"
        )
    word_count = len(text.split())
    if word_count > max_words:
        return f'{owner} "{text}" is {word_count} words - relationship labels must be at most {max_words}'
    return None


def edge_label_verbosity_issues(spec: DiagramSpec, *, max_words: int = _MAX_RELATIONSHIP_LABEL_WORDS) -> list[str]:
    """Catches the exact, real, confirmed failure mode behind this check's
    own existence: a relationship label built by mechanically concatenating
    a relationship name with provenance/confidence/metadata markers (e.g.
    "raised by · prov · conf" on a concept_map edge) instead of writing one
    short natural phrase - DIAGRAM_SYSTEM's prompt asks for this (see the
    rule immediately following the node label/detail rule above), but
    nothing previously checked it, unlike node `label`/`detail`, which this
    same prompt DOES get a hard word-count rule for. Operates on the SPEC,
    before rendering - cheap (no SVG parse), and lets the finding fold into
    the SAME combined structural retry every other spec-level check already
    uses (see `generate_for_block`'s `needs_label_rewrite`), rather than a
    separate render-then-recheck round trip.

    Checks every `DiagramEdge.label` (every kind) and every relationship-
    node's own `label`/`detail` (`shape_role == "relationship"`, the
    er_diagram convention - see this file's own er_diagram prompt section)
    - a model forced to keep an EDGE label short could just as easily move
    the same metadata cluster onto the relationship node's own text
    instead, so both fields need the same check, not just one of them.
    Empty means every relationship-bearing label already reads cleanly."""
    issues: list[str] = []
    for edge in spec.edges:
        issue = _relationship_label_issue(edge.label, owner="Edge label", max_words=max_words)
        if issue:
            issues.append(issue)
    for node in spec.nodes:
        if (node.shape_role or "").strip().lower() != "relationship":
            continue
        for field_name, value in (("label", node.label), ("detail", node.detail)):
            issue = _relationship_label_issue(value, owner=f"Relationship node {field_name}", max_words=max_words)
            if issue:
                issues.append(issue)
    return issues


# Conservative, deterministic, zero-LLM-call keyword classifier for the
# "denser/more technical" direction - the counterpart to
# app.course.blocks.normalizer's own `_reads_as_young_learner_audience`
# (reused directly below for the "simpler" direction, never duplicated).
# Biased toward false negatives, same convention: an ambiguous or
# unrecognised phrasing returns False, never guessed into True.
_ADVANCED_AUDIENCE_KEYWORDS = (
    "advanced", "expert", "experts", "professional", "professionals", "graduate",
    "postgraduate", "phd", "doctoral", "researcher", "researchers", "specialist",
    "specialists", "practitioner", "practitioners",
)


def _reads_as_advanced_audience(text: str) -> bool:
    combined = text.lower()
    return any(keyword in combined for keyword in _ADVANCED_AUDIENCE_KEYWORDS)


def audience_multiplier(template_audience: str) -> float:
    """How much to scale `max_nodes_for`/`max_edges_for`'s own ceiling for
    this course's stated audience - a real, confirmed gap this closes:
    DIAGRAM_SYSTEM's own audience-adaptation instruction (see
    `_design_brief`'s own `audience_block` above) is prompt-only, with no
    code backstop, unlike raster illustrations' deterministic young-learner
    override (`apply_young_audience_apparatus_override`). This never
    relaxes factual/technical correctness - only how many nodes/edges a
    diagram is ALLOWED to reach and (via the caller) how tight the
    relationship-label word cap is; a "children" course still gets a
    factually correct diagram, just a structurally simpler one. 1.0
    (unchanged behaviour) whenever the audience text doesn't clearly read
    as young/beginner or advanced - the same "never guess, only act on an
    unambiguous signal" convention `_reads_as_young_learner_audience`
    already established."""
    if not template_audience.strip():
        return 1.0
    if _reads_as_young_learner_audience(template_audience):
        return 0.6
    if _reads_as_advanced_audience(template_audience):
        return 1.15
    return 1.0


# A deliberately rough, uncalibrated per-node character budget for the
# aggregate text-volume signal below - every other check in this module
# (node-count, edge-count, per-label length) is a precise, individually-
# justified number; this one is not, by design: there's no real-world
# calibration data yet on what "too much total text" means per kind. Still
# recorded as a finding for review at THIS looser budget regardless of
# whether the retry-gating check below fires - see total_text_volume_
# issue's own docstring.
_SOFT_TEXT_BUDGET_PER_NODE = 70
# Diagram-quality audit follow-up (Phase 4/6 - density should trigger a
# real action, not just an advisory log line): `_needs_density_retry` below
# promotes this signal into an actual corrective-retry trigger, but only at
# a materially higher bar than the advisory budget above - this multiplier
# exists specifically so only a genuinely excessive case (not merely "a
# little over" an admittedly uncalibrated guide) ever spends a real retry
# call on it; the advisory finding keeps using the original, looser budget
# for logging/review either way.
_TEXT_VOLUME_RETRY_MULTIPLIER = 1.6


def _text_volume_totals(spec: DiagramSpec) -> tuple[int, int]:
    total_chars = sum(len(n.label) + len(n.detail) for n in spec.nodes) + sum(len(e.label) for e in spec.edges)
    node_count = max(len([n for n in spec.nodes if n.label.strip()]), 1)
    return total_chars, node_count


def total_text_volume_issue(spec: DiagramSpec) -> str | None:
    """A rough, ADVISORY aggregate: are there hints that this diagram
    simply has too much total text for one picture, even though every
    individual label/detail/edge-label already passes its own check? Always
    recorded as a finding for later review at this (generous, uncalibrated)
    budget regardless of whether `_needs_density_retry` below also fires -
    the two are deliberately independent, since this budget was never
    tuned to double as a retry-worthy threshold. None when the spec stays
    under budget."""
    total_chars, node_count = _text_volume_totals(spec)
    budget = node_count * _SOFT_TEXT_BUDGET_PER_NODE
    if total_chars <= budget:
        return None
    return (
        f"Total diagram text ({total_chars} characters across all labels/details) is well above a "
        f"rough {budget}-character guide for {node_count} nodes - consider shortening text or "
        "simplifying the diagram (advisory only, not a blocking issue)."
    )


def _needs_density_retry(spec: DiagramSpec) -> bool:
    """The actual GATING check (see `_TEXT_VOLUME_RETRY_MULTIPLIER`'s own
    comment for why this is a materially higher bar than `total_text_volume_
    issue`'s own advisory budget) - only a diagram whose total text is
    SUBSTANTIALLY over the rough per-node guide triggers a real corrective
    retry over it."""
    total_chars, node_count = _text_volume_totals(spec)
    budget = node_count * _SOFT_TEXT_BUDGET_PER_NODE * _TEXT_VOLUME_RETRY_MULTIPLIER
    return total_chars > budget


# Pre-generation planning follow-up (Phase 2/6): only "simple" has a strict
# enough implied ceiling to be worth checking the model's own stated
# estimate against - "moderate"/"complex" are deliberately left unchecked
# (no numeric promise attached to them this codebase could verify without
# guessing), see `DiagramSpec.complexity_estimate`'s own docstring.
_SIMPLE_COMPLEXITY_MAX_NODES = 6


def _complexity_mismatch_issue(spec: DiagramSpec) -> str | None:
    """A real, checkable signal the retry feedback can point at directly:
    the model estimated its OWN output as "simple" (see `DiagramSpec.
    complexity_estimate`) but then produced more nodes than a genuinely
    simple diagram needs - either the estimate was never honestly applied,
    or this content needed a bigger shape than "simple" and the model
    should have said so. `None` when `complexity_estimate` isn't "simple"
    (nothing to check it against) or the node count is within bounds."""
    if spec.complexity_estimate.strip().lower() != "simple":
        return None
    labelled_count = len([n for n in spec.nodes if n.label.strip()])
    if labelled_count <= _SIMPLE_COMPLEXITY_MAX_NODES:
        return None
    return (
        f"This diagram was estimated as 'simple' but has {labelled_count} nodes - more than a "
        f"genuinely simple diagram needs (roughly {_SIMPLE_COMPLEXITY_MAX_NODES} or fewer). Either "
        "consolidate down to match a genuinely simple shape, or this content actually needs a "
        "'moderate'/'complex' structure - don't just relabel the estimate without changing the content."
    )


# Multi-diagram splitting (diagram quality audit, Phase 2/6 of the "big 5"
# deferred items): when a diagram genuinely needs more nodes than its own
# kind can show in one picture, the ONLY existing outcomes were "consolidate
# (losing real content)" or "withhold entirely and fall back to an unrelated
# AI illustration" - never "split it into several linked diagrams", even
# though that is strictly better than discarding real, topic-grounded
# structure. Deliberately scoped to the two kinds where a split is PROVABLY
# safe (zero information loss, no judgment call, no new LLM call) rather
# than a generic splitter for every RELATIONSHIP_KINDS/SEQUENTIAL_KINDS
# member - see `_linear_chain_split`'s own docstring for exactly what
# "provably safe" means here and why a relationship-shaped or branching
# diagram is deliberately NOT covered (the same "do not introduce a generic
# patcher without understanding the existing layout algorithms" discipline
# the er_diagram label-shrink fix already followed this session).
_LINEAR_SPLITTABLE_KINDS = frozenset({"flow_chart", "process"})
# A sane ceiling on how many parts one split will ever produce - beyond
# this, a chain this long is more likely mis-structured content (everything
# crammed into one diagram's `nodes` list that should have been several
# separate blocks from the start) than a genuinely single procedure, and
# shipping 5+ linked diagram blocks for one brief starts to work against
# the "clear, scannable" goal splitting exists to serve in the first place.
_MAX_SPLIT_PARTS = 4


def _linear_chain_split(spec: DiagramSpec) -> list[DiagramSpec] | None:
    """`None` whenever a split isn't applicable, isn't needed, or - most
    importantly - can't be done LOSSLESSLY: this only ever splits a diagram
    whose `edges` form EXACTLY the chain node[0]->node[1]->...->node[n-1]
    (one edge between every consecutive pair, nothing else) - a flow_chart/
    process CAN legitimately branch or merge (an edge skipping ahead, two
    edges into one node), and this function refuses to touch any of those,
    since cutting a branching graph into parts by node POSITION alone would
    silently drop whichever edge crosses a cut. Otherwise returns 2 or more
    parts, each overlapping the next at exactly one shared boundary node (the
    last node of part N is repeated as the first node of part N+1) so the
    split reads as one continuous procedure across several pictures, not
    disconnected fragments - every node and edge from the original appears
    in one part or two adjacent ones, never dropped, never invented.

    Only called when `spec.is_usable()` has already failed for having MORE
    labelled nodes than `max_nodes_for(kind)` allows - `n <= cap` returns
    None (nothing to split). The number of parts needed to keep every part
    at or under the cap is computed directly (never guessed or hardcoded to
    2), then capped at `_MAX_SPLIT_PARTS` - beyond that, None: better to
    fall through to today's existing withhold than ship a chain of parts
    this is no longer confident is still "one diagram that just needed
    splitting" rather than mis-structured content."""
    kind = spec.normalised_kind()
    if kind not in _LINEAR_SPLITTABLE_KINDS:
        return None
    nodes = [node for node in spec.nodes if node.label.strip()]
    n = len(nodes)
    cap = max_nodes_for(kind)
    if n <= cap:
        return None
    node_ids = [node.id for node in nodes]
    if len(set(node_ids)) != n or not all(node_ids):
        return None  # blank/duplicate ids can't be safely keyed by position
    required_pairs = [(node_ids[i], node_ids[i + 1]) for i in range(n - 1)]
    edge_pairs = [(edge.source, edge.target) for edge in spec.edges]
    if len(edge_pairs) != len(required_pairs) or set(edge_pairs) != set(required_pairs):
        return None  # a branch, merge, or missing/extra edge - not a pure chain
    edge_by_pair = {pair: edge for pair, edge in zip(edge_pairs, spec.edges)}

    # Each part shares one boundary node with its neighbour(s), so `k` parts
    # together cover k * (cap - 1) + 1 nodes at most - the smallest `k` that
    # can fit all `n` nodes under that bound.
    total_edges = n - 1
    part_count = -(-total_edges // (cap - 1))  # ceil(total_edges / (cap - 1))
    if part_count < 2 or part_count > _MAX_SPLIT_PARTS:
        return None

    base_edges, remainder = divmod(total_edges, part_count)
    title = spec.title.strip()
    parts: list[DiagramSpec] = []
    cursor = 0
    for index in range(part_count):
        edges_in_part = base_edges + (1 if index < remainder else 0)
        start, end = cursor, cursor + edges_in_part  # node index range [start, end] inclusive
        part_nodes = [node.model_copy(deep=True) for node in nodes[start : end + 1]]
        part_edges = [edge_by_pair[pair].model_copy(deep=True) for pair in required_pairs[start:end]]
        parts.append(spec.model_copy(deep=True, update={
            "nodes": part_nodes, "edges": part_edges,
            "title": (
                f"{title} (Part {index + 1} of {part_count})" if title else f"Part {index + 1} of {part_count}"
            ),
        }))
        cursor = end

    # Verify directly rather than trust the arithmetic above blindly (every
    # part must independently satisfy the exact bound this whole function
    # exists to guarantee) - a violation here means bail out to the ordinary
    # withhold path, never ship a part that itself still needs splitting.
    if any(not (2 <= len(part.nodes) <= cap) for part in parts):
        return None
    return parts


# Retry-before-withhold for `is_usable()`'s own per-kind requirements beyond
# node count (diagram quality audit follow-up): node count already gets a
# real retry chance via `needs_consolidation`/`_complexity_mismatch_issue`
# above BEFORE `is_usable()` is ever checked - but every OTHER per-kind
# requirement `DiagramSpec.is_usable()` enforces (concept_map/er_diagram/
# data_flow_diagram/circuit_schematic needing real edges, swimlane needing
# 2+ distinct lanes, venn needing exactly 2-3 nodes, iceberg needing both an
# above- and below-the-waterline node) got NONE - a model that produced,
# say, a concept_map with zero edges was withheld immediately with a
# generic "not enough usable structure" message, no specific diagnosis, no
# chance to fix that one named thing. This closes that gap the same way,
# folded into the SAME combined retry as every other structural check here
# - zero new AI call, just a specific feedback sentence added to whichever
# retry call was already going to happen (or triggering one on its own when
# nothing else would have).
def _usability_gap_feedback(spec: DiagramSpec) -> str | None:
    """`None` whenever `spec.is_usable()` already passes, the gap is the
    node-count cap (that already has its own dedicated feedback above, see
    `needs_consolidation`), or this kind has no extra diagnosable
    requirement beyond node count (most kinds - this only names the
    specific per-kind cases `is_usable()` itself checks, see that method's
    own per-kind branches, never guesses at a generic one)."""
    if spec.is_usable():
        return None
    kind = spec.normalised_kind()
    labelled = [n for n in spec.nodes if n.label.strip()]
    if not (2 <= len(labelled) <= max_nodes_for(kind)):
        return None  # the node-count cap already has its own dedicated retry feedback
    if kind == "concept_map":
        has_labelled_edge = any(e.label.strip() for e in spec.edges)
        if not (spec.edges and (has_labelled_edge or len(labelled) >= 3)):
            return (
                "This concept_map has no real relationships wired between its nodes - add at least "
                "one `edges` entry connecting two nodes, with a specific, labelled relationship type "
                "(not just a bare list of unconnected boxes)."
            )
    elif kind in ("er_diagram", "data_flow_diagram", "circuit_schematic"):
        if not spec.edges:
            return (
                f"This {kind} has nodes but no `edges` connecting them - a {kind} is exactly its "
                "connections; add the relationships/flows between the nodes you already have."
            )
    elif kind == "swimlane":
        lanes = {n.lane.strip() for n in spec.nodes if n.lane.strip()}
        if not spec.edges:
            return "This swimlane diagram has no `edges` showing work moving between steps - add them."
        if len(lanes) < 2:
            return (
                f"This swimlane diagram only has {len(lanes)} distinct `lane` value(s) set - "
                "swimlane's whole point is showing work crossing roles/actors, so set at least 2 "
                "distinct lane values across the nodes."
            )
    elif kind == "venn":
        if not (2 <= len(labelled) <= 3):
            return (
                f"A venn diagram only reads cleanly with 2 or 3 overlapping circles - this has "
                f"{len(labelled)} labelled nodes; consolidate or expand to exactly 2 or 3."
            )
    elif kind == "iceberg":
        if not (any(n.level == 0 for n in spec.nodes) and any(n.level >= 1 for n in spec.nodes)):
            return (
                "This iceberg diagram needs at least one node at level 0 (above the waterline, "
                "visible) AND at least one node at level >= 1 (below it, hidden) - set `level` on "
                "each node to show that split."
            )
    return None


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
        existing_outline: dict | None = None,
        template_audience: str = "",
        template_style: str = "",
    ) -> DiagramSpec:
        system = DIAGRAM_SYSTEM
        if pin_kind and kind_hint:
            # Cost optimisation only - opportunistic and best-effort, never a
            # hard requirement. `kind_hint` here isn't always a real,
            # trimmable DIAGRAM_KINDS value (e.g. the schematic-retry path's
            # own "schematic" placeholder when no real hint is set) - any
            # failure here must fall back to the full prompt, exactly as
            # reliable as before this change, never a new way for a diagram
            # request to fail outright.
            try:
                system = _build_trimmed_diagram_system(kind_hint)
            except Exception as exc:  # noqa: BLE001 - cost optimisation must never reduce reliability
                log.info(
                    "Could not build a trimmed DIAGRAM_SYSTEM for kind_hint=%r (%s) - "
                    "using the full prompt instead",
                    kind_hint, exc,
                )
        return await self.ai.structured(
            schema=DiagramSpec,
            system=system,
            user=_diagram_user_prompt(
                purpose=purpose,
                prompt=prompt,
                caption=caption,
                course_title=course_title,
                memory=memory,
                kind_hint=kind_hint,
                pin_kind=pin_kind,
                qa_feedback=qa_feedback,
                existing_outline=existing_outline,
                template_audience=template_audience,
                template_style=template_style,
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
        template_audience: str = "",
        template_style: str = "",
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
                template_audience=template_audience, template_style=template_style,
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
        source_excerpt: str = "",
        cited_source_titles: Sequence[str] = (),
    ) -> bool:
        content = block.content
        purpose = str(content.get("purpose") or "")
        prompt = str(content.get("prompt") or "")
        caption = str(content.get("caption") or "")
        kind_hint = str(content.get("diagram_kind") or "").strip().lower().replace(" ", "_")
        # Audience/style propagation fix: the uploaded template's own stated
        # audience/image_guidance, threaded through every `_request_spec`
        # call below (initial + every retry) - never a hardcoded audience
        # branch, just the template author's own free text passed through.
        template_audience = getattr(template, "audience", "") or ""
        template_style = getattr(template, "image_guidance", "") or ""
        if not (purpose.strip() or prompt.strip()):
            log.warning(
                "Diagram block %s has no purpose/prompt - withholding rather than falling back to "
                "an unrelated AI illustration", block.id,
            )
            _withhold_structured_diagram(
                block, detail="This diagram block has no purpose or prompt text to generate from."
            )
            return True

        # Root-cause fix (Phase 2 card-grid audit): `schematic` is PERMANENTLY
        # RETIRED at this layer - DIAGRAM_SYSTEM above explicitly instructs
        # the model to never produce it, "regardless of what the brief
        # describes". A stale `diagram_kind: "schematic"` hint (written by an
        # older/non-compliant writer pass - see app.agents.prompts's own
        # "PERMANENTLY RETIRED... go straight to illustration" guidance,
        # which assumes the writer never reaches this call with it) used to
        # be sent through to `_request_spec` anyway, where the model - torn
        # between "REQUESTED DIAGRAM TYPE: schematic" and "never produce
        # schematic" - would pick some OTHER kind as its best guess, almost
        # always "comparison" (the one kind generic enough to describe
        # anything as a flat list of things). `_kind_conflicts` used to miss
        # this substitution entirely (see its own comment on the fix), and
        # even with that fixed, retrying a "schematic" hint just repeats the
        # exact same contradiction. A real, confirmed case: a bench-wiring
        # apparatus request (DC rail, shunt resistor, Kelvin sense taps,
        # oscilloscope channels) shipped as 3 dense text cards instead of a
        # labelled picture. Short-circuiting here - before any model call -
        # and setting `illustration_style: "textbook"` directly on the block
        # is what actually delivers the "goes straight to illustration"
        # behaviour DIAGRAM_SYSTEM's own comment already claimed happened.
        # Best-effort label recovery: the original diagram-authoring prompt
        # routinely names exact quoted labels ('label "R_shunt (low-side
        # shunt)"') - pulling those into `expected_labels` lets the raster
        # illustration path's existing text-verification machinery
        # (ImageService._generate_with_text_check) actually confirm this
        # picture's labels are legible and correct, instead of shipping an
        # unverified wall of text baked into pixels on faith. Bounded (at
        # most 10, under 40 chars each) so a prose brief with many
        # incidental quote marks can't flood expected_labels with junk.
        if kind_hint == "schematic":
            brief_text = f"{purpose}\n{prompt}\n{caption}"
            # Authors quote labels with either mark inconsistently (confirmed
            # across real blocks: one used `"Kelvin sense+: S+"`, another
            # `'C = 100 μF'`) - matching both, not just double quotes, is
            # part of what makes this recovery reliable. The OTHER half:
            # only a quote preceded by some form of "label" within the
            # nearby text counts - a real, confirmed case shows why a bare
            # "any quoted substring" scan is too eager: one brief's prompt
            # used quotes loosely for descriptive emphasis ('slow droop -
            # fit tau to measured data'), not as a literal on-image text
            # requirement, and treating that full clause as a REQUIRED exact
            # label sent an impossible-to-render phrase through text
            # verification, which correctly (if unhelpfully) withheld the
            # image rather than ship it wrong - a worse outcome than simply
            # never having claimed that phrase as a label in the first
            # place. Requiring a nearby "label(ed/led)" cue is what the
            # bench-wiring case (the confirmed, fixed report) actually relied
            # on throughout ('label "...""', 'labeled exactly "..."') and
            # correctly excludes the looser, advisory-only quoting style.
            label_cue = re.compile(r"labell?ed?", re.IGNORECASE)
            quoted = [
                m.group(1)
                for m in re.finditer(r'["\']([^"\']{1,40})["\']', brief_text)
                if label_cue.search(brief_text[max(0, m.start() - 30):m.start()])
            ]
            seen: list[str] = []
            for label in quoted:
                label = label.strip()
                if label and label not in seen:
                    seen.append(label)
                if len(seen) >= 10:
                    break
            log.info(
                "Diagram block %s requested the retired 'schematic' kind - routing directly to a "
                "textbook illustration instead of asking the model for a kind it's told never to "
                "produce (%s label(s) recovered from the prompt)", block.id, len(seen),
            )
            block.content = merge_content(
                block.type, block.content,
                {
                    # Reclassified for good, not just this one render - a
                    # LATER regeneration (e.g. a "Replace Image" click) must
                    # land straight on the illustration path next time too,
                    # never loop back through this same retired-kind
                    # contradiction again. Harmless for THIS call: ImageService
                    # .generate_for_block already captured its own `kind`
                    # local variable before invoking this method, so this
                    # mutation can't change which branch the CURRENT call
                    # takes (that's `illustration_style`'s job, checked
                    # separately after this returns - see its own comment
                    # there).
                    "kind": "illustration",
                    "diagram_kind": "",
                    "illustration_style": "textbook",
                    **({"expected_labels": seen} if seen else {}),
                },
            )
            return False

        # A real, confirmed bug this closes: `replace_image` (the editor's
        # "fix this diagram" workflow - see app.course.document.patcher's
        # own replace_image handling) clears `content["path"]` so this
        # block gets regenerated, but NEVER cleared `content["spec"]` - the
        # existing, already-rendered structure just sat there unused while
        # this call built a request from `purpose`/`prompt`/`caption`
        # ALONE, with zero awareness the old diagram (its exact nodes,
        # edges and labels) ever existed. The model then had no choice but
        # to invent a brand-new diagram from the editor's text description,
        # which is why a "fix the overlapping numbers" correction came back
        # as a different diagram instead of the same one with that one
        # thing fixed. Feeding the existing structure back in (see
        # DIAGRAM_SYSTEM's own "CURRENT DIAGRAM" instructions on what this
        # means) turns this into a genuine correction - a fresh first-time
        # generation (no `spec` in content yet) is completely unaffected,
        # `existing_outline` is simply empty/falsy for it.
        existing_outline: dict | None = None
        raw_existing_spec = content.get("spec")
        if isinstance(raw_existing_spec, dict) and raw_existing_spec:
            try:
                existing_outline = diagram_outline(DiagramSpec.model_validate(raw_existing_spec))
            except Exception as exc:  # noqa: BLE001 - a malformed old spec must never block regeneration
                log.warning("Could not reuse the existing spec for %s as correction context: %s", block.id, exc)

        memory = await self.memory.build_context(
            stage="visual", course_title=course_title, chapter_title=purpose or prompt, template=template
        )
        memory_text = memory.render()

        try:
            spec = await self._request_spec(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory=memory_text, kind_hint=kind_hint, pin_kind=False, block_id=block.id,
                existing_outline=existing_outline,
                template_audience=template_audience, template_style=template_style,
            )
        except Exception as exc:  # noqa: BLE001 - withhold rather than fall back to illustration
            log.warning("Diagram spec generation failed for %s: %s", block.id, exc)
            _withhold_structured_diagram(block, detail=f"Diagram generation failed: {str(exc)[:200]}")
            return True

        spec = self._normalise(spec)

        # Five independent structural checks against the SAME initial spec -
        # too many nodes, too many edges, verbose/metadata-cluttered
        # relationship labels, the wrong shape, and (for a flow chart) broken
        # Start/End connectivity. These used to each fire their own
        # sequential corrective retry (up to 3 extra AI calls before content
        # QA even ran); now every issue found is collected up front and
        # folded into a SINGLE retry call, cutting the worst case from 3
        # extra calls to 1 without dropping any of the checks (the two
        # newest - edge count and label verbosity - follow the exact same
        # "collect, don't retry individually" discipline the first three
        # already established).
        # Scales the node/edge ceilings and the relationship-label word cap
        # to this course's own stated audience - see audience_multiplier's
        # own docstring for the gap this closes (audience adaptation used
        # to be prompt-only, no code backstop). 1.0 (every ceiling below
        # exactly as before) whenever the audience text isn't clearly
        # young/beginner or advanced.
        audience_scale = audience_multiplier(template_audience)
        labelled_count = len([n for n in spec.nodes if n.label.strip()])
        cap = max(2, round(max_nodes_for(spec.normalised_kind()) * audience_scale))
        needs_consolidation = labelled_count > cap
        # "Not every relationship deserves a visible arrow" - scoped to the
        # kinds whose own prompt section invites a rich web of labelled
        # relationships (concept_map/er_diagram/network_mesh); every other
        # kind's own shape already bounds its edge count, so max_edges_for
        # returns an effectively-unbounded ceiling there and this never
        # fires. See max_edges_for's own docstring (schemas/diagram.py).
        edge_count = len(spec.edges)
        edge_cap = max(2, round(max_edges_for(spec.normalised_kind(), labelled_count) * audience_scale))
        needs_edge_consolidation = edge_count > edge_cap
        # The real, confirmed failure mode this whole fix exists for: a
        # relationship label built by concatenating a relationship name with
        # provenance/confidence/metadata markers (e.g. "raised by · prov ·
        # conf") instead of one short natural phrase. See
        # edge_label_verbosity_issues's own docstring. A young/beginner
        # audience gets a tighter word cap (3, not 4) - never looser for an
        # advanced one, since conciseness is the right default everywhere.
        label_max_words = 3 if audience_scale < 0.8 else _MAX_RELATIONSHIP_LABEL_WORDS
        label_issues = edge_label_verbosity_issues(spec, max_words=label_max_words)
        needs_label_rewrite = bool(label_issues)
        # The writer asked for a specific shape (e.g. a relationship diagram)
        # but the model reached for a sequential one anyway.
        needs_kind_correction = _kind_conflicts(kind_hint, spec.normalised_kind())
        # A bounded procedure needs exactly one Start and one shared End -
        # a real, confirmed case: a spec with three separate terminal nodes
        # (Close, Escalate, Keep Task) none of which pointed at a single
        # shared End left one path with nothing to connect to.
        connectivity_issues = _flow_chart_connectivity_issues(spec)
        # Density-management follow-up (Phase 4/6): a sixth, independent
        # structural check - total text volume SUBSTANTIALLY over the rough
        # per-node guide (see `_needs_density_retry`'s own comment on why
        # this needs a materially higher bar than the advisory-only finding
        # below uses). Folds into the exact same combined retry as the
        # other five, never a separate AI call of its own.
        needs_density_retry = _needs_density_retry(spec)
        # Pre-generation planning follow-up (Phase 2/6): a seventh,
        # independent structural check - the model's OWN stated
        # `complexity_estimate` disagreeing with what it actually produced.
        # Same combined retry, never a separate AI call.
        complexity_mismatch = _complexity_mismatch_issue(spec)
        # Carries over into the final, successful save below (see its own
        # merge_content call) ONLY if the retry fails to resolve them -
        # starts at whatever the first attempt found; replaced with [] the
        # moment a clean corrected spec is accepted, so a since-resolved
        # problem is never persisted as if it still shipped. render_diagram_
        # svg's own connectivity repairs still guarantee no REAL edge is
        # ever silently dropped even if a persistent issue ships anyway.
        unresolved_connectivity_issues = connectivity_issues
        # Same "persist only if still unresolved after the retry" contract
        # as connectivity above - label verbosity/edge count are "cosmetic"
        # (never withheld, see _CRITICAL_RENDER_ISSUE_KINDS below), so a
        # diagram that still fails after the one retry ships anyway, flagged.
        unresolved_label_issues = label_issues
        unresolved_edge_count_issue = needs_edge_consolidation
        # Retry-before-withhold follow-up: an eighth, independent structural
        # check - see `_usability_gap_feedback`'s own docstring. Same
        # combined retry, never a separate AI call.
        usability_gap_feedback = _usability_gap_feedback(spec)

        if (
            needs_consolidation or needs_edge_consolidation or needs_label_rewrite
            or needs_kind_correction or connectivity_issues or needs_density_retry
            or complexity_mismatch or usability_gap_feedback
        ):
            feedback_parts: list[str] = []
            retry_kind_hint = spec.normalised_kind()
            if needs_consolidation:
                feedback_parts.append(
                    f"Your previous attempt had {labelled_count} nodes, more than a "
                    f"{spec.normalised_kind()} diagram can clearly show (maximum {cap}). "
                    "Consolidate closely related steps so the total is at most "
                    f"{cap} nodes, while keeping the sequence faithful to the brief - "
                    "merge an action with its immediate follow-up rather than dropping "
                    "any genuinely distinct stage."
                )
            if needs_edge_consolidation:
                feedback_parts.append(
                    f"Your previous attempt had {edge_count} relationships/edges, more than this "
                    f"diagram can clearly show (maximum {edge_cap}). Not every relationship deserves "
                    "a visible arrow - keep only the ones most essential to the brief, and group, "
                    "merge or omit the rest rather than drawing every possible connection."
                )
            if needs_label_rewrite:
                feedback_parts.append(
                    "Some relationship labels were too long or read as a metadata-tag cluster rather "
                    "than a natural phrase: " + "; ".join(label_issues) + ". Rewrite each as a short "
                    "(1-4 word) natural phrase answering 'what does this connection mean' (e.g. "
                    "'depends on', 'generates', 'references') - never repeat provenance, confidence, "
                    "timestamps or IDs on the label itself; move that detail to an attribute node or "
                    "leave it out entirely."
                )
            if needs_kind_correction:
                feedback_parts.append(
                    f"You produced a '{spec.normalised_kind()}' diagram, but this brief needs "
                    f"a '{kind_hint}' one - use that shape instead."
                )
                retry_kind_hint = kind_hint
            if connectivity_issues:
                feedback_parts.append(
                    "The rendered flowchart had structural problems: " + "; ".join(connectivity_issues) + ". "
                    "Every node except the single Start must have at least one real incoming edge; every "
                    "node except the single End must have at least one real outgoing edge (or loop back to "
                    "an earlier step). Route every terminal outcome (closing, escalating, keeping, etc.) "
                    "to the SAME single End node rather than leaving it as its own separate dead end."
                )
                if not needs_kind_correction:
                    retry_kind_hint = "flow_chart"
            if needs_density_retry:
                feedback_parts.append(
                    "This diagram's total text (across every label/detail/edge-label combined) is "
                    "substantially more than one picture can show clearly, even though no single "
                    "label is individually too long. Shorten wording throughout, drop any detail "
                    "that repeats what another node already says, and consider consolidating "
                    "closely related nodes - the diagram as a whole needs to read as noticeably "
                    "lighter, not just one or two labels trimmed."
                )
            if complexity_mismatch:
                feedback_parts.append(complexity_mismatch)
            if usability_gap_feedback:
                feedback_parts.append(usability_gap_feedback)
            log.info(
                "Diagram for %s needs correction (consolidation=%s, edges=%s, labels=%s, kind=%s, "
                "connectivity=%s, density=%s, complexity_mismatch=%s, usability_gap=%s) - retrying "
                "once with combined feedback",
                block.id, needs_consolidation, needs_edge_consolidation, needs_label_rewrite,
                needs_kind_correction, bool(connectivity_issues), needs_density_retry,
                bool(complexity_mismatch), bool(usability_gap_feedback),
            )
            try:
                corrected = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint=retry_kind_hint, pin_kind=True, block_id=block.id,
                    qa_feedback=" ".join(feedback_parts), existing_outline=existing_outline,
                    template_audience=template_audience, template_style=template_style,
                )
                corrected = self._normalise(corrected)
                corrected_count = len([n for n in corrected.nodes if n.label.strip()])
                kind_ok = not needs_kind_correction or not _kind_conflicts(kind_hint, corrected.normalised_kind())
                count_ok = not needs_consolidation or corrected_count <= cap
                if kind_ok and count_ok:
                    spec = corrected
                    unresolved_connectivity_issues = (
                        _flow_chart_connectivity_issues(corrected) if connectivity_issues else []
                    )
                    # Label/edge-count issues are never part of the
                    # accept/reject gate above (kind_ok/count_ok) - they're
                    # cosmetic, same as connectivity, so the corrected spec
                    # is always accepted once the GATING checks pass, and
                    # these are simply re-measured against whatever actually
                    # shipped, exactly like unresolved_connectivity_issues.
                    unresolved_label_issues = (
                        edge_label_verbosity_issues(corrected, max_words=label_max_words)
                        if needs_label_rewrite else []
                    )
                    unresolved_edge_count_issue = (
                        len(corrected.edges)
                        > max(2, round(max_edges_for(corrected.normalised_kind(), corrected_count) * audience_scale))
                        if needs_edge_consolidation else False
                    )
                # Otherwise keep the first spec rather than looping further -
                # is_usable() and render_diagram_svg's own repairs still get
                # the final say, same safety net as before.
            except Exception as exc:  # noqa: BLE001 - keep the first spec, don't fail the block
                log.warning("Diagram corrective retry failed for %s: %s", block.id, exc)

        pending_split_payloads: list[dict] = []
        if not spec.is_usable():
            parts = _linear_chain_split(spec)
            rendered_later_parts: list[tuple[DiagramSpec, bytes, int, int]] = []
            if parts is not None:
                try:
                    for later_part in parts[1:]:
                        later_svg, later_width, later_height = render_diagram_svg(later_part, template.theme)
                        # Every part after the first never goes through this
                        # function's own downstream render-QA retry (each is
                        # inserted as its own block by a later document-
                        # level pass, see `app.course.document.diagram_
                        # splitting`) - so a critical defect must be caught
                        # HERE, before committing to the split, or it would
                        # ship unchecked. The first part keeps exactly its
                        # normal QA treatment below (it's simply `spec` from
                        # here on).
                        if any(
                            issue.kind in _CRITICAL_RENDER_ISSUE_KINDS
                            for issue in validate_rendered_svg(later_svg, kind=later_part.normalised_kind())
                        ):
                            parts = None
                            break
                        rendered_later_parts.append((later_part, later_svg, later_width, later_height))
                except Exception as exc:  # noqa: BLE001 - fall through to the ordinary withhold path below
                    log.warning("Diagram split render failed for %s: %s", block.id, exc)
                    parts = None
            if parts is not None:
                log.info(
                    "Diagram for %s had %s labelled nodes, over the %s-node cap for %s - splitting "
                    "into %s linked diagrams instead of withholding",
                    block.id, len([n for n in spec.nodes if n.label.strip()]),
                    max_nodes_for(spec.normalised_kind()), spec.normalised_kind(), len(parts),
                )
                spec = parts[0]
                pending_split_payloads = [
                    {
                        "spec": later_part.model_dump(mode="json"),
                        "svg_base64": base64.b64encode(later_svg).decode("ascii"),
                        "width": later_width,
                        "height": later_height,
                    }
                    for later_part, later_svg, later_width, later_height in rendered_later_parts
                ]
            else:
                log.info(
                    "Diagram spec for %s (kind=%s) is not usable (%s nodes, %s edges) - withholding "
                    "rather than falling back to an unrelated AI illustration",
                    block.id, spec.normalised_kind(), len(spec.nodes), len(spec.edges),
                )
                _withhold_structured_diagram(
                    block,
                    detail=(
                        f"The generated {spec.normalised_kind()} diagram did not have enough usable "
                        "structure (labelled nodes/edges) to render."
                    ),
                )
                return True

        # Structural usability (right number of labelled nodes/edges) says
        # nothing about whether the content is actually ABOUT the brief -
        # the gap behind "a flowchart with boxes but no meaningful
        # information". Bounded content-relevance retries, same shape as
        # every other QA step here. A persistent GARBLED-TEXT failure is
        # withheld rather than shipped unreadable or swapped for an
        # unrelated illustration (visual-generation policy fix - see the
        # garbled-text check right after this retry loop) - a persistent
        # off-topic/generic/missing-concept/confirmed-contradiction failure
        # ships the best attempt anyway, flagged for review (fallback-
        # cascade root-cause fix - see this module's own docstring), since
        # the retry loop below already always keeps the single best spec
        # seen across every attempt.
        #
        # Up to _MAX_CONTENT_QA_RETRIES attempts (not just one): a real,
        # confirmed case showed a SINGLE retry wasn't always enough for the
        # model to actually fold in a specific missing concept (e.g. "show
        # an explicit frame-conversion/rotation step") even when told
        # exactly what was missing - the diagram fell back to the generic,
        # unlabelled illustration path every time even though a second
        # attempt, with the SAME feedback, often does land it. Still bounded
        # (never unlimited) and still feeds back the latest failure's exact
        # reasons each time, same as the one-retry version did.
        brief = "\n".join(part for part in (purpose, prompt, caption) if part)[:MAX_BRIEF_CHARS]
        content_result = await evaluate_content(
            spec, brief=brief, course_title=course_title, ai=self.ai, settings=self.settings,
            source_excerpt=source_excerpt, cited_source_titles=cited_source_titles,
            template_audience=template_audience,
        )
        # `spec`/`content_result` always track the BEST attempt seen so far
        # (never regress to a worse one at the end), but the retry loop
        # itself keeps spending its bounded budget off the MOST RECENT
        # attempt's own feedback even when an attempt didn't improve on the
        # issue count - a later retry can still fix a *different* instance
        # of the same issue type (e.g. the missing concept's wording) that a
        # raw count comparison can't see.
        latest_result = content_result
        for _ in range(_MAX_CONTENT_QA_RETRIES):
            if latest_result.passed:
                break
            log.info(
                "Diagram content QA failed for %s (%s) - retrying with targeted feedback",
                block.id, [issue.reason for issue in latest_result.issues],
            )
            try:
                retried = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint=spec.normalised_kind(), pin_kind=True, block_id=block.id,
                    qa_feedback=(
                        "The previous attempt's content was rejected for these reasons: "
                        + "; ".join(issue.detail for issue in latest_result.issues)
                        + ". Make every label specific and genuinely about this exact brief - "
                        "never a generic placeholder - and cover every concept the brief named."
                    ),
                    existing_outline=existing_outline,
                    template_audience=template_audience, template_style=template_style,
                )
                retried = self._normalise(retried)
                if retried.normalised_kind() != spec.normalised_kind() or not retried.is_usable():
                    break
                retried_result = await evaluate_content(
                    retried, brief=brief, course_title=course_title, ai=self.ai, settings=self.settings,
                    source_excerpt=source_excerpt, cited_source_titles=cited_source_titles,
                    template_audience=template_audience,
                )
                latest_result = retried_result
                if retried_result.passed or len(retried_result.issues) < len(content_result.issues):
                    spec, content_result = retried, retried_result
            except Exception as exc:  # noqa: BLE001 - keep the best spec, don't fail the block
                log.warning("Diagram content-QA retry failed for %s: %s", block.id, exc)
                break

        # Fallback-cascade root-cause fix, refined by the visual-generation
        # policy fix: a diagram whose text is actually BROKEN (corrupted
        # homoglyphs, or an LLM-confirmed misspelled/scrambled label) is
        # never shippable regardless of how on-topic it otherwise is - that
        # now withholds the image entirely rather than falling back to an
        # unrelated illustration (a structured diagram kind must never
        # become raster art). A CONFIRMED contradiction with the course's
        # own source text (`contradicts_source_text`/`numeric_contradicts_
        # source_text`/`identifier_value_contradicts_source_text`) is a
        # genuinely different case from broken text: the diagram itself is
        # still legible and structurally sound, it asserts something
        # questionable - so it now joins the "ship anyway, flagged" bucket
        # below with every other content-QA issue (off_topic/generic_
        # content/missing_key_concept/a claim the source excerpt simply
        # doesn't address either way), all of which are judgment calls
        # about how WELL the diagram covers the brief, not a claim its
        # real, legible labels are unreadable - see this module's own
        # docstring for why shipping that anyway, flagged for review, beats
        # discarding a correctly-shaped diagram for an unrelated
        # illustration.
        content_qa_unresolved: list[dict] = []
        if not content_result.passed:
            # Visual-generation policy fix: a structured diagram kind must
            # NEVER fall back to an unrelated AI illustration, even when its
            # text is confirmed garbled/misspelled after every bounded
            # retry. Garbled/misspelled labels make the diagram itself
            # genuinely unreadable (the same bar `is_usable()` already
            # applies to structure), so this withholds the image entirely -
            # the same treatment the raster-illustration path already gives
            # "garbled text", and the honest alternative to either shipping
            # unreadable labels or swapping in an unrelated picture.
            # `CITATION_LEAKED_INTO_LABEL` (real production case: a citation
            # tag like "[G2.7]" baked into a node label) joins this same
            # hard-blocking set rather than the "ship anyway, flagged"
            # bucket below - it is a confirmed, deterministic content defect
            # (a diagram describing which document a fact came from, not
            # the subject matter itself), not a judgment call about how WELL
            # the diagram covers its brief.
            garbled_types = {STRUCTURALLY_GARBLED_TEXT, MISSPELLED_OR_GARBLED_TEXT, CITATION_LEAKED_INTO_LABEL}
            if any(issue.reason in garbled_types for issue in content_result.issues):
                log.info(
                    "Diagram for %s still has garbled/misspelled/citation-leaked text after %s retries - "
                    "withholding rather than falling back to an unrelated AI illustration",
                    block.id, _MAX_CONTENT_QA_RETRIES,
                )
                garbled_findings = visual_qa_finding_entries(
                    [
                        (issue.reason, issue.detail, issue.deterministic)
                        for issue in content_result.issues
                        if issue.reason in garbled_types
                    ],
                    resolution="image_withheld_after_qa_failure",
                )
                _withhold_structured_diagram(
                    block,
                    detail="; ".join(f["detail"] for f in garbled_findings)[:300],
                    extra_findings=garbled_findings,
                )
                return True
            log.info(
                "Diagram for %s still has content-coverage or source-consistency issues after %s "
                "retries (%s) - shipping the best attempt anyway, flagged for review, rather than "
                "falling back to an unrelated AI illustration",
                block.id, _MAX_CONTENT_QA_RETRIES, [issue.reason for issue in content_result.issues],
            )
            # Split out of the general "ship anyway, flagged" bucket below:
            # a claim the source excerpt doesn't address (ambiguous, not
            # contradicted) is a genuinely different, less-severe case that
            # must route to `app.services.quality_framework`'s existing
            # `review_findings` bucket specifically (the same
            # `severity="blocker"` + `verification_status="unverified"`
            # combination that field already defines). Every other
            # content-QA issue - including a CONFIRMED source-text
            # contradiction (`contradicts_source_text`/`numeric_contradicts_
            # source_text`/`identifier_value_contradicts_source_text`), which
            # used to fall back to illustration above - is a legible,
            # structurally sound diagram whose CONTENT is in question, not
            # its readability; it ships anyway, flagged `major`
            # (`rendered_with_unresolved_issues`), never discarded for an
            # unrelated illustration (visual-generation policy fix: never
            # fall back for a structured diagram kind).
            grounding_issues = [i for i in content_result.issues if i.reason == SOURCE_GROUNDING_UNSUPPORTED]
            other_issues = [i for i in content_result.issues if i.reason != SOURCE_GROUNDING_UNSUPPORTED]
            content_qa_unresolved = visual_qa_finding_entries(
                [(issue.reason, issue.detail, issue.deterministic) for issue in other_issues],
                resolution="rendered_with_unresolved_issues",
            ) + visual_qa_finding_entries(
                [(issue.reason, issue.detail, issue.deterministic) for issue in grounding_issues],
                resolution="source_grounding_unsupported",
            )

        if spec.normalised_kind() == "schematic":
            spec = await self._resolve_and_check_schematic(
                spec, purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory_text=memory_text, kind_hint=kind_hint, block_id=block.id,
                template_audience=template_audience, template_style=template_style,
            )

        spec = _with_figure_caption(spec, block)
        try:
            svg_bytes, width, height = render_diagram_svg(spec, template.theme)
        except Exception as exc:  # noqa: BLE001 - withhold rather than fall back to illustration
            log.warning("Diagram rendering failed for %s: %s", block.id, exc)
            _withhold_structured_diagram(block, detail=f"Diagram rendering failed: {str(exc)[:200]}")
            return True

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
        #
        # Graph is exempt too: this check's overlap/clipping/"shared edge
        # segment" heuristics are built around discrete node boxes and the
        # arrows connecting them - a plotted axis/curve/bar has none of
        # those, and a confirmed real case showed it flagging a plotted
        # function's own curve as a "shared edge segment" with the x-axis
        # line, falling the graph back to an unreliable raster illustration
        # for no real defect. Graph gets its own geometry discipline
        # instead (real auto-computed axis ranges, nice_ticks spacing) - see
        # app.render.diagram_renderer._layout_graph.
        #
        # flow_chart/process/cycle/concept_map/conceptual are NOT exempt -
        # an earlier version of this comment claimed `_shape_bbox` couldn't
        # recognise the new ring/wheel rendering's `<circle>` node shapes;
        # that was never true (diagram_render_qa.py's `_shape_bbox` already
        # handled `circle`/`ellipse`/`polygon`, not just `rect`) - the two
        # REAL gaps were `text_overflow_issues` assuming left-anchored
        # text (every ring/wheel label uses `text-anchor="middle"`,
        # double-counting half the text as overflow) and
        # `readability_issues` hardcoding the old box renderer's
        # LABEL_SIZE instead of RING_LABEL_SIZE, the new, smaller size
        # these kinds actually use - both fixed directly in
        # diagram_render_qa.py, confirmed against real ring/wheel output
        # (app/render/diagram_renderer.py's own "renders_clean" test
        # suite). Real QA coverage for these kinds is worth having - it
        # catches a genuinely broken diagram the same way it always did
        # for the box-based kinds.
        # puzzle_pieces is exempt too: its pieces are drawn as `<path>`
        # shapes (diagram_render_qa._shape_bbox has no notion of a path's
        # own bounding box, so they're silently invisible to the overlap
        # check either way) that DELIBERATELY interlock - each piece's tab
        # bulges a few px into its neighbour's notch, by construction, the
        # entire point of the shape. A bbox-overlap check built for
        # discrete, non-touching node boxes would misread that intentional
        # interlock as a collision defect; the pieces' own fixed,
        # non-overlapping grid spacing (see _layout_puzzle_pieces) is
        # already what guarantees they never ACTUALLY collide.
        _exempt_kinds = ("schematic", "graph", "puzzle_pieces")
        issues = [] if spec.normalised_kind() in _exempt_kinds else validate_rendered_svg(svg_bytes, kind=spec.normalised_kind())
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
                    existing_outline=existing_outline,
                    template_audience=template_audience, template_style=template_style,
                )
                retried = self._normalise(retried)
                if retried.normalised_kind() == spec.normalised_kind() and retried.is_usable():
                    retried = _with_figure_caption(retried, block)
                    retried_svg, retried_w, retried_h = render_diagram_svg(retried, template.theme)
                    retried_issues = validate_rendered_svg(retried_svg, kind=retried.normalised_kind())
                    if len(retried_issues) < len(issues):
                        spec, svg_bytes, width, height, issues = retried, retried_svg, retried_w, retried_h, retried_issues
            except Exception as exc:  # noqa: BLE001 - keep the first render, don't fail the block
                log.warning("Diagram render-QA retry failed for %s: %s", block.id, exc)

        # Conditional-shipping policy fix: split the remaining render-QA
        # issues (if any survived the corrective retry above) into critical
        # (never shippable - see `_CRITICAL_RENDER_ISSUE_KINDS`'s own
        # comment) and cosmetic. Critical issues withhold, the same
        # treatment `is_usable()`/confirmed-garbled-text already get -
        # structurally unable to be downgraded to a quality flag.
        critical_render_issues = [issue for issue in issues if issue.kind in _CRITICAL_RENDER_ISSUE_KINDS]
        issues = [issue for issue in issues if issue.kind not in _CRITICAL_RENDER_ISSUE_KINDS]
        if critical_render_issues:
            # Universal kind-fallback safety net: this kind's own layout
            # geometry failed CRITICALLY twice in a row now (the initial
            # render, then the targeted same-kind retry just above) - a
            # real, confirmed case this closes (a decision_tree whose
            # level-skipping branch shape deterministically crossed a
            # sibling node, however the labels/structure were reworded)
            # showed this is sometimes a genuine bug in ONE kind's own
            # layout code, not a content problem retrying the SAME kind
            # again could ever fix - the model has no way to predict which
            # structure will trigger a specific kind's geometry bug ahead
            # of time. Rather than audit every one of this module's ~49
            # layout algorithms for this class of bug (a far bigger,
            # separate undertaking), this gives the model ONE more attempt
            # with a DIFFERENT kind for the exact same content before
            # withholding - the same content expressed through a different
            # structural shape, which sidesteps whichever kind's layout
            # code was the actual problem, universally, regardless of
            # which kind that turns out to be. Only adopted if it's a
            # genuinely different kind (never trusted if the model ignored
            # the instruction) AND renders with no critical issue of its
            # own; otherwise this is a no-op and withholding proceeds
            # exactly as before.
            try:
                fallback_spec = await self._request_spec(
                    purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                    memory=memory_text, kind_hint="", pin_kind=False, block_id=block.id,
                    qa_feedback=(
                        f"The '{spec.normalised_kind()}' diagram kind produced a layout defect "
                        "rendering this content that a targeted retry could not fix ("
                        + ", ".join(sorted({issue.kind for issue in critical_render_issues}))
                        + f"). Pick a DIFFERENT diagram kind for this same content - never "
                        f"'{spec.normalised_kind()}' again."
                    ),
                    existing_outline=existing_outline,
                    template_audience=template_audience, template_style=template_style,
                )
                fallback_spec = self._normalise(fallback_spec)
                if fallback_spec.normalised_kind() != spec.normalised_kind() and fallback_spec.is_usable():
                    fallback_spec = _with_figure_caption(fallback_spec, block)
                    fallback_svg, fallback_w, fallback_h = render_diagram_svg(fallback_spec, template.theme)
                    fallback_issues = (
                        [] if fallback_spec.normalised_kind() in _exempt_kinds
                        else validate_rendered_svg(fallback_svg, kind=fallback_spec.normalised_kind())
                    )
                    fallback_critical = [i for i in fallback_issues if i.kind in _CRITICAL_RENDER_ISSUE_KINDS]
                    if not fallback_critical:
                        log.info(
                            "Diagram for %s recovered via a different kind (%s -> %s) after a critical "
                            "rendered-geometry issue", block.id, spec.normalised_kind(), fallback_spec.normalised_kind(),
                        )
                        spec, svg_bytes, width, height = fallback_spec, fallback_svg, fallback_w, fallback_h
                        issues = [i for i in fallback_issues if i.kind not in _CRITICAL_RENDER_ISSUE_KINDS]
                        critical_render_issues = []
            except Exception as exc:  # noqa: BLE001 - keep the original failure, don't fail the block
                log.warning("Diagram kind-fallback retry failed for %s: %s", block.id, exc)

        if critical_render_issues:
            log.info(
                "Diagram for %s still has a critical rendered-geometry issue after retry (%s) - "
                "withholding rather than shipping a diagram that could mislead about what connects "
                "to what, or publishing unverifiable/invisible content",
                block.id, [issue.kind for issue in critical_render_issues],
            )
            critical_render_findings = visual_qa_finding_entries(
                [(issue.kind, issue.detail, True) for issue in critical_render_issues],
                resolution="image_withheld_after_qa_failure",
            )
            _withhold_structured_diagram(
                block,
                detail="; ".join(f["detail"] for f in critical_render_findings)[:300],
                extra_findings=critical_render_findings,
            )
            return True
        # Visual-generation policy fix: a structured diagram kind must never
        # fall back to an unrelated AI illustration, even when its rendered
        # geometry still has COSMETIC issues after one corrective retry (an
        # edge sharing a segment with another, a duplicate arrowhead, an
        # overlapping label). The diagram is still a real, correctly-shaped,
        # legible answer to the brief - a layout imperfection, not a reason
        # to discard it for a textless, unrelated picture - so it ships
        # anyway, flagged `major` (`rendered_with_unresolved_issues`, same
        # bucket flow_chart-connectivity/drift findings already use below),
        # exactly the "ship anyway, flagged for review" treatment this
        # module already gives every other recoverable QA issue.
        if issues:
            log.info(
                "Diagram for %s still has %s cosmetic rendered layout issue(s) after retry - shipping "
                "anyway, flagged for review, rather than falling back to an unrelated AI illustration",
                block.id, len(issues),
            )

        # Course-wide recurring-concept consistency (diagram quality audit
        # follow-up, Phase 8): applied here, once, on whichever spec just
        # won (initial render, same-kind retry, or the kind-fallback) -
        # never on an intermediate candidate that might still be discarded.
        # Only actually re-renders (a cheap, deterministic, zero-AI-call
        # pass) when an icon genuinely needed overriding, so this is a true
        # no-op for the common case where nothing needed to change.
        if apply_diagram_consistency(course_id, spec):
            try:
                svg_bytes, width, height = render_diagram_svg(spec, template.theme)
            except Exception as exc:  # noqa: BLE001 - keep the pre-override render rather than fail the block
                log.warning("Diagram consistency re-render failed for %s: %s", block.id, exc)

        relative = self.storage.save_asset(course_id, svg_bytes, extension="svg")
        # See _correction_drift_issue's own docstring - a no-op ([], None)
        # whenever this generation wasn't a replace_image correction at all
        # (existing_outline unset), so a fresh first-time generation never
        # gets a finding here.
        drift_issue = _correction_drift_issue(existing_outline, spec)
        under_correction_issue = _under_correction_issue(raw_existing_spec, prompt, spec)
        findings = [("flow_chart_connectivity", detail, True) for detail in unresolved_connectivity_issues]
        # Same deterministic-finding contract as flow_chart_connectivity
        # above - edge_label_verbosity_issues/max_edges_for are pure string/
        # count checks, never an LLM judgment call.
        findings.extend(("edge_label_verbosity", detail, True) for detail in unresolved_label_issues)
        if unresolved_edge_count_issue:
            findings.append((
                "edge_count_exceeded",
                f"{len(spec.edges)} relationships/edges exceeds this diagram's own clarity ceiling.",
                True,
            ))
        # Advisory-only (see total_text_volume_issue's own docstring) -
        # measured against whatever spec actually shipped (post-retry, if
        # one ran), never gates the retry itself.
        text_volume_issue = total_text_volume_issue(spec)
        if text_volume_issue:
            log.info("Diagram for %s: %s", block.id, text_volume_issue)
            findings.append(("diagram_text_volume_advisory", text_volume_issue, True))
        # Same "measured fresh against whatever actually shipped" contract
        # as text_volume_issue just above.
        final_complexity_mismatch = _complexity_mismatch_issue(spec)
        if final_complexity_mismatch:
            findings.append(("diagram_complexity_mismatch", final_complexity_mismatch, True))
        # True (literal, quality-governance follow-up Phase 3.7): RenderQAIssue
        # (app.services.diagram_render_qa) is pure SVG/geometry parsing - no
        # AIClient import anywhere in that module, so every issue here is
        # genuinely deterministic, never an LLM judgment call.
        findings.extend((issue.kind, issue.detail, True) for issue in issues)
        if drift_issue:
            findings.append(("replace_image_correction_drift", drift_issue, True))
        if under_correction_issue:
            findings.append(("replace_image_correction_not_applied", under_correction_issue, True))
        block.content = merge_content(
            block.type,
            block.content,
            {
                "path": relative,
                "asset_id": relative.rsplit("/", 1)[-1],
                "generated": True,
                "error": None,
                # Real, confirmed bug (PDF/visual-pipeline audit,
                # doc_81d0b1d44cae): a block that was WITHHELD on an earlier
                # attempt (`omitted_after_qa_failure: true`, no `path`) and
                # later regenerates cleanly here kept that stale `true`
                # forever - this success path never reset it, unlike
                # `visual_qa_findings` just below, whose own comment already
                # states the exact same "a re-render that resolves the
                # problem must never leave a stale earlier-attempt marker"
                # rule. `image_generation_failed()` happens to also check
                # `path` itself, so the compact placeholder never actually
                # reappeared - but the raw field stayed a live lie (a real
                # image shipped while its own content claimed "withheld"),
                # confirmed on a real regenerated block.
                "omitted_after_qa_failure": False,
                "width": width,
                "height": height,
                # What was actually drawn, so a later review can check the
                # diagram against the text it sits beside.
                "diagram_outline": diagram_outline(spec),
                # Edit Visuals (flowchart editor): persist the structured spec
                # itself, not just the rendered SVG - without this there is
                # nothing for a later edit to load (see
                # DocumentService.update_visual_spec/preview_visual_spec,
                # which read/write this exact key, same as
                # concept_visual_service.py already does for
                # concept_experience). A course generated before this field
                # existed simply has no "spec" key here - see
                # DocumentService.reconstruct_diagram_spec for that case.
                "spec": spec.model_dump(mode="json"),
                # Quality-governance follow-up: previously this state (retry
                # exhausted, shipping anyway) was visible only in the log
                # warning above - a real reviewer or the quality report had
                # no way to see it. Set explicitly on every successful save
                # (including the clean "[]" case), same reasoning as
                # concept_visual_service.py's identical comment: a re-render
                # that resolves the problem must never leave a STALE finding
                # from an earlier, worse attempt (merge_content is a shallow
                # merge - an omitted key here would silently keep whatever
                # was already on the block). Same resolution string as that
                # module's own "rendered anyway, unresolved" case;
                # `deterministic=True` because `_flow_chart_connectivity_
                # issues` is pure in/out-degree counting, no AI judgment call.
                # `content_qa_unresolved` (built above, same resolution) is
                # concatenated in rather than replacing this - the two are
                # independent issue sets (connectivity/drift vs content-
                # coverage) that can both be non-empty on the same block.
                "visual_qa_findings": (
                    visual_qa_finding_entries(findings, resolution="rendered_with_unresolved_issues")
                    if findings else []
                ) + content_qa_unresolved,
                # Multi-diagram splitting (see `_linear_chain_split`): every
                # later part's already-rendered SVG, in order, picked up and
                # inserted as its own sibling block by
                # `app.course.document.diagram_splitting`'s own
                # document-level pass - this service has no access to the
                # document/page to insert a block itself. Explicitly reset
                # to `[]` on every save (not just when absent), same
                # "never leave a stale earlier-attempt marker" rule
                # `omitted_after_qa_failure` above already follows - a
                # block that no longer needs splitting must not keep a
                # previous attempt's stale later parts around.
                "pending_diagram_splits": pending_split_payloads,
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
