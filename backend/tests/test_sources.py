"""End-to-end source tracking (MOM Phase 2.1): research citations already
persisted per-chapter (see ResearchAgent._merge_citations / storage.
save_research) must survive into the built document as a real
"Sources & Further Reading" page plus structured DocumentMeta.sources - not
just live as research context the writer silently drops.
"""

from __future__ import annotations

from app.course.document.builder import SOURCES_SECTION_KEY, build_document, reflow_document
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.draft import GeneratedChapter
from app.schemas.research import ChapterResearch, Reference, ResearchPayload


def _blueprint(*chapter_ids: str) -> CourseBlueprint:
    return CourseBlueprint(
        course_title="Sources Test Course",
        audience="engineers",
        template_id="technical_v1",
        course_summary="A course for testing source tracking.",
        chapters=[
            BlueprintChapter(id=cid, title=f"Chapter {i}", order=i)
            for i, cid in enumerate(chapter_ids, start=1)
        ],
    )


def _chapter(chapter_id: str, number: int, title: str) -> GeneratedChapter:
    return GeneratedChapter(chapter_id=chapter_id, chapter_number=number, title=title, summary="")


def _research(chapter_id: str, references: list[Reference]) -> ChapterResearch:
    return ChapterResearch(
        chapter_id=chapter_id,
        chapter_title="",
        mode="fast",
        model="gpt-5-mini",
        payload=ResearchPayload(references=references),
    )


def test_build_document_renders_a_sources_page_from_persisted_research(storage):
    template = load_template("technical_v1")
    chapters = [
        _chapter("chapter_1", 1, "Foundations"),
        _chapter("chapter_2", 2, "Advanced Topics"),
    ]
    storage.save_research(
        "course_sources_test",
        _research(
            "chapter_1",
            [Reference(title="Official Python Docs", url="https://docs.python.org/3/", note="")],
        ),
        chapter_number=1,
    )
    storage.save_research(
        "course_sources_test",
        _research(
            "chapter_2",
            [
                # Same URL as chapter_1's reference - must dedupe, not double.
                Reference(title="Official Python Docs", url="https://docs.python.org/3/", note=""),
                Reference(title="PEP 8", url="https://peps.python.org/pep-0008/", note=""),
            ],
        ),
        chapter_number=2,
    )

    document = build_document(
        course_id="course_sources_test",
        blueprint=_blueprint("chapter_1", "chapter_2"),
        template=template,
        chapters=chapters,
        storage=storage,
    )

    # Structured form.
    assert len(document.meta.sources) == 2  # deduped by URL
    docs_source = next(s for s in document.meta.sources if s.url == "https://docs.python.org/3/")
    assert sorted(docs_source.chapter_ids) == ["chapter_1", "chapter_2"]
    pep8_source = next(s for s in document.meta.sources if s.url == "https://peps.python.org/pep-0008/")
    assert pep8_source.chapter_ids == ["chapter_2"]

    # Rendered form: a real page with a "Sources & Further Reading" heading
    # (course-sources follow-up: renamed from the plain "Sources" this test
    # originally asserted) and both URLs, placed after every chapter content
    # page.
    sources_blocks = [
        block
        for page in document.pages
        for block in page.blocks
        if block.meta.section_key == SOURCES_SECTION_KEY
    ]
    assert any(
        b.type is BlockType.HEADING and b.content.get("text") == "Sources & Further Reading"
        for b in sources_blocks
    )
    rendered_text = " ".join(
        str(b.content.get("text", "")) for b in sources_blocks if b.type is BlockType.PARAGRAPH
    )
    assert "https://docs.python.org/3/" in rendered_text
    assert "https://peps.python.org/pep-0008/" in rendered_text
    # Rendered once each, not once per citing chapter.
    assert rendered_text.count("https://docs.python.org/3/") == 1

    sources_page_numbers = {
        page.page_number for page in document.pages
        if any(b.meta.section_key == SOURCES_SECTION_KEY for b in page.blocks)
    }
    last_page_number = document.pages[-1].page_number
    assert max(sources_page_numbers) == last_page_number


def test_no_research_discloses_the_basis_instead_of_inventing_sources(storage):
    """A chapter with no persisted research (mock mode, research skipped, a
    pre-existing course) must never get fabricated references."""
    template = load_template("technical_v1")
    chapters = [_chapter("chapter_1", 1, "Foundations")]

    document = build_document(
        course_id="course_no_research_test",
        blueprint=_blueprint("chapter_1"),
        template=template,
        chapters=chapters,
        storage=storage,
    )

    assert document.meta.sources == []
    sources_text = " ".join(
        str(b.content.get("text", ""))
        for page in document.pages
        for b in page.blocks
        if b.meta.section_key == SOURCES_SECTION_KEY and b.type is BlockType.PARAGRAPH
    )
    assert "training knowledge" in sources_text
    assert "http" not in sources_text  # no invented URL


def test_reflow_keeps_sources_separate_from_chapter_content(storage):
    """A real, confirmed risk this guards against: reflow_document's own
    content-remix (triggered after image generation / an edit) must never
    interleave the Sources page's blocks with chapter content blocks - they
    stay their own trailing page(s) across a reflow, not just the initial
    build."""
    template = load_template("technical_v1")
    chapters = [_chapter("chapter_1", 1, "Foundations")]
    storage.save_research(
        "course_reflow_sources_test",
        _research("chapter_1", [Reference(title="A Source", url="https://example.test/a")]),
        chapter_number=1,
    )
    document = build_document(
        course_id="course_reflow_sources_test",
        blueprint=_blueprint("chapter_1"),
        template=template,
        chapters=chapters,
        storage=storage,
    )
    before_source_block_ids = {
        b.id for page in document.pages for b in page.blocks if b.meta.section_key == SOURCES_SECTION_KEY
    }
    assert before_source_block_ids

    reflow_document(document, template)

    after_source_pages = [
        page for page in document.pages
        if any(b.meta.section_key == SOURCES_SECTION_KEY for b in page.blocks)
    ]
    # Every page holding a sources block holds ONLY sources blocks - never
    # mixed in with a chapter content block on the same page.
    for page in after_source_pages:
        assert all(b.meta.section_key == SOURCES_SECTION_KEY for b in page.blocks)
    after_source_block_ids = {
        b.id for page in after_source_pages for b in page.blocks
    }
    assert after_source_block_ids == before_source_block_ids


# --- course-sources follow-up: richer reference metadata --------------------


def test_richer_reference_fields_survive_into_document_meta_and_rendered_text(storage):
    template = load_template("technical_v1")
    chapters = [_chapter("chapter_1", 1, "Foundations")]
    storage.save_research(
        "course_rich_sources_test",
        _research(
            "chapter_1",
            [
                Reference(
                    title="Python Official Documentation",
                    url="https://docs.python.org/3/",
                    author="Python Software Foundation",
                    source_type="official documentation",
                    relevance="Covers Python syntax, data types and language features.",
                )
            ],
        ),
        chapter_number=1,
    )

    document = build_document(
        course_id="course_rich_sources_test",
        blueprint=_blueprint("chapter_1"),
        template=template,
        chapters=chapters,
        storage=storage,
    )

    source = document.meta.sources[0]
    assert source.author == "Python Software Foundation"
    assert source.source_type == "official documentation"
    assert source.relevance == "Covers Python syntax, data types and language features."

    rendered_text = " ".join(
        str(b.content.get("text", ""))
        for page in document.pages
        for b in page.blocks
        if b.meta.section_key == SOURCES_SECTION_KEY and b.type is BlockType.PARAGRAPH
    )
    assert "Covers Python syntax, data types and language features." in rendered_text
    assert "Python Software Foundation" in rendered_text
    assert "official documentation" in rendered_text
    # The URL still appears exactly once - richer metadata never duplicates
    # the existing title+URL line's own link.
    assert rendered_text.count("https://docs.python.org/3/") == 1


def test_references_without_the_new_fields_still_render_fine(storage):
    """Backward compatibility: a reference built the old way (no author/
    source_type/relevance at all) must render exactly as it always did -
    no blank 'Covers:' or trailing dash line appended for fields that were
    never set."""
    template = load_template("technical_v1")
    chapters = [_chapter("chapter_1", 1, "Foundations")]
    storage.save_research(
        "course_plain_sources_test",
        _research("chapter_1", [Reference(title="A Source", url="https://example.test/a", note="")]),
        chapter_number=1,
    )

    document = build_document(
        course_id="course_plain_sources_test",
        blueprint=_blueprint("chapter_1"),
        template=template,
        chapters=chapters,
        storage=storage,
    )

    source = document.meta.sources[0]
    assert source.author == ""
    assert source.source_type == ""
    assert source.relevance == ""

    paragraph_text = next(
        str(b.content.get("text", ""))
        for page in document.pages
        for b in page.blocks
        if b.meta.section_key == SOURCES_SECTION_KEY and b.type is BlockType.PARAGRAPH
    )
    assert paragraph_text == "A Source — https://example.test/a"


def test_a_research_file_persisted_before_the_new_fields_existed_still_loads(storage):
    """A genuinely old on-disk research artifact has no author/source_type/
    relevance keys in its JSON at all (not even blank ones) - this must
    still parse and build a document, not crash or silently drop the source."""
    research = _research(
        "chapter_1", [Reference(title="Legacy Source", url="https://example.test/legacy")]
    )
    payload = research.model_dump(mode="json")
    for ref in payload["payload"]["references"]:
        del ref["author"], ref["source_type"], ref["relevance"]
    path = storage.research_path("course_legacy_sources_test", "chapter_1", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")

    template = load_template("technical_v1")
    document = build_document(
        course_id="course_legacy_sources_test",
        blueprint=_blueprint("chapter_1"),
        template=template,
        chapters=[_chapter("chapter_1", 1, "Foundations")],
        storage=storage,
    )

    assert document.meta.sources[0].url == "https://example.test/legacy"
    assert document.meta.sources[0].author == ""


def test_dedup_across_chapters_keeps_the_first_seen_metadata(storage):
    template = load_template("technical_v1")
    chapters = [
        _chapter("chapter_1", 1, "Foundations"),
        _chapter("chapter_2", 2, "Advanced Topics"),
    ]
    storage.save_research(
        "course_dedup_metadata_test",
        _research(
            "chapter_1",
            [Reference(title="Shared Source", url="https://example.test/shared", relevance="First seen.")],
        ),
        chapter_number=1,
    )
    storage.save_research(
        "course_dedup_metadata_test",
        _research(
            "chapter_2",
            # Same URL, different relevance text - first-seen must win, same
            # as title/note already behave for a duplicate URL.
            [Reference(title="Shared Source", url="https://example.test/shared", relevance="Second mention.")],
        ),
        chapter_number=2,
    )

    document = build_document(
        course_id="course_dedup_metadata_test",
        blueprint=_blueprint("chapter_1", "chapter_2"),
        template=template,
        chapters=chapters,
        storage=storage,
    )

    assert len(document.meta.sources) == 1
    assert document.meta.sources[0].relevance == "First seen."
    assert sorted(document.meta.sources[0].chapter_ids) == ["chapter_1", "chapter_2"]
