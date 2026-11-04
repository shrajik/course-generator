"""Admin knowledge base tables (Memory 1) in PostgreSQL.

Additive only: creates new `kb_*` tables, one sequence and one single-row epoch
table. No existing table is altered, and nothing reads these tables until the
Postgres knowledge backend is switched on, so applying this changes no behaviour.
The model and the reasoning are in app/db/kb_models.py.

Requires the `vector` extension (created by 20260912_0008) and pgvector 0.5.0+
for the partial HNSW index on live chunks.

Revision ID: 20261103_0012
Revises: 20261026_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20261103_0012"
down_revision = "20261026_0011"
branch_labels = None
depends_on = None

# Must match app.db.models.EMBEDDING_DIMENSIONS.
EMBEDDING_DIMENSIONS = 1536

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)
EMPTY_OBJECT = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'[]'::jsonb")
NOW = sa.text("now()")


def _pk() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True)


def _created() -> sa.Column:
    return sa.Column("created_at", TS, nullable=False, server_default=NOW)


def _updated() -> sa.Column:
    return sa.Column("updated_at", TS, nullable=False, server_default=NOW)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Citation numbers `G{number}.{ordinal}`: from a sequence so a number is never reissued.
    op.execute("CREATE SEQUENCE kb_item_number_seq START WITH 1 INCREMENT BY 1")

    op.create_table(
        "kb_categories",
        _pk(),
        sa.Column("parent_id", UUID, sa.ForeignKey("kb_categories.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_suggested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("centroid", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("centroid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived_at", TS, nullable=True),
        _created(),
        _updated(),
    )
    op.create_index("ix_kb_categories_parent_id", "kb_categories", ["parent_id"])
    # Slugs are unique among siblings; the COALESCE makes root categories (no parent) count too.
    op.execute(
        "CREATE UNIQUE INDEX uq_kb_categories_parent_slug ON kb_categories "
        "(coalesce(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), slug)"
    )

    # kb_items and kb_item_versions reference each other (live/latest version), so the
    # two foreign keys on kb_items are added once both tables exist.
    op.create_table(
        "kb_items",
        _pk(),
        sa.Column("number", sa.Integer(), nullable=False, server_default=sa.text("nextval('kb_item_number_seq')")),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("live_version_id", UUID, nullable=True),
        sa.Column("latest_version_id", UUID, nullable=True),
        sa.Column("category_id", UUID, sa.ForeignKey("kb_categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("properties", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("origin", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("next_ordinal", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("suggestions", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("centroid", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _created(),
        _updated(),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("archived_at", TS, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        sa.Column("deleted_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("purged_at", TS, nullable=True),
        sa.Column("purged_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || coalesce(description, ''))", persisted=True),
        ),
        sa.UniqueConstraint("number", name="uq_kb_items_number"),
        sa.UniqueConstraint("public_id", name="uq_kb_items_public_id"),
        sa.CheckConstraint(
            _in("lifecycle_status", ("draft", "published", "unpublished", "archived")), name="ck_kb_items_lifecycle"
        ),
        sa.CheckConstraint(_in("origin", ("migrated", "upload", "manual")), name="ck_kb_items_origin"),
    )
    op.create_index("ix_kb_items_lifecycle_status", "kb_items", ["lifecycle_status"])
    op.create_index("ix_kb_items_category_id", "kb_items", ["category_id"])
    op.create_index("ix_kb_items_needs_review", "kb_items", ["needs_review"])
    op.create_index("ix_kb_items_deleted_at", "kb_items", ["deleted_at"])
    op.create_index("ix_kb_items_tsv", "kb_items", ["tsv"], postgresql_using="gin")

    op.create_table(
        "kb_item_versions",
        _pk(),
        sa.Column("item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("processing_status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("warnings", JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("body_md", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _created(),
        sa.Column("live_from", TS, nullable=True),
        sa.Column("live_until", TS, nullable=True),
        sa.UniqueConstraint("item_id", "version_no", name="uq_kb_item_versions_item_no"),
        sa.CheckConstraint(
            _in("processing_status", ("queued", "parsing", "chunking", "embedding", "classifying", "ready", "failed")),
            name="ck_kb_item_versions_processing",
        ),
    )
    op.create_index("ix_kb_item_versions_item_id", "kb_item_versions", ["item_id"])
    op.create_foreign_key("fk_kb_items_live_version", "kb_items", "kb_item_versions", ["live_version_id"], ["id"])
    op.create_foreign_key("fk_kb_items_latest_version", "kb_items", "kb_item_versions", ["latest_version_id"], ["id"])

    op.create_table(
        "kb_sources",
        _pk(),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outline", JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("extracted_meta", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        _created(),
        sa.UniqueConstraint("sha256", name="uq_kb_sources_sha256"),
    )

    op.create_table(
        "kb_version_sources",
        sa.Column("version_id", UUID, sa.ForeignKey("kb_item_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_id", UUID, sa.ForeignKey("kb_sources.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_kb_version_sources_source_id", "kb_version_sources", ["source_id"])

    op.create_table(
        "kb_chunks",
        _pk(),
        sa.Column("item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_id", UUID, sa.ForeignKey("kb_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("heading", sa.Text(), nullable=False, server_default=""),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("words", sa.Integer(), nullable=False),
        sa.Column("embed_hash", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("embed_model", sa.String(100), nullable=True),
        sa.Column("tokens", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("topics", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("retired_at", TS, nullable=True),
        _created(),
        sa.UniqueConstraint("item_id", "ordinal", name="uq_kb_chunks_item_ordinal"),
    )
    op.create_index("ix_kb_chunks_embed_hash", "kb_chunks", ["embed_hash", "embed_model"])
    # Only live chunks are indexed: draft, archived and superseded content never enters
    # these, which keeps filtered semantic search from returning too few results.
    op.execute(
        "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE is_live AND embedding IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_kb_chunks_tokens_live ON kb_chunks USING gin (tokens) WHERE is_live")
    op.execute("CREATE INDEX ix_kb_chunks_live ON kb_chunks (item_id) WHERE is_live")

    op.create_table(
        "kb_version_chunks",
        sa.Column("version_id", UUID, sa.ForeignKey("kb_item_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chunk_id", UUID, sa.ForeignKey("kb_chunks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_kb_version_chunks_chunk_id", "kb_version_chunks", ["chunk_id"])

    op.create_table(
        "kb_tags",
        _pk(),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("name_key", sa.String(64), nullable=False),
        _created(),
        sa.UniqueConstraint("name_key", name="uq_kb_tags_name_key"),
    )
    op.create_table(
        "kb_item_tags",
        sa.Column("item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", UUID, sa.ForeignKey("kb_tags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="admin"),
    )
    op.create_index("ix_kb_item_tags_tag_id", "kb_item_tags", ["tag_id"])

    op.create_table(
        "kb_relations",
        _pk(),
        sa.Column("from_item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("to_item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation_type", sa.String(24), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _created(),
        sa.UniqueConstraint("from_item_id", "to_item_id", "relation_type", name="uq_kb_relations_edge"),
        sa.CheckConstraint("from_item_id <> to_item_id", name="ck_kb_relations_not_self"),
        sa.CheckConstraint(
            _in("relation_type", ("related_to", "references", "derived_from", "depends_on", "supersedes", "duplicate_of")),
            name="ck_kb_relations_type",
        ),
    )
    op.create_index("ix_kb_relations_from_item_id", "kb_relations", ["from_item_id"])
    op.create_index("ix_kb_relations_to_item_id", "kb_relations", ["to_item_id"])

    op.create_table(
        "kb_entities",
        _pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="concept"),
        _created(),
        sa.UniqueConstraint("normalized_name", "kind", name="uq_kb_entities_name_kind"),
    )
    op.create_table(
        "kb_item_entities",
        _pk(),
        sa.Column("item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID, sa.ForeignKey("kb_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_id", UUID, sa.ForeignKey("kb_chunks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
    )
    op.create_index("ix_kb_item_entities_item_id", "kb_item_entities", ["item_id"])
    op.create_index("ix_kb_item_entities_entity_id", "kb_item_entities", ["entity_id"])

    op.create_table(
        "kb_activity",
        _pk(),
        sa.Column("item_id", UUID, sa.ForeignKey("kb_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        _created(),
    )
    op.create_index("ix_kb_activity_item_created", "kb_activity", ["item_id", "created_at"])

    op.create_table(
        "kb_jobs",
        _pk(),
        sa.Column("version_id", UUID, sa.ForeignKey("kb_item_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default="ingest"),
        sa.Column("stage", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("run_after", TS, nullable=False, server_default=NOW),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column("locked_at", TS, nullable=True),
        sa.Column("heartbeat_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("checkpoint", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        _created(),
        _updated(),
        sa.CheckConstraint(_in("status", ("queued", "running", "succeeded", "failed", "dead")), name="ck_kb_jobs_status"),
    )
    op.create_index("ix_kb_jobs_version_id", "kb_jobs", ["version_id"])
    op.create_index("ix_kb_jobs_claim", "kb_jobs", ["status", "run_after"])

    op.create_table(
        "kb_retrieval_log",
        _pk(),
        sa.Column("retrieved_at", TS, nullable=False, server_default=NOW),
        sa.Column("item_number", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("chunk_id", UUID, sa.ForeignKey("kb_chunks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version_id", UUID, nullable=True),
        sa.Column("course_id", sa.String(64), nullable=True),
        sa.Column("chapter_id", sa.String(64), nullable=True),
        sa.Column("query_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_kb_retrieval_log_retrieved_at", "kb_retrieval_log", ["retrieved_at"])
    op.create_index("ix_kb_retrieval_log_course_id", "kb_retrieval_log", ["course_id"])
    op.create_index("ix_kb_retrieval_log_ref", "kb_retrieval_log", ["item_number", "ordinal"])

    op.create_table(
        "kb_epoch",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="0"),
        _updated(),
        sa.CheckConstraint("id = 1", name="ck_kb_epoch_single_row"),
    )
    op.execute("INSERT INTO kb_epoch (id, epoch) VALUES (1, 0)")


def downgrade() -> None:
    # Reverse order of creation. The `vector` extension is deliberately left in place
    # (as 20260912_0008 does): other tables use it.
    for table in (
        "kb_epoch",
        "kb_retrieval_log",
        "kb_jobs",
        "kb_activity",
        "kb_item_entities",
        "kb_entities",
        "kb_relations",
        "kb_item_tags",
        "kb_tags",
        "kb_version_chunks",
        "kb_chunks",
        "kb_version_sources",
        "kb_sources",
    ):
        op.drop_table(table)
    op.drop_constraint("fk_kb_items_live_version", "kb_items", type_="foreignkey")
    op.drop_constraint("fk_kb_items_latest_version", "kb_items", type_="foreignkey")
    op.drop_table("kb_item_versions")
    op.drop_table("kb_items")
    op.drop_table("kb_categories")
    op.execute("DROP SEQUENCE kb_item_number_seq")
