"""Which passages a new version keeps, and which citation numbers they carry.

A citation `G{n}.{k}` must only ever mean one text. So a passage's `ordinal` is its
identity, not its position, and these rules decide it when a new version of an item
arrives:

* An incoming passage whose embedded text (heading + text) is identical to one the item
  already has **is that passage**: it reuses the existing immutable row, its ordinal and
  - if it has one - its vector. Nothing is copied and nothing is embedded again. Position
  and order do not matter, so moving a section keeps its citations. The source file is
  only a tie-breaker (identical text in two files pairs each with its own file first):
  replacing a file with its updated version changes the file's hash but must not change
  the citations of the passages that did not change. A reused passage keeps the source
  attribution it was created with.
* The previous version's passages are matched first; then passages the item had in
  *earlier* versions (so reverting an edit restores the original citations).
* Anything else is new and takes the next ordinal. Ordinals are never reused, so the
  ordinal of an edited or removed passage stays pointing at the text it had.
* A reused passage that has no vector for the current embedding model is handed back
  for embedding (this is also how a retry fills the gaps an earlier attempt left).

Pure: no database, no model calls.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExistingChunk:
    id: object
    ordinal: int
    embed_hash: str
    part: str | None  # which source file (or "authored") it came from
    embed_model: str | None = None
    has_embedding: bool = False


@dataclass(frozen=True)
class IncomingChunk:
    embed_hash: str
    part: str | None
    # Whatever the caller needs to create the row (text, headings, page...) travels along.
    payload: object = None


@dataclass
class PlannedChunk:
    incoming: IncomingChunk
    ordinal: int
    reuse: ExistingChunk | None
    # "previous" (kept from the last version), "history" (restored from an earlier one), "new".
    origin: str
    needs_embedding: bool


@dataclass
class LineagePlan:
    chunks: list[PlannedChunk] = field(default_factory=list)
    # Passages of the previous version this version does not carry.
    dropped: list[ExistingChunk] = field(default_factory=list)
    next_ordinal: int = 1

    @property
    def reused(self) -> int:
        return sum(1 for c in self.chunks if c.reuse is not None)

    @property
    def created(self) -> int:
        return sum(1 for c in self.chunks if c.reuse is None)

    @property
    def to_embed(self) -> list[PlannedChunk]:
        return [c for c in self.chunks if c.needs_embedding]


def plan_lineage(
    previous: Sequence[ExistingChunk],
    incoming: Sequence[IncomingChunk],
    *,
    next_ordinal: int,
    history: Sequence[ExistingChunk] = (),
    embed_model: str | None = None,
) -> LineagePlan:
    """`previous`: the chunks of the version being replaced. `history`: chunks the item
    had in any earlier version (not in `previous`). `next_ordinal`: the item's counter."""
    if next_ordinal < 1 or any(next_ordinal <= c.ordinal for c in (*previous, *history)):
        raise ValueError("next_ordinal must be above every ordinal the item has ever used")

    def pool(chunks: Sequence[ExistingChunk]) -> dict[str, list[ExistingChunk]]:
        queues: dict[str, list[ExistingChunk]] = defaultdict(list)
        for chunk in sorted(chunks, key=lambda c: c.ordinal):
            queues[chunk.embed_hash].append(chunk)
        return queues

    def take(queue: list[ExistingChunk] | None, part: str | None) -> ExistingChunk | None:
        """The lowest-ordinal passage of the same file if there is one, else the lowest overall."""
        if not queue:
            return None
        for position, candidate in enumerate(queue):
            if candidate.part == part:
                return queue.pop(position)
        return queue.pop(0)

    from_previous, from_history = pool(previous), pool(history)
    plan = LineagePlan(next_ordinal=next_ordinal)
    used: set[object] = set()

    for item in incoming:
        reuse, origin = None, "new"
        for source, name in ((from_previous, "previous"), (from_history, "history")):
            reuse = take(source.get(item.embed_hash), item.part)
            if reuse is not None:
                origin = name
                break
        if reuse is not None:
            used.add(reuse.id)
            keeps_vector = reuse.has_embedding and reuse.embed_model == embed_model
            plan.chunks.append(PlannedChunk(item, reuse.ordinal, reuse, origin, needs_embedding=not keeps_vector))
        else:
            plan.chunks.append(PlannedChunk(item, plan.next_ordinal, None, "new", needs_embedding=True))
            plan.next_ordinal += 1

    plan.dropped = [c for c in previous if c.id not in used]
    return plan
