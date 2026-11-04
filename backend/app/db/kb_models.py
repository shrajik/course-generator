"""The admin knowledge base (Memory 1) in PostgreSQL.

Replaces the file store under `data/sources/global/` (see
app/services/knowledge_service.py). Nothing reads or writes these tables until the
Postgres backend is switched on, so the migration that creates them changes no
behaviour.

The shape, and why:

* An **item** is what an admin manages (title, category, tags, lifecycle); a
  **version** is an immutable snapshot of its content; a **source** is a stored
  file. One version can draw on many sources, and on text typed in directly.
* **Chunks are immutable and belong to the item**, not to a version. A version
  references them through `kb_version_chunks`, so an unchanged chunk is shared
  by every version that contains it and is never embedded twice.
* A chunk's `ordinal` is its identity within the item and is never reused:
  `G{item.number}.{ordinal}` is the citation, and it can only ever mean one text.
  Edited or removed chunks are retired (`retired_at`), not overwritten, so an old
  citation still resolves to the text it originally pointed at.
* `lifecycle_status` (the admin's decision: draft/published/...) is deliberately
  separate from a version's `processing_status` (what the pipeline has done) and
  from a job's `status` (whether the worker is running it).
* `is_live` marks chunks of published items' live versions. The vector and text
  indexes are partial on it, so draft, archived and superseded content never
  enters them - that is what keeps filtered semantic search accurate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models import EMBEDDING_DIMENSIONS

LIFECYCLE_STATUSES = ("draft", "published", "unpublished", "archived")
PROCESSING_STATUSES = ("queued", "parsing", "chunking", "embedding", "classifying", "ready", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")
ITEM_ORIGINS = ("migrated", "upload", "manual")
RELATION_TYPES = ("related_to", "references", "derived_from", "depends_on", "supersedes", "duplicate_of")

_ZERO_UUID = "'00000000-0000-0000-0000-000000000000'::uuid"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KbCategory(Base):
    """An admin-defined category, nested by `parent_id`. Independent of course
    generation. `is_suggested` marks one the classifier proposed and an admin has
    not yet approved; `centroid` (the mean of its items' vectors) is what new
    items are matched against."""

    __tablename__ = "kb_categories"
    __table_args__ = (
        Index("uq_kb_categories_parent_slug", func.coalesce(sql_text("parent_id"), sql_text(_ZERO_UUID)), "slug", unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_categories.id", ondelete="RESTRICT"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_suggested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    centroid_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class KbItem(Base):
    __tablename__ = "kb_items"
    __table_args__ = (
        CheckConstraint(_in("lifecycle_status", LIFECYCLE_STATUSES), name="ck_kb_items_lifecycle"),
        CheckConstraint(_in("origin", ITEM_ORIGINS), name="ck_kb_items_origin"),
        Index("ix_kb_items_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # The permanent number in `G{number}.{ordinal}` citations. Never reused: it comes
    # from a sequence, and a purged item keeps its row as a stub.
    number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, server_default=sql_text("nextval('kb_item_number_seq')"))
    # The id the existing API and stored course records use (`src_` + 12 hex);
    # migrated items keep their original one.
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft", index=True)
    # The version retrieval serves while published (kept after unpublishing so it can
    # be restored), and the newest version whether or not it is live.
    live_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_item_versions.id", use_alter=True, name="fk_kb_items_live_version"), nullable=True
    )
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_item_versions.id", use_alter=True, name="fk_kb_items_latest_version"), nullable=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    origin: Mapped[str] = mapped_column(String(16), nullable=False, server_default="upload")
    # Next chunk ordinal to hand out; ordinals are never reissued.
    next_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # What the classifier proposed and the admin has not yet accepted:
    # {"title": ..., "description": ..., "tags": [...], "category": {"id": ..., "name": ..., "confidence": 0.8}}
    suggestions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"), index=True)
    # Mean of the live chunk vectors: matches the item to categories and spots near-duplicates.
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft delete (trash). Restorable until purged.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Purge removes the content but keeps this row, so the number stays reserved and
    # activity history keeps its foreign key.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || coalesce(description, ''))", persisted=True),
    )


class KbItemVersion(Base):
    __tablename__ = "kb_item_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "version_no", name="uq_kb_item_versions_item_no"),
        CheckConstraint(_in("processing_status", PROCESSING_STATUSES), name="ck_kb_item_versions_processing"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-fatal findings, e.g. "12 passages have no embedding (keyword search only)".
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    # Knowledge written directly rather than uploaded; chunked like a document.
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()
    # When this version was live; lets a course's generation time be matched to it.
    live_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KbSource(Base):
    """A stored file, kept once however many items use it (unique by sha256)."""

    __tablename__ = "kb_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Where the original lives (a path under DATA_DIR today, a blob key later).
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    outline: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    extracted_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()


class KbVersionSource(Base):
    __tablename__ = "kb_version_sources"

    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_item_versions.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_sources.id", ondelete="RESTRICT"), primary_key=True, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class KbChunk(Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("item_id", "ordinal", name="uq_kb_chunks_item_ordinal"),
        Index("ix_kb_chunks_embed_hash", "embed_hash", "embed_model"),
        # Only live chunks are indexed (see the module docstring).
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sql_text("is_live AND embedding IS NOT NULL"),
        ),
        Index("ix_kb_chunks_tokens_live", "tokens", postgresql_using="gin", postgresql_where=sql_text("is_live")),
        Index("ix_kb_chunks_live", "item_id", postgresql_where=sql_text("is_live")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_sources.id", ondelete="SET NULL"), nullable=True)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]"))
    # " > ".join(heading_path), kept so the text index can include it.
    heading: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    words: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of the exact text that is embedded (heading + text): equal hash and model
    # means the vector can be reused instead of paying for it again.
    embed_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The distinct search terms of heading + text, produced by the same tokenizer the file
    # library used (app/ingestion/retrieval.tokenize), and how many term occurrences the
    # passage has. Keyword ranking uses them for document frequencies and length
    # normalisation, so it scores exactly as the file index did.
    tokens: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]"))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Section-level topic labels, so retrieval can filter inside a mixed document.
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]"))
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()


class KbVersionChunk(Base):
    __tablename__ = "kb_version_chunks"

    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_item_versions.id", ondelete="CASCADE"), primary_key=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_chunks.id", ondelete="CASCADE"), primary_key=True, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class KbTag(Base):
    __tablename__ = "kb_tags"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Lower-cased, for case-insensitive uniqueness.
    name_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _now()


class KbItemTag(Base):
    __tablename__ = "kb_item_tags"

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_tags.id", ondelete="CASCADE"), primary_key=True, index=True)
    # Who attached it: an admin, or the classifier.
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="admin")


class KbRelation(Base):
    """A typed edge between two items. Stored from the start; retrieval does not
    use it until the graph phase."""

    __tablename__ = "kb_relations"
    __table_args__ = (
        UniqueConstraint("from_item_id", "to_item_id", "relation_type", name="uq_kb_relations_edge"),
        CheckConstraint("from_item_id <> to_item_id", name="ck_kb_relations_not_self"),
        CheckConstraint(_in("relation_type", RELATION_TYPES), name="ck_kb_relations_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    from_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False, index=True)
    to_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # "admin", or "ai" for one the system proposed.
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="admin")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()


class KbEntity(Base):
    """A concept or named entity items mention. Schema only for now."""

    __tablename__ = "kb_entities"
    __table_args__ = (UniqueConstraint("normalized_name", "kind", name="uq_kb_entities_name_kind"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="concept")
    created_at: Mapped[datetime] = _now()


class KbItemEntity(Base):
    __tablename__ = "kb_item_entities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_chunks.id", ondelete="SET NULL"), nullable=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1")


class KbActivity(Base):
    """Who did what to an item. No cascade from the item: history outlives a purge."""

    __tablename__ = "kb_activity"
    __table_args__ = (Index("ix_kb_activity_item_created", "item_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_items.id", ondelete="RESTRICT"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()


class KbJob(Base):
    """One unit of durable background work (ingestion, re-embedding). A worker
    claims it with a lease and refreshes `heartbeat_at`; a lease that expires means
    the worker died and another may take it."""

    __tablename__ = "kb_jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_kb_jobs_status"),
        Index("ix_kb_jobs_claim", "status", "run_after"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_item_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, server_default="ingest")
    stage: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    run_after: Mapped[datetime] = _now()
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class KbRetrievalLog(Base):
    """Which passages were offered to which course chapter, and when: what ties a
    citation to the moment a course was generated. Survives a purge (the item
    number and ordinal are copied, the chunk link is not required)."""

    __tablename__ = "kb_retrieval_log"
    __table_args__ = (Index("ix_kb_retrieval_log_ref", "item_number", "ordinal"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    item_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_chunks.id", ondelete="SET NULL"), nullable=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    course_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chapter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class KbEpoch(Base):
    """One row. Bumped on any change to what is retrievable; caches compare it."""

    __tablename__ = "kb_epoch"
    __table_args__ = (CheckConstraint("id = 1", name="ck_kb_epoch_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = _now()


KB_TABLES = tuple(
    table.name for table in Base.metadata.sorted_tables if table.name.startswith("kb_")
)
