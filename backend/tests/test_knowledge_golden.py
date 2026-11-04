"""Golden retrieval results for the knowledge library (see tests/golden_knowledge.py)."""

from __future__ import annotations

import pytest

from app.services.knowledge_service import get_knowledge_service
from app.services.source_service import get_source_service
from tests import golden_knowledge as golden


@pytest.fixture
async def loaded():
    library = get_knowledge_service()
    for name, data in golden.CORPUS:
        record = library.upload(name, data)
        await get_source_service().wait(record.source_id)
    return library


async def test_golden_queries_offer_the_pinned_top_passage(loaded):
    for query, expected in golden.GOLDEN.items():
        refs = [e.ref for e in await loaded.search(query)]
        assert (refs[0] if refs else None) == expected, query


async def test_the_corpus_keeps_its_permanent_numbers(loaded):
    assert [(d.filename, d.global_number) for d in loaded.list_documents()] == [
        ("policy.txt", 1), ("cooking.txt", 2), ("security.txt", 3)
    ]
