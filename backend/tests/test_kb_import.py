"""Importing the file library into PostgreSQL: what is read, what is built, what is compared.

The first group needs no database: it builds the rows an import would write and
checks the invariants the migration promises (numbers, citations, vectors, lifecycle),
and that the comparison used as the cutover parity check really detects drift. The
last tests run the import for real and are skipped unless TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import copy
import os
import uuid

import pytest

from app.scripts import kb_import, kb_snapshot
from app.services.knowledge_service import get_knowledge_service
from app.services.source_service import get_source_service
from tests import golden_knowledge as golden

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
MODEL = "text-embedding-3-small"


@pytest.fixture
async def library():
    service = get_knowledge_service()
    for name, data in golden.CORPUS:
        record = service.upload(name, data)
        await get_source_service().wait(record.source_id)
    return service


def _legacy():
    docs, skipped = kb_import.load_legacy(get_source_service())
    return docs, skipped


def _view(rows: kb_import.DocRows) -> kb_import.DbDoc:
    """What the database would hold for these rows (no database needed)."""
    item = rows.item
    return kb_import.DbDoc(
        number=item["number"],
        public_id=item["public_id"],
        lifecycle_status=item["lifecycle_status"],
        description=item["description"],
        chunks=[
            kb_import.DbChunk(
                c["ordinal"], c["text"], c["heading_path"], c["page"], c["embed_hash"], c["embedding"], c["is_live"], c["tokens"], c["token_count"]
            )
            for c in rows.chunks
        ],
    )


async def test_every_ready_document_is_loaded_with_its_number_and_passages(library):
    docs, skipped = _legacy()
    assert skipped == []
    assert [(d.record.global_number, d.record.filename) for d in docs] == [(1, "policy.txt"), (2, "cooking.txt"), (3, "security.txt")]
    for doc in docs:
        assert len(doc.chunks) == doc.record.chunk_count and len(doc.vectors) == len(doc.chunks)
        assert doc.original_key == f"sources/global/{doc.record.source_id}/original.txt"


async def test_rows_keep_citations_text_and_vectors_exactly(library):
    for doc in _legacy()[0]:
        rows = kb_import.build_rows(doc, embed_model=MODEL)
        # [G{n}.{k}] is unchanged: ordinal = old index + 1, in the same order.
        assert [c["ordinal"] for c in rows.chunks] == [chunk.index + 1 for chunk in doc.chunks]
        assert [c["text"] for c in rows.chunks] == [chunk.text for chunk in doc.chunks]
        assert [c["embedding"] for c in rows.chunks] == doc.vectors  # copied, never re-embedded
        assert rows.item["number"] == doc.record.global_number
        assert rows.item["public_id"] == doc.record.source_id
        assert rows.item["next_ordinal"] == len(doc.chunks) + 1
        assert rows.item["origin"] == "migrated"
        assert rows.version["version_no"] == 1 and rows.version["processing_status"] == "ready"
        assert rows.source["sha256"] == doc.record.sha256
        assert [link["position"] for link in rows.version_chunks] == list(range(len(doc.chunks)))
        assert all(c["embed_model"] == (MODEL if c["embedding"] is not None else None) for c in rows.chunks)


async def test_a_document_switched_on_stays_published_and_one_switched_off_is_unpublished(library):
    first = library.list_documents()[0]
    library.update(first.source_id, enabled=False)
    docs, _ = _legacy()
    by_number = {d.record.global_number: kb_import.build_rows(d, embed_model=MODEL) for d in docs}
    assert by_number[1].item["lifecycle_status"] == "unpublished"
    assert not any(c["is_live"] for c in by_number[1].chunks) and by_number[1].version["live_from"] is None
    assert by_number[2].item["lifecycle_status"] == "published"
    assert all(c["is_live"] for c in by_number[2].chunks) and by_number[2].version["live_from"] is not None


async def test_a_faithful_copy_compares_clean_and_every_kind_of_drift_is_caught(library):
    doc = _legacy()[0][0]
    rows = kb_import.build_rows(doc, embed_model=MODEL)
    assert kb_import.compare(doc, _view(rows)) == []

    def drifted(change) -> list[str]:
        view = copy.deepcopy(_view(rows))
        change(view)
        return kb_import.compare(doc, view)

    assert kb_import.compare(doc, None) == [f"{doc.record.source_id}: not in the database"]
    assert any("number" in p for p in drifted(lambda v: setattr(v, "number", 99)))
    assert any("lifecycle" in p for p in drifted(lambda v: setattr(v, "lifecycle_status", "draft")))
    assert any("text differs" in p for p in drifted(lambda v: setattr(v.chunks[0], "text", "changed")))
    assert any("ordinal" in p for p in drifted(lambda v: setattr(v.chunks[0], "ordinal", 7)))
    assert any("passages" in p for p in drifted(lambda v: v.chunks.pop()))
    assert any("is_live" in p for p in drifted(lambda v: setattr(v.chunks[0], "is_live", False)))
    assert any("search terms" in p for p in drifted(lambda v: setattr(v.chunks[0], "tokens", ["other"])))
    assert any("search terms" in p for p in drifted(lambda v: setattr(v.chunks[0], "token_count", 1)))
    if doc.vectors[0] is not None:
        assert any("bit-identical" in p for p in drifted(lambda v: v.chunks[0].embedding.__setitem__(0, v.chunks[0].embedding[0] + 1e-3)))
        assert any("presence" in p for p in drifted(lambda v: setattr(v.chunks[0], "embedding", None)))


async def test_loading_is_read_only(library):
    root = get_source_service().root.parent
    before = kb_snapshot.manifest_of(root)
    docs, _ = _legacy()
    kb_import.legacy_counter(get_source_service())
    kb_import.build_rows(docs[0], embed_model=MODEL)
    assert kb_snapshot.manifest_of(root) == before  # not one byte changed


async def test_what_cannot_be_imported_is_reported_not_imported(library):
    first = library.list_documents()[0]
    meta = get_source_service().root / "global" / first.source_id / "meta.json"
    meta.write_text(meta.read_text(encoding="utf-8").replace('"status": "ready"', '"status": "failed"'), encoding="utf-8")
    broken = get_source_service().root / "global" / "src_000000000000"
    broken.mkdir()
    (broken / "meta.json").write_text("not json", encoding="utf-8")

    docs, skipped = _legacy()
    assert [d.record.global_number for d in docs] == [2, 3]
    assert {s.source_id: s.reason for s in skipped} == {
        first.source_id: "status is 'failed', not ready",
        "src_000000000000": "unreadable meta.json",
    }


async def test_the_corpus_really_carries_vectors(library):
    # The vector checks above mean something only if the documents have vectors.
    assert all(doc.embedded == len(doc.chunks) > 0 for doc in _legacy()[0])


def test_the_counter_is_read_and_a_missing_one_is_zero(tmp_path):
    from app.core.config import get_settings
    from app.services.source_service import SourceService

    service = SourceService(settings=get_settings())
    assert kb_import.legacy_counter(service) == 0
    (service.root / "global").mkdir(parents=True)
    (service.root / "global" / "_counter.json").write_text('{"last": 7}', encoding="utf-8")
    assert kb_import.legacy_counter(service) == 7


def test_titles_are_made_from_the_filename():
    class R:
        filename = "refund_policy v2.final.pdf"

    assert kb_import._title_for(R()) == "refund policy v2.final"  # noqa: SLF001


def test_the_centroid_is_the_mean_of_the_unit_vectors_and_absent_without_vectors():
    assert kb_import.centroid_of([None, None]) is None
    assert kb_import.centroid_of([[3.0, 0.0], [0.0, 5.0], None]) == [0.5, 0.5]


# --- against a real database ---------------------------------------------------------

needs_db = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
async def factory(db_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_schema)
    monkeypatch.setenv("USE_DATABASE", "true")
    from app.core.config import reset_settings_cache
    from app.db.session import get_session_factory

    reset_settings_cache()
    return get_session_factory()


def _renumber(docs, offset: int):
    """Give the documents numbers no other test run has used, so the shared test database
    can be imported into repeatedly. public_id is made unique too."""
    for doc in docs:
        doc.record.global_number += offset
        doc.record.source_id = f"src_{uuid.uuid4().hex[:12]}"
        doc.record.sha256 = uuid.uuid4().hex + uuid.uuid4().hex
    return docs


@needs_db
async def test_import_then_verify_is_clean_and_a_rerun_changes_nothing(library, factory):
    docs = _renumber(_legacy()[0], offset=10_000 + uuid.uuid4().int % 1_000_000)
    first = await kb_import.import_documents(factory, docs, counter=max(d.record.global_number for d in docs), embed_model=MODEL)
    assert [r.outcome for r in first] == ["imported"] * 3
    assert await kb_import.verify_documents(factory, docs, counter=max(d.record.global_number for d in docs)) == []
    again = await kb_import.import_documents(factory, docs, counter=max(d.record.global_number for d in docs), embed_model=MODEL)
    assert [r.outcome for r in again] == ["already_imported"] * 3


@needs_db
async def test_the_number_sequence_ends_above_every_number_issued_and_is_never_lowered(library, factory):
    from sqlalchemy import text

    docs = _renumber(_legacy()[0], offset=20_000 + uuid.uuid4().int % 1_000_000)
    top = max(d.record.global_number for d in docs)
    await kb_import.import_documents(factory, docs, counter=top + 5, embed_model=MODEL)  # the counter was ahead
    assert await kb_import.sequence_problem(factory, top + 5) is None
    async with factory() as session, session.begin():
        await kb_import.raise_number_sequence(session, 1)  # asking for less must not lower it
        value = (await session.execute(text("SELECT nextval('kb_item_number_seq')"))).scalar()
    assert value > top + 5


@needs_db
async def test_a_number_held_by_another_document_is_an_error_not_an_overwrite(library, factory):
    docs = _renumber(_legacy()[0], offset=30_000 + uuid.uuid4().int % 1_000_000)
    await kb_import.import_documents(factory, docs[:1], counter=0, embed_model=MODEL)
    clash = _renumber(_legacy()[0][:1], offset=0)[0]
    clash.record.global_number = docs[0].record.global_number
    result = await kb_import.import_documents(factory, [clash], counter=0, embed_model=MODEL)
    assert result[0].outcome == "error" and "already belongs to" in result[0].note
