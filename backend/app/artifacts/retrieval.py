"""Finding approved artifacts, and resolving a citation to the exact version cited.

Only the **active, approved** version of an **unarchived** artifact is ever searched: the vector and
keyword indexes are partial on `is_active`, so pending, rejected and superseded versions are not merely
filtered out, they are not in the index. (`ArtifactService.check_invariants` keeps `is_active` honest.)

Ranking is the knowledge base's - the same BM25 arithmetic (`kb_ranking`) fused with cosine similarity -
over this memory's own tables. Near-copies are removed (two courses that taught the same thing the same way
should not both spend tokens), and each artifact is cut extractively to what is relevant to the question.

A citation is `A{number}.{version}`: artifact number and version number, so it names one immutable text.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.db import am_models as am
from app.db.pgvector_check import use_iterative_scan
from app.db.session import get_session_factory
from app.ingestion.retrieval import tokenize
from app.services import kb_ranking
from app.schemas.source import SourceExcerpt
from app.services.embedding_service import EmbeddingService, get_embedding_service

log = get_logger(__name__)

_REF_RE = re.compile(r"^A(\d+)\.(\d+)$")
_KEYWORD_POOL = 300
_COLUMNS = """
    v.id AS version_id, v.version_no, v.search_text, v.asset, v.source_refs, a.id AS artifact_id, a.number, a.public_id,
    a.artifact_type, a.title, a.course_title, a.course_id, a.chapter_id
"""


class ArtifactExcerpt(BaseModel):
    """One approved artifact as offered to a writer, with the citation it must use."""

    model_config = ConfigDict(extra="forbid")

    ref: str  # "A7.2" = artifact 7, version 2
    artifact_id: str  # the public id
    artifact_type: str
    title: str
    course_title: str = ""
    text: str
    score: float = 0.0
    source_refs: list[str] = []
    asset: dict[str, Any] | None = None


def _excerpt(row, score: float = 0.0, body: str | None = None) -> ArtifactExcerpt:
    return ArtifactExcerpt(
        ref=f"A{row.number}.{row.version_no}", artifact_id=row.public_id, artifact_type=row.artifact_type, title=row.title,
        course_title=row.course_title, text=body if body is not None else row.search_text, score=score,
        source_refs=list(row.source_refs or []), asset=row.asset,
    )


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class ArtifactMemory:
    def __init__(
        self,
        settings: Settings | None = None,
        embeddings: EmbeddingService | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        log_retrievals: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.embeddings = embeddings or get_embedding_service()
        self._factory = session_factory
        self.log_retrievals = log_retrievals
        self._stats: tuple[int, int, float] | None = None  # (epoch, versions, average length)

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    async def has_artifacts(self) -> bool:
        async with self.factory() as session:
            return bool((await session.execute(text("SELECT EXISTS (SELECT 1 FROM am_artifact_versions WHERE is_active)"))).scalar())

    async def search(
        self,
        query: str,
        *,
        types: tuple[str, ...] = (),
        course_id: str | None = None,
        exclude_course_id: str | None = None,
        limit: int | None = None,
        trace: tuple[str, str] | None = None,
    ) -> list[ArtifactExcerpt]:
        """The approved artifacts that best answer `query`, optionally only of some types, only of one
        course, or never of one course (a course being written must not be offered its own work)."""
        if not query.strip():
            return []
        limit = limit or self.settings.artifact_excerpts_per_chapter
        vector = await self.embeddings.embed_one(query)
        async with self.factory() as session:
            epoch = int((await session.execute(text("SELECT epoch FROM am_epoch WHERE id = 1"))).scalar() or 0)
            total, average = await self._corpus(session, epoch)
            if total == 0:
                return []
            allowed = await self._allowed(session, types, course_id, exclude_course_id)
            if allowed is not None and not allowed:
                return []
            semantic = await self._semantic(session, vector, limit, allowed)
            keyword, keyword_rows = await self._keyword(session, query, total, average, allowed)
            ranked = kb_ranking.fuse(keyword, [r.ref for r in semantic], limit=limit * 2)
            rows = {r.ref: r for r in [*semantic, *keyword_rows]}
        keep = set(kb_ranking.near_duplicates_removed([(ref, rows[ref].text) for ref, _ in ranked if ref in rows]))
        terms = kb_ranking.query_terms(query)
        out: list[ArtifactExcerpt] = []
        used = 0
        for ref, score in ranked:
            if ref not in keep or ref not in rows or len(out) >= limit:
                continue
            body = kb_ranking.compress(rows[ref].text, terms, self.settings.artifact_excerpt_chars)
            if used + len(body) > self.settings.artifact_total_chars and out:
                break
            used += len(body)
            out.append(rows[ref].model_copy(update={"text": body, "score": round(score, 5)}))
        if trace is not None and out and self.log_retrievals:
            await self._log(query, out, trace)
        return out

    async def _corpus(self, session: AsyncSession, epoch: int) -> tuple[int, float]:
        if self._stats is not None and self._stats[0] == epoch:
            return self._stats[1], self._stats[2]
        row = (await session.execute(text("SELECT count(*) AS n, coalesce(avg(token_count), 0) AS average FROM am_artifact_versions WHERE is_active"))).one()
        self._stats = (epoch, int(row.n), float(row.average))
        return int(row.n), float(row.average)

    @staticmethod
    async def _allowed(session: AsyncSession, types: tuple[str, ...], course_id: str | None, exclude: str | None) -> list[uuid.UUID] | None:
        if not types and course_id is None and exclude is None:
            return None
        where, params = ["a.status = 'active'"], {}
        if types:
            where.append("a.artifact_type = ANY(CAST(:types AS text[]))")
            params["types"] = list(types)
        if course_id is not None:
            where.append("a.course_id = :course")
            params["course"] = course_id
        if exclude is not None:
            where.append("a.course_id IS DISTINCT FROM :exclude")
            params["exclude"] = exclude
        return [r.id for r in await session.execute(text(f"SELECT a.id FROM am_artifacts a WHERE {' AND '.join(where)}"), params)]

    async def _semantic(self, session: AsyncSession, vector: list[float] | None, limit: int, allowed: list[uuid.UUID] | None) -> list[ArtifactExcerpt]:
        if vector is None or len(vector) != self.settings.embedding_dimensions:
            return []
        narrow = ""
        params: dict[str, Any] = {}
        if allowed is not None:
            await use_iterative_scan(session)
            narrow, params = "AND k.artifact_id = ANY(CAST(:allowed AS uuid[]))", {"allowed": allowed}
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH near AS (
                        SELECT k.id, 1 - (k.embedding <=> CAST(:q AS vector)) AS similarity FROM am_artifact_versions k
                        WHERE k.is_active AND k.embedding IS NOT NULL {narrow}
                        ORDER BY k.embedding <=> CAST(:q AS vector) LIMIT :pool
                    )
                    SELECT {_COLUMNS} FROM near JOIN am_artifact_versions v ON v.id = near.id JOIN am_artifacts a ON a.id = v.artifact_id
                    WHERE near.similarity >= :minimum ORDER BY near.similarity DESC
                    """
                ),
                {"q": _vector_literal(vector), "pool": max(limit * 4, 12), "minimum": self.settings.artifact_min_semantic, **params},
            )
        ).all()
        return [_excerpt(r) for r in rows]

    async def _keyword(self, session: AsyncSession, query: str, total: int, average: float, allowed: list[uuid.UUID] | None) -> tuple[dict[str, float], list[ArtifactExcerpt]]:
        terms = kb_ranking.query_terms(query)
        if not terms:
            return {}, []
        frequency = {
            r.term: int(r.frequency)
            for r in await session.execute(
                text("SELECT t.term, count(v.id) AS frequency FROM unnest(CAST(:terms AS text[])) AS t(term) LEFT JOIN am_artifact_versions v ON v.is_active AND v.tokens @> ARRAY[t.term] GROUP BY t.term"),
                {"terms": terms},
            )
        }
        if not any(frequency.values()):
            return {}, []
        narrow = "AND v.artifact_id = ANY(CAST(:allowed AS uuid[]))" if allowed is not None else ""
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS}, v.tokens FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id
                    JOIN LATERAL (SELECT count(*) AS shared FROM unnest(CAST(:terms AS text[])) AS t(term) WHERE v.tokens @> ARRAY[t.term]) m ON true
                    WHERE v.is_active AND v.tokens && CAST(:terms AS text[]) AND m.shared >= :needed {narrow}
                    ORDER BY m.shared DESC LIMIT :pool
                    """
                ),
                {"terms": terms, "needed": kb_ranking.needed_terms(terms, 2), "pool": _KEYWORD_POOL, **({"allowed": allowed} if allowed is not None else {})},
            )
        ).all()
        found = [_excerpt(r) for r in rows]
        scores = kb_ranking.keyword_scores(
            terms, [kb_ranking.KeywordCandidate(e.ref, tokenize(e.text)) for e in found],
            total=total, document_frequency=frequency, average_length=average, min_coverage=self.settings.artifact_min_coverage, min_terms=2,
        )
        return scores, found

    async def _log(self, query: str, offered: list[ArtifactExcerpt], trace: tuple[str, str]) -> None:
        """Which artifacts a chapter was offered: the provenance of reuse. Best effort."""
        digest = hashlib.sha256(" ".join(query.lower().split()).encode("utf-8")).hexdigest()
        try:
            async with self.factory() as session, session.begin():
                versions = {f"A{r.number}.{r.version_no}": (r.artifact_id, r.version_id) for r in await session.execute(
                    text("SELECT a.number, v.version_no, a.id AS artifact_id, v.id AS version_id FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id WHERE v.is_active AND a.public_id = ANY(CAST(:ids AS text[]))"),
                    {"ids": [e.artifact_id for e in offered]},
                )}
                await session.execute(
                    insert(am.AmRetrievalLog),
                    [{"artifact_id": versions[e.ref][0], "version_id": versions[e.ref][1], "course_id": trace[0][:64], "chapter_id": trace[1][:64], "query_hash": digest} for e in offered if e.ref in versions],
                )
        except Exception as exc:  # noqa: BLE001 - never fails the search it describes
            log.warning("Could not record the artifact retrieval: %s", exc)

    # --- citations ----------------------------------------------------------------------------------------------
    async def resolve(self, ref: str, *, include_unapproved: bool = False) -> dict[str, Any]:
        """What `A7.2` is: its content, who approved it and when, what it was written from, and whether it is
        still the version retrieval offers. Approved versions only, unless an admin asks for the rest."""
        match = _REF_RE.match(ref.strip())
        if match is None:
            raise ValidationFailedError("An artifact citation looks like A7.2.")
        async with self.factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT a.public_id, a.number, a.artifact_type, a.title, a.course_id, a.chapter_id, a.course_title, a.status,
                               v.version_no, v.review_status, v.content, v.asset, v.source_refs, v.generation_run_id, v.model_info,
                               v.approved_at, v.approved_by, v.review_comment, v.is_active,
                               (SELECT max(v2.version_no) FROM am_artifact_versions v2 WHERE v2.artifact_id = a.id AND v2.review_status = 'approved') AS newest_approved
                        FROM am_artifacts a JOIN am_artifact_versions v ON v.artifact_id = a.id WHERE a.number = :n AND v.version_no = :v
                        """
                    ),
                    {"n": int(match.group(1)), "v": int(match.group(2))},
                )
            ).first()
        if row is None or (row.review_status != "approved" and not include_unapproved):
            raise NotFoundError("No such approved artifact version.")
        return {**dict(row._mapping), "ref": f"A{row.number}.{row.version_no}", "superseded": row.review_status == "approved" and row.version_no != row.newest_approved}

    async def passages(self, refs: Sequence[str], *, limit: int = 50) -> list[SourceExcerpt]:
        """Approved artifact versions by citation, in the shape the course editor shows a cited passage in.

        Unknown, malformed and not-yet-approved references are left out, not reported: this answers "what do
        these citations point at", and an unapproved version is never something a course may cite."""
        wanted = []
        for ref in refs:
            match = _REF_RE.match(ref.strip())
            if match:
                wanted.append((int(match.group(1)), int(match.group(2))))
        wanted = list(dict.fromkeys(wanted))[:limit]
        if not wanted:
            return []
        params: dict[str, Any] = {}
        pairs = []
        for index, (number, version) in enumerate(wanted):
            params[f"n{index}"], params[f"v{index}"] = number, version
            pairs.append(f"(:n{index}, :v{index})")
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT a.number, v.version_no, a.public_id, a.artifact_type, a.title, a.course_title, v.search_text "
                        "FROM am_artifacts a JOIN am_artifact_versions v ON v.artifact_id = a.id "
                        f"WHERE v.review_status = 'approved' AND (a.number, v.version_no) IN ({', '.join(pairs)})"
                    ),
                    params,
                )
            ).all()
        return [
            SourceExcerpt(
                ref=f"A{r.number}.{r.version_no}", source_id=r.public_id, filename=r.title or r.artifact_type,
                heading=f"{r.artifact_type.replace('_', ' ')}" + (f" · from \"{r.course_title}\"" if r.course_title else ""),
                text=r.search_text[:6000],
            )
            for r in rows
        ]
