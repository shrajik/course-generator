"""Deterministic quality gate for a semantically-authored `schematic`
diagram, run after `app.render.schematic_layout.resolve_schematic_layout`
and before `render_diagram_svg`.

Two distinct questions, per the brief this exists to satisfy:

1. Is it technically valid? (no collisions, nothing clipped, not
   overloaded) - ordinary geometry checks.
2. Is it actually well-composed? (a clear focal object, real whitespace,
   visual balance) - heuristics over the same geometry, not a second
   rendering pass and not a vision-model call. Every check here only needs
   what the layout step already computed (each shape's x/y/width/height in
   0..1 panel space), so it's cheap enough to run on every schematic without
   new infrastructure.

Only wired into the `schematic` kind - the other six diagram kinds already
have proven, dedicated layout code (and their own regression tests) and are
explicitly out of scope for this pass. See DiagramService for how a failure
here turns into one targeted retry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.render.schematic_layout import _parse_anchor
from app.schemas.diagram import DiagramSpec, SchematicShape, SchematicState

# --- structured failure reasons ---------------------------------------------

LABEL_COLLISION = "label_collision"
OBJECT_COLLISION = "object_collision"
ARROW_TEXT_COLLISION = "arrow_text_collision"
LEADER_LINE_CROSSING = "leader_line_crossing"
CANVAS_CLIPPING = "canvas_clipping"
EXCESSIVE_DENSITY = "excessive_density"
POOR_WHITESPACE = "poor_whitespace"
WEAK_FOCAL_HIERARCHY = "weak_focal_hierarchy"
POOR_VISUAL_BALANCE = "poor_visual_balance"
TOO_MANY_ANNOTATIONS = "too_many_annotations"
MISSING_OBJECT = "missing_object"
MISSING_RELATIONSHIP = "missing_relationship"
WRONG_VISUAL_TYPE = "wrong_visual_type"

# Shape types that annotate/connect rather than occupy their own "object"
# footprint the way block/circle/coil/gauge do - excluded from density,
# balance and focal-dominance maths (a thin arrow shouldn't count as visual
# weight) but still checked for collisions and clipping.
_CONNECTOR_TYPES = {"flow", "arrow", "label"}

_DENSITY_LIMIT = 0.55  # fraction of panel area objects may occupy before it reads as crowded
_MIN_MARGIN_FROM_EDGE = 0.03  # panel-fraction clearance a shape must keep from 0/1
_BALANCE_TOLERANCE = 0.22  # max allowed centroid drift from panel centre (0.5, 0.5)
_FOCAL_MIN_DOMINANCE = 0.85  # primary's area must be at least this fraction of the largest secondary's


@dataclass
class DiagramQAIssue:
    reason: str
    detail: str


@dataclass
class DiagramQAResult:
    issues: list[DiagramQAIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    def feedback(self) -> str:
        """One paragraph naming exactly what to change - never a bare "try
        again". Fed into the retry prompt by DiagramService."""
        if not self.issues:
            return ""
        lines = [f"- {issue.detail}" for issue in self.issues]
        return (
            "The previous schematic failed quality review for these specific reasons:\n"
            + "\n".join(lines)
            + "\nFix exactly these problems while keeping every correct label, object and "
            "relationship from the brief - do not start over from scratch."
        )


def _box(shape: SchematicShape) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) in panel-fraction space."""
    return (
        shape.x - shape.width / 2,
        shape.y - shape.height / 2,
        shape.x + shape.width / 2,
        shape.y + shape.height / 2,
    )


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    al, at, ar, ab = a
    bl, bt, br, bb = b
    return al < br and bl < ar and at < bb and bt < ab


def _segment_intersects_box(
    p1: tuple[float, float], p2: tuple[float, float], box: tuple[float, float, float, float]
) -> bool:
    """True if the line segment p1->p2 passes through `box` - used for
    arrow/flow-vs-other-shape and arrow/flow-vs-arrow/flow crossing checks.
    Cheap parametric clip (Liang-Barsky) rather than a general geometry
    library - this is the only shape of test these diagrams need."""
    x1, y1 = p1
    x2, y2 = p2
    left, top, right, bottom = box
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x1 - left),
        (dx, right - x1),
        (-dy, y1 - top),
        (dy, bottom - y1),
    ):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def _connector_endpoint(shape: SchematicShape, positions: dict[str, tuple[float, float]]) -> tuple[float, float]:
    if shape.target_id and shape.target_id in positions:
        return positions[shape.target_id]
    # No resolvable target: project a short distance in `rotation`'s
    # direction, matching how diagram_renderer draws an untargeted arrow/flow.
    length = max(shape.width, shape.height)
    angle = math.radians(shape.rotation)
    return (shape.x + length * math.cos(angle), shape.y + length * math.sin(angle))


def _evaluate_state(shapes: list[SchematicShape], learning_objective: str) -> list[DiagramQAIssue]:
    issues: list[DiagramQAIssue] = []
    if not shapes:
        return issues

    positions = {s.id: (s.x, s.y) for s in shapes if s.id}
    boxes = {id(s): _box(s) for s in shapes}

    # --- canvas_clipping ----------------------------------------------------
    clipped = [
        s for s in shapes
        if boxes[id(s)][0] < 0 or boxes[id(s)][1] < 0 or boxes[id(s)][2] > 1 or boxes[id(s)][3] > 1
    ]
    if clipped:
        names = ", ".join(s.label or s.id or s.type for s in clipped[:4])
        issues.append(DiagramQAIssue(
            CANVAS_CLIPPING,
            f"These shapes extend outside the visible panel: {names}. Shrink or reposition them "
            "so every shape (and its label) stays fully inside the frame.",
        ))

    # --- object_collision / label_collision ---------------------------------
    # `label`-type shapes are standalone annotations; a collision between two
    # of them reads as overlapping text specifically, everything else as
    # overlapping objects. A shape deliberately anchored "inside:"/"center"
    # onto another is *meant* to sit inside its reference (a nucleus inside a
    # cell, a core inside an atom) - that specific pair is expected overlap,
    # not a defect, and would otherwise make every nested composition fail.
    def _is_intentional_nesting(a: SchematicShape, b: SchematicShape) -> bool:
        for shape, other in ((a, b), (b, a)):
            keyword, target = _parse_anchor(shape.anchor)
            if keyword in ("inside", "center") and (not target or target == other.id):
                return True
        return False

    colliding_pairs: list[tuple[SchematicShape, SchematicShape]] = []
    for i, a in enumerate(shapes):
        for b in shapes[i + 1 :]:
            if _overlaps(boxes[id(a)], boxes[id(b)]) and not _is_intentional_nesting(a, b):
                colliding_pairs.append((a, b))
    label_collisions = [(a, b) for a, b in colliding_pairs if a.type == "label" or b.type == "label"]
    object_collisions = [(a, b) for a, b in colliding_pairs if a.type != "label" and b.type != "label"]
    if label_collisions:
        names = ", ".join(f"{a.label or a.id}/{b.label or b.id}" for a, b in label_collisions[:3])
        issues.append(DiagramQAIssue(
            LABEL_COLLISION,
            f"These label pairs overlap: {names}. Move labels outside the objects they describe "
            "and space them so no two labels touch.",
        ))
    if object_collisions:
        names = ", ".join(f"{a.label or a.id}/{b.label or b.id}" for a, b in object_collisions[:3])
        issues.append(DiagramQAIssue(
            OBJECT_COLLISION,
            f"These objects overlap: {names}. Increase spacing (or reduce object count) so no two "
            "objects touch.",
        ))

    # --- arrow_text_collision / leader_line_crossing ------------------------
    connectors = [s for s in shapes if s.type in ("flow", "arrow")]
    non_connectors = [s for s in shapes if s.type not in ("flow", "arrow")]
    text_hits: list[str] = []
    for connector in connectors:
        start = (connector.x, connector.y)
        end = _connector_endpoint(connector, positions)
        for other in non_connectors:
            if other is connector:
                continue
            if _segment_intersects_box(start, end, boxes[id(other)]) and not _overlaps(
                boxes[id(connector)], boxes[id(other)]
            ):
                # Touching its own start/target object is expected; only a
                # THIRD object's box in the way is a real collision.
                if other.id not in (connector.id, connector.target_id):
                    text_hits.append(f"{connector.label or connector.id} crosses {other.label or other.id}")
    if text_hits:
        issues.append(DiagramQAIssue(
            ARROW_TEXT_COLLISION,
            "These arrows/flow lines pass through unrelated objects or their labels: "
            + "; ".join(text_hits[:3])
            + ". Reroute or reposition so connecting lines never cross a third object's text.",
        ))

    crossings: list[str] = []
    for i, a in enumerate(connectors):
        a_end = _connector_endpoint(a, positions)
        for b in connectors[i + 1 :]:
            b_end = _connector_endpoint(b, positions)
            if _segments_cross((a.x, a.y), a_end, (b.x, b.y), b_end):
                crossings.append(f"{a.label or a.id}/{b.label or b.id}")
    if crossings:
        issues.append(DiagramQAIssue(
            LEADER_LINE_CROSSING,
            "These connecting lines cross each other: " + "; ".join(crossings[:3])
            + ". Reposition the objects they connect so the lines don't cross.",
        ))

    # --- excessive_density / poor_whitespace --------------------------------
    objects = [s for s in shapes if s.type not in _CONNECTOR_TYPES]
    occupied = sum(max(s.width, 0) * max(s.height, 0) for s in objects)
    if occupied > _DENSITY_LIMIT:
        issues.append(DiagramQAIssue(
            EXCESSIVE_DENSITY,
            f"Objects occupy about {occupied * 100:.0f}% of the panel, which reads as crowded. "
            "Reduce the number of secondary objects or shrink them so at least half the panel "
            "stays open space.",
        ))
    elif objects:
        lefts = [s.x - s.width / 2 for s in objects]
        rights = [s.x + s.width / 2 for s in objects]
        tops = [s.y - s.height / 2 for s in objects]
        bottoms = [s.y + s.height / 2 for s in objects]
        margin = min(min(lefts), 1 - max(rights), min(tops), 1 - max(bottoms))
        if margin < _MIN_MARGIN_FROM_EDGE:
            issues.append(DiagramQAIssue(
                POOR_WHITESPACE,
                "Objects sit too close to the panel edge, leaving no breathing room. Shrink or "
                "recentre the composition so a visible margin surrounds everything.",
            ))

    # --- weak_focal_hierarchy / poor_visual_balance -------------------------
    primary = next((s for s in objects if s.role == "primary"), None)
    secondaries = [s for s in objects if s is not primary]
    if primary is None and objects:
        issues.append(DiagramQAIssue(
            WEAK_FOCAL_HIERARCHY,
            "No object is marked as the primary/focal subject. Choose the one central object the "
            "diagram is actually about and mark it as primary; every other object should read as "
            "secondary to it.",
        ))
    elif primary is not None and secondaries:
        primary_area = primary.width * primary.height
        largest_secondary = max(s.width * s.height for s in secondaries)
        if primary_area < largest_secondary * _FOCAL_MIN_DOMINANCE:
            issues.append(DiagramQAIssue(
                WEAK_FOCAL_HIERARCHY,
                f"The primary object ('{primary.label or primary.id}') is not visually dominant - "
                "a secondary object is as large or larger. Make the primary object the largest "
                "shape in the diagram.",
            ))

    if objects:
        total_area = sum(s.width * s.height for s in objects) or 1.0
        cx = sum(s.x * s.width * s.height for s in objects) / total_area
        cy = sum(s.y * s.width * s.height for s in objects) / total_area
        if abs(cx - 0.5) > _BALANCE_TOLERANCE or abs(cy - 0.5) > _BALANCE_TOLERANCE:
            issues.append(DiagramQAIssue(
                POOR_VISUAL_BALANCE,
                "The composition's visual weight is concentrated off to one side rather than "
                "balanced around the centre. Redistribute secondary objects around the primary "
                "object instead of clustering them in one area.",
            ))

    # --- missing_relationship ------------------------------------------------
    known_ids = {s.id for s in shapes if s.id}
    dangling = [
        s for s in shapes
        if s.type in ("flow", "arrow") and s.target_id and s.target_id not in known_ids
    ]
    if dangling:
        names = ", ".join(s.label or s.id for s in dangling[:3])
        issues.append(DiagramQAIssue(
            MISSING_RELATIONSHIP,
            f"These relationships point at an object that doesn't exist: {names}. Point each "
            "relationship at one of the actual objects in this diagram, or remove it.",
        ))

    return issues


def _segments_cross(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], p4: tuple[float, float]
) -> bool:
    def orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def evaluate_schematic(spec: DiagramSpec) -> DiagramQAResult:
    """Run every check against an already-laid-out schematic spec (call
    after `resolve_schematic_layout`, before `render_diagram_svg`). A
    coordinate-authored (legacy) spec is still checked - the geometry checks
    are meaningful either way - but will typically pass or fail on its own
    merits rather than anything this module changed."""
    if spec.normalised_kind() != "schematic":
        return DiagramQAResult()

    states: list[SchematicState] = spec.states if spec.states else (
        [SchematicState(caption="", shapes=spec.shapes)] if spec.shapes else []
    )
    if not states:
        return DiagramQAResult(issues=[DiagramQAIssue(MISSING_OBJECT, "The schematic has no shapes at all.")])

    issues: list[DiagramQAIssue] = []
    seen_reasons: set[str] = set()
    for state in states:
        for issue in _evaluate_state(state.shapes, spec.learning_objective):
            # One mention per reason across the whole diagram is enough
            # feedback for a retry - repeating "object_collision" once per
            # panel just makes the prompt noisier, not more actionable.
            if issue.reason in seen_reasons:
                continue
            seen_reasons.add(issue.reason)
            issues.append(issue)

    return DiagramQAResult(issues=issues)
