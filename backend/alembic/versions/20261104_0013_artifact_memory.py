"""Artifact memory (Memory 2): SME-approved, immutable, reusable course artifacts.

Additive only. Entirely separate from the admin knowledge base (`kb_*`): its own tables,
sequence and epoch, and no foreign key into Memory 1 - a link to knowledge points at a
passage by its citation number (`G{number}.{ordinal}`), which never changes meaning. The
two memories share no storage, permissions or lifecycle; only the links connect them.

The rule that matters most is enforced here, in the database, not left to application code:
a version that has been approved is **locked**, and a trigger refuses any change to its
content or provenance and any attempt to delete it. Editing approved work means a new version
(which needs its own approval). The only things that may still change on a locked version are
`is_active` (which approved version is current) and, once, a missing `embedding` being filled.

Revision ID: 20261104_0013
Revises: 20261103_0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20261104_0013"
down_revision = "20261103_0012"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 1536  # must match app.db.models.EMBEDDING_DIMENSIONS

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
EMPTY_OBJECT = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'[]'::jsonb")

ARTIFACT_TYPES = ("chapter", "visual", "questions", "assessment", "activity", "case_study", "other")
REVIEW_STATUSES = ("pending_review", "approved", "changes_requested")
LINK_TYPES = (
    "belongs_to_course", "contains_chapter", "references_knowledge", "derived_from",
    "related_to", "supersedes", "contains_visual", "contains_questions",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


_GUARD = """
CREATE FUNCTION am_guard_locked_version() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.locked THEN
            RAISE EXCEPTION 'An approved artifact version cannot be deleted' USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.locked THEN
        IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id OR NEW.version_no IS DISTINCT FROM OLD.version_no
           OR NEW.content IS DISTINCT FROM OLD.content OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
           OR NEW.search_text IS DISTINCT FROM OLD.search_text OR NEW.asset IS DISTINCT FROM OLD.asset
           OR NEW.tokens IS DISTINCT FROM OLD.tokens OR NEW.token_count IS DISTINCT FROM OLD.token_count
           OR NEW.generation_run_id IS DISTINCT FROM OLD.generation_run_id OR NEW.model_info IS DISTINCT FROM OLD.model_info
           OR NEW.source_refs IS DISTINCT FROM OLD.source_refs
           OR NEW.review_status IS DISTINCT FROM OLD.review_status OR NEW.locked IS DISTINCT FROM OLD.locked
           OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at OR NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
           OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at OR NEW.review_comment IS DISTINCT FROM OLD.review_comment
           OR NEW.approved_by IS DISTINCT FROM OLD.approved_by OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
           OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at
           -- A vector may be filled in once, if indexing had to wait; never changed.
           OR (OLD.embedding IS NOT NULL AND (NEW.embedding IS DISTINCT FROM OLD.embedding OR NEW.embed_model IS DISTINCT FROM OLD.embed_model))
        THEN
            RAISE EXCEPTION 'An approved artifact version is immutable: create a new version instead' USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SEQUENCE am_artifact_number_seq START WITH 1 INCREMENT BY 1")

    op.create_table(
        "am_artifacts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("number", sa.Integer(), nullable=False, server_default=sa.text("nextval('am_artifact_number_seq')")),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("artifact_type", sa.String(24), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("course_id", sa.String(64), nullable=True),
        sa.Column("chapter_id", sa.String(64), nullable=True),
        sa.Column("course_title", sa.String(300), nullable=False, server_default=""),
        # Identity within a course ("chapter:ch_2", "block:blk_9"): re-submitting the same course finds the
        # same artifact again instead of creating a second one.
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("properties", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("latest_version_id", UUID, nullable=True),
        sa.Column("active_version_id", UUID, nullable=True),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("number", name="uq_am_artifacts_number"),
        sa.UniqueConstraint("public_id", name="uq_am_artifacts_public_id"),
        sa.CheckConstraint(_in("artifact_type", ARTIFACT_TYPES), name="ck_am_artifacts_type"),
        sa.CheckConstraint(_in("status", ("active", "archived")), name="ck_am_artifacts_status"),
    )
    op.execute("CREATE UNIQUE INDEX uq_am_artifacts_course_key ON am_artifacts (course_id, key) WHERE course_id IS NOT NULL")
    op.create_index("ix_am_artifacts_course", "am_artifacts", ["course_id"])
    op.create_index("ix_am_artifacts_type", "am_artifacts", ["artifact_type"])

    op.create_table(
        "am_artifact_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("artifact_id", UUID, sa.ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending_review"),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("asset", JSONB, nullable=True),
        sa.Column("generation_run_id", sa.String(64), nullable=True),
        sa.Column("model_info", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        # What it was written from: citations such as G2.5 (admin knowledge) and S1.3 (a course's own sources).
        sa.Column("source_refs", JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("tokens", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("embed_model", sa.String(100), nullable=True),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("submitted_at", TS, nullable=True),
        sa.Column("reviewed_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", TS, nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("approved_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("artifact_id", "version_no", name="uq_am_versions_artifact_no"),
        sa.CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_am_versions_review"),
        # Approved and locked are the same fact; only approved work can be the active version.
        sa.CheckConstraint("locked = (review_status = 'approved')", name="ck_am_versions_locked_iff_approved"),
        sa.CheckConstraint("(review_status = 'approved') = (approved_at IS NOT NULL)", name="ck_am_versions_approved_at"),
        sa.CheckConstraint("NOT is_active OR review_status = 'approved'", name="ck_am_versions_active_is_approved"),
    )
    op.create_index("ix_am_versions_artifact", "am_artifact_versions", ["artifact_id"])
    op.create_index("ix_am_versions_embed_hash", "am_artifact_versions", ["content_hash"])
    op.create_foreign_key("fk_am_artifacts_latest", "am_artifacts", "am_artifact_versions", ["latest_version_id"], ["id"])
    op.create_foreign_key("fk_am_artifacts_active", "am_artifacts", "am_artifact_versions", ["active_version_id"], ["id"])
    # Only the active, approved version of each artifact is indexed: pending, rejected and superseded
    # versions can never appear in normal retrieval, and never crowd out a result.
    op.execute(
        "CREATE INDEX ix_am_versions_embedding_hnsw ON am_artifact_versions USING hnsw (embedding vector_cosine_ops) "
        "WHERE is_active AND embedding IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_am_versions_tokens_active ON am_artifact_versions USING gin (tokens) WHERE is_active")
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER am_guard_locked_version BEFORE UPDATE OR DELETE ON am_artifact_versions "
        "FOR EACH ROW EXECUTE FUNCTION am_guard_locked_version()"
    )

    op.create_table(
        "am_links",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("from_artifact_id", UUID, sa.ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("link_type", sa.String(24), nullable=False),
        # Exactly one target. A course is named by its id; admin knowledge (Memory 1) by citation number -
        # no foreign key across the two memories.
        sa.Column("target_artifact_id", UUID, sa.ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("target_course_id", sa.String(64), nullable=True),
        sa.Column("target_kb_number", sa.Integer(), nullable=True),
        sa.Column("target_kb_ordinal", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(_in("link_type", LINK_TYPES), name="ck_am_links_type"),
        sa.CheckConstraint(
            "(target_artifact_id IS NOT NULL)::int + (target_course_id IS NOT NULL)::int + (target_kb_number IS NOT NULL)::int = 1",
            name="ck_am_links_one_target",
        ),
        sa.CheckConstraint("target_kb_ordinal IS NULL OR target_kb_number IS NOT NULL", name="ck_am_links_ordinal_needs_number"),
        sa.CheckConstraint("target_artifact_id IS NULL OR target_artifact_id <> from_artifact_id", name="ck_am_links_not_self"),
        sa.CheckConstraint(
            "(link_type = 'belongs_to_course' AND target_course_id IS NOT NULL) "
            "OR (link_type = 'references_knowledge' AND target_kb_number IS NOT NULL) "
            "OR (link_type NOT IN ('belongs_to_course', 'references_knowledge') AND target_artifact_id IS NOT NULL)",
            name="ck_am_links_type_matches_target",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_am_links_edge ON am_links (from_artifact_id, link_type, "
        "coalesce(target_artifact_id::text, ''), coalesce(target_course_id, ''), coalesce(target_kb_number, -1), coalesce(target_kb_ordinal, -1))"
    )
    op.create_index("ix_am_links_target_artifact", "am_links", ["target_artifact_id"])
    op.create_index("ix_am_links_target_kb", "am_links", ["target_kb_number", "target_kb_ordinal"])

    op.create_table(
        "am_activity",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("artifact_id", UUID, sa.ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_id", UUID, nullable=True),
        sa.Column("actor_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_am_activity_artifact", "am_activity", ["artifact_id", "created_at"])

    op.create_table(
        "am_retrieval_log",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("retrieved_at", TS, nullable=False, server_default=NOW),
        sa.Column("artifact_id", UUID, sa.ForeignKey("am_artifacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column("course_id", sa.String(64), nullable=True),
        sa.Column("chapter_id", sa.String(64), nullable=True),
        sa.Column("query_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_am_retrieval_log_artifact", "am_retrieval_log", ["artifact_id"])
    op.create_index("ix_am_retrieval_log_course", "am_retrieval_log", ["course_id"])

    op.create_table(
        "am_epoch",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint("id = 1", name="ck_am_epoch_single_row"),
    )
    op.execute("INSERT INTO am_epoch (id, epoch) VALUES (1, 0)")


def downgrade() -> None:
    for table in ("am_epoch", "am_retrieval_log", "am_activity", "am_links"):
        op.drop_table(table)
    op.drop_constraint("fk_am_artifacts_latest", "am_artifacts", type_="foreignkey")
    op.drop_constraint("fk_am_artifacts_active", "am_artifacts", type_="foreignkey")
    # The guard refuses to delete approved versions - the downgrade is the one deliberate exception.
    op.execute("DROP TRIGGER am_guard_locked_version ON am_artifact_versions")
    op.drop_table("am_artifact_versions")
    op.execute("DROP FUNCTION am_guard_locked_version()")
    op.drop_table("am_artifacts")
    op.execute("DROP SEQUENCE am_artifact_number_seq")
