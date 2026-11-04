"""Run the PostgreSQL knowledge backend beside the file one and compare.

With `kb_shadow_compare` on, every library search is also put to the PostgreSQL
backend *in the background*, after the file backend has already answered. The file
answer is what the course gets; the other is only compared and counted. It adds no
latency to generation (the comparison is not awaited), no extra embedding (the query
vector is cached, see EmbeddingService) and cannot fail a search (every error is
caught and counted). Counts and the most recent differences are kept in memory and
logged, which is the evidence the cutover gate asks for.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.schemas.source import SourceExcerpt

log = get_logger(__name__)

OUTCOMES = ("match", "reordered", "partial", "different", "error")


@dataclass
class ShadowStats:
    counts: dict[str, int] = field(default_factory=lambda: {name: 0 for name in OUTCOMES})
    library_matches: int = 0
    library_differences: int = 0
    recent_differences: deque = field(default_factory=lambda: deque(maxlen=25))

    @property
    def searches(self) -> int:
        return sum(self.counts.values())


_stats = ShadowStats()
_tasks: set[asyncio.Task] = set()
_backend = None


def get_shadow_stats() -> ShadowStats:
    return _stats


def reset_shadow_stats() -> None:
    global _stats, _backend
    _stats = ShadowStats()
    _backend = None


def classify(files: list[SourceExcerpt], database: list[SourceExcerpt]) -> str:
    """How the two answers relate. `match` is the same passages in the same order;
    `reordered` the same passages in a different order; `partial` the same top passage
    but a different set; anything else is `different`."""
    a, b = [e.ref for e in files], [e.ref for e in database]
    if a == b:
        return "match"
    if set(a) == set(b):
        return "reordered"
    if a and b and a[0] == b[0]:
        return "partial"
    return "different"


def _record(outcome: str, query: str, files: list[SourceExcerpt], database: list[SourceExcerpt]) -> None:
    _stats.counts[outcome] += 1
    if outcome != "match":
        _stats.recent_differences.append(
            {"outcome": outcome, "query": query[:200], "files": [e.ref for e in files], "database": [e.ref for e in database]}
        )
        log.warning("KB shadow %s: files=%s database=%s query=%r", outcome, [e.ref for e in files], [e.ref for e in database], query[:80])


def _get_backend():
    global _backend
    if _backend is None:
        from app.services.knowledge_pg import PostgresKnowledgeBackend  # lazy: it imports the file service

        _backend = PostgresKnowledgeBackend(log_retrievals=False)
    return _backend


async def compare_search(query: str, files: list[SourceExcerpt], *, limit: int | None, char_budget: int | None, backend=None) -> str:
    try:
        database = await (backend or _get_backend()).asearch(query, limit=limit, char_budget=char_budget)
    except Exception as exc:  # noqa: BLE001 - a shadow run must never affect the real one
        _stats.counts["error"] += 1
        log.warning("KB shadow search failed: %s", exc)
        return "error"
    outcome = classify(files, database)
    _record(outcome, query, files, database)
    return outcome


async def compare_library(files_refs: set[str], *, backend=None) -> bool:
    """Whether both backends hold the same live passages."""
    try:
        database = set((await (backend or _get_backend()).alibrary()).passages)
    except Exception as exc:  # noqa: BLE001
        log.warning("KB shadow library comparison failed: %s", exc)
        return False
    same = database == files_refs
    if same:
        _stats.library_matches += 1
    else:
        _stats.library_differences += 1
        log.warning("KB shadow library differs: only in files=%s only in database=%s", sorted(files_refs - database)[:10], sorted(database - files_refs)[:10])
    return same


def schedule(coroutine) -> None:
    """Fire and forget, keeping a reference so the task is not collected mid-run."""
    task = asyncio.get_running_loop().create_task(coroutine)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for scheduled comparisons (tests and shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
