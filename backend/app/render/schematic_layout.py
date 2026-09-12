"""Semantic schematic layout: turns a blueprint-authored `schematic` state
(shapes carrying `role`/`anchor`/`priority`/`size` - see SchematicShape) into
the same coordinate-authored shape list `app.render.diagram_renderer` has
always consumed.

This is deliberately a pre-processing step, not a renderer change:
`render_diagram_svg` still does exactly what it always did - draw shapes at
whatever x/y/width/height they carry. The only thing that changes is *who*
computes those numbers. A shape nobody has "opted in" to the semantic path
(no `anchor`/`role` set anywhere in its state) passes through completely
unchanged, which is what keeps every hand-authored spec (tests, the pre-
blueprint offline mock) rendering exactly as before.

The technique mirrors what `diagram_renderer._layout_concept_map` already
does for labelled-box diagrams (radius derived from box size, ring
distribution, true box-edge intersection for connecting lines) - applied to
the small illustrated-shape vocabulary instead of boxes, and to a topic-
agnostic anchor grammar instead of a fixed hub-and-spoke shape.
"""

from __future__ import annotations

import math

from app.schemas.diagram import (
    _PRIORITY_RANK,
    DiagramSpec,
    SchematicShape,
    SchematicState,
)

# Concrete size-class -> panel-fraction (width, height). Bounded on purpose:
# no single object should be able to dominate the panel just because the
# model asked for "large" everywhere, and every object of the same class
# renders at the same scale - "consistent scale" from the composition rules.
_SIZE_FRACTIONS: dict[str, tuple[float, float]] = {
    "small": (0.14, 0.14),
    "medium": (0.22, 0.22),
    "large": (0.30, 0.26),
}
_DEFAULT_SIZE = "medium"

# Minimum gap (panel fraction) kept between adjacent shape edges - the same
# role a fixed pixel gap constant plays in diagram_renderer's box layouts,
# just expressed in the 0..1 panel space schematic shapes already use.
_MARGIN = 0.05
_MAX_RESOLUTION_PASSES = 6


def _is_semantically_authored(shapes: list[SchematicShape]) -> bool:
    return any(shape.anchor.strip() or shape.role.strip() for shape in shapes)


def _size_fraction(shape: SchematicShape) -> tuple[float, float]:
    if shape.size in _SIZE_FRACTIONS:
        return _SIZE_FRACTIONS[shape.size]
    if shape.width and shape.height:
        return shape.width, shape.height
    return _SIZE_FRACTIONS[_DEFAULT_SIZE]


def _parse_anchor(anchor: str) -> tuple[str, str]:
    """"orbit:magnet" -> ("orbit", "magnet"); "center" -> ("center", "")."""
    anchor = anchor.strip()
    if ":" in anchor:
        keyword, _, target = anchor.partition(":")
        return keyword.strip().lower(), target.strip()
    return anchor.lower(), ""


def _enforce_annotation_budget(
    shapes: list[SchematicShape], max_annotations: int
) -> list[SchematicShape]:
    """Critical shapes (and the primary one) are never dropped; among the
    rest, keep the highest-priority ones up to the budget, breaking ties by
    declaration order. This is the deterministic stand-in for "important:
    show if space allows, optional: omit when crowded" - a budget on count
    rather than a whitespace solver, which is enough to stop a diagram from
    being overloaded without new infrastructure."""
    if max_annotations <= 0 or len(shapes) <= max_annotations:
        return shapes

    must_keep_ids = {id(s) for s in shapes if s.role == "primary" or s.priority == "critical"}
    optional_pool = [s for s in shapes if id(s) not in must_keep_ids]
    optional_pool.sort(key=lambda s: _PRIORITY_RANK.get(s.priority, 1))

    budget_left = max(max_annotations - len(must_keep_ids), 0)
    kept_ids = must_keep_ids | {id(s) for s in optional_pool[:budget_left]}
    # Preserve original declaration order in the output.
    return [s for s in shapes if id(s) in kept_ids]


def _merge_inside_anchors(shapes: list[SchematicShape]) -> list[SchematicShape]:
    """A shape anchored "inside:<id>" onto a block/circle/gauge parent whose
    own `sublabel` is still empty is folded into that parent's `sublabel`
    instead of being drawn as a second, independently-positioned shape.

    `diagram_renderer` already has a proven, non-overlapping way to show
    nested content - `_draw_circle`'s (and `_draw_block`'s/`_draw_gauge`'s)
    sublabel branch, exercised by
    test_circle_shape_renders_with_an_inner_nucleus_when_sublabel_is_set.
    Placing a *second* shape near the parent's centre instead would put its
    own label right where the parent's own centred label already sits (the
    parent doesn't know a child rendered on top of it and shorten its own
    text) - reusing the existing mechanism sidesteps that entirely rather
    than trying to out-position it."""
    by_id = {s.id: s for s in shapes if s.id}
    merged_ids: set[int] = set()
    for shape in shapes:
        keyword, target_ref = _parse_anchor(shape.anchor)
        if keyword != "inside" or not target_ref or shape.type in ("flow", "arrow"):
            continue
        target = by_id.get(target_ref)
        if target is None or target.type not in ("block", "circle", "gauge") or target.sublabel.strip():
            continue
        target.sublabel = shape.label
        merged_ids.add(id(shape))
    if not merged_ids:
        return shapes
    return [s for s in shapes if id(s) not in merged_ids]


def _resolve_state(shapes: list[SchematicShape], max_annotations: int) -> list[SchematicShape]:
    if not shapes or not _is_semantically_authored(shapes):
        return shapes

    shapes = [s.model_copy() for s in shapes]
    shapes = _merge_inside_anchors(shapes)
    primary = next((s for s in shapes if s.role == "primary"), shapes[0])
    if primary.role != "primary":
        primary.role = "primary"

    # Trim to budget before doing any position maths - a dropped shape
    # should never influence how its would-be siblings are spread out, and
    # there's no point computing a position for something that won't render.
    shapes = _enforce_annotation_budget(shapes, max_annotations)
    if not any(s is primary for s in shapes):
        # The chosen primary was dropped by budget trimming (only possible
        # when max_annotations is smaller than 1, an unreasonable config) -
        # fall back to whatever survived.
        primary = shapes[0]

    positions: dict[str, tuple[float, float]] = {}
    sizes: dict[str, tuple[float, float]] = {}
    for shape in shapes:
        sizes[shape.id or str(id(shape))] = _size_fraction(shape)

    primary_key = primary.id or str(id(primary))
    positions[primary_key] = (0.5, 0.5)

    remaining = [s for s in shapes if s is not primary]

    def key_of(shape: SchematicShape) -> str:
        return shape.id or str(id(shape))

    # Every shape anchored to the same reference is placed by ONE call,
    # regardless of anchor keyword (a "left_of:rotor" and an "orbit:rotor"
    # shape are siblings sharing the rotor's clearance) - this is what
    # guarantees siblings never collide with each other, since they're all
    # positioned against the same shared radius/angle budget rather than
    # each anchor keyword computing its own placement in isolation.
    resolved_ids = {primary_key}
    passes = 0
    while remaining and passes < _MAX_RESOLUTION_PASSES:
        passes += 1
        still_pending: list[SchematicShape] = []
        groups: dict[str, list[SchematicShape]] = {}
        for shape in remaining:
            _, target = _parse_anchor(shape.anchor)
            ref = target or primary_key
            if ref not in resolved_ids and ref != key_of(shape):
                still_pending.append(shape)
                continue
            groups.setdefault(ref, []).append(shape)

        for ref, group in groups.items():
            ref_pos = positions.get(ref, (0.5, 0.5))
            ref_w, ref_h = sizes.get(ref, _SIZE_FRACTIONS[_DEFAULT_SIZE])
            _place_children(ref_pos, ref_w, ref_h, group, sizes, positions, key_of)
            for shape in group:
                resolved_ids.add(key_of(shape))

        remaining = still_pending

    # Anything left after the pass limit (e.g. a circular anchor chain) gets
    # a safe fallback: orbit the primary, same as an unset anchor.
    if remaining:
        ref_pos = positions.get(primary_key, (0.5, 0.5))
        ref_w, ref_h = sizes.get(primary_key, _SIZE_FRACTIONS[_DEFAULT_SIZE])
        _place_children(ref_pos, ref_w, ref_h, remaining, sizes, positions, key_of)

    for shape in shapes:
        k = key_of(shape)
        x, y = positions.get(k, (0.5, 0.5))
        w, h = sizes.get(k, _SIZE_FRACTIONS[_DEFAULT_SIZE])
        shape.x, shape.y = x, y
        shape.width, shape.height = w, h

    return _enforce_annotation_budget(shapes, max_annotations)


_FIXED_ANCHOR_ANGLES = {"above": -90.0, "below": 90.0, "left_of": 180.0, "right_of": 0.0}


def _place_children(
    ref_pos: tuple[float, float],
    ref_w: float,
    ref_h: float,
    children: list[SchematicShape],
    sizes: dict[str, tuple[float, float]],
    positions: dict[str, tuple[float, float]],
    key_of,
) -> None:
    """Place every shape anchored to one reference. Two independent rings,
    both centred on the reference's own position:

    - a small *inner* ring for "inside"/"center" shapes (nested content -
      e.g. a nucleus inside a cell), radius scaled to the reference's own
      size so it never reaches the reference's edge;
    - a normal *outer* ring for everything else (directional anchors get a
      fixed angle on it, "orbit"/unset ones fill the remaining angles).

    Splitting these is what stops a nested shape from being pulled out to
    the same radius as its non-nested siblings, and stops a "center"-anchored
    secondary from landing exactly on top of the primary object, which
    always separately occupies true panel-centre (0.5, 0.5).

    One shared radius per ring, derived from (a) clearing the reference's
    own footprint, (b) fitting the ring's shape count without them touching
    each other, and (c) never pushing a shape's own box outside the panel -
    fixes both the sibling-collision and the canvas-clipping failure modes
    structurally, rather than leaving it to quality review + a retry that
    would just reproduce the same geometry.
    """
    rx, ry = ref_pos
    inner_ids = {id(s) for s in children if _parse_anchor(s.anchor)[0] in ("inside", "center")}
    inner = [s for s in children if id(s) in inner_ids]
    outer = [s for s in children if id(s) not in inner_ids]

    if inner:
        _place_ring(rx, ry, ref_w, ref_h, inner, sizes, positions, key_of, inward=True)
    if outer:
        _place_ring(rx, ry, ref_w, ref_h, outer, sizes, positions, key_of, inward=False)


def _place_ring(
    rx: float,
    ry: float,
    ref_w: float,
    ref_h: float,
    ring: list[SchematicShape],
    sizes: dict[str, tuple[float, float]],
    positions: dict[str, tuple[float, float]],
    key_of,
    *,
    inward: bool,
) -> None:
    max_dim = max(max(sizes[key_of(s)]) for s in ring)
    ref_bounding = math.hypot(ref_w, ref_h) / 2
    count = len(ring)
    # The *ideal* radius, ignoring the panel edge entirely - canvas bounds
    # are enforced per-shape, per-angle below (_max_radius_along), since a
    # reference near one edge can still have plenty of room on other sides;
    # clamping the whole ring to one isotropic "worst direction" radius here
    # would shrink it even for shapes whose own angle has room to spare.
    if inward:
        radius = ref_bounding * 0.4
    else:
        clearance_radius = ref_bounding + max_dim / 2 + _MARGIN * 2
        neighbour_radius = (
            (max_dim + _MARGIN) / (2 * math.sin(math.pi / count)) if count >= 2 else 0.0
        )
        radius = max(clearance_radius, neighbour_radius, 0.12)

    fixed: dict[int, float] = {}
    free: list[SchematicShape] = []
    for shape in ring:
        keyword, _ = _parse_anchor(shape.anchor)
        if not inward and keyword in _FIXED_ANCHOR_ANGLES:
            fixed[id(shape)] = math.radians(_FIXED_ANCHOR_ANGLES[keyword])
        else:
            free.append(shape)

    # Free shapes fill the remaining angular budget evenly, starting at the
    # top and stepping past any angle a fixed anchor already claimed.
    used_angles = sorted(fixed.values())
    min_gap = (2 * math.pi / count) * 0.6 if count else 0.0

    def _too_close(angle: float) -> bool:
        return any(abs(((angle - used + math.pi) % (2 * math.pi)) - math.pi) < min_gap for used in used_angles)

    angle = -math.pi / 2
    step = 2 * math.pi / max(count, 1)
    for shape in free:
        guard = 0
        while _too_close(angle) and guard < 12:
            angle += step / 3
            guard += 1
        fixed[id(shape)] = angle
        used_angles.append(angle)
        angle += step

    for shape in ring:
        theta = fixed[id(shape)]
        w, h = sizes[key_of(shape)]
        half_w, half_h = w / 2 + _MARGIN, h / 2 + _MARGIN
        # The shared ring radius can still be too far for THIS shape's angle
        # specifically when the reference sits near a corner (plenty of room
        # on one side, none on another) - clamping x/y independently after
        # the fact would distort the ring into an overlap; shrinking this
        # one shape's own radius along its own angle keeps it circular
        # everywhere it can be and only pulled in where it must be.
        personal_radius = min(radius, _max_radius_along(rx, ry, theta, half_w, half_h))
        if personal_radius < max(w, h) / 2:
            # The requested direction has essentially no room at all (the
            # reference itself sits too close to that edge) - rather than
            # collapse onto the reference's own centre, use whichever
            # direction actually has room. A last-resort escape, not the
            # primary placement strategy.
            for candidate in (i * math.pi / 4 for i in range(8)):
                candidate_radius = min(radius, _max_radius_along(rx, ry, candidate, half_w, half_h))
                if candidate_radius > personal_radius:
                    theta, personal_radius = candidate, candidate_radius
        x = rx + personal_radius * math.cos(theta)
        y = ry + personal_radius * math.sin(theta)
        positions[key_of(shape)] = (x, y)


def _max_radius_along(rx: float, ry: float, angle: float, half_w: float, half_h: float) -> float:
    """How far a ray from (rx, ry) at `angle` can travel before a box of
    half-size (half_w, half_h) centred on it would cross the panel edge -
    the same true rectangle-ray intersection `diagram_renderer._ray_box_exit`
    already uses for connecting lines, applied here to the panel boundary
    instead of another shape's box."""
    lo_x, hi_x = half_w, 1 - half_w
    lo_y, hi_y = half_h, 1 - half_h
    ux, uy = math.cos(angle), math.sin(angle)
    candidates: list[float] = []
    if ux > 1e-9:
        candidates.append((hi_x - rx) / ux)
    elif ux < -1e-9:
        candidates.append((lo_x - rx) / ux)
    if uy > 1e-9:
        candidates.append((hi_y - ry) / uy)
    elif uy < -1e-9:
        candidates.append((lo_y - ry) / uy)
    return max(min(candidates), 0.0) if candidates else 0.0


def resolve_schematic_layout(spec: DiagramSpec) -> DiagramSpec:
    """No-op for every kind except `schematic`, and a no-op for a schematic
    whose shapes are all coordinate-authored (see `_is_semantically_authored`)
    - existing callers that hand-build a DiagramSpec and call
    `render_diagram_svg` directly are completely unaffected either way."""
    if spec.normalised_kind() != "schematic":
        return spec

    max_annotations = spec.max_annotations or 5

    if spec.states:
        new_states = [
            SchematicState(caption=state.caption, shapes=_resolve_state(state.shapes, max_annotations))
            for state in spec.states
        ]
        return spec.model_copy(update={"states": new_states})
    if spec.shapes:
        return spec.model_copy(update={"shapes": _resolve_state(spec.shapes, max_annotations)})
    return spec
