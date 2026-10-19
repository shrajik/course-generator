"""Parsing, chunking and ranking of uploaded source documents."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.ingestion.chunker import MAX_WORDS, chunk_document
from app.ingestion.parsers import (
    IngestionError,
    ParsedBlock,
    ParsedDocument,
    looks_like_heading,
    parse,
    parse_docx,
    parse_pdf,
    parse_text,
)
from app.ingestion.retrieval import Candidate, bm25_scores, cosine, rank, tokenize
from tests.sample_files import POLICY_TEXT, make_docx, make_pdf


# --- parsers: text ------------------------------------------------------------


def test_plain_text_finds_caps_and_numbered_headings_and_joins_wrapped_lines():
    doc = parse_text(POLICY_TEXT, markdown=False)
    kinds = [(b.kind, b.text) for b in doc.blocks]
    assert kinds[0] == ("heading", "REFUND POLICY")
    assert ("heading", "1.1 Eligibility") in kinds and ("heading", "1.2 Escalation") in kinds
    assert doc.blocks[1].kind == "paragraph"


def test_numbered_headings_get_nested_levels():
    doc = parse_text(b"1 Top\n\ntext\n\n1.2.3 Deep\n\nmore", markdown=False)
    levels = {b.text: b.level for b in doc.blocks if b.kind == "heading"}
    assert levels == {"1 Top": 1, "1.2.3 Deep": 3}


def test_markdown_headings_use_their_hash_level():
    doc = parse_text(b"# One\n\ntext\n\n## Two\n\nmore text here.", markdown=True)
    assert [(b.text, b.level) for b in doc.blocks if b.kind == "heading"] == [("One", 1), ("Two", 2)]


def test_text_in_other_encodings_is_decoded():
    assert parse_text("Café menu".encode("utf-16"), markdown=False).blocks
    assert "Café" in parse_text("Café menu items.".encode("latin-1"), markdown=False).blocks[0].text


def test_an_empty_text_file_is_refused():
    with pytest.raises(IngestionError, match="empty"):
        parse_text(b"  \n\n ", markdown=False)


@pytest.mark.parametrize("line,expected", [
    ("INTRODUCTION", True), ("2.1 Scope", True), ("A normal sentence ends here.", False),
    ("Short but not a title", False), ("2024", False), ("x" * 90, False),
])
def test_heading_heuristic(line, expected):
    assert looks_like_heading(line) is expected


# --- parsers: docx ------------------------------------------------------------


def test_docx_keeps_heading_levels_lists_and_tables_in_order():
    doc = parse_docx(make_docx())
    summary = [(b.kind, b.level, b.text[:20]) for b in doc.blocks]
    assert summary[0] == ("heading", 1, "Refund Policy")
    assert ("heading", 2, "Eligibility") in summary
    assert any(b.kind == "paragraph" and b.text.startswith("- Digital") for b in doc.blocks)
    table = next(b for b in doc.blocks if b.kind == "table")
    assert "Region | Deadline" in table.text and "EU | 30 days" in table.text
    assert doc.blocks[-1].kind == "table"


def test_a_zip_that_is_not_a_word_document_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(IngestionError, match="Word"):
        parse_docx(buffer.getvalue())
    with pytest.raises(IngestionError, match="valid Word"):
        parse_docx(b"not a zip at all")


def test_an_oversized_zip_is_refused_before_it_is_opened(monkeypatch):
    monkeypatch.setattr("app.ingestion.parsers.MAX_DOCX_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(IngestionError, match="unreasonable"):
        parse_docx(make_docx())


# --- parsers: pdf -------------------------------------------------------------


def test_pdf_text_is_extracted_with_page_numbers():
    pdf = make_pdf([
        ["REFUND POLICY", "Refunds are issued within fourteen days of a valid request."],
        ["SECOND PAGE", "Escalations go to the billing team within two business days."],
    ])
    doc = parse_pdf(pdf)
    assert doc.page_count == 2
    by_page = {b.page for b in doc.blocks}
    assert by_page == {1, 2}
    assert any(b.kind == "heading" and b.text == "SECOND PAGE" and b.page == 2 for b in doc.blocks)
    assert any("fourteen days" in b.text for b in doc.blocks)


def test_a_scanned_pdf_is_refused_with_advice():
    with pytest.raises(IngestionError, match="scanned"):
        parse_pdf(make_pdf([[], [], []]))


def test_a_corrupt_pdf_is_refused_cleanly():
    with pytest.raises(IngestionError):
        parse_pdf(b"%PDF-1.4\nthis is not really a pdf")


def test_a_pdf_over_the_page_limit_is_refused(monkeypatch):
    monkeypatch.setattr("app.ingestion.parsers.MAX_PAGES", 2)
    with pytest.raises(IngestionError, match="limit"):
        parse_pdf(make_pdf([["Some text on the first page here."]] * 3))


def test_an_unsupported_kind_is_refused():
    with pytest.raises(IngestionError, match="Unsupported"):
        parse("exe", b"x")


# --- chunker ------------------------------------------------------------------


def _doc(*blocks: ParsedBlock) -> ParsedDocument:
    return ParsedDocument(blocks=list(blocks))


def _p(text: str, page: int | None = None) -> ParsedBlock:
    return ParsedBlock("paragraph", text, page=page)


def _h(text: str, level: int = 1) -> ParsedBlock:
    return ParsedBlock("heading", text, level=level)


def test_a_chunk_never_crosses_a_heading_and_carries_its_path():
    chunks = chunk_document(_doc(_h("Refunds"), _p("A " * 50), _h("Eligibility", 2), _p("B " * 50), _h("Other"), _p("C " * 30)))
    assert [c.heading_path for c in chunks] == [["Refunds"], ["Refunds", "Eligibility"], ["Other"]]
    assert all(("A" in c.text) != ("B" in c.text) for c in chunks[:2])


def test_paragraphs_are_packed_up_to_the_target_size():
    paragraph = " ".join(["word"] * 100)
    chunks = chunk_document(_doc(_h("T"), *[_p(paragraph)] * 7))
    assert 2 <= len(chunks) <= 4
    assert all(c.words <= MAX_WORDS + 60 for c in chunks)


def test_a_very_long_paragraph_is_split_on_sentences():
    sentence = "This is a complete sentence about refunds and returns. "
    chunks = chunk_document(_doc(_h("T"), _p(sentence * 120)))
    assert len(chunks) >= 3
    assert all(c.text.rstrip().endswith(".") for c in chunks)


def test_one_enormous_sentence_without_punctuation_is_still_cut():
    chunks = chunk_document(_doc(_h("T"), _p("word " * 1200)))
    assert len(chunks) >= 3 and all(c.words <= MAX_WORDS for c in chunks)


def test_consecutive_chunks_in_one_section_overlap():
    paragraph = ". ".join(f"Sentence number {i} says something distinct" for i in range(30)) + "."
    chunks = chunk_document(_doc(_h("T"), _p(paragraph), _p(paragraph), _p(paragraph)))
    assert len(chunks) >= 2
    first_tail = chunks[0].text.split(". ")[-1]
    assert first_tail.rstrip(".") in chunks[1].text


def test_overlap_does_not_leak_across_a_heading():
    chunks = chunk_document(_doc(_h("One"), _p("Alpha " * 340), _h("Two"), _p("Beta " * 20)))
    second_section = [c for c in chunks if c.heading_path == ["Two"]]
    assert second_section and "Alpha" not in second_section[0].text


def test_a_table_stands_in_its_own_chunk():
    chunks = chunk_document(_doc(_h("T"), _p("Intro text here."), ParsedBlock("table", "A | B\n1 | 2"), _p("After the table.")))
    table_chunk = next(c for c in chunks if "A | B" in c.text)
    assert "After the table" not in table_chunk.text


def test_pages_and_indexes_are_recorded_and_empty_documents_give_no_chunks():
    chunks = chunk_document(_doc(_h("T"), _p("first " * 5, page=3), _h("U"), _p("second " * 5, page=4)))
    assert [(c.index, c.page) for c in chunks] == [(0, 3), (1, 4)]
    assert chunk_document(ParsedDocument()) == []
    assert chunk_document(_doc(_h("Only a heading"))) == []


def test_chunking_is_deterministic():
    doc = _doc(_h("T"), *[_p(f"Paragraph {i} " + "text " * 90) for i in range(8)])
    assert [c.text for c in chunk_document(doc)] == [c.text for c in chunk_document(doc)]


# --- retrieval ----------------------------------------------------------------

CANDIDATES = [
    Candidate("refund", "Refunds are issued within fourteen days when the product is returned unused."),
    Candidate("shipping", "Orders ship from the warehouse within two business days by courier."),
    Candidate("billing", "The billing team handles disputed invoices and escalated refund cases."),
]


def test_tokenizing_drops_stopwords_and_keeps_technical_terms():
    assert tokenize("The C++ and .NET code is on v2.1") == ["c++", "net", "code", "v2.1"]


def test_word_forms_meet_after_stemming():
    assert tokenize("Refunds refunded refunding refund") == ["refund"] * 4
    assert tokenize("policies") == ["policy"] and tokenize("access") == ["access"]


def test_keyword_ranking_prefers_the_passage_about_the_query():
    ranked = rank("how long do refunds take", CANDIDATES)
    assert ranked[0][0] == "refund" and {key for key, _ in ranked} == {"refund", "billing"}


def test_a_query_matching_nothing_returns_nothing():
    assert rank("quantum chromodynamics", CANDIDATES) == []
    assert rank("", CANDIDATES) == [] and rank("refunds", []) == []


def test_rare_terms_outweigh_common_ones():
    scores = bm25_scores("billing refund", CANDIDATES)
    assert scores["billing"] > scores["refund"]


def test_semantic_ranking_uses_vectors_when_there_is_no_keyword_overlap():
    candidates = [Candidate("a", "alpha", vector=[1.0, 0.0]), Candidate("b", "beta", vector=[0.0, 1.0])]
    assert rank("zzz", candidates, query_vector=[0.9, 0.1])[0][0] == "a"


def test_fusion_rewards_agreement_between_keyword_and_semantic():
    candidates = [
        Candidate("refund", CANDIDATES[0].text, vector=[1.0, 0.0]),
        Candidate("billing", CANDIDATES[2].text, vector=[0.0, 1.0]),
    ]
    assert rank("refund policy", candidates, query_vector=[1.0, 0.1])[0][0] == "refund"


def test_chunks_without_a_vector_still_rank_by_keywords():
    candidates = [Candidate("refund", CANDIDATES[0].text, vector=None)]
    assert rank("refunds", candidates, query_vector=[1.0, 0.0])[0][0] == "refund"


def test_the_limit_is_respected_and_cosine_handles_zero_vectors():
    many = [Candidate(f"c{i}", f"refund note {i}") for i in range(20)]
    assert len(rank("refund", many, limit=5)) == 5
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
