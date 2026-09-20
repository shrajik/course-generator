"""Unit tests for the colorful, static course-contents index page - both the
renderer itself (app.render.toc_renderer) and its wiring into the document
builder (app.course.document.builder._toc_page / build_document)."""

from __future__ import annotations

import re

from app.course.document.builder import build_document
from app.course.templates.registry import load_template
from app.render.toc_renderer import TocChapter, estimate_toc_pixel_size, render_toc_html
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

    def test_cards_use_a_variety_of_colours_not_one_repeated_colour(self):
        chapters = [_chapter(i, f"Chapter {i}") for i in range(1, 4)]
        html = render_toc_html(chapters, course_title="Test Course", theme=TemplateTheme()).decode("utf-8")
        fills = re.findall(r"--toc-fill:(#[0-9a-fA-F]{6})", html)
        assert len(set(fills)) > 1

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
    def test_build_document_produces_a_colorful_toc_image_block(self, storage):
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
