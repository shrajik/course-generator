"""Semantic memory: pgvector extension + memory_embeddings table.

One embedding row per (source_type, source_id) - Template, VisualKnowledge,
CourseSample or GenerationRun - looked up generically rather than adding a
vector column to each of those four tables. See MemoryEmbedding in
app/db/models.py for the full reasoning.

Requires the `vector` extension's files to be present on the Postgres server
(e.g. the `pgvector/pgvector:pg16` image, or `postgres:16` with the pgvector
package installed) - `CREATE EXTENSION` cannot install the extension itself
if the server binary isn't there. If it isn't, this migration fails cleanly
and the database is left exactly as it was (transactional DDL) - the
application keeps working on keyword-only retrieval either way; only the
semantic half of hybrid retrieval needs this table.
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20260912_0008"
down_revision = "20260911_0007"
branch_labels = None
depends_on = None

# Must match app.db.models.EMBEDDING_DIMENSIONS / Settings.embedding_dimensions
# (text-embedding-3-small). Changing the embedding model to a different
# width needs a follow-up migration, not just a config change.
EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    timestamp_type = sa.DateTime(timezone=True)

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_embeddings",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedded_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("created_at", timestamp_type, nullable=False),
        sa.Column("updated_at", timestamp_type, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", name="uq_memory_embeddings_source"),
    )
    op.create_index("ix_memory_embeddings_source_type", "memory_embeddings", ["source_type"])
    op.create_index("ix_memory_embeddings_source_id", "memory_embeddings", ["source_id"])

    # HNSW: no training/list-count tuning needed as the table grows (unlike
    # ivfflat), and build time on an empty table is instant - safe to create
    # inline here. If this table ever grows very large before a next
    # migration touches it, rebuilding/adding an index on a populated table
    # should use `CREATE INDEX CONCURRENTLY` outside a transaction block
    # (Alembic: wrap that migration in `with op.get_context().autocommit_block():`)
    # to avoid holding a long write lock - not needed here since it starts empty.
    op.execute(
        "CREATE INDEX ix_memory_embeddings_embedding_hnsw ON memory_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_embeddings_embedding_hnsw")
    op.drop_index("ix_memory_embeddings_source_id", table_name="memory_embeddings")
    op.drop_index("ix_memory_embeddings_source_type", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
    # Deliberately NOT dropping the `vector` extension - it's cheap to leave
    # installed, and dropping it is only safe if nothing else in the database
    # has come to depend on the `vector` type since this migration ran.
