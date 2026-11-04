"""Reading the knowledge base for the admin: lists, one item's full picture, the
category tree, and what a citation points to.

Citations resolve in two layers (see docs/KB_CUTOVER_RUNBOOK.md):

* **What it says** - exact. A passage's ordinal is never reused and passages are immutable,
  so `G{n}.{k}` is the text that was cited, whether or not it is live now.
* **Whether it was live when the course was generated** - only as precise as the evidence:
  the retrieval log when a course id is given (the moment the passage was actually offered),
  else a timestamp the caller supplies, else unknown.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError, ValidationFailedError
from app.db.session import get_session_factory

_REF_RE = re.compile(r"^G(\d+)\.(\d+)$")

# One definition of what a row in the item list shows, used by the list and by the detail view.
_SUMMARY_SELECT = """
    SELECT i.id, i.number, i.public_id, i.title, i.description, i.lifecycle_status, i.deleted_at, i.needs_review, i.origin,
           i.category_id, c.name AS category_name, i.updated_at, i.live_version_id, i.latest_version_id,
           v.processing_status, v.processing_error,
           (SELECT count(*) FROM kb_version_sources vs WHERE vs.version_id = i.latest_version_id) AS source_count,
           (SELECT coalesce(array_agg(t.name ORDER BY t.name), '{}') FROM kb_item_tags it JOIN kb_tags t ON t.id = it.tag_id WHERE it.item_id = i.id) AS tags
    FROM kb_items i
    LEFT JOIN kb_categories c ON c.id = i.category_id
    LEFT JOIN kb_item_versions v ON v.id = i.latest_version_id
"""


def _summary(row) -> dict:
    return {
        "id": row.id, "number": row.number, "public_id": row.public_id, "title": row.title, "description": row.description,
        "lifecycle": row.lifecycle_status, "deleted": row.deleted_at is not None, "needs_review": row.needs_review, "origin": row.origin,
        "category": {"id": row.category_id, "name": row.category_name} if row.category_id else None,
        "tags": list(row.tags or []), "source_count": row.source_count,
        "processing": row.processing_status, "processing_error": row.processing_error,
        # A published item whose newest version is not the one being served.
        "unpublished_changes": row.lifecycle_status == "published" and row.latest_version_id != row.live_version_id,
        "updated_at": row.updated_at,
    }


class KbQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._factory = session_factory

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    # --- the item list ------------------------------------------------------------------------
    async def list_items(
        self,
        *,
        q: str | None = None,
        category_id: uuid.UUID | None = None,
        tag: str | None = None,
        lifecycle: str | None = None,
        needs_review: bool | None = None,
        deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """(page of items, total matching). Search covers title, description and tags; a
        category filter includes everything beneath that category."""
        where = ["i.purged_at IS NULL", "i.deleted_at IS NOT NULL" if deleted else "i.deleted_at IS NULL"]
        params: dict = {}
        if q and q.strip():
            where.append(
                "(i.tsv @@ plainto_tsquery('english', :q) OR i.title ILIKE :like OR EXISTS ("
                "SELECT 1 FROM kb_item_tags it JOIN kb_tags t ON t.id = it.tag_id WHERE it.item_id = i.id AND t.name_key LIKE :like))"
            )
            params.update(q=q.strip(), like=f"%{q.strip().lower()}%")
        if category_id is not None:
            where.append(
                "i.category_id IN (WITH RECURSIVE tree AS (SELECT id FROM kb_categories WHERE id = :category "
                "UNION SELECT c2.id FROM kb_categories c2 JOIN tree t ON c2.parent_id = t.id) SELECT id FROM tree)"
            )
            params["category"] = category_id
        if tag:
            where.append("EXISTS (SELECT 1 FROM kb_item_tags it JOIN kb_tags t ON t.id = it.tag_id WHERE it.item_id = i.id AND t.name_key = :tag)")
            params["tag"] = tag.strip().lower()
        if lifecycle:
            where.append("i.lifecycle_status = :lifecycle")
            params["lifecycle"] = lifecycle
        if needs_review is not None:
            where.append("i.needs_review = :needs_review")
            params["needs_review"] = needs_review
        clause = " AND ".join(where)
        async with self.factory() as session:
            total = (await session.execute(text(f"SELECT count(*) FROM kb_items i WHERE {clause}"), params)).scalar() or 0
            rows = (
                await session.execute(
                    text(f"{_SUMMARY_SELECT} WHERE {clause} ORDER BY i.updated_at DESC, i.number DESC LIMIT :limit OFFSET :offset"),
                    {**params, "limit": limit, "offset": offset},
                )
            ).all()
        return [_summary(row) for row in rows], int(total)

    # --- one item ---------------------------------------------------------------------------------
    async def item_detail(self, item_id: uuid.UUID) -> dict:
        """The item as the admin's drawer shows it: summary, proposals, every version with its
        files and processing state, and its relationships in both directions."""
        async with self.factory() as session:
            row = (await session.execute(text(f"{_SUMMARY_SELECT} WHERE i.id = :i AND i.purged_at IS NULL"), {"i": item_id})).first()
            if row is None:
                raise NotFoundError("Knowledge item not found")
            extra = (await session.execute(text("SELECT properties, suggestions FROM kb_items WHERE id = :i"), {"i": item_id})).one()
            versions = (
                await session.execute(
                    text(
                        """
                        SELECT v.id, v.version_no, v.processing_status, v.processing_error, v.warnings, v.change_note, v.created_at, v.live_from, v.live_until,
                               (v.id = i.live_version_id AND i.lifecycle_status = 'published' AND i.deleted_at IS NULL) AS is_live,
                               (SELECT count(*) FROM kb_version_chunks vc WHERE vc.version_id = v.id) AS passages,
                               (SELECT coalesce(array_agg(s.filename ORDER BY vs.position), '{}') FROM kb_version_sources vs JOIN kb_sources s ON s.id = vs.source_id WHERE vs.version_id = v.id) AS files,
                               (v.body_md IS NOT NULL) AS has_text
                        FROM kb_item_versions v JOIN kb_items i ON i.id = v.item_id WHERE v.item_id = :i ORDER BY v.version_no DESC
                        """
                    ),
                    {"i": item_id},
                )
            ).all()
            relations = (
                await session.execute(
                    text(
                        """
                        SELECT r.id, r.relation_type, r.note, r.source, 'out' AS direction, o.id AS other_id, o.number AS other_number, o.title AS other_title
                        FROM kb_relations r JOIN kb_items o ON o.id = r.to_item_id WHERE r.from_item_id = :i AND o.purged_at IS NULL
                        UNION ALL
                        SELECT r.id, r.relation_type, r.note, r.source, 'in', o.id, o.number, o.title
                        FROM kb_relations r JOIN kb_items o ON o.id = r.from_item_id WHERE r.to_item_id = :i AND o.purged_at IS NULL
                        """
                    ),
                    {"i": item_id},
                )
            ).all()
        return {
            **_summary(row),
            "properties": extra.properties,
            "suggestions": extra.suggestions,
            "live_version_id": row.live_version_id,
            "latest_version_id": row.latest_version_id,
            "versions": [dict(v._mapping) for v in versions],
            "relations": [dict(r._mapping) for r in relations],
        }

    async def chunks(self, item_id: uuid.UUID, *, version_id: uuid.UUID | None = None, offset: int = 0, limit: int = 50) -> list[dict]:
        """The passages of a version (default: the newest), with their citation numbers."""
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT c.ordinal, 'G' || i.number || '.' || c.ordinal AS ref, c.heading, c.page, c.text, c.words, c.topics,
                               (c.embedding IS NOT NULL) AS embedded, c.is_live
                        FROM kb_items i JOIN kb_version_chunks vc ON vc.version_id = coalesce(:v, i.latest_version_id)
                        JOIN kb_chunks c ON c.id = vc.chunk_id
                        WHERE i.id = :i AND i.purged_at IS NULL ORDER BY vc.position OFFSET :offset LIMIT :limit
                        """
                    ),
                    {"i": item_id, "v": version_id, "offset": offset, "limit": limit},
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def activity(self, item_id: uuid.UUID, *, limit: int = 100) -> list[dict]:
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text("SELECT id, action, detail, actor_id, created_at FROM kb_activity WHERE item_id = :i ORDER BY created_at DESC, id LIMIT :limit"),
                    {"i": item_id, "limit": limit},
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    # --- categories and tags ---------------------------------------------------------------------------
    async def categories(self) -> list[dict]:
        """Every category with how many items sit directly in it; the admin builds the tree from `parent_id`."""
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT c.id, c.parent_id, c.name, c.slug, c.description, c.sort_order, c.is_suggested,
                               (SELECT count(*) FROM kb_items i WHERE i.category_id = c.id AND i.deleted_at IS NULL AND i.purged_at IS NULL) AS item_count
                        FROM kb_categories c WHERE c.archived_at IS NULL ORDER BY c.sort_order, c.name
                        """
                    )
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def tags(self) -> list[dict]:
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT t.name, count(i.id) AS item_count FROM kb_tags t "
                        "LEFT JOIN kb_item_tags it ON it.tag_id = t.id LEFT JOIN kb_items i ON i.id = it.item_id AND i.deleted_at IS NULL AND i.purged_at IS NULL "
                        "GROUP BY t.name ORDER BY count(i.id) DESC, t.name"
                    )
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    # --- citations -----------------------------------------------------------------------------------------
    async def resolve_citation(self, ref: str, *, course_id: str | None = None, at: datetime | None = None) -> dict:
        """What `ref` (e.g. G2.5) says, whether it is live now, and - when the evidence allows -
        whether it was live when the course was generated."""
        match = _REF_RE.match(ref.strip())
        if match is None:
            raise ValidationFailedError("A citation looks like G2.5.")
        number, ordinal = int(match.group(1)), int(match.group(2))
        async with self.factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT c.id AS chunk_id, c.heading, c.page, c.text, c.is_live, c.retired_at, i.id AS item_id, i.title, i.lifecycle_status,
                               i.deleted_at, s.filename
                        FROM kb_items i JOIN kb_chunks c ON c.item_id = i.id AND c.ordinal = :o LEFT JOIN kb_sources s ON s.id = c.source_id
                        WHERE i.number = :n AND i.purged_at IS NULL
                        """
                    ),
                    {"n": number, "o": ordinal},
                )
            ).first()
            if row is None:
                raise NotFoundError("That passage does not exist, or its item was permanently removed.")
            moment, basis = at, ("supplied" if at is not None else None)
            if course_id:
                logged = (
                    await session.execute(
                        text("SELECT min(retrieved_at) AS first FROM kb_retrieval_log WHERE course_id = :c AND item_number = :n AND ordinal = :o"),
                        {"c": course_id, "n": number, "o": ordinal},
                    )
                ).scalar()
                if logged is not None:
                    moment, basis = logged, "retrieval_log"
            live_then = None
            if moment is not None:
                live_then = bool(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT 1 FROM kb_version_chunks vc JOIN kb_item_versions v ON v.id = vc.version_id
                                WHERE vc.chunk_id = :c AND v.live_from IS NOT NULL AND v.live_from <= :t AND (v.live_until IS NULL OR v.live_until > :t) LIMIT 1
                                """
                            ),
                            {"c": row.chunk_id, "t": moment},
                        )
                    ).first()
                )
        return {
            "ref": f"G{number}.{ordinal}", "text": row.text, "heading": row.heading, "page": row.page,
            "filename": row.filename or row.title, "item": {"id": row.item_id, "title": row.title, "number": number, "lifecycle": row.lifecycle_status, "deleted": row.deleted_at is not None},
            "live_now": bool(row.is_live), "superseded": not row.is_live, "retired_at": row.retired_at,
            "generation_time": moment, "generation_time_basis": basis, "live_at_generation": live_then,
        }
