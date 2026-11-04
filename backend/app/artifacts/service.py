"""The artifact lifecycle: submit, review, approve (lock), version, archive, link.

    generated -> pending_review -> approved (locked, indexed)  |  changes_requested -> pending_review ...

* **Submitting** a course (the existing "submit for review" step) captures its artifacts as
  `pending_review`. Resubmitting finds the same artifacts again: unchanged ones are left alone,
  changed ones that were never approved are updated in place, and changed approved ones get a new
  version - approved work is never edited.
* **Approving** locks a version. It is the only way anything enters retrieval: the version becomes
  the artifact's active one and its vector is stored, in one transaction; the previous approved
  version stays available by citation but is no longer offered.
* **Editing** approved content is `new_version`: a fresh pending version that needs its own approval.
* The database enforces the lock (a trigger refuses any change to an approved version), so no code
  path here - or added later - can alter approved work by accident.

Nothing here reads or writes the admin knowledge base. A link to knowledge is a citation number.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.artifacts.extract import ArtifactDraft, canonical_hash, embedding_text, extract_artifacts, text_of
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.course.fact_check import extract_refs
from app.db import am_models as am
from app.db.session import get_session_factory
from app.ingestion.retrieval import tokenize
from app.schemas.document import CourseDocument
from app.services.embedding_service import EmbeddingService, get_embedding_service

log = get_logger(__name__)

_KB_REF_RE = re.compile(r"^G(\d+)\.(\d+)$")
_CHILD_LINK = {"visual": "contains_visual", "questions": "contains_questions", "assessment": "contains_questions"}


def _uuid(value: object) -> uuid.UUID | None:
    """A user id as a UUID, or None for anything that is not one: history records who when it can."""
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class SyncResult:
    created: int = 0
    new_versions: int = 0
    updated: int = 0
    resubmitted: int = 0
    unchanged: int = 0
    duplicates: list[str] = field(default_factory=list)


async def _bump_epoch(session: AsyncSession) -> None:
    await session.execute(update(am.AmEpoch).where(am.AmEpoch.id == 1).values(epoch=am.AmEpoch.epoch + 1, updated_at=func.now()))


async def _log(session: AsyncSession, artifact_id: uuid.UUID, version_id: uuid.UUID | None, actor: object, action: str, **detail: Any) -> None:
    await session.execute(
        insert(am.AmActivity),
        [{"id": uuid.uuid4(), "artifact_id": artifact_id, "version_id": version_id, "actor_id": _uuid(actor), "action": action, "detail": detail}],
    )


def _now() -> datetime:
    """For bulk inserts, which take values rather than SQL expressions."""
    return datetime.now(timezone.utc)


def _indexed(search_text: str) -> dict[str, Any]:
    terms = tokenize(search_text)
    return {"tokens": sorted(set(terms)), "token_count": len(terms)}


class ArtifactService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        embeddings: EmbeddingService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._factory = session_factory
        self.embeddings = embeddings or get_embedding_service()

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self._factory or get_session_factory()

    async def _lock(self, session: AsyncSession, artifact_id: uuid.UUID) -> am.AmArtifact:
        artifact = (await session.execute(select(am.AmArtifact).where(am.AmArtifact.id == artifact_id).with_for_update())).scalar_one_or_none()
        if artifact is None:
            raise NotFoundError("Artifact not found")
        return artifact

    # --- submitting a course ---------------------------------------------------------------
    async def sync_course(
        self,
        *,
        course_id: str,
        course_title: str,
        document: CourseDocument,
        offered: Mapping[str, Sequence[str]] | None = None,
        generation_run_id: str | None = None,
        model_info: Mapping[str, Any] | None = None,
        actor_id: object = None,
    ) -> SyncResult:
        """Capture the document's artifacts for review. Safe to repeat: see the module docstring."""
        drafts = extract_artifacts(document, offered=offered)
        result = SyncResult()
        ids: dict[str, uuid.UUID] = {}
        async with self.factory() as session, session.begin():
            for draft in drafts:
                ids[draft.key] = await self._sync_one(session, course_id, course_title, draft, generation_run_id, dict(model_info or {}), actor_id, result)
            for draft in drafts:  # links need every artifact to exist first
                await self._link_draft(session, ids, course_id, draft)
            if drafts:
                await _bump_epoch(session)
        return result

    async def _sync_one(self, session, course_id, course_title, draft: ArtifactDraft, run_id, model_info, actor_id, result: SyncResult) -> uuid.UUID:
        existing = (
            await session.execute(select(am.AmArtifact).where(am.AmArtifact.course_id == course_id, am.AmArtifact.key == draft.key).with_for_update())
        ).scalar_one_or_none()
        fields = {
            "content": draft.content, "content_hash": draft.content_hash, "search_text": draft.search_text, "asset": draft.asset,
            "source_refs": list(draft.source_refs), "generation_run_id": run_id, "model_info": model_info, **_indexed(draft.search_text),
        }
        now = func.now()
        if existing is None:
            artifact_id, version_id = uuid.uuid4(), uuid.uuid4()
            await session.execute(
                insert(am.AmArtifact),
                [{"id": artifact_id, "public_id": f"art_{uuid.uuid4().hex[:12]}", "artifact_type": draft.artifact_type, "title": draft.title,
                  "course_id": course_id, "chapter_id": draft.chapter_id, "course_title": course_title[:300], "key": draft.key, "created_by": _uuid(actor_id)}],
            )
            await session.execute(insert(am.AmArtifactVersion), [{"id": version_id, "artifact_id": artifact_id, "version_no": 1, "created_by": _uuid(actor_id), "submitted_at": _now(), **fields}])
            await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == artifact_id).values(latest_version_id=version_id))
            await _log(session, artifact_id, version_id, actor_id, "submitted", version=1)
            result.created += 1
            twin = await session.scalar(
                select(am.AmArtifact.public_id).join(am.AmArtifactVersion, am.AmArtifactVersion.id == am.AmArtifact.active_version_id)
                .where(am.AmArtifactVersion.content_hash == draft.content_hash, am.AmArtifact.artifact_type == draft.artifact_type, am.AmArtifact.id != artifact_id).limit(1)
            )
            if twin is not None:  # identical to approved work from elsewhere: noted, never merged
                result.duplicates.append(twin)
                await _log(session, artifact_id, version_id, actor_id, "duplicate_noted", of=twin)
            return artifact_id

        latest = await session.get(am.AmArtifactVersion, existing.latest_version_id)
        await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == existing.id).values(title=draft.title, course_title=course_title[:300], updated_at=now))
        if latest.content_hash == draft.content_hash:
            if latest.review_status == "changes_requested":  # resubmitted as it was: back in the queue
                await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == latest.id).values(review_status="pending_review", submitted_at=now))
                await _log(session, existing.id, latest.id, actor_id, "resubmitted", version=latest.version_no)
                result.resubmitted += 1
            else:
                result.unchanged += 1
        elif not latest.locked:  # never approved: still the author's to change
            await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == latest.id).values(review_status="pending_review", submitted_at=now, **fields))
            await _log(session, existing.id, latest.id, actor_id, "updated", version=latest.version_no)
            result.updated += 1
        else:  # approved work is never edited: this is the next version
            version_id = uuid.uuid4()
            await session.execute(insert(am.AmArtifactVersion), [{"id": version_id, "artifact_id": existing.id, "version_no": latest.version_no + 1, "created_by": _uuid(actor_id), "submitted_at": _now(), **fields}])
            await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == existing.id).values(latest_version_id=version_id))
            await _log(session, existing.id, version_id, actor_id, "submitted", version=latest.version_no + 1)
            result.new_versions += 1
        return existing.id

    async def _link_draft(self, session, ids: dict[str, uuid.UUID], course_id: str, draft: ArtifactDraft) -> None:
        source = ids[draft.key]
        await self._add_link(session, source, "belongs_to_course", target_course_id=course_id)
        for ref in draft.source_refs:
            match = _KB_REF_RE.match(ref)
            if match:  # admin knowledge it was written from; the other memory is only ever named, never joined
                await self._add_link(session, source, "references_knowledge", target_kb_number=int(match.group(1)), target_kb_ordinal=int(match.group(2)))
        for child_key in draft.children:
            await self._add_link(session, source, _CHILD_LINK.get(await self._type_of(session, ids[child_key]), "related_to"), target_artifact_id=ids[child_key])

    @staticmethod
    async def _type_of(session: AsyncSession, artifact_id: uuid.UUID) -> str:
        return await session.scalar(select(am.AmArtifact.artifact_type).where(am.AmArtifact.id == artifact_id))

    @staticmethod
    async def _add_link(session: AsyncSession, source: uuid.UUID, link_type: str, *, note: str = "", created_by: uuid.UUID | None = None, **target: Any) -> None:
        await session.execute(
            text(
                "INSERT INTO am_links (id, from_artifact_id, link_type, target_artifact_id, target_course_id, target_kb_number, target_kb_ordinal, note, created_by) "
                "VALUES (:id, :f, :t, :ta, :tc, :kn, :ko, :note, :by) ON CONFLICT DO NOTHING"
            ),
            {"id": uuid.uuid4(), "f": source, "t": link_type, "ta": target.get("target_artifact_id"), "tc": target.get("target_course_id"),
             "kn": target.get("target_kb_number"), "ko": target.get("target_kb_ordinal"), "note": note, "by": created_by},
        )

    # --- review ---------------------------------------------------------------------------------------
    async def approve_course(self, course_id: str, reviewer_id: object, comment: str | None = None) -> int:
        """The course was approved: every artifact version pending for it is approved with it,
        together or not at all. Returns how many."""
        async with self.factory() as session:
            pending = (
                await session.execute(
                    select(am.AmArtifactVersion).join(am.AmArtifact, am.AmArtifact.id == am.AmArtifactVersion.artifact_id)
                    .where(am.AmArtifact.course_id == course_id, am.AmArtifactVersion.review_status == "pending_review")
                )
            ).scalars().all()
            wanted = [(v.id, v.search_text) for v in pending]
        if not wanted:
            return 0
        vectors = await self._embed([t for _, t in wanted])
        async with self.factory() as session, session.begin():
            for (version_id, _), vector in zip(wanted, vectors):
                await self._approve(session, version_id, reviewer_id, comment, vector)
            await _bump_epoch(session)
        return len(wanted)

    async def approve_version(self, version_id: uuid.UUID, reviewer_id: object, comment: str | None = None) -> None:
        """Approve one artifact on its own (SME review of a single chapter, visual or question set)."""
        async with self.factory() as session:
            version = await session.get(am.AmArtifactVersion, version_id)
            if version is None:
                raise NotFoundError("Version not found")
            search_text = version.search_text
        (vector,) = await self._embed([search_text])
        async with self.factory() as session, session.begin():
            await self._approve(session, version_id, reviewer_id, comment, vector)
            await _bump_epoch(session)

    async def _embed(self, texts: list[str]) -> list[list[float] | None]:
        return await self.embeddings.embed_batch([embedding_text(t) for t in texts])

    async def _approve(self, session: AsyncSession, version_id: uuid.UUID, reviewer_id: object, comment: str | None, vector: list[float] | None) -> None:
        version = await session.get(am.AmArtifactVersion, version_id)
        artifact = await self._lock(session, version.artifact_id)
        await session.refresh(version)  # the state after we hold the lock
        if version.review_status != "pending_review":
            raise ConflictError("Only a version waiting for review can be approved.")
        if artifact.latest_version_id != version.id:
            raise ConflictError("A newer version exists; review that one.")
        reviewer = _uuid(reviewer_id)
        await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.artifact_id == artifact.id, am.AmArtifactVersion.is_active).values(is_active=False))
        await session.execute(
            update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == version.id).values(
                review_status="approved", locked=True, is_active=artifact.status == "active", approved_by=reviewer, approved_at=func.now(),
                reviewed_by=reviewer, reviewed_at=func.now(), review_comment=comment,
                embedding=vector, embed_model=self.settings.embedding_model if vector is not None else None,
            )
        )
        await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == artifact.id).values(active_version_id=version.id, updated_at=func.now()))
        await _log(session, artifact.id, version.id, reviewer_id, "approved", version=version.version_no, indexed=vector is not None)

    async def request_changes_course(self, course_id: str, reviewer_id: object, comment: str) -> int:
        """The course was sent back: its pending versions go back to the author with the reviewer's reason."""
        async with self.factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(am.AmArtifactVersion.id, am.AmArtifactVersion.artifact_id, am.AmArtifactVersion.version_no)
                    .join(am.AmArtifact, am.AmArtifact.id == am.AmArtifactVersion.artifact_id)
                    .where(am.AmArtifact.course_id == course_id, am.AmArtifactVersion.review_status == "pending_review")
                )
            ).all()
            for row in rows:
                await self._request_changes(session, row.id, row.artifact_id, row.version_no, reviewer_id, comment)
        return len(rows)

    async def request_changes_version(self, version_id: uuid.UUID, reviewer_id: object, comment: str) -> None:
        if not comment.strip():
            raise ValidationFailedError("A reason is required when requesting changes.")
        async with self.factory() as session, session.begin():
            version = await session.get(am.AmArtifactVersion, version_id)
            if version is None:
                raise NotFoundError("Version not found")
            if version.review_status != "pending_review":
                raise ConflictError("Only a version waiting for review can be sent back.")
            await self._request_changes(session, version.id, version.artifact_id, version.version_no, reviewer_id, comment)

    @staticmethod
    async def _request_changes(session, version_id, artifact_id, version_no, reviewer_id, comment: str) -> None:
        await session.execute(
            update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == version_id, am.AmArtifactVersion.review_status == "pending_review").values(
                review_status="changes_requested", reviewed_by=_uuid(reviewer_id), reviewed_at=func.now(), review_comment=comment
            )
        )
        await _log(session, artifact_id, version_id, reviewer_id, "changes_requested", version=version_no, comment=comment[:500])

    # --- editing and archiving -----------------------------------------------------------------------------
    async def new_version(
        self,
        artifact_id: uuid.UUID,
        content: dict[str, Any],
        *,
        actor_id: object,
        title: str | None = None,
        asset: dict[str, Any] | None = None,
        change_note: str = "",
    ) -> uuid.UUID:
        """Change an artifact's content. Approved work is never edited, so this is a new version that
        needs its own approval (and the approved one keeps being what retrieval offers meanwhile). A
        version still awaiting review or sent back is updated in place - nobody has approved it yet."""
        if not isinstance(content, dict) or not content:
            raise ValidationFailedError("The content cannot be empty.")
        search = text_of(content)
        fields = {
            "content": content, "content_hash": canonical_hash(content), "search_text": search, "asset": asset,
            "source_refs": list(dict.fromkeys(extract_refs(search))), **_indexed(search),
        }
        async with self.factory() as session, session.begin():
            artifact = await self._lock(session, artifact_id)
            latest = await session.get(am.AmArtifactVersion, artifact.latest_version_id)
            if latest.content_hash == fields["content_hash"] and asset == latest.asset:
                raise ConflictError("That is the same as the current version.")
            values = {"title": " ".join(title.split())[:300], "updated_at": func.now()} if title and title.strip() else {"updated_at": func.now()}
            if latest.locked:
                version_id = uuid.uuid4()
                await session.execute(insert(am.AmArtifactVersion), [{"id": version_id, "artifact_id": artifact_id, "version_no": latest.version_no + 1, "created_by": _uuid(actor_id), "submitted_at": _now(), "generation_run_id": None, "model_info": {"edited": True, "note": change_note[:300]}, **fields}])
                values["latest_version_id"] = version_id
                action, number = "version_added", latest.version_no + 1
            else:
                version_id = latest.id
                await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == version_id).values(review_status="pending_review", submitted_at=func.now(), **fields))
                action, number = "updated", latest.version_no
            await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == artifact_id).values(**values))
            await _log(session, artifact_id, version_id, actor_id, action, version=number, note=change_note[:300])
            return version_id

    async def set_archived(self, artifact_id: uuid.UUID, archived: bool, *, actor_id: object) -> None:
        """Archiving takes the artifact out of retrieval without losing anything; restoring puts its
        active approved version back."""
        async with self.factory() as session, session.begin():
            artifact = await self._lock(session, artifact_id)
            if (artifact.status == "archived") == archived:
                raise ConflictError("It is already " + ("archived." if archived else "active."))
            await session.execute(update(am.AmArtifact).where(am.AmArtifact.id == artifact_id).values(status="archived" if archived else "active", updated_at=func.now()))
            if artifact.active_version_id is not None:
                await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == artifact.active_version_id).values(is_active=not archived))
            await _bump_epoch(session)
            await _log(session, artifact_id, None, actor_id, "archived" if archived else "restored")

    # --- links ------------------------------------------------------------------------------------------------
    async def add_link(
        self, from_artifact: uuid.UUID, link_type: str, *, to_artifact: uuid.UUID | None = None, course_id: str | None = None,
        kb_number: int | None = None, kb_ordinal: int | None = None, note: str = "", actor_id: object = None,
    ) -> None:
        if link_type not in am.LINK_TYPES:
            raise ValidationFailedError(f"Unknown relationship '{link_type}'.")
        async with self.factory() as session:
            try:
                async with session.begin():
                    await self._lock(session, from_artifact)
                    if to_artifact is not None and await session.get(am.AmArtifact, to_artifact) is None:
                        raise NotFoundError("Artifact not found")
                    if link_type == "supersedes" and to_artifact is not None and await self._supersedes_reaches(session, to_artifact, from_artifact):
                        raise ConflictError("That would make an artifact supersede itself through a chain.")
                    await self._add_link(session, from_artifact, link_type, note=note[:1000], created_by=_uuid(actor_id), target_artifact_id=to_artifact,
                                         target_course_id=course_id, target_kb_number=kb_number, target_kb_ordinal=kb_ordinal)
                    await _log(session, from_artifact, None, actor_id, "link_added", type=link_type)
            except Exception as exc:  # the database's own checks say what a link may point at
                from sqlalchemy.exc import IntegrityError

                if isinstance(exc, IntegrityError):
                    raise ValidationFailedError("That relationship does not fit: check its type and its target.") from exc
                raise

    @staticmethod
    async def _supersedes_reaches(session: AsyncSession, start: uuid.UUID, target: uuid.UUID) -> bool:
        row = await session.execute(
            text(
                "WITH RECURSIVE chain AS (SELECT target_artifact_id AS id FROM am_links WHERE from_artifact_id = :s AND link_type = 'supersedes' "
                "UNION SELECT l.target_artifact_id FROM am_links l JOIN chain c ON l.from_artifact_id = c.id WHERE l.link_type = 'supersedes') "
                "SELECT 1 FROM chain WHERE id = :t LIMIT 1"
            ),
            {"s": start, "t": target},
        )
        return row.first() is not None

    async def remove_link(self, link_id: uuid.UUID) -> None:
        async with self.factory() as session, session.begin():
            if (await session.execute(text("DELETE FROM am_links WHERE id = :i RETURNING id"), {"i": link_id})).first() is None:
                raise NotFoundError("Relationship not found")

    # --- indexing and integrity --------------------------------------------------------------------------------
    async def index_missing(self, limit: int = 100) -> int:
        """Embed approved versions that were approved while embedding was unavailable. The lock allows
        exactly this: a missing vector may be filled in once, never changed."""
        async with self.factory() as session:
            rows = (await session.execute(select(am.AmArtifactVersion.id, am.AmArtifactVersion.search_text).where(am.AmArtifactVersion.locked, am.AmArtifactVersion.embedding.is_(None)).limit(limit))).all()
        if not rows:
            return 0
        vectors = await self._embed([r.search_text for r in rows])
        done = 0
        async with self.factory() as session, session.begin():
            for row, vector in zip(rows, vectors):
                if vector is not None:
                    await session.execute(update(am.AmArtifactVersion).where(am.AmArtifactVersion.id == row.id).values(embedding=vector, embed_model=self.settings.embedding_model))
                    done += 1
            if done:
                await _bump_epoch(session)
        return done

    async def check_invariants(self) -> list[str]:
        """Disagreements between an artifact's state and what retrieval can see. Empty means consistent."""
        checks = {
            "versions are active although they are not their artifact's active approved version, or it is archived": """
                SELECT count(*) FROM am_artifact_versions v JOIN am_artifacts a ON a.id = v.artifact_id
                WHERE v.is_active AND NOT (a.active_version_id = v.id AND a.status = 'active' AND v.review_status = 'approved')""",
            "active, unarchived artifacts have an approved version that is not active": """
                SELECT count(*) FROM am_artifacts a JOIN am_artifact_versions v ON v.id = a.active_version_id
                WHERE a.status = 'active' AND NOT v.is_active""",
            "an artifact has more than one active version": """
                SELECT count(*) FROM (SELECT artifact_id FROM am_artifact_versions WHERE is_active GROUP BY artifact_id HAVING count(*) > 1) d""",
        }
        problems = []
        async with self.factory() as session:
            for message, sql in checks.items():
                if count := (await session.execute(text(sql))).scalar():
                    problems.append(f"{count} {message}")
        return problems


def public_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_service: ArtifactService | None = None


def get_artifact_service() -> ArtifactService:
    global _service
    if _service is None:
        _service = ArtifactService()
    return _service
