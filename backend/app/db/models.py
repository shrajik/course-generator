"""Minimum relational models for the hybrid migration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.roles import DEFAULT_ROLE
from app.db.base import Base

# Must match `Settings.embedding_dimensions` (default: text-embedding-3-small's
# 1536) AND the Vector(...) width the migration creates the column with.
# Changing the embedding model to one with a different width needs a new
# migration (ALTER COLUMN ... TYPE vector(N)) alongside this constant.
EMBEDDING_DIMENSIONS = 1536


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=DEFAULT_ROLE.value)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    auth_sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="auth_sessions")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    template_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # Nullable: courses created before ownership existed have no owner, and a
    # deleted user's courses fall back to ownerless (admin-only) rather than
    # disappearing - see ON DELETE SET NULL in the migration.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Human review workflow (separate from `status`, which tracks the AI
    # generation pipeline - "draft" until the author submits it).
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # `passive_deletes=True` on all four: each child's FK already has
    # `ondelete="CASCADE"` at the DB level (see the migrations), and every one
    # of those FK columns is NOT NULL. Without `passive_deletes`, the ORM's
    # default behaviour on `session.delete(course)` is to try to null out
    # each child's FK itself before deleting the parent - which is not
    # cascade-delete, and which fails outright against a NOT NULL column
    # (`IntegrityError: null value in column "course_pk"`). `passive_deletes`
    # tells the ORM to leave deletion of these children to the database's own
    # ON DELETE CASCADE instead.
    document: Mapped["Document | None"] = relationship(
        back_populates="course", uselist=False, passive_deletes=True
    )
    blueprints: Mapped[list["Blueprint"]] = relationship(
        back_populates="course", passive_deletes=True
    )
    generation_runs: Mapped[list["GenerationRun"]] = relationship(
        back_populates="course", passive_deletes=True
    )
    activities: Mapped[list["CourseActivity"]] = relationship(
        back_populates="course", passive_deletes=True
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    course_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    course: Mapped[Course] = relationship(back_populates="document")


class Blueprint(Base):
    __tablename__ = "blueprints"
    __table_args__ = (UniqueConstraint("course_pk", name="uq_blueprints_course_pk"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    blueprint_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    course: Mapped[Course] = relationship(back_populates="blueprints")


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    course: Mapped[Course] = relationship(back_populates="generation_runs")


class Template(Base):
    """A course template. Rows are immutable once created - "editing" a
    template inserts a new row with `version + 1` and flips `is_current` so
    courses that already point at an older `template_id` (`{slug}_v{n}`,
    stored on `Course.template_id`) never see content change under them. See
    app/services/template_service.py.
    """

    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("slug", "version", name="uq_templates_slug_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The versioned id every course actually stores, e.g. "my_template_v3".
    template_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Full CourseTemplate payload (schemas/template.py) - reuses that schema
    # rather than reinventing columns for sections/theme/block_styles/etc.
    template_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The version a course picker / `load_template(slug)` resolves to. Only
    # one row per slug may be current at a time (enforced in the service).
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # Archiving hides a template family from new-course pickers without
    # touching any row a course already references.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VisualKnowledge(Base):
    """Registry of reusable visuals - points at an asset file that already
    exists under a course's own `assets/` directory (see StorageService); the
    bytes are never copied. This is what lets the AI memory layer discover
    and reference past diagrams/illustrations instead of regenerating from
    scratch every time.
    """

    __tablename__ = "visual_knowledge"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: a visual can be registered without (or after losing) its
    # source course - the asset_path is what actually resolves it.
    course_pk: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Course-relative path, e.g. "assets/image_003.svg" - resolved through
    # the same StorageService.asset_abs_path() every other asset uses.
    asset_path: Mapped[str] = mapped_column(String(300), nullable=False)
    # Denormalised alongside course_pk (same reasoning as CourseActivity.user_email
    # below): lets the registry keep reading sensibly, and avoids relationship
    # lazy-loads, which don't work across SQLAlchemy's async session boundary.
    course_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="illustration")
    diagram_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    # "Approved/locked" per the FRD - only approved visuals are offered back
    # to generation by default; anything registered starts unapproved.
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CourseSample(Base):
    """Marks an existing course as a reusable generation reference. Never
    copies course content - `course_pk` points at the real Course/Document
    rows, which are read fresh at retrieval time.
    """

    __tablename__ = "course_samples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_pk: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True, unique=True
    )
    course_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CourseActivity(Base):
    """Append-only audit log: one row per notable action on a course."""

    __tablename__ = "course_activities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_pk: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable + a denormalised email snapshot: the log should still read
    # sensibly ("jane@x.com approved this") even after the user is deleted.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    user_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    course: Mapped[Course] = relationship(back_populates="activities")


class MemoryEmbedding(Base):
    """One embedding vector for one memory source row - Template,
    VisualKnowledge, CourseSample or GenerationRun, identified by
    `(source_type, source_id)`. A dedicated table rather than a column on
    each of those four tables: it reuses one repository/service instead of
    duplicating embedding storage+invalidation four times, avoids altering
    four existing tables, and lets a hybrid query search across types (or
    just one) without four near-identical queries. `source_id` is a plain
    UUID with no FK constraint - it can point at any of the four tables, and
    a formal multi-table FK isn't expressible in SQL; services that hard-
    delete a source row (VisualKnowledge/CourseSample) also delete its
    embedding row explicitly.

    No row here does NOT mean "broken" - it just means "not embedded yet"
    (a record created before this feature, or created while embeddings were
    disabled/failing). Retrieval always falls back to keyword search for
    such rows; app/scripts/backfill_embeddings.py fills them in.
    """

    __tablename__ = "memory_embeddings"
    __table_args__ = (UniqueConstraint("source_type", "source_id", name="uq_memory_embeddings_source"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    # sha256 hex of the exact text that was embedded - lets the caller detect
    # "source content changed since this embedding was made" without ever
    # comparing floating-point vectors.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The text actually embedded, capped by the caller - useful for the admin
    # "embedding status" view and for debugging retrieval quality.
    embedded_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
