"""Turning a course document into artifacts: what becomes reusable, and how identity stays stable."""

from __future__ import annotations

from app.artifacts.extract import EMBED_CHARS, MAX_SEARCH_CHARS, canonical_hash, embedding_text, extract_artifacts, text_of
from app.schemas.blocks import BlockType
from app.schemas.document import Block, BlockMeta, CourseDocument, Page


def block(type_: BlockType, content: dict, chapter: str | None = "ch_1", **meta) -> Block:
    return Block(type=type_, content=content, meta=BlockMeta(chapter_id=chapter, **meta))


def document(*blocks: Block) -> CourseDocument:
    return CourseDocument(document_id="doc_1", course_id="course_1", course_title="Refund Handling", template_id="t", pages=[Page(blocks=list(blocks))])


def sample() -> CourseDocument:
    return document(
        block(BlockType.HEADING, {"text": "How refunds work", "level": 1}, chapter_number=1),
        block(BlockType.PARAGRAPH, {"text": "Refunds are issued within fourteen days [G2.5]. See the course notes [S1.3]."}),
        block(BlockType.IMAGE, {"caption": "The refund flow", "alt": "A flowchart", "generated": True, "path": "assets/flow.png", "asset_id": "img_1", "prompt": "draw a flow", "width": 800, "height": 400}),
        block(BlockType.IMAGE, {"caption": "A decorative swirl", "decorative": True, "generated": True, "path": "assets/swirl.png"}),
        block(BlockType.IMAGE, {"caption": "Never produced", "generated": False}),
        block(BlockType.QUIZ, {"title": "Knowledge Check", "questions": [{"question": "How many days?", "options": ["7", "14"], "answer": "14", "explanation": "Policy [G2.5]."}]}),
        block(BlockType.QUIZ, {"title": "Final Assessment", "questions": [{"question": "Who escalates?", "answer": "billing"}]}),
        block(BlockType.QUIZ, {"title": "Empty quiz", "questions": []}),
        block(BlockType.EXERCISE, {"title": "Process a refund", "instructions": "Walk through a refund request.", "steps": ["Check the order", "Issue the refund"]}),
        block(BlockType.CASE_STUDY, {"title": "The late return", "context": "A customer returned a product after twenty days.", "outcome": "Refused politely."}),
        block(BlockType.HEADING, {"text": "Escalation", "level": 1}, chapter="ch_2", chapter_number=2),
        block(BlockType.PARAGRAPH, {"text": "Disputes go to the billing team."}, chapter="ch_2"),
        block(BlockType.PARAGRAPH, {"text": "A cover or table-of-contents block belongs to no chapter."}, chapter=None),
    )


def by_key(drafts):
    return {d.key: d for d in drafts}


def test_a_course_becomes_a_chapter_artifact_each_plus_its_reusable_parts():
    drafts = extract_artifacts(sample())
    kinds = sorted((d.artifact_type, d.title) for d in drafts)
    assert kinds == sorted([
        ("chapter", "How refunds work"), ("chapter", "Escalation"), ("visual", "The refund flow"),
        ("questions", "Knowledge Check"), ("assessment", "Final Assessment"), ("activity", "Process a refund"), ("case_study", "The late return"),
    ])
    assert all(d.key.startswith(("chapter:", "block:")) for d in drafts)


def test_a_chapter_contains_its_blocks_and_names_its_children():
    drafts = by_key(extract_artifacts(sample()))
    chapter = drafts["chapter:ch_1"]
    assert [b["type"] for b in chapter.content["blocks"]][:2] == ["heading", "paragraph"] and len(chapter.content["blocks"]) == 10
    child_types = sorted(drafts[key].artifact_type for key in chapter.children)
    assert child_types == ["activity", "assessment", "case_study", "questions", "visual"]
    assert drafts["chapter:ch_2"].children == () and drafts["chapter:ch_2"].chapter_id == "ch_2"
    assert "cover" not in " ".join(d.search_text for d in drafts.values())  # a block outside every chapter is left out


def test_only_a_visual_that_was_produced_and_means_something_is_reusable():
    visuals = [d for d in extract_artifacts(sample()) if d.artifact_type == "visual"]
    assert len(visuals) == 1
    assert visuals[0].asset == {"path": "assets/flow.png", "asset_id": "img_1", "alt": "A flowchart", "caption": "The refund flow", "width": 800, "height": 400}
    assert "draw a flow" not in visuals[0].search_text  # the generation prompt is not what the picture shows


def test_provenance_comes_from_the_text_and_from_what_the_writer_was_offered():
    doc = sample()
    drafts = by_key(extract_artifacts(doc))
    plain = drafts["chapter:ch_1"]
    assert plain.source_refs == ("G2.5", "S1.3")
    offered = by_key(extract_artifacts(doc, offered={"ch_1": ["G2.5", "G7.1"], "ch_2": ["G9.9"]}))
    assert offered["chapter:ch_1"].source_refs == ("G2.5", "S1.3", "G7.1") and offered["chapter:ch_2"].source_refs == ("G9.9",)
    quiz = next(d for d in drafts.values() if d.title == "Knowledge Check")
    assert quiz.source_refs == ("G2.5",)  # the quiz's own explanation cites it too


def test_identity_is_stable_and_the_hash_changes_only_when_content_does():
    document_ = sample()
    first, again = by_key(extract_artifacts(document_)), by_key(extract_artifacts(document_))
    assert first.keys() == again.keys() and all(first[k].content_hash == again[k].content_hash for k in first)
    edited = document_.model_copy(deep=True)
    next(b for _, b in edited.iter_blocks() if b.type == BlockType.PARAGRAPH).content["text"] = "Refunds are issued within thirty days."
    after = by_key(extract_artifacts(edited))
    assert after.keys() == first.keys()  # the same artifacts, so a resubmission finds them
    assert after["chapter:ch_1"].content_hash != first["chapter:ch_1"].content_hash
    assert after["chapter:ch_2"].content_hash == first["chapter:ch_2"].content_hash


def test_the_hash_ignores_key_order_and_text_skips_noise():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})
    # Ids are minted per document: the same words under different ids are the same content.
    assert canonical_hash({"blocks": [{"id": "blk_1", "text": "x"}]}) == canonical_hash({"blocks": [{"id": "blk_9", "text": "x"}]})
    assert text_of({"title": "T", "path": "x.png", "items": ["one", " ", "two"], "n": 3}) == "T one two"
    assert len(text_of({"t": "x" * (MAX_SEARCH_CHARS + 5000)})) == MAX_SEARCH_CHARS


def test_the_same_words_in_another_document_are_the_same_content():
    one, other = by_key(extract_artifacts(sample())), by_key(extract_artifacts(sample()))  # two documents: different block ids throughout
    assert set(one) != set(other)  # their keys differ (they name blocks) ...
    assert sorted(d.content_hash for d in one.values()) == sorted(d.content_hash for d in other.values())  # ... their content does not


def test_an_empty_or_chapterless_document_yields_nothing():
    assert extract_artifacts(document()) == []
    assert extract_artifacts(document(block(BlockType.PARAGRAPH, {"text": "orphan"}, chapter=None))) == []


def test_a_long_artifact_is_embedded_from_its_whole_length_not_just_its_opening():
    short = "A short chapter about refunds."
    assert embedding_text(short) == short
    parts = [f"SECTION{i} filler " * 130 for i in range(10)]  # each section names itself throughout
    long = " ".join(parts)
    sample = embedding_text(long)
    assert len(sample) <= EMBED_CHARS and sample.startswith("SECTION0")
    reached = {i for i in range(10) if f"SECTION{i}" in sample}
    assert len(reached) >= 5 and max(reached) >= 8  # it reaches deep into the text, not only the first sections
    assert embedding_text(long) == sample  # deterministic: the same artifact always embeds the same way
