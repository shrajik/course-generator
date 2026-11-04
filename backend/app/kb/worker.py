"""The durable ingestion worker for the PostgreSQL knowledge base.

A version is processed by a job row, not by an in-process task, so a restart loses
nothing: the job is still there, and a job whose worker went silent is reclaimed once
its lease expires (heartbeat older than `retry.LEASE_SECONDS`). Several workers - several
uvicorn processes - can run at once; `FOR UPDATE SKIP LOCKED` hands each job to one.

The stages, in order, and what survives a failure:

1. parse + chunk each source file and any typed text;
2. **persist**: decide, by `lineage.plan_lineage`, which passages already exist (reuse the
   row, its citation number and its vector) and which are new, and write the new ones with
   no vector. This one transaction sets `checkpoint.persisted`, so a retry never plans
   twice (which would burn ordinals);
3. **embed**: vectors for passages that have none - first copied from any identical
   passage already embedded under the same model, then requested from the provider in
   batches, each batch saved as it completes. A retry embeds only what is still missing;
4. **classify**: item centroid, a proposed category, section topics, and a proposed
   title/description/tags (rules always; one small-model call if enabled). It can never
   fail ingestion;
5. the version becomes `ready`. It is **not** made live: publishing is an admin's action.

Failure handling is app/kb/retry.py's policy: permanent errors fail at once with a message
an admin can act on; transient ones are retried with backoff; when embedding retries run out
the version is still finished, searchable by keyword, with a warning.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db import kb_models as kb
from app.db.session import get_session_factory
from app.ingestion.chunker import chunk_document
from app.ingestion.parsers import IngestionError, parse, parse_text
from app.ingestion.retrieval import tokenize
from app.kb import classify, lineage, retry
from app.kb.service import attach_tags, bump_epoch, log_activity, move_category
from app.schemas.source import SourceChunk
from app.services.embedding_service import EmbeddingService, content_hash, get_embedding_service
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

_PARSE_TIMEOUT_SECONDS = 180
_AUTHORED = "authored"
_LOOKUP_BATCH = 1000


@dataclass
class Job:
    id: uuid.UUID
    version_id: uuid.UUID
    attempts: int
    max_attempts: int


class KbWorker:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        embeddings: EmbeddingService | None = None,
        ai: AIClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._factory = session_factory
        self.embeddings = embeddings or get_embedding_service()
        self._ai = ai
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    # --- the loop ----------------------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                handled = await self.run_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any one failure
                log.exception("Knowledge worker iteration failed")
                handled = False
            if not handled:
                try:
                    await asyncio.wait_for(stop.wait(), self.settings.kb_worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run_once(self) -> bool:
        """Claim and process one job. False when there was nothing to do."""
        job = await self._claim()
        if job is None:
            return False
        beat = asyncio.create_task(self._heartbeat(job.id))
        try:
            await self._process(job)
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
        return True

    async def drain(self, limit: int = 50) -> int:
        """Process jobs until none is ready (tests and scripts)."""
        handled = 0
        while handled < limit and await self.run_once():
            handled += 1
        return handled

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        interval = max(retry.LEASE_SECONDS / 3, 1.0)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.factory() as session, session.begin():
                    await session.execute(update(kb.KbJob).where(kb.KbJob.id == job_id).values(heartbeat_at=func.now()))
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat for job %s failed: %s", job_id, exc)

    # --- claiming -----------------------------------------------------------------------
    async def _claim(self) -> Job | None:
        async with self.factory() as session, session.begin():
            # A job that keeps killing its worker is not retried forever.
            await session.execute(
                text(
                    "UPDATE kb_jobs SET status = 'dead', last_error = 'The worker stopped repeatedly while processing this.', updated_at = now() "
                    "WHERE status = 'running' AND attempts >= max_attempts AND heartbeat_at < now() - make_interval(secs => :lease)"
                ),
                {"lease": retry.LEASE_SECONDS},
            )
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE kb_jobs SET status = 'running', locked_by = :worker, locked_at = now(), heartbeat_at = now(),
                                           attempts = attempts + 1, updated_at = now()
                        WHERE id = (
                            SELECT id FROM kb_jobs
                            WHERE (status = 'queued' AND run_after <= now())
                               OR (status = 'running' AND heartbeat_at < now() - make_interval(secs => :lease))
                            ORDER BY run_after
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        RETURNING id, version_id, attempts, max_attempts
                        """
                    ),
                    {"worker": self.worker_id, "lease": retry.LEASE_SECONDS},
                )
            ).first()
        return None if row is None else Job(row.id, row.version_id, row.attempts, row.max_attempts)

    # --- processing ------------------------------------------------------------------------
    async def _process(self, job: Job) -> None:
        try:
            await self._ingest(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - decided by the retry policy below
            await self._on_failure(job, exc)
            return
        await self._finish(job, warnings=[])

    async def _on_failure(self, job: Job, error: Exception) -> None:
        decision = retry.decide(error, attempts=job.attempts, max_attempts=job.max_attempts, degradable=isinstance(error, retry.EmbeddingIncomplete))
        log.warning("Knowledge job %s: %s (%s)", job.id, decision.action, error)
        message = str(error) if isinstance(error, IngestionError) or retry.classify_error(error) == "permanent" else "The document could not be processed."
        async with self.factory() as session, session.begin():
            if decision.action == "retry":
                await session.execute(
                    update(kb.KbJob).where(kb.KbJob.id == job.id).values(
                        status="queued", locked_by=None, last_error=str(error)[:500], updated_at=func.now(),
                        run_after=func.now() + timedelta(seconds=decision.delay_seconds),
                    )
                )
                return
            if decision.action == "degrade":
                pass
            else:
                status = "failed" if decision.action == "fail" else "dead"
                if decision.action == "dead":
                    message = f"Processing failed after {job.attempts} attempts. Try again later."
                await session.execute(update(kb.KbJob).where(kb.KbJob.id == job.id).values(status=status, locked_by=None, last_error=str(error)[:500], updated_at=func.now()))
                version = await session.get(kb.KbItemVersion, job.version_id)
                await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == job.version_id).values(processing_status="failed", processing_error=message[:1000]))
                if version is not None:
                    await log_activity(session, version.item_id, None, "processing_failed", version=version.version_no, error=message[:300])
                return
        # Embedding retries ran out: finish anyway, searchable by keyword.
        missing = await self._missing_vectors(job.version_id)
        await self._finish(job, warnings=[f"{missing} passage(s) have no embedding, so they are found by keyword search only."])

    async def _finish(self, job: Job, *, warnings: list[str]) -> None:
        try:
            await self._classify(job.version_id)
        except Exception:  # noqa: BLE001 - classification never fails a version
            log.exception("Classification failed for version %s", job.version_id)
            warnings = [*warnings, "Automatic classification could not run."]
        async with self.factory() as session, session.begin():
            version = await session.get(kb.KbItemVersion, job.version_id)
            await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == job.version_id).values(processing_status="ready", processing_error=None, warnings=warnings))
            await session.execute(update(kb.KbJob).where(kb.KbJob.id == job.id).values(status="succeeded", locked_by=None, last_error=None, updated_at=func.now()))
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == version.item_id).values(updated_at=func.now()))
            await log_activity(session, version.item_id, None, "processed", version=version.version_no, warnings=warnings)

    async def _set_status(self, version_id: uuid.UUID, status: str, job_id: uuid.UUID | None = None) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(update(kb.KbItemVersion).where(kb.KbItemVersion.id == version_id).values(processing_status=status))
            if job_id is not None:
                await session.execute(update(kb.KbJob).where(kb.KbJob.id == job_id).values(stage=status, heartbeat_at=func.now()))

    # --- stage 1-2: parse, chunk, persist ---------------------------------------------------
    async def _ingest(self, job: Job) -> None:
        async with self.factory() as session:
            version = await session.get(kb.KbItemVersion, job.version_id)
            if version is None:
                raise NotFoundError("Version not found")
            persisted = bool((await session.get(kb.KbJob, job.id)).checkpoint.get("persisted"))
        if not persisted:
            await self._persist(job, version)
        await self._set_status(job.version_id, "embedding", job.id)
        await self._embed(job.version_id)

    async def _parse_parts(self, version: kb.KbItemVersion) -> list[tuple[str, uuid.UUID | None, list[SourceChunk]]]:
        parts: list[tuple[str, uuid.UUID | None, list[SourceChunk]]] = []
        async with self.factory() as session:
            sources = (
                await session.execute(
                    select(kb.KbSource).join(kb.KbVersionSource, kb.KbVersionSource.source_id == kb.KbSource.id)
                    .where(kb.KbVersionSource.version_id == version.id).order_by(kb.KbVersionSource.position)
                )
            ).scalars().all()
        for source in sources:
            path = os.path.join(self.settings.data_dir, source.storage_key)
            try:
                data = await asyncio.to_thread(lambda p=path: open(p, "rb").read())
            except OSError as exc:
                raise IngestionError(f"The stored file '{source.filename}' is missing.") from exc
            try:
                parsed = await asyncio.wait_for(asyncio.to_thread(parse, source.kind, data), _PARSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise IngestionError("Reading the document took too long.") from exc
            if not source.word_count:  # what the document is, read once and kept with the file
                async with self.factory() as session, session.begin():
                    await session.execute(
                        update(kb.KbSource).where(kb.KbSource.id == source.id).values(
                            page_count=parsed.page_count, word_count=parsed.word_count, outline=parsed.outline()
                        )
                    )
            parts.append((source.sha256, source.id, await asyncio.to_thread(chunk_document, parsed)))
        if version.body_md:
            parsed = parse_text(version.body_md.encode("utf-8"), markdown=True)
            parts.append((_AUTHORED, None, await asyncio.to_thread(chunk_document, parsed)))
        return parts

    async def _persist(self, job: Job, version: kb.KbItemVersion) -> None:
        await self._set_status(job.version_id, "parsing", job.id)
        parts = await self._parse_parts(version)
        await self._set_status(job.version_id, "chunking", job.id)

        incoming: list[lineage.IncomingChunk] = []
        for part, source_id, chunks in parts:
            for chunk in chunks:
                incoming.append(lineage.IncomingChunk(content_hash(chunk.embed_text()), part, {"chunk": chunk, "source_id": source_id}))
        if not incoming:
            raise IngestionError("No usable text was found in the document.")

        model = self.settings.embedding_model
        async with self.factory() as session, session.begin():
            item = (await session.execute(select(kb.KbItem).where(kb.KbItem.id == version.item_id).with_for_update())).scalar_one()
            previous_id = await session.scalar(
                select(kb.KbItemVersion.id).where(kb.KbItemVersion.item_id == item.id, kb.KbItemVersion.version_no < version.version_no)
                .order_by(kb.KbItemVersion.version_no.desc()).limit(1)
            )
            existing = (await session.execute(select(kb.KbChunk).where(kb.KbChunk.item_id == item.id))).scalars().all()
            in_previous = set()
            if previous_id is not None:
                in_previous = set((await session.scalars(select(kb.KbVersionChunk.chunk_id).where(kb.KbVersionChunk.version_id == previous_id))).all())
            parts_by_source = {s.id: s.sha256 for s in (await session.execute(select(kb.KbSource).where(kb.KbSource.id.in_({c.source_id for c in existing if c.source_id})))).scalars().all()} if existing else {}

            def as_existing(chunk: kb.KbChunk) -> lineage.ExistingChunk:
                return lineage.ExistingChunk(chunk.id, chunk.ordinal, chunk.embed_hash, parts_by_source.get(chunk.source_id, _AUTHORED), chunk.embed_model, chunk.embedding is not None)

            plan = lineage.plan_lineage(
                [as_existing(c) for c in existing if c.id in in_previous],
                incoming,
                next_ordinal=item.next_ordinal,
                history=[as_existing(c) for c in existing if c.id not in in_previous],
                embed_model=model,
            )
            fresh_hashes = [p.incoming.embed_hash for p in plan.chunks if p.reuse is None]
            known = await self._known_vectors(session, fresh_hashes, model)

            rows, links = [], []
            for position, planned in enumerate(plan.chunks):
                if planned.reuse is not None:
                    chunk_id = planned.reuse.id
                else:
                    chunk_id = uuid.uuid4()
                    chunk: SourceChunk = planned.incoming.payload["chunk"]
                    terms = tokenize(chunk.embed_text())
                    vector = known.get(planned.incoming.embed_hash)
                    rows.append({
                        "id": chunk_id, "item_id": item.id, "ordinal": planned.ordinal, "source_id": planned.incoming.payload["source_id"],
                        "heading_path": list(chunk.heading_path), "heading": chunk.heading, "page": chunk.page, "text": chunk.text,
                        "words": chunk.words, "embed_hash": planned.incoming.embed_hash, "embedding": vector,
                        "embed_model": model if vector is not None else None, "tokens": sorted(set(terms)), "token_count": len(terms),
                    })
                links.append({"version_id": version.id, "chunk_id": chunk_id, "position": position})
            for start in range(0, len(rows), 200):
                await session.execute(insert(kb.KbChunk), rows[start : start + 200])
            await session.execute(insert(kb.KbVersionChunk), links)
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item.id).values(next_ordinal=plan.next_ordinal))
            await session.execute(update(kb.KbJob).where(kb.KbJob.id == job.id).values(checkpoint={"persisted": True, "reused": plan.reused, "created": plan.created}))
            await log_activity(session, item.id, None, "chunked", version=version.version_no, reused=plan.reused, created=plan.created, dropped=len(plan.dropped))

    # --- stage 3: embed ---------------------------------------------------------------------
    @staticmethod
    async def _known_vectors(session: AsyncSession, hashes: list[str], model: str) -> dict[str, list[float]]:
        """Vectors already paid for: any passage, in any item, with the same embedded text under
        the same model."""
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(hashes))
        for start in range(0, len(unique), _LOOKUP_BATCH):
            rows = await session.execute(
                select(kb.KbChunk.embed_hash, kb.KbChunk.embedding)
                .where(kb.KbChunk.embed_hash.in_(unique[start : start + _LOOKUP_BATCH]), kb.KbChunk.embed_model == model, kb.KbChunk.embedding.is_not(None))
            )
            for embed_hash, vector in rows.all():
                found.setdefault(embed_hash, [float(x) for x in vector])
        return found

    async def _missing_vectors(self, version_id: uuid.UUID) -> int:
        async with self.factory() as session:
            return await session.scalar(
                select(func.count()).select_from(kb.KbChunk).join(kb.KbVersionChunk, kb.KbVersionChunk.chunk_id == kb.KbChunk.id)
                .where(kb.KbVersionChunk.version_id == version_id, kb.KbChunk.embedding.is_(None))
            ) or 0

    async def _embed(self, version_id: uuid.UUID) -> None:
        if not self.embeddings.enabled:
            return  # switched off on purpose: keyword search only, not a failure
        model = self.settings.embedding_model
        async with self.factory() as session:
            pending = (
                await session.execute(
                    select(kb.KbChunk).join(kb.KbVersionChunk, kb.KbVersionChunk.chunk_id == kb.KbChunk.id)
                    .where(kb.KbVersionChunk.version_id == version_id, kb.KbChunk.embedding.is_(None)).order_by(kb.KbVersionChunk.position)
                )
            ).scalars().all()
            todo = [(c.id, c.embed_hash, f"{c.heading}\n{c.text}" if c.heading else c.text) for c in pending]
            known = await self._known_vectors(session, [h for _, h, _ in todo], model)
        if not todo:
            return
        async with self.factory() as session, session.begin():
            for chunk_id, embed_hash, _ in todo:
                if embed_hash in known:
                    await session.execute(update(kb.KbChunk).where(kb.KbChunk.id == chunk_id).values(embedding=known[embed_hash], embed_model=model))
        todo = [t for t in todo if t[1] not in known]
        size = self.settings.embedding_batch_size
        for start in range(0, len(todo), size):
            batch = todo[start : start + size]
            vectors = await self.embeddings.embed_batch([text_ for _, _, text_ in batch])
            async with self.factory() as session, session.begin():
                for (chunk_id, _, _), vector in zip(batch, vectors):
                    if vector is not None and len(vector) == self.settings.embedding_dimensions:
                        await session.execute(update(kb.KbChunk).where(kb.KbChunk.id == chunk_id).values(embedding=vector, embed_model=model))
            await self._touch(version_id)
        if await self._missing_vectors(version_id):
            raise retry.EmbeddingIncomplete("Some passages could not be embedded.")

    async def _touch(self, version_id: uuid.UUID) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(update(kb.KbJob).where(kb.KbJob.version_id == version_id, kb.KbJob.status == "running").values(heartbeat_at=func.now()))

    # --- stage 4: classify ----------------------------------------------------------------------
    def _client(self) -> AIClient:
        return self._ai or get_ai_client()

    async def _model_metadata(self, filename: str, outline: list[str], texts: list[str], existing_tags: list[str]) -> classify.MetadataSuggestion | None:
        if not self.settings.kb_ai_metadata:
            return None
        client = self._client()
        if client.is_mock:
            return None
        system, user = classify.build_metadata_prompt(filename, outline, texts, existing_tags)
        try:
            return await client.structured(
                schema=classify.MetadataSuggestion, system=system, user=user, model=self.settings.kb_metadata_model or None,
                purpose="kb_metadata", max_output_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001 - the free rules still produce a draft
            log.warning("Metadata suggestion failed, using rules: %s", exc)
            return None

    async def _classify(self, version_id: uuid.UUID) -> None:
        async with self.factory() as session:
            version = await session.get(kb.KbItemVersion, version_id)
            item = await session.get(kb.KbItem, version.item_id)
            chunks = (
                await session.execute(
                    select(kb.KbChunk).join(kb.KbVersionChunk, kb.KbVersionChunk.chunk_id == kb.KbChunk.id)
                    .where(kb.KbVersionChunk.version_id == version_id).order_by(kb.KbVersionChunk.position)
                )
            ).scalars().all()
            source_names = (
                await session.scalars(
                    select(kb.KbSource.filename).join(kb.KbVersionSource, kb.KbVersionSource.source_id == kb.KbSource.id)
                    .where(kb.KbVersionSource.version_id == version_id).order_by(kb.KbVersionSource.position)
                )
            ).all()
            outline: list[str] = []
            for source in (await session.scalars(select(kb.KbSource.outline).join(kb.KbVersionSource, kb.KbVersionSource.source_id == kb.KbSource.id).where(kb.KbVersionSource.version_id == version_id))).all():
                outline.extend(source or [])
            categories = [
                classify.CategoryCentroid(str(c.id), c.name, [float(x) for x in c.centroid], c.centroid_count)
                for c in (await session.execute(select(kb.KbCategory).where(kb.KbCategory.archived_at.is_(None), kb.KbCategory.is_suggested.is_(False), kb.KbCategory.centroid.is_not(None)))).scalars().all()
            ]
            existing_tags = list((await session.scalars(select(kb.KbTag.name).limit(50))).all())
            has_tags = await session.scalar(select(func.count()).select_from(kb.KbItemTag).where(kb.KbItemTag.item_id == item.id)) or 0

        vectors = [None if c.embedding is None else [float(x) for x in c.embedding] for c in chunks]
        centroid = classify.mean_vector(vectors)
        match = classify.match_category(centroid, categories)
        texts = [c.text for c in chunks]
        filename = source_names[0] if source_names else item.title
        model = await self._model_metadata(filename, outline, texts, existing_tags)
        suggestions, needs_review = classify.assemble_suggestions(filename=filename, outline=outline, chunks=texts, category=match, model=model)
        topics = classify.label_sections(vectors, categories)

        async with self.factory() as session, session.begin():
            item = (await session.execute(select(kb.KbItem).where(kb.KbItem.id == item.id).with_for_update())).scalar_one()
            for chunk, labels in zip(chunks, topics):
                if labels and not chunk.topics:
                    await session.execute(update(kb.KbChunk).where(kb.KbChunk.id == chunk.id).values(topics=labels))
            values: dict = {"centroid": centroid, "suggestions": suggestions, "needs_review": needs_review, "updated_at": func.now()}
            if version.version_no == 1:
                # The first version fills in what the admin left blank; later ones only refresh
                # the proposal, so nothing the admin chose is ever overwritten.
                if (item.properties or {}).get("title_auto") and suggestions["generated_by"] == "model" and suggestions["title"]:
                    values["title"] = suggestions["title"][:300]
                if not item.description and suggestions["description"]:
                    values["description"] = suggestions["description"][:2000]
                if item.category_id is None and match.status == "assigned":
                    target = uuid.UUID(match.category_id)
                    await move_category(session, None, target, centroid)
                    values["category_id"] = target
                if not has_tags:
                    await attach_tags(session, item.id, [t["name"] for t in suggestions["tags"][:6]], "ai")
            await session.execute(update(kb.KbItem).where(kb.KbItem.id == item.id).values(**values))
