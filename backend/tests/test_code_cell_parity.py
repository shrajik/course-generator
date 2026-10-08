"""The editor and the backend must agree on a code cell's geometry.

The cell's size is decided in three places that cannot import one another: the
layout engine (reserves room), the PDF stylesheet (draws it), and the editor's
React component (draws it on screen). When their numbers drift the next block
is painted over the cell - the exact bug this guards against. Rather than hope
three copies stay in step, this reads them and compares.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.course.document import code_cell as py

REPO = Path(__file__).resolve().parents[2]
TS_FILE = REPO / "frontend" / "src" / "lib" / "content" / "code-cell.ts"
TEMPLATE = REPO / "backend" / "app" / "render" / "templates" / "course.html.j2"

# TypeScript key -> Python constant
PAIRS = {
    "headH": "HEAD_H",
    "border": "BORDER",
    "font": "FONT",
    "rowH": "ROW_H",
    "codePadY": "CODE_PAD_Y",
    "gutterW": "GUTTER_W",
    "textPadR": "TEXT_PAD_R",
    "outGap": "OUT_GAP",
    "outPadY": "OUT_PAD_Y",
    "outPadX": "OUT_PAD_X",
    "outHeadH": "OUT_HEAD_H",
    "outHeadGap": "OUT_HEAD_GAP",
    "streamGap": "STREAM_GAP",
    "maxOutputLines": "MAX_OUTPUT_LINES",
}

needs_frontend = pytest.mark.skipif(
    not TS_FILE.exists(), reason="the frontend source is not mounted in this environment"
)


def _ts_cell() -> dict[str, float]:
    block = re.search(r"export const CELL = \{(.*?)\} as const;", TS_FILE.read_text("utf-8"), re.S)
    assert block, "CELL constant not found in code-cell.ts"
    return {k: float(v) for k, v in re.findall(r"(\w+):\s*([\d.]+),", block.group(1))}


@needs_frontend
@pytest.mark.parametrize(("ts_key", "py_name"), sorted(PAIRS.items()))
def test_the_editor_and_the_backend_use_the_same_number(ts_key: str, py_name: str) -> None:
    assert _ts_cell()[ts_key] == float(getattr(py, py_name)), (
        f"CELL.{ts_key} (editor) and {py_name} (backend) differ - a cell would be drawn "
        "taller or shorter than the room reserved for it"
    )


@needs_frontend
def test_every_editor_constant_is_checked() -> None:
    assert set(_ts_cell()) == set(PAIRS), "a new CELL constant needs a counterpart in PAIRS"


def test_the_pdf_stylesheet_uses_the_same_numbers() -> None:
    """The `.cell-*` CSS is the third copy. Check the numbers that set heights."""
    # Template expressions contain "}", which would end a rule early.
    css = re.sub(r"\{\{.*?\}\}", "X", TEMPLATE.read_text("utf-8"))

    def rule(selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
        assert match, f"{selector} not found in course.html.j2"
        return match.group(1)

    assert f"height: {py.HEAD_H:g}px" in rule(".cell-head")
    assert f"padding: {py.CODE_PAD_Y:g}px 0" in rule(".cell-code")
    assert f"min-height: {py.ROW_H:g}px" in rule(".cell-row")
    assert f"line-height: {py.ROW_H:g}px" in rule(".cell-row")
    assert f"flex: 0 0 {py.GUTTER_W:g}px" in rule(".cell-ln")
    assert f"padding-right: {py.TEXT_PAD_R:g}px" in rule(".cell-tx")
    assert f"margin-top: {py.OUT_GAP:g}px" in rule(".cell-out")
    assert f"padding: {py.OUT_PAD_Y:g}px {py.OUT_PAD_X:g}px" in rule(".cell-out")
    assert f"height: {py.OUT_HEAD_H:g}px" in rule(".cell-out-head")
    assert f"margin-top: {py.OUT_HEAD_GAP:g}px" in rule(".cell-out-body")
    assert f"line-height: {py.ROW_H:g}px" in rule(".cell-out-body pre")
    assert f"margin-top: {py.STREAM_GAP:g}px" in rule(".cell-out-body pre + pre")
    assert f"border: {py.BORDER:g}px solid" in rule(".cell-code")
    assert f"border: {py.BORDER:g}px solid" in rule(".cell-out")
