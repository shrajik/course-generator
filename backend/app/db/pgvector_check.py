"""What the connected PostgreSQL can do with pgvector.

The knowledge base depends on pgvector's HNSW index (0.5.0+) and, for filtered
searches, on its iterative index scans (0.8.0+). The compose image tag used to
float, so the running server's version is not guaranteed - this reports it.
Never raises: an unreachable database or a missing extension is a result, not a
startup failure, matching how the rest of the app treats pgvector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import get_session_factory

log = get_logger(__name__)

MIN_HNSW = (0, 5, 0)
MIN_ITERATIVE_SCAN = (0, 8, 0)


@dataclass(frozen=True)
class PgvectorStatus:
    installed: bool
    version: str | None = None
    error: str | None = None

    @property
    def parsed(self) -> tuple[int, ...]:
        return parse_version(self.version)

    @property
    def supports_hnsw(self) -> bool:
        return self.installed and self.parsed >= MIN_HNSW

    @property
    def supports_iterative_scan(self) -> bool:
        return self.installed and self.parsed >= MIN_ITERATIVE_SCAN


def parse_version(version: str | None) -> tuple[int, ...]:
    """`"0.8.0"` -> (0, 8, 0); anything unparseable -> () which compares below every minimum."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


async def check_pgvector() -> PgvectorStatus:
    try:
        async with get_session_factory()() as session:
            version = await session.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    except Exception as exc:  # noqa: BLE001 - reporting must never block startup
        return PgvectorStatus(installed=False, error=str(exc)[:200])
    return PgvectorStatus(installed=version is not None, version=version)


async def log_pgvector_status() -> PgvectorStatus:
    status = await check_pgvector()
    if status.error:
        log.warning("pgvector check skipped: %s", status.error)
    elif not status.installed:
        log.warning("pgvector is not installed in this database: semantic search is unavailable")
    elif not status.supports_hnsw:
        log.warning("pgvector %s is older than %s: HNSW indexes are unavailable", status.version, ".".join(map(str, MIN_HNSW)))
    else:
        log.info("pgvector %s (iterative scan: %s)", status.version, "yes" if status.supports_iterative_scan else "no")
    return status


_iterative_scan_supported: bool | None = None


async def use_iterative_scan(session) -> None:
    """Filtered vector search: let the HNSW index keep scanning until enough rows pass the filter
    (pgvector 0.8+), instead of returning fewer results than exist. A no-op on older servers.
    Applies to the current transaction only."""
    global _iterative_scan_supported
    if _iterative_scan_supported is None:
        version = (await session.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar()
        _iterative_scan_supported = parse_version(version) >= MIN_ITERATIVE_SCAN
    if _iterative_scan_supported:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
