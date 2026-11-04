"""Artifact memory (Memory 2) tables - see migration 20261104_0013 for the reasoning.

SME-approved, immutable, reusable course artifacts. Wholly separate from the admin knowledge
base (`kb_models`): nothing here has a foreign key into it, and nothing there into this. The only
connection is a link that names a knowledge passage by citation number.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models import EMBEDDING_DIMENSIONS

ARTIFACT_TYPES = ("chapter", "visual", "questions", "assessment", "activity", "case_study", "other")
REVIEW_STATUSES = ("pending_review", "approved", "changes_requested")
LINK_TYPES = (
    "belongs_to_course", "contains_chapter", "references_knowledge", "derived_from",
    "related_to", "supersedes", "contains_visual", "contains_questions",
)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AmArtifact(Base):
    __tablename__ = "am_artifacts"
    __table_args__ = (
        UniqueConstraint("number", name="uq_am_artifacts_number"),
        UniqueConstraint("public_id", name="uq_am_artifacts_public_id"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_am_artifacts_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The permanent number in an `A{number}.{version}` citation.
    number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("nextval('am_artifact_number_seq')"))
    public_id: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    course_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    course_title: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("am_artifact_versions.id", use_alter=True, name="fk_am_artifacts_latest"), nullable=True)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("am_artifact_versions.id", use_alter=True, name="fk_am_artifacts_active"), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class AmArtifactVersion(Base):
    """One version of an artifact. Once approved it is locked - the database refuses to change it."""

    __tablename__ = "am_artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version_no", name="uq_am_versions_artifact_no"),
        CheckConstraint("locked = (review_status = 'approved')", name="ck_am_versions_locked_iff_approved"),
        CheckConstraint("(review_status = 'approved') = (approved_at IS NOT NULL)", name="ck_am_versions_approved_at"),
        CheckConstraint("NOT is_active OR review_status = 'approved'", name="ck_am_versions_active_is_approved"),
        Index(
            "ix_am_versions_embedding_hnsw", "embedding", postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"}, postgresql_where=sql_text("is_active AND embedding IS NOT NULL"),
        ),
        Index("ix_am_versions_tokens_active", "tokens", postgresql_using="gin", postgresql_where=sql_text("is_active")),
    )

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending_review")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    asset: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB(none_as_null=True), nullable=True)  # a visual's file; SQL NULL otherwise
    generation_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    source_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    tokens: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=sql_text("'{}'::text[]"))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))


class AmLink(Base):
    __tablename__ = "am_links"
    __table_args__ = (
        CheckConstraint(
            "(target_artifact_id IS NOT NULL)::int + (target_course_id IS NOT NULL)::int + (target_kb_number IS NOT NULL)::int = 1",
            name="ck_am_links_one_target",
        ),
        CheckConstraint("target_kb_ordinal IS NULL OR target_kb_number IS NOT NULL", name="ck_am_links_ordinal_needs_number"),
        CheckConstraint("target_artifact_id IS NULL OR target_artifact_id <> from_artifact_id", name="ck_am_links_not_self"),
        CheckConstraint(
            "(link_type = 'belongs_to_course' AND target_course_id IS NOT NULL) "
            "OR (link_type = 'references_knowledge' AND target_kb_number IS NOT NULL) "
            "OR (link_type NOT IN ('belongs_to_course', 'references_knowledge') AND target_artifact_id IS NOT NULL)",
            name="ck_am_links_type_matches_target",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    from_artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False)
    link_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=True, index=True)
    target_course_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_kb_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_kb_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _now()


class AmActivity(Base):
    __tablename__ = "am_activity"
    __table_args__ = (Index("ix_am_activity_artifact", "artifact_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()


class AmRetrievalLog(Base):
    __tablename__ = "am_retrieval_log"

    id: Mapped[uuid.UUID] = _pk()
    retrieved_at: Mapped[datetime] = _now()
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chapter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AmEpoch(Base):
    __tablename__ = "am_epoch"
    __table_args__ = (CheckConstraint("id = 1", name="ck_am_epoch_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = _now()


AM_TABLES = tuple(table.name for table in Base.metadata.sorted_tables if table.name.startswith("am_"))
