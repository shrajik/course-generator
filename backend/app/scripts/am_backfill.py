"""Capture the artifacts of courses already under review into Approved Artifact Memory.

Submitting a course for review captures its artifacts, best effort. A course submitted while the
artifact tables did not exist yet (migration 0013 not applied) was therefore never captured and the
reviewer sees nothing. This repeats that capture for every course still in review. It is idempotent:
unchanged artifacts are not versioned again.

    python -m app.scripts.am_backfill            # every course in review
    python -m app.scripts.am_backfill COURSE_ID  # one course
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.db.session import get_session_factory
from app.services.course_service import get_course_service


async def main(only: str | None) -> int:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text("SELECT course_id, owner_id FROM courses WHERE review_status = 'in_review'")
            )
        ).all()
    service = get_course_service()
    done = 0
    for course_id, owner_id in rows:
        if only and course_id != only:
            continue
        record = await service.get_course_record(course_id)
        await service._artifact_memory("submit", record, str(owner_id) if owner_id else None)  # noqa: SLF001
        done += 1
        print(f"captured {course_id}")
    print(f"{done} course(s) processed")
    return done


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
