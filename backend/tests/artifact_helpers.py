"""Shared test cleanup for artifact memory.

Approved artifacts are reused by every course generated afterwards, so a test that approves some must not
leave them behind for unrelated tests. Approved rows are locked, so cleanup lifts the guard the way the
migration downgrade does - this is the one place that is acceptable.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db.session import get_session_factory


async def wipe_artifact_memory() -> None:
    async with get_session_factory()() as session, session.begin():
        await session.execute(text("ALTER TABLE am_artifact_versions DISABLE TRIGGER am_guard_locked_version"))
        await session.execute(text("TRUNCATE am_artifacts, am_epoch CASCADE"))
        await session.execute(text("INSERT INTO am_epoch (id, epoch) VALUES (1, 0)"))
        await session.execute(text("ALTER TABLE am_artifact_versions ENABLE TRIGGER am_guard_locked_version"))
