"""Unit tests for the proper, textbook-style Table of Contents page - both
the renderer itself (app.render.toc_renderer) and its wiring into the
document builder (app.course.document.builder._toc_page / build_document /
finalize_toc_page)."""

from __future__ import annotations

import re

from app.course.document.builder import build_document, finalize_toc_page
from app.course.templates.registry import load_template
from app.render.toc_renderer import TocChapter, TocSection, estimate_toc_pixel_size, render_toc_html
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.draft import GeneratedChapter
from app.schemas.template import TemplateTheme


def _chapter(number: int, title: str, summary: str = "") -> TocChapter:
    return TocChapter(number=number, title=title, summary=summary)


class TestEstimateTocPixelSize:
    def test_returns_the_reference_content_width(self):
        width, _ = estimate_toc_pixel_size([_chapter(1, "One")])
        assert width == 666

    def test_height_grows_with_chapter_count(self):
        few = [_chapter(i, f"Chapter {i}") for i in range(1, 3)]
        many = [_chapter(i, f"Chapter {i}") for i in range(1, 11)]
        _, few_height = estimate_toc_pixel_size(few)
        _, many_height = estimate_toc_pixel_size(many)
        assert many_height > few_height

    def test_height_is_capped_at_roughly_one_page_even_for_many_chapters(self):
        many = [
            _chapter(i, f"Chapter {i}: a reasonably descriptive title", "A summary sentence long enough to wrap onto a couple of lines in the card.")
            for i in range(1, 25)
        ]
        _, height = estimate_toc_pixel_size(many)
        assert height <= 863  # CONTENT_HEIGHT (963) minus the caption/padding reserve


class TestRenderTocHtml:
    def test_renders_every_chapter_title(self):
        chapters = [_chapter(1, "Foundations"), _chapter(2, "Advanced Topics")]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "Foundations" in html
        assert "Advanced Topics" in html
        assert "Test Course" in html

    def test_renders_as_a_static_picture_with_no_script_or_buttons(self):
        chapters = [_chapter(1, "Foundations")]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "toc-root" in html
        assert "<script" not in html
        assert "<button" not in html

    def test_renders_the_four_expected_column_headers(self):
        chapters = [_chapter(1, "Foundations")]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        for header in ("No.", "Title", "What You Will Learn", "Page"):
            assert header in html

    def test_renders_as_a_real_table_element(self):
        chapters = [_chapter(1, "Foundations"), _chapter(2, "Advanced Topics")]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "<table" in html
        assert html.count("<tr") >= 3  # header row + 2 chapter rows

    def test_a_chapters_real_page_number_is_shown_right_aligned(self):
        chapters = [TocChapter(1, "Foundations", "The basics.", page=3)]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert '<td class="toc-page">3</td>' in html

    def test_a_chapter_with_no_known_page_yet_shows_a_blank_cell_not_a_zero(self):
        """The placeholder pass (_toc_page, before finalize_toc_page ever
        runs) has no real page numbers yet - must never print a literal
        "0" page number."""
        chapters = [_chapter(1, "Foundations")]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert '<td class="toc-page"></td>' in html
        assert ">0<" not in html

    def test_sub_section_rows_carry_decimal_numbering_and_their_own_page(self):
        chapters = [
            TocChapter(
                1, "Foundations", "The basics.", page=3,
                sections=[
                    TocSection("1.1", "Learning Objectives", page=4, learn_line="See what you'll be able to do."),
                    TocSection("1.2", "Key Terms", page=5),
                ],
            )
        ]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "1.1" in html and "Learning Objectives" in html
        assert "1.2" in html and "Key Terms" in html
        assert '<td class="toc-page">4</td>' in html
        assert '<td class="toc-page">5</td>' in html

    def test_a_long_what_you_will_learn_sentence_is_never_truncated_with_an_ellipsis(self):
        long_summary = (
            "This chapter walks through every stage of the triage flow in careful, explicit detail "
            "so that a reader with no prior exposure to the topic can follow each decision point."
        )
        chapters = [TocChapter(1, "Foundations", long_summary)]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert long_summary in html
        assert "…" not in html

    def test_a_normal_sized_toc_is_not_scaled(self):
        chapters = [_chapter(i, f"Chapter {i}") for i in range(1, 4)]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "transform:scale(" not in html

    def test_a_toc_with_many_chapters_is_scaled_down_to_fit(self):
        chapters = [
            _chapter(i, f"Chapter {i}: a reasonably descriptive title", "A summary sentence long enough to wrap onto a couple of lines in the card.")
            for i in range(1, 25)
        ]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        assert "transform:scale(" in html

    def test_the_scaled_wrapper_height_matches_what_was_reserved(self):
        chapters = [
            _chapter(i, f"Chapter {i}: a reasonably descriptive title", "A summary sentence long enough to wrap onto a couple of lines in the card.")
            for i in range(1, 25)
        ]
        _, reserved_height = estimate_toc_pixel_size(chapters)
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        match = re.search(r"height:(\d+)px", html)
        assert match is not None
        assert int(match.group(1)) == reserved_height


class TestTocPageBuilder:
    def test_build_document_produces_a_toc_table_image_block(self, storage):
        template = load_template("technical_v1")
        blueprint = CourseBlueprint(
            course_title="Lang Graph",
            audience="engineers",
            template_id="technical_v1",
            course_summary="A hands-on course.",
            chapters=[
                BlueprintChapter(id="chapter_1", title="Foundations", order=1),
                BlueprintChapter(id="chapter_2", title="Advanced Topics", order=2),
            ],
        )
        chapters = [
            GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="Foundations", summary="The basics."),
            GeneratedChapter(chapter_id="chapter_2", chapter_number=2, title="Advanced Topics", summary="Going further."),
        ]
        document = build_document(
            course_id="course_toc_test",
            blueprint=blueprint,
            template=template,
            chapters=chapters,
            storage=storage,
        )
        toc_pages = [page for page in document.pages if page.kind == "toc"]
        assert len(toc_pages) == 1
        toc_blocks = toc_pages[0].blocks
        assert len(toc_blocks) == 1
        block = toc_blocks[0]
        assert block.type is BlockType.IMAGE
        assert block.content["kind"] == "toc"
        assert block.content["path"]

        abs_path = storage.asset_abs_path("course_toc_test", block.content["path"])
        assert abs_path.exists()
        html = abs_path.read_text(encoding="utf-8")
        assert "Foundations" in html
        assert "Advanced Topics" in html

    def test_a_single_chapter_course_gets_no_toc_page(self, storage):
        template = load_template("technical_v1")
        blueprint = CourseBlueprint(
            course_title="Solo Chapter Course",
            audience="engineers",
            template_id="technical_v1",
            course_summary="One chapter only.",
            chapters=[BlueprintChapter(id="chapter_1", title="Everything", order=1)],
        )
        chapters = [
            GeneratedChapter(chapter_id="chapter_1", chapter_number=1, title="Everything", summary="It all fits here."),
        ]
        document = build_document(
            course_id="course_solo_test",
            blueprint=blueprint,
            template=template,
            chapters=chapters,
            storage=storage,
        )
        assert not [page for page in document.pages if page.kind == "toc"]


class TestFinalizeTocPage:
    """The two-pass contract: build_document's own _toc_page is a
    placeholder (no real page numbers - every page number is still 0 at
    that point, since chapter content hasn't been laid out yet);
    finalize_toc_page runs once the whole document's pagination is final
    and replaces that SAME block with real numbers, touching nothing
    else."""

    def _document_and_chapters(self, storage):
        template = load_template("technical_v1")
        blueprint = CourseBlueprint(
            course_title="Finalize TOC Course",
            audience="engineers",
            template_id="technical_v1",
            course_summary="A course for finalize_toc_page.",
            chapters=[
                BlueprintChapter(id="chapter_1", title="Foundations", order=1),
                BlueprintChapter(id="chapter_2", title="Advanced Topics", order=2),
            ],
        )
        chapters = [
            GeneratedChapter(
                chapter_id="chapter_1",
                chapter_number=1,
                title="Foundations",
                summary="The basics, start to finish.",
                blocks=[
                    {"type": "heading", "content": {"text": "Foundations", "level": 1}},
                    {"type": "paragraph", "content": {"text": "Intro paragraph. " * 10}},
                    {"type": "heading", "content": {"text": "Learning Objectives", "level": 3}},
                    {"type": "learning_objectives", "content": {"items": ["Understand the basics."]}},
                    {"type": "paragraph", "content": {"text": "Body content. " * 60}},
                ],
            ),
            GeneratedChapter(
                chapter_id="chapter_2",
                chapter_number=2,
                title="Advanced Topics",
                summary="Going further with the material.",
                blocks=[
                    {"type": "heading", "content": {"text": "Advanced Topics", "level": 1}},
                    {"type": "paragraph", "content": {"text": "More content. " * 80}},
                ],
            ),
        ]
        document = build_document(
            course_id="course_finalize_toc",
            blueprint=blueprint,
            template=template,
            chapters=chapters,
            storage=storage,
        )
        return document, chapters, template

    def test_placeholder_toc_has_no_real_page_numbers(self, storage):
        document, _chapters, _template = self._document_and_chapters(storage)
        toc_block = next(b for p in document.pages if p.kind == "toc" for b in p.blocks)
        html = storage.asset_abs_path("course_finalize_toc", toc_block.content["path"]).read_text(encoding="utf-8")
        assert ">0<" not in html

    def test_finalize_fills_in_real_page_numbers_and_sub_sections(self, storage):
        document, chapters, template = self._document_and_chapters(storage)
        finalize_toc_page(document, chapters, template, storage=storage)

        toc_block = next(b for p in document.pages if p.kind == "toc" for b in p.blocks)
        html = storage.asset_abs_path("course_finalize_toc", toc_block.content["path"]).read_text(encoding="utf-8")
        assert "1.1" in html
        assert "Learning Objectives" in html
        # Every page number shown is a real, positive content-page number.
        pages_shown = [int(n) for n in re.findall(r'<td class="toc-page">(\d+)</td>', html)]
        assert pages_shown  # at least one real number was actually filled in
        assert all(n >= 3 for n in pages_shown)  # content starts after cover (1) + toc (2)

    def test_finalize_touches_only_the_toc_block_not_any_other_page(self, storage):
        document, chapters, template = self._document_and_chapters(storage)
        before = [(page.page_number, page.kind, [b.id for b in page.blocks]) for page in document.pages]

        finalize_toc_page(document, chapters, template, storage=storage)

        after = [(page.page_number, page.kind, [b.id for b in page.blocks]) for page in document.pages]
        assert before == after  # same pages, same order, same blocks - only the TOC block's content changed
