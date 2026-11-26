"""Course-wide recurring-concept visual consistency (diagram quality audit
follow-up, Phase 8 - "the same concept should look the same everywhere in
one course"). Confirmed gap this closes: icon assignment for both the
`diagram` pipeline (DiagramNode.icon) and the `concept_experience` pipeline
(VisualEntity.icon) was previously decided independently, from scratch, by
whichever single AI call happened to generate that one diagram - the same
recurring concept (e.g. "Database") could get a different icon in every
diagram it appeared in across a course, with nothing remembering the
earlier choice.

Deliberately simple and deterministic: an in-process, first-seen-wins
registry keyed by (pipeline, course_id, normalised label) -> icon, bounded
by an LRU cap on how many (pipeline, course_id) buckets it keeps (same
in-process caching convention this codebase already uses elsewhere - see
app.course.templates.registry.load_template's own `@lru_cache(maxsize=64)`,
the direct precedent this mirrors). No new AI call, no database table.

Deployment reality this was verified against (production-verification
follow-up): the app's own deploy config (docker-compose.prod.yml) runs
`uvicorn ... --workers 1` - a single process, no replicas anywhere in this
repo's deployment manifests. An in-process registry genuinely provides
real course-wide consistency for this confirmed architecture. It does NOT
survive a process restart, and would NOT be shared across multiple worker
processes or multiple server instances if the deployment ever changed to
either - this is a real, accepted limitation (documented here, not hidden),
not a silently-assumed guarantee; if the deployment ever adds workers or
replicas, this registry would need to move to shared storage (e.g. the
existing database) to keep working as "course-wide."

Thread/async safety: every public method below does its read-modify-write
entirely in synchronous code with no `await` point inside it, so under
asyncio's single-threaded, cooperative scheduling (confirmed: `ImageService.
generate_missing` runs multiple `generate_for_block` calls concurrently via
`asyncio.gather`, but each one only calls into this module between its own
`await` points, never during one) a call here can never be interleaved by
another coroutine mid-update - no lock is needed, and none is used. Two
diagrams racing to be the FIRST to introduce a brand-new label resolve to
"whichever one's generation call happened to finish first" - a benign,
accepted nondeterminism (someone's valid icon choice wins), never data
corruption.

CRITICAL FIX (production-verification follow-up): `diagram` and
`concept_experience` use two INCOMPATIBLE icon vocabularies, confirmed by
reading both renderers directly - `app.render.diagram_renderer._ICON_DRAWERS`
is a fixed set of semantic KEYWORDS ("database", "server", "user"...)
looked up and drawn as an SVG pictogram, while `app.render.
concept_experience_renderer._entity_icon_char` does `if icon: return icon`
- it renders whatever string `VisualEntity.icon` holds as LITERAL DISPLAY
TEXT/an emoji character (confirmed by this codebase's own existing test
fixtures, e.g. "🏭", "📚", "⚡" - never a keyword). An earlier version of
this module shared ONE registry across both pipelines, which would have
silently corrupted either one (forcing a diagram's "database" keyword onto
a concept_experience card renders the literal word "database" as a badge
character; forcing a concept_experience emoji onto a DiagramNode.icon field
is simply unrecognised by `_ICON_DRAWERS` and draws nothing). Each pipeline
now gets its own, completely separate namespace - never cross-applied - and
the diagram pipeline additionally validates every icon it would record or
apply against the real `_ICON_DRAWERS` keyword set, so a model's typo/
unrecognised icon string can never get recorded as the course's "canonical"
choice and later silently downgrade a DIFFERENT diagram's own, correctly-
recognised icon to a blank badge.

Scoped to `icon` only, not colour: colour is assigned very differently
across the ~49 diagram kinds (a fixed cycling palette, a semantic
`color_role`, or a raw hex override only some kinds even read - see
diagram_renderer._node_colors/_cycling_palette) - forcing one specific
colour across kinds with their own already-cohesive palettes would often
clash rather than help, so colour consistency is left to each kind's own
existing palette logic."""

from __future__ import annotations

import re
from collections import OrderedDict

from app.render.diagram_renderer import _ICON_DRAWERS
from app.schemas.diagram import DiagramSpec

_DIAGRAM_NAMESPACE = "diagram"
_CONCEPT_NAMESPACE = "concept"

# Same bound `load_template`'s own `@lru_cache(maxsize=64)` uses - see this
# module's own docstring for why an unbounded dict would be a slow memory
# leak in a long-lived single process serving many courses over its
# lifetime, never restarting between them.
_MAX_TRACKED_BUCKETS = 64


def _normalised_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


def _normalised_diagram_icon(icon: str) -> str:
    return icon.strip().lower().replace(" ", "_").replace("-", "_")


def _is_recognised_diagram_icon(icon: str) -> bool:
    """Only a keyword `app.render.diagram_renderer._draw_icon` would
    actually draw something for - see this module's own docstring on why
    recording/applying an unrecognised one would be worse than not sharing
    an icon at all (a silent downgrade from a working badge to a blank
    one)."""
    return _normalised_diagram_icon(icon) in _ICON_DRAWERS


class VisualConsistencyRegistry:
    def __init__(self, *, max_buckets: int = _MAX_TRACKED_BUCKETS) -> None:
        # OrderedDict as a simple LRU: `move_to_end` on every touch, evict
        # from the front once over `max_buckets` - one bucket per (pipeline,
        # course_id) pair, never per label (a single course's own label
        # count is bounded by realistic content, not worth a second cap).
        self._buckets: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self._max_buckets = max_buckets

    def _bucket(self, namespace: str, course_id: str) -> dict[str, str]:
        key = (namespace, course_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = {}
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_buckets:
            self._buckets.popitem(last=False)
        return bucket

    def known_icons(self, namespace: str, course_id: str) -> dict[str, str]:
        """A copy of this (pipeline, course)'s own label->icon assignments
        so far - never the live dict, so a caller can't accidentally
        mutate the registry's own state."""
        return dict(self._buckets.get((namespace, course_id), {}))

    def apply_and_record(
        self, namespace: str, course_id: str, labels_and_icons: list[tuple[str, str]], *, is_valid_icon
    ) -> dict[str, str]:
        """For each (label, icon) pair a freshly generated spec actually
        has, within this one (pipeline, course) bucket: if this bucket has
        already seen this label with a DIFFERENT icon recorded, return the
        icon that should be used instead (the caller overrides that node/
        entity's own icon with it); otherwise, if this label is new to the
        bucket and the model gave it a real, VALID (per `is_valid_icon`)
        icon, remember it for every later diagram in this same pipeline.
        Returns {label (as given, not normalised): icon_to_use} only for
        labels that need overriding - a label whose icon already matches,
        or that has no valid icon at all either way, is simply omitted."""
        bucket = self._bucket(namespace, course_id)
        overrides: dict[str, str] = {}
        for label, icon in labels_and_icons:
            key = _normalised_label(label)
            if not key:
                continue
            known = bucket.get(key)
            icon = icon.strip()
            if known:
                if icon != known:
                    overrides[label] = known
            elif icon and is_valid_icon(icon):
                bucket[key] = icon
        return overrides


_registry = VisualConsistencyRegistry()


def get_visual_consistency_registry() -> VisualConsistencyRegistry:
    return _registry


def apply_diagram_consistency(course_id: str, spec: DiagramSpec) -> bool:
    """Mutates `spec.nodes` in place - overrides any node whose label the
    course has already assigned a RECOGNISED icon to elsewhere (in another
    `diagram`-kind spec - never a concept_experience one, see this module's
    own docstring on why those vocabularies can never mix), and records any
    new label this diagram is the first to use. Call this on the FINAL spec
    that's actually about to ship (after every retry has already run), so
    the registry only ever learns from - and only ever corrects - what a
    reader will really see. Returns True iff it actually changed an icon -
    the caller's cue to re-render (cheap, deterministic, no AI call) so the
    override is reflected in the picture, not just the stored spec JSON."""
    registry = get_visual_consistency_registry()
    pairs = [(node.label, node.icon) for node in spec.nodes if node.label.strip()]
    overrides = registry.apply_and_record(
        _DIAGRAM_NAMESPACE, course_id, pairs, is_valid_icon=_is_recognised_diagram_icon
    )
    if not overrides:
        return False
    for node in spec.nodes:
        replacement = overrides.get(node.label)
        if replacement is not None:
            node.icon = replacement
    return True


def apply_concept_consistency(course_id: str, spec: DiagramSpec) -> bool:
    """Mirrors `apply_diagram_consistency`, for the concept_experience
    pipeline's `spec.entities` (VisualEntity.icon) instead of `spec.nodes` -
    its OWN separate namespace in the same registry (never shared with the
    `diagram` pipeline's keyword vocabulary - see this module's own
    docstring). Any non-empty string is accepted here (concept_experience
    has no fixed icon enum to validate against - it renders whatever string
    is given as literal display text/an emoji). Returns True iff it
    actually changed an icon, same re-render cue as the diagram version."""
    registry = get_visual_consistency_registry()
    pairs = [(entity.label, entity.icon) for entity in spec.entities if entity.label.strip()]
    overrides = registry.apply_and_record(
        _CONCEPT_NAMESPACE, course_id, pairs, is_valid_icon=lambda icon: True
    )
    if not overrides:
        return False
    for entity in spec.entities:
        replacement = overrides.get(entity.label)
        if replacement is not None:
            entity.icon = replacement
    return True
