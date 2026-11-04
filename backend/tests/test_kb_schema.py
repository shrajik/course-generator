"""The knowledge-base tables: migration 0012 against the models.

The first group runs everywhere: it renders the migration to SQL (no database) and
checks it creates exactly the tables and columns the models declare, that it can be
reversed, and that the revision chain is intact. The second group, skipped unless
TEST_DATABASE_URL is configured, applies it for real and checks the constraints and
indexes the design depends on.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.db import kb_models
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION = BACKEND / "alembic" / "versions" / "20261103_0012_knowledge_base.py"
DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _migration():
    spec = importlib.util.spec_from_file_location("kb_migration_0012", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(direction: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        getattr(_migration(), direction)()
    return buffer.getvalue()


def _created_tables(sql: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for name, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        columns = set()
        for line in body.splitlines():
            token = line.strip().split(" ")[0]
            if token and token not in {"PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK"}:
                columns.add(token)
        tables[name] = columns
    return tables


def test_the_migration_creates_exactly_the_tables_and_columns_the_models_declare():
    created = _created_tables(_render("upgrade"))
    declared = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables if t.name.startswith("kb_")}
    assert set(created) == set(declared) == set(kb_models.KB_TABLES)
    for table, columns in declared.items():
        assert created[table] == columns, table


def test_the_migration_is_reversible_and_leaves_the_vector_extension():
    upgrade, downgrade = _render("upgrade"), _render("downgrade")
    for table in kb_models.KB_TABLES:
        assert f"DROP TABLE {table};" in downgrade
    assert "DROP SEQUENCE kb_item_number_seq" in downgrade
    assert "DROP EXTENSION" not in downgrade
    assert "CREATE SEQUENCE kb_item_number_seq" in upgrade


def test_no_existing_table_is_altered():
    sql = _render("upgrade")
    assert not re.search(r"ALTER TABLE (?!kb_)", sql)
    assert not re.search(r"DROP ", sql)


def test_only_live_chunks_are_indexed_for_vector_and_keyword_search():
    sql = _render("upgrade")
    assert "USING hnsw (embedding vector_cosine_ops) WHERE is_live AND embedding IS NOT NULL" in sql
    assert "USING gin (tokens) WHERE is_live" in sql


def test_the_revision_chain_is_linear_and_0012_follows_0011():
    script = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision("20261103_0012").down_revision == "20261026_0011"


def test_lifecycle_and_processing_are_separate_fields_with_their_own_vocabularies():
    items, versions = Base.metadata.tables["kb_items"], Base.metadata.tables["kb_item_versions"]
    assert "lifecycle_status" in items.c and "processing_status" not in items.c
    assert "processing_status" in versions.c and "lifecycle_status" not in versions.c
    assert set(kb_models.LIFECYCLE_STATUSES).isdisjoint({"ready", "failed", "queued"})


# --- against a real database ---------------------------------------------------------

needs_db = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
async def session(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    from app.core.config import reset_settings_cache
    from app.db.session import get_session_factory

    reset_settings_cache()
    async with get_session_factory()() as session:
        yield session
        await session.rollback()


def _item(**fields) -> kb_models.KbItem:
    fields.setdefault("public_id", f"src_{uuid.uuid4().hex[:12]}")
    fields.setdefault("title", "Refund policy")
    return kb_models.KbItem(**fields)


@needs_db
async def test_item_numbers_come_from_a_sequence_and_new_items_are_drafts(session):
    first, second = _item(), _item()
    session.add_all([first, second])
    await session.flush()
    await session.refresh(first)
    await session.refresh(second)
    assert second.number == first.number + 1
    assert first.lifecycle_status == "draft" and first.next_ordinal == 1 and first.origin == "upload"


@needs_db
async def test_check_constraints_reject_bad_values(session):
    from sqlalchemy.exc import IntegrityError

    for bad in (_item(lifecycle_status="live"), _item(origin="web")):
        session.add(bad)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@needs_db
async def test_a_relation_cannot_point_at_itself_or_repeat(session):
    from sqlalchemy.exc import IntegrityError

    a, b = _item(), _item()
    session.add_all([a, b])
    await session.flush()
    session.add(kb_models.KbRelation(from_item_id=a.id, to_item_id=a.id, relation_type="related_to"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    a, b = _item(), _item()
    session.add_all([a, b])
    await session.flush()
    session.add(kb_models.KbRelation(from_item_id=a.id, to_item_id=b.id, relation_type="supersedes"))
    await session.flush()
    session.add(kb_models.KbRelation(from_item_id=a.id, to_item_id=b.id, relation_type="supersedes"))
    with pytest.raises(IntegrityError):
        await session.flush()


@needs_db
async def test_sibling_category_slugs_are_unique_including_at_the_root(session):
    from sqlalchemy.exc import IntegrityError

    slug = f"policies-{uuid.uuid4().hex[:6]}"
    session.add(kb_models.KbCategory(name="Policies", slug=slug))
    await session.flush()
    session.add(kb_models.KbCategory(name="Policies again", slug=slug))
    with pytest.raises(IntegrityError):
        await session.flush()


@needs_db
async def test_chunk_ordinals_are_unique_per_item_and_the_live_indexes_exist(session):
    from sqlalchemy.exc import IntegrityError

    item = _item()
    session.add(item)
    await session.flush()
    chunk = dict(item_id=item.id, ordinal=1, text="Refunds take fourteen days.", words=4, embed_hash="h")
    session.add(kb_models.KbChunk(**chunk))
    await session.flush()
    session.add(kb_models.KbChunk(**chunk))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    names = set(
        (await session.execute(sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'kb_chunks'"))).scalars()
    )
    assert {"ix_kb_chunks_embedding_hnsw", "ix_kb_chunks_tokens_live"} <= names


@needs_db
async def test_the_epoch_row_exists_and_is_the_only_one(session):
    from sqlalchemy.exc import IntegrityError

    assert (await session.execute(sa.text("SELECT epoch FROM kb_epoch WHERE id = 1"))).scalar() is not None
    session.add(kb_models.KbEpoch(id=2))
    with pytest.raises(IntegrityError):
        await session.flush()
