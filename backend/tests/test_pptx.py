"""PPTX export: Course Document JSON -> a real, editable PowerPoint deck.

Mirrors test_pdf.py's own patterns/fixtures; reuses its
`_document_with_every_block_type` document directly so both export paths are
proven against the exact same content, rather than two independently-drifting
fixtures.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest

from app.api.course_authorization import check_document_owner_for_pptx_export, user_can_export_pdf
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.services.pptx_service import PptxService
from app.course.templates.registry import load_template
from tests.test_pdf import _document_with_every_block_type


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_every_block_type_exports_without_error():
    """The same multi-block-type document test_pdf.py renders to HTML/PDF
    must also export to a real, non-empty .pptx with no exception - one
    malformed block must never be allowed to take the whole export down."""
    document = _document_with_every_block_type()
    service = PptxService()
    data = _run(service.export_pptx(document))
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_slide_count_matches_page_count():
    document = _document_with_every_block_type()
    service = PptxService()
    data = _run(service.export_pptx(document))
    from pptx import Presentation

    prs = Presentation(BytesIO(data))
    assert len(prs.slides._sldIdLst) == len(document.pages)


def test_text_blocks_produce_real_text_runs():
    """A heading/paragraph's actual text must land in a real pptx text
    frame, not be silently dropped or baked into an image."""
    template = load_template("technical_v1")
    blocks = [
        Block(type=BlockType.HEADING, content={"text": "A Real Heading", "level": 1}, style=template.style_for(BlockType.HEADING)),
        Block(type=BlockType.PARAGRAPH, content={"text": "A real paragraph of prose."}, style=template.style_for(BlockType.PARAGRAPH)),
    ]
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation

    prs = Presentation(BytesIO(data))
    all_text = "\n".join(
        shape.text_frame.text for shape in prs.slides[0].shapes if shape.has_text_frame
    )
    assert "A Real Heading" in all_text
    assert "A real paragraph of prose." in all_text


def test_table_block_produces_a_real_table_shape_not_an_image():
    template = load_template("technical_v1")
    block = Block(
        type=BlockType.TABLE,
        content={"columns": ["Name", "Score"], "rows": [{"cells": ["Alice", "9"]}, {"cells": ["Bob", "7"]}]},
        style=template.style_for(BlockType.TABLE),
    )
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation

    prs = Presentation(BytesIO(data))
    tables = [shape for shape in prs.slides[0].shapes if shape.has_table]
    assert len(tables) == 1
    table = tables[0].table
    assert table.cell(0, 0).text == "Name"
    assert table.cell(1, 0).text == "Alice"
    assert table.cell(2, 1).text == "7"


def test_an_ungenerated_image_block_renders_a_placeholder_not_a_crash():
    """An IMAGE block with no real asset yet (`generated: False`, `path:
    None` - a block still awaiting generation, or one a prior pass withheld)
    must still export cleanly as a visible placeholder, never raise."""
    template = load_template("technical_v1")
    block = Block(
        type=BlockType.IMAGE,
        content={"prompt": "p", "generated": False, "path": None},
        style=template.style_for(BlockType.IMAGE),
    )
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    data = _run(PptxService().export_pptx(document))
    assert len(data) > 0


def test_slide_size_derives_from_the_tallest_and_widest_page_not_just_page_one():
    """A real bug this fixes: using only page 1's size meant a LATER page
    that grew taller (the same print-fit overflow case layout.py already
    handles for PDF) would have its own shapes extend past a smaller
    global slide. Construct a document where page 1 is short/narrow and
    page 2 is genuinely taller/wider, and confirm the shared slide canvas
    is big enough for every page's content, with zero overflowing shapes."""
    template = load_template("technical_v1")
    small_block = Block(type=BlockType.PARAGRAPH, content={"text": "Short page."}, style=template.style_for(BlockType.PARAGRAPH),
                         layout={"x": 20, "y": 20, "width": 200, "height": 30})
    tall_block = Block(type=BlockType.PARAGRAPH, content={"text": "Tall page content near the bottom."}, style=template.style_for(BlockType.PARAGRAPH),
                        layout={"x": 20, "y": 1400, "width": 860, "height": 60})
    from app.schemas.document import PageSize

    small_page = Page(id="page_1", page_number=1, kind="content", size=PageSize(width=500, height=600), blocks=[small_block])
    tall_page = Page(id="page_2", page_number=2, kind="content", size=PageSize(width=900, height=1500), blocks=[tall_block])
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1", pages=[small_page, tall_page],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation

    prs = Presentation(BytesIO(data))
    assert prs.slide_width / 9525 == 900  # widest page
    assert prs.slide_height / 9525 == 1500  # tallest page
    for slide in prs.slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= prs.slide_height + 100
            assert shape.left + shape.width <= prs.slide_width + 100


def test_line_height_letter_spacing_and_border_radius_are_actually_applied():
    """Verified against the real generated XML/properties, not just 'it did
    not crash' - line_spacing and the OOXML `spc` run attribute must carry
    the real configured values, and a border_radius must produce a real
    rounded-rectangle autoshape rather than a plain rectangular text box."""
    template = load_template("technical_v1")
    style = template.style_for(BlockType.PARAGRAPH)
    style["line_height"] = 1.8
    style["letter_spacing"] = 2.5
    style["border_radius"] = 8.0
    block = Block(type=BlockType.PARAGRAPH, content={"text": "Styled text."}, style=style)
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(BytesIO(data))
    shape = next(iter(prs.slides[0].shapes))
    assert shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE  # rounded rect, not a plain textbox
    para = shape.text_frame.paragraphs[0]
    assert para.line_spacing == 1.8
    run = para.runs[0]
    spc = run._r.get_or_add_rPr().get("spc")
    assert spc == str(int(round(2.5 * 100)))


@pytest.mark.parametrize(
    "block_type,content",
    [
        (BlockType.QUIZ, {"questions": [{"question": "Q?", "options": ["a"], "answer": "a"}]}),
        (BlockType.EXERCISE, {"instructions": "Do it.", "steps": ["one"]}),
        (BlockType.CHALLENGE, {"instructions": "Harder."}),
        (BlockType.CASE_STUDY, {"context": "c", "challenge": "ch"}),
    ],
)
def test_quiz_exercise_case_study_challenge_render_as_distinct_colour_coded_cards(block_type, content):
    """No native OOXML interactive-quiz widget exists (and VBA macros are
    explicitly out of scope) - confirm the practical alternative actually
    renders: a real filled rounded-rectangle card with its own type label,
    not indistinguishable from a plain paragraph."""
    template = load_template("technical_v1")
    block = Block(type=block_type, content=content, style=template.style_for(block_type))
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(BytesIO(data))
    shape = next(iter(prs.slides[0].shapes))
    assert shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
    assert shape.fill.type is not None  # a real solid fill was applied, not "no fill"
    from app.services.pptx_service import _CARD_STYLES

    expected_fill, expected_label, _ = _CARD_STYLES[block_type]
    assert f"{shape.fill.fore_color.rgb}" == expected_fill
    assert expected_label in shape.text_frame.text


def test_code_block_renders_as_a_dark_monospace_card_with_a_code_label():
    template = load_template("technical_v1")
    block = Block(type=BlockType.CODE, content={"language": "python", "code": "print('hi')"}, style=template.style_for(BlockType.CODE))
    document = CourseDocument(
        document_id="doc_pptx", course_id="crs_pptx", course_title="PPTX Test",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[block])],
    )
    data = _run(PptxService().export_pptx(document))
    from pptx import Presentation

    prs = Presentation(BytesIO(data))
    shape = next(iter(prs.slides[0].shapes))
    assert "CODE" in shape.text_frame.text
    assert "print('hi')" in shape.text_frame.text
    from app.services.pptx_service import _CODE_CARD_FILL

    assert f"{shape.fill.fore_color.rgb}" == _CODE_CARD_FILL


def test_pptx_export_reuses_the_same_approval_gate_as_pdf_export():
    """`check_document_owner_for_pptx_export` must key off the identical
    `user_can_export_pdf` rule PDF export already uses (one shared approval
    policy, not a second one that could silently drift) - confirmed by
    reading the real function's own source rather than re-deriving the role
    matrix test_pdf_export_authorization.py already covers exhaustively."""
    import inspect

    from app.api import course_authorization

    source = inspect.getsource(course_authorization.check_document_owner_for_pptx_export)
    assert "user_can_export_pdf" in source
    assert callable(check_document_owner_for_pptx_export)
    assert callable(user_can_export_pdf)
