"""Code cells as document blocks: height, structure, pagination.

The properties under test are the ones that were broken:

* a cell's reserved height must include everything it draws (head, code, output),
  and grow with its code and output - no fixed heights;
* code and output are two independent boxes - the output is never inside the
  code box, and nothing is absolutely positioned;
* the PDF never draws past a sheet: a cell that does not fit the rest of a page
  moves whole, and only a cell taller than any page is split;
* the stored document is never altered by printing.

Pure-Python tests run everywhere. The `browser` tests measure real layout in
Chromium and skip where it is not installed (they run in the backend image).
"""

from __future__ import annotations

import copy
from html.parser import HTMLParser

import pytest

from app.course.document.builder import _page_size_for
from app.course.document.code_cell import (
    BORDER,
    CODE_PAD_Y,
    HEAD_H,
    MAX_OUTPUT_LINES,
    OUT_GAP,
    OUT_HEAD_GAP,
    OUT_HEAD_H,
    OUT_PAD_Y,
    ROW_H,
    code_lines,
    output_view,
)
from app.course.document.layout import estimate_height, flow_blocks
from app.course.document.layout_validator import page_overflow
from app.course.document.print import paginate_for_print
from app.course.templates.registry import load_template
from app.render.html_renderer import render_document_html
from app.schemas.blocks import BlockType
from app.schemas.document import Block, BlockMeta, CourseDocument, DocumentMeta, Page, PageSize

TEMPLATE = load_template("technical_v1")
SHEET = TEMPLATE.theme.geometry()

STRIP = 2 * BORDER + 2 * OUT_PAD_Y + OUT_HEAD_H  # a header-only output box


def make_cell(code: str, *, language: str = "python", execution: dict | None = None,
              block_id: str = "block_cell0001", caption: str = "") -> Block:
    content = {"language": language, "code": code, "caption": caption}
    if execution is not None:
        content["execution"] = {"language": language, "source": code, **execution}
    return Block(id=block_id, type=BlockType.CODE_CELL, content=content, meta=BlockMeta())


def ran(code: str, stdout: str = "ok\n", **kw) -> Block:
    return make_cell(code, execution={"status": "success", "stdout": stdout, "stderr": "",
                                      "execution_time": 0.02}, **kw)


def lines_of(n: int) -> str:
    return "\n".join(f"print({i})" for i in range(n))


def paragraph(text: str, block_id: str) -> Block:
    return Block(id=block_id, type=BlockType.PARAGRAPH, content={"text": text}, meta=BlockMeta())


def document(blocks: list[Block]) -> CourseDocument:
    """A stored document: laid out by the engine (cells whole, never split) with
    each page grown to hold what it was given - exactly what the builder makes."""
    pages_of = flow_blocks(blocks, geometry=SHEET)
    return CourseDocument(
        document_id="doc_layout", course_id="crs_layout", course_title="Layout",
        template_id="technical_v1", meta=DocumentMeta(),
        pages=[
            Page(id=f"page_{n}", page_number=n, kind="content",
                 size=_page_size_for(page_blocks, SHEET), blocks=page_blocks)
            for n, page_blocks in enumerate(pages_of, 1)
        ],
    )


# --- height is computed from content, never fixed -----------------------------


def never_run_height(code: str) -> float:
    rows = len(code_lines(code))
    return HEAD_H + (2 * BORDER + 2 * CODE_PAD_Y + rows * ROW_H) + OUT_GAP + STRIP


@pytest.mark.parametrize("n", [3, 10, 30, 50, 80])
def test_code_height_is_exactly_its_rows(n: int) -> None:
    """Head + code box + the "not executed" strip. No slack, no fixed height."""
    cell = make_cell(lines_of(n))
    assert estimate_height(cell) == pytest.approx(never_run_height(lines_of(n)))


def test_each_extra_line_of_code_adds_exactly_one_row() -> None:
    assert estimate_height(make_cell(lines_of(11))) - estimate_height(make_cell(lines_of(10))) == ROW_H


def test_a_trailing_newline_does_not_add_a_phantom_row() -> None:
    assert code_lines("a\nb\n") == ["a", "b"]
    assert estimate_height(make_cell("a\nb\n")) == estimate_height(make_cell("a\nb"))


def test_an_empty_cell_still_has_a_row() -> None:
    assert code_lines("") == [""]
    assert estimate_height(make_cell("")) == pytest.approx(never_run_height(""))


def test_a_long_line_wraps_into_more_rows_and_more_height() -> None:
    short = estimate_height(make_cell("x = 1"))
    wrapped = estimate_height(make_cell("x = '" + "a" * 300 + "'"))
    assert wrapped >= short + 2 * ROW_H


@pytest.mark.parametrize("out_lines", [1, 5, 20])
def test_output_height_follows_its_lines(out_lines: int) -> None:
    code = lines_of(3)
    stdout = "".join(f"line {i}\n" for i in range(out_lines))
    expected = (HEAD_H + 2 * BORDER + 2 * CODE_PAD_Y + 3 * ROW_H + OUT_GAP
                + 2 * BORDER + 2 * OUT_PAD_Y + OUT_HEAD_H + OUT_HEAD_GAP + out_lines * ROW_H)
    assert estimate_height(ran(code, stdout)) == pytest.approx(expected)


def test_output_is_capped_for_print_not_unbounded() -> None:
    capped = estimate_height(ran(lines_of(3), "x\n" * 500))
    # 40 rows + the "N more lines not shown" row.
    assert capped == pytest.approx(
        estimate_height(ran(lines_of(3), "x\n" * MAX_OUTPUT_LINES)) + ROW_H
    )


def test_stdout_and_stderr_are_both_reserved() -> None:
    both = make_cell("x", execution={"status": "error", "stdout": "a\n", "stderr": "b\n",
                                     "execution_time": 0.1})
    only_out = ran("x", "a\n")
    assert estimate_height(both) > estimate_height(only_out)


def test_a_cell_with_no_output_reserves_one_row_for_the_placeholder() -> None:
    assert estimate_height(ran("x", "")) > estimate_height(make_cell("x"))


@pytest.mark.parametrize("status", ["success", "error", "timeout"])
def test_every_execution_status_has_a_reserved_height(status: str) -> None:
    cell = make_cell("x", execution={"status": status, "stdout": "o\n", "stderr": "", "execution_time": 1})
    assert estimate_height(cell) > never_run_height("x")


def test_unexecuted_and_stale_cells_reserve_the_same_one_line_strip() -> None:
    unrun = make_cell("print(2)")
    stale = make_cell("print(2)", execution={"status": "success", "stdout": "OLD\n", "stderr": "",
                                              "execution_time": 0.1})
    stale.content["execution"]["source"] = "print(1)"  # the code has changed since
    assert estimate_height(stale) == estimate_height(unrun)


# --- output states -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("success", "OUTPUT · SUCCESS · 0.02s"),
        ("error", "OUTPUT · ERROR · 0.42s"),
        ("timeout", "OUTPUT · TIMEOUT · 10.00s"),
    ],
)
def test_output_labels_match_the_spec(status: str, label: str) -> None:
    seconds = {"success": 0.02, "error": 0.42, "timeout": 10.0}[status]
    view = output_view(make_cell("x", execution={"status": status, "stdout": "", "stderr": "",
                                                  "execution_time": seconds}).content)
    assert view.label == label


def test_not_executed_and_stale_labels() -> None:
    assert output_view(make_cell("x").content).label == "OUTPUT · NOT EXECUTED"
    stale = make_cell("x", execution={"status": "success", "stdout": "", "stderr": "", "execution_time": 0})
    stale.content["execution"]["source"] = "different"
    view = output_view(stale.content)
    assert view.label == "OUTPUT · STALE"
    assert view.stdout == ""  # withheld, never shown


# --- structure: two independent boxes ------------------------------------------


class _Tree(HTMLParser):
    """Just enough of a DOM to ask 'is X inside Y?'."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, set[str]]] = []
        self.ancestors_of: dict[str, list[set[str]]] = {}

    def handle_starttag(self, tag, attrs):
        classes = set(dict(attrs).get("class", "").split())
        for cls in classes:
            self.ancestors_of.setdefault(cls, []).append({c for _, cs in self.stack for c in cs})
        if tag not in {"br", "img", "meta", "link", "input"}:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def render(blocks: list[Block]) -> str:
    return render_document_html(document(blocks), TEMPLATE)


def test_output_is_a_sibling_of_the_code_box_never_inside_it() -> None:
    tree = _Tree()
    tree.feed(render([ran(lines_of(4), "hello\n")]))
    assert tree.ancestors_of["cell-out"], "the output box must be rendered"
    for ancestors in tree.ancestors_of["cell-out"]:
        assert "cell-code" not in ancestors
    for ancestors in tree.ancestors_of["cell-stdout"]:
        assert "cell-code" not in ancestors


def test_the_block_box_carries_no_dark_chrome() -> None:
    """The regression behind the 'giant dark rectangle': the template's code
    style was painted on the whole block, so the output inherited light text on
    a light box."""
    html = render([ran("print(1)")])
    box = next(line for line in html.splitlines() if "b-code_cell" in line)
    for forbidden in ("background", "color:", "padding", "border"):
        assert forbidden not in box, f"block box must not set {forbidden!r}: {box}"


def test_the_template_gives_a_cell_typography_but_no_chrome() -> None:
    """A cell draws its own boxes. A background/colour/padding on the template
    entry is how the whole block once ended up dark navy."""
    style = TEMPLATE.style_for(BlockType.CODE_CELL)
    assert style, "an allowed block type must have a style entry"
    for chrome in ("background", "color", "padding", "border_color", "border_radius", "border_width"):
        assert chrome not in style, f"code_cell style must not set {chrome}"


def test_a_cell_ignores_chrome_on_its_own_block_style() -> None:
    """Even if a stored block still carries the old dark style, it is not drawn."""
    from app.schemas.document import BlockStyle

    cell = ran("print(1)")
    cell.style = BlockStyle(background="#0f172a", color="#e2e8f0", padding=16, border_radius=10)
    html = render([cell])
    box = next(line for line in html.splitlines() if "b-code_cell" in line)
    assert "#0f172a" not in box and "#e2e8f0" not in box and "padding" not in box


def test_cell_css_never_uses_absolute_positioning() -> None:
    html = render([ran("print(1)")])
    cell_rules = [line for line in html.splitlines() if ".cell-" in line]
    assert cell_rules
    assert not any("absolute" in line for line in cell_rules)
    # nor does the cell's own markup
    start = html.index('<div class="cell-head">')
    assert "absolute" not in html[start:html.index("</body>")].split('class="footer"')[0]


def test_line_numbers_are_rendered_in_order() -> None:
    html = render([make_cell(lines_of(5))])
    numbers = [int(part.split("<")[0]) for part in html.split('<span class="cell-ln">')[1:]]
    assert numbers == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("language", ["python", "javascript", "cpp", "java", "go", "rust", "sql", "zig"])
def test_the_renderer_is_language_agnostic(language: str) -> None:
    html = render([ran("some code", language=language)])
    assert "cell-code" in html and "OUTPUT · SUCCESS" in html


def test_static_code_is_a_light_box_with_no_run_or_output() -> None:
    """A static block shares the cell's light box but has no output or status."""
    static = Block(id="block_static01", type=BlockType.CODE, content={"language": "python",
                   "code": "def f():\n    pass", "caption": ""}, meta=BlockMeta())
    html = render([static])
    body = html[html.index("<body"):]
    assert "cell-code" in body and "Python" in body
    assert "code-panel" not in body and "cell-out" not in body and "OUTPUT" not in body


def test_static_code_height_follows_the_line_count() -> None:
    from app.course.document.code_cell import HEAD_H, ROW_H
    from app.course.document.layout import estimate_height

    def height(n: int) -> float:
        return estimate_height(Block(id="block_h0000001", type=BlockType.CODE, meta=BlockMeta(),
                               content={"language": "python", "code": "\n".join(["x = 1"] * n)}))

    assert height(11) - height(1) == 10 * ROW_H
    assert height(1) < HEAD_H + 2 * ROW_H + 20  # no fixed or padded-out height


# --- pagination ----------------------------------------------------------------


def test_a_normal_document_is_printed_exactly_as_stored() -> None:
    doc = document([paragraph("hello", "block_p0000001"), ran(lines_of(5))])
    assert paginate_for_print(doc, TEMPLATE) is doc


def test_a_cell_that_fits_a_page_is_never_split() -> None:
    """Moved whole to the next page - never half code on one and half on the next."""
    filler = paragraph("word " * 700, "block_p0000002")  # most of a page
    blocks = [filler, ran(lines_of(20), "o\n" * 5)]
    pages = flow_blocks(blocks, geometry=SHEET, split_oversized_cells=True)
    cells = [b for page in pages for b in page if b.type is BlockType.CODE_CELL]
    assert len(cells) == 1
    assert not cells[0].content.get("cell_continued")
    assert cells[0].content["code"] == lines_of(20)


@pytest.mark.parametrize("n", [30, 55, 90])
def test_a_cell_taller_than_a_page_is_split_so_nothing_leaves_the_sheet(n: int) -> None:
    stored = document([ran(lines_of(n), "x\n" * 20)])
    before = stored.model_dump()
    printed = paginate_for_print(stored, TEMPLATE)

    assert stored.model_dump() == before, "printing must not alter the stored document"
    assert len(printed.pages) > len(stored.pages)
    for page in printed.pages:
        assert page.size.height == SHEET.height
        assert page_overflow(page) <= 2.0, f"page {page.page_number} overflows its sheet"


def test_fragments_keep_every_line_once_and_number_them_continuously() -> None:
    code = lines_of(70)
    pages = flow_blocks([ran(code, "x\n" * 20)], geometry=SHEET, split_oversized_cells=True)
    fragments = [b for page in pages for b in page if b.type is BlockType.CODE_CELL]
    assert len(fragments) >= 2

    seen: list[str] = []
    expected_offset = 0
    for fragment in fragments:
        if fragment.content.get("cell_hide_code"):
            continue
        assert fragment.content.get("cell_line_offset", 0) == expected_offset
        lines = code_lines(fragment.content["code"])
        seen.extend(lines)
        expected_offset += len(lines)
    assert seen == code_lines(code), "no line may be lost or duplicated"


def test_only_the_last_fragment_shows_the_output() -> None:
    pages = flow_blocks([ran(lines_of(70), "x\n" * 20)], geometry=SHEET, split_oversized_cells=True)
    fragments = [b for page in pages for b in page if b.type is BlockType.CODE_CELL]
    shows = [not f.content.get("cell_hide_output") for f in fragments]
    assert shows == [False] * (len(fragments) - 1) + [True]


def test_output_alone_moves_when_the_code_fits_but_the_output_does_not() -> None:
    pages = flow_blocks([ran(lines_of(30), "x\n" * 20)], geometry=SHEET, split_oversized_cells=True)
    fragments = [b for page in pages for b in page if b.type is BlockType.CODE_CELL]
    assert [f.content.get("cell_hide_code", False) for f in fragments] == [False, True]
    assert fragments[0].content["code"] == lines_of(30)


def test_a_fragment_is_judged_against_the_whole_cells_code() -> None:
    """A fragment holds a slice of the code, but its output belongs to the whole."""
    html = render_document_html(
        paginate_for_print(document([ran(lines_of(70), "RESULT\n" * 3)]), TEMPLATE),
        TEMPLATE,
    )
    assert "RESULT" in html
    assert "OUTPUT · SUCCESS" in html
    assert "(continued)" in html


def test_stale_output_stays_withheld_across_fragments() -> None:
    cell = ran(lines_of(70), "OLD-OUTPUT\n")
    cell.content["code"] = lines_of(70) + "\nprint('edited')"  # edited after the run
    html = render_document_html(paginate_for_print(document([cell]), TEMPLATE), TEMPLATE)
    assert "OLD-OUTPUT" not in html
    assert "OUTPUT · STALE" in html


def test_no_code_line_is_lost_from_the_pdf() -> None:
    html = render_document_html(
        paginate_for_print(document([ran(lines_of(90), "x\n")]), TEMPLATE), TEMPLATE
    )
    for i in range(90):
        assert html.count(f">print({i})<") == 1, f"print({i}) must appear exactly once"


def test_pages_are_renumbered_after_printing() -> None:
    printed = paginate_for_print(document([ran(lines_of(90), "x\n" * 20)]), TEMPLATE)
    assert [p.page_number for p in printed.pages] == list(range(1, len(printed.pages) + 1))
    assert [p.id for p in printed.pages] == [f"page_{n}" for n in range(1, len(printed.pages) + 1)]


def test_a_stored_document_keeps_its_cells_whole() -> None:
    """The editor edits one cell, not fragments: pagination for the stored
    document never splits one."""
    pages = flow_blocks([ran(lines_of(90), "x\n" * 20)], geometry=SHEET)
    cells = [b for page in pages for b in page if b.type is BlockType.CODE_CELL]
    assert len(cells) == 1
    assert cells[0].content["code"] == lines_of(90)


# --- real layout in a browser ---------------------------------------------------
# Async Playwright: the sync API cannot run inside pytest-asyncio's event loop.
# These skip (rather than fail) where Chromium is not installed; they run in the
# backend image, which has it.


async def _natural_heights(html: str) -> list[dict]:
    """Each block's height with its min-height removed: what it really needs."""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            chromium = await p.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {exc}")
        page = await chromium.new_page(viewport={"width": int(SHEET.width), "height": int(SHEET.height)})
        await page.set_content(html, wait_until="load")
        await page.add_style_tag(content=".block { min-height: 0 !important; }")
        out = await page.evaluate(
            "() => [...document.querySelectorAll('.block')].map(el => ({ top: el.offsetTop,"
            " height: el.offsetHeight,"
            " page: [...document.querySelectorAll('.page')].indexOf(el.closest('.page')) }))"
        )
        await chromium.close()
    return out


CASES = {
    "3 lines, never run": make_cell(lines_of(3)),
    "10 lines, 5 output": ran(lines_of(10), "o\n" * 5),
    "30 lines, 20 output": ran(lines_of(30), "o\n" * 20),
    "50 lines, 1 output": ran(lines_of(50), "ok\n"),
    "error traceback": make_cell("1/0", execution={"status": "error", "stdout": "", "execution_time": 0.4,
        "stderr": "Traceback (most recent call last):\n  File \"main.py\", line 1\nZeroDivisionError: division by zero\n"}),
    "timeout": make_cell("while True: pass", execution={"status": "timeout", "stdout": "", "execution_time": 10.0,
        "stderr": "Execution timed out after 10s"}),
    "long output lines wrap": ran("x", ("w" * 220 + "\n") * 4),
    "long code line wraps": make_cell("x = '" + "word " * 80 + "'"),
    "output over the print cap": ran(lines_of(4), "line\n" * 120),
    "stdout and stderr": make_cell("x", execution={"status": "error", "stdout": "a\nb\n", "stderr": "bad\n",
                                                     "execution_time": 0.1}),
    "empty output": ran("x", ""),
    "stale": ran("print(1)", "OLD\n"),
    "with caption": make_cell("print(1)", caption="A short caption under the code"),
}
CASES["stale"].content["code"] = "print(2)"


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_estimate_matches_what_chromium_draws(name: str) -> None:
    """The root cause of every overlap: a reserved height smaller than what is
    painted. It may reserve a little more (never less), but not much more."""
    cell = copy.deepcopy(CASES[name])
    doc = document([cell])
    reserved = estimate_height(cell)
    cell.layout.height = reserved
    (measured,) = await _natural_heights(render_document_html(doc, TEMPLATE))

    assert measured["height"] <= reserved + 1.5, (
        f"{name}: draws {measured['height']}px but only {reserved}px is reserved - "
        "the next block would be painted over it"
    )
    assert reserved - measured["height"] <= 24, (
        f"{name}: reserves {reserved}px but draws {measured['height']}px - dead space"
    )


async def test_a_mixed_page_has_no_overlap_and_nothing_outside_the_sheet() -> None:
    """Heading, paragraphs, cells with output, a long cell: every block starts
    below the previous one and every page ends inside its sheet."""
    blocks = [
        paragraph("Intro. " + "word " * 80, "block_p0000010"),
        ran(lines_of(3), "hi\n", block_id="block_cell0010"),
        paragraph("Between. " + "word " * 120, "block_p0000011"),
        ran(lines_of(12), "o\n" * 8, block_id="block_cell0011"),
        make_cell(lines_of(5), block_id="block_cell0012"),
        paragraph("After. " + "word " * 60, "block_p0000012"),
        ran(lines_of(60), "o\n" * 25, block_id="block_cell0013"),
        paragraph("End. " + "word " * 40, "block_p0000013"),
    ]
    printed = paginate_for_print(document(blocks), TEMPLATE)
    heights = await _natural_heights(render_document_html(printed, TEMPLATE))

    for page_number in sorted({h["page"] for h in heights}):
        rows = sorted((h for h in heights if h["page"] == page_number), key=lambda h: h["top"])
        for upper, lower in zip(rows, rows[1:]):
            assert upper["top"] + upper["height"] <= lower["top"] + 1.5, (
                f"page {page_number + 1}: a block overlaps the one below it"
            )
        assert rows[-1]["top"] + rows[-1]["height"] <= SHEET.height - 60, (
            f"page {page_number + 1} draws past the sheet"
        )
