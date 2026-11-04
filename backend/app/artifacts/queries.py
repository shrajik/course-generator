"""Reading artifact memory for reviewers and admins: the review queue, one artifact's full picture.

A reviewer's question is "what is waiting for me?", so the list is organised around each artifact's
*newest* version and its review state; the detail view shows every version, because an approved
version's history is the point of keeping them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.db.session import get_session_factory

_SUMMARY = """
    SELECT a.id, a.number, a.public_id, a.artifact_type, a.title, a.description, a.course_id, a.chapter_id, a.course_title, a.status,
           a.updated_at, v.id AS latest_version_id, v.version_no AS latest_version, v.review_status, v.review_comment, v.submitted_at,
           av.version_no AS active_version, (a.active_version_id IS NOT NULL) AS ever_approved
    FROM am_artifacts a
    JOIN am_artifact_versions v ON v.id = a.latest_version_id
    LEFT JOIN am_artifact_versions av ON av.id = a.active_version_id
"""


class ArtifactQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._factory = session_factory

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    async def list(
        self, *, artifact_type: str | None = None, course_id: str | None = None, review_status: str | None = None,
        status: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[list[dict], int]:
        """(page, total). `review_status` is the newest version's: `pending_review` is the reviewer's queue."""
        where, params = ["TRUE"], {}
        for column, key, value in (("a.artifact_type", "type", artifact_type), ("a.course_id", "course", course_id), ("v.review_status", "review", review_status), ("a.status", "status", status)):
            if value:
                where.append(f"{column} = :{key}")
                params[key] = value
        if q and q.strip():
            where.append("(a.title ILIKE :like OR a.course_title ILIKE :like)")
            params["like"] = f"%{q.strip()}%"
        clause = " AND ".join(where)
        async with self.factory() as session:
            total = (await session.execute(text(f"SELECT count(*) FROM am_artifacts a JOIN am_artifact_versions v ON v.id = a.latest_version_id WHERE {clause}"), params)).scalar() or 0
            rows = (
                await session.execute(
                    text(f"{_SUMMARY} WHERE {clause} ORDER BY (v.review_status = 'pending_review') DESC, v.submitted_at DESC NULLS LAST, a.number DESC LIMIT :limit OFFSET :offset"),
                    {**params, "limit": limit, "offset": offset},
                )
            ).all()
        return [dict(r._mapping) for r in rows], int(total)

    async def detail(self, artifact_id: uuid.UUID) -> dict:
        async with self.factory() as session:
            row = (await session.execute(text(f"{_SUMMARY} WHERE a.id = :i"), {"i": artifact_id})).first()
            if row is None:
                raise NotFoundError("Artifact not found")
            versions = (
                await session.execute(
                    text(
                        """
                        SELECT id, version_no, review_status, content_hash, search_text, asset, source_refs, generation_run_id, model_info, locked, is_active,
                               (embedding IS NOT NULL) AS indexed, created_at, submitted_at, reviewed_by, reviewed_at, review_comment, approved_by, approved_at
                        FROM am_artifact_versions WHERE artifact_id = :i ORDER BY version_no DESC
                        """
                    ),
                    {"i": artifact_id},
                )
            ).all()
            links = (
                await session.execute(
                    text(
                        """
                        SELECT l.id, l.link_type, l.note, 'out' AS direction, l.target_course_id, l.target_kb_number, l.target_kb_ordinal,
                               t.id AS other_id, t.number AS other_number, t.title AS other_title
                        FROM am_links l LEFT JOIN am_artifacts t ON t.id = l.target_artifact_id WHERE l.from_artifact_id = :i
                        UNION ALL
                        SELECT l.id, l.link_type, l.note, 'in', NULL, NULL, NULL, f.id, f.number, f.title
                        FROM am_links l JOIN am_artifacts f ON f.id = l.from_artifact_id WHERE l.target_artifact_id = :i
                        """
                    ),
                    {"i": artifact_id},
                )
            ).all()
            activity = (
                await session.execute(
                    text("SELECT id, version_id, action, detail, actor_id, created_at FROM am_activity WHERE artifact_id = :i ORDER BY created_at DESC, id LIMIT 100"),
                    {"i": artifact_id},
                )
            ).all()
        return {**dict(row._mapping), "versions": [dict(v._mapping) for v in versions], "links": [dict(l._mapping) for l in links], "activity": [dict(a._mapping) for a in activity]}

    async def counts(self) -> dict[str, int]:
        """The numbers a reviewer's badge needs."""
        async with self.factory() as session:
            rows = (await session.execute(text("SELECT v.review_status, count(*) AS n FROM am_artifacts a JOIN am_artifact_versions v ON v.id = a.latest_version_id GROUP BY 1"))).all()
        found = {r.review_status: int(r.n) for r in rows}
        return {"pending_review": found.get("pending_review", 0), "approved": found.get("approved", 0), "changes_requested": found.get("changes_requested", 0)}
