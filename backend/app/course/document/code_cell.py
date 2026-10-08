"""The code cell as a document block: its geometry, its states, its output.

One module is the source of truth for three consumers that previously each had
their own idea of how tall a cell is - and disagreed:

* the layout engine   (`layout._code_cell_height`) reserves room for it,
* the PDF renderer    (`html_renderer._cell_context` + the `.cell-*` CSS)
  draws it,
* the editor          (`CodeCellBlock.tsx`) mirrors the same constants.

If the reserved height is smaller than what is drawn, the next block is
painted over it; if it is larger, the page has dead space. So every number
that decides a height lives here, next to the CSS that must match it.

Structure (identical in the editor and the PDF):

    +-- head row ------------------------------------------+  HEAD_H
    |  PYTHON CODE                       (editor: language, Run)
    +-- code box (bordered, light grey) ---------------------+
    |  1  import sys                                         |  rows * ROW_H
    |  2  print("hi")                                        |
    +--------------------------------------------------------+
         OUT_GAP
    +-- output box (bordered, white) -------------------------+
    |  OUTPUT . SUCCESS . 0.02s                              |  OUT_HEAD_H
    |  hi                                                    |  rows * ROW_H
    +--------------------------------------------------------+

Code and output are two independent boxes in normal flow. Output is never
drawn inside the code box and nothing here is absolutely positioned.

Output is shown only while it still belongs to the cell's code. A stale
result is *omitted*, never shown: presenting output as if it came from code it
did not come from is the failure this whole feature exists to avoid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.schemas.blocks import execution_is_current

# --- geometry, in CSS px ------------------------------------------------------
# Mirrored by the `.cell-*` rules in render/templates/course.html.j2 and by
# frontend/src/components/blocks/CodeCellBlock.tsx. Change them together.

HEAD_H = 30.0          # language label / toolbar row above the code box
BORDER = 1.0
FONT = 12.5
ROW_H = 18.0           # one rendered line of code or output
CODE_PAD_Y = 8.0       # vertical padding inside the code box
GUTTER_W = 40.0        # line-number column
TEXT_PAD_R = 12.0      # right padding of the code text
OUT_GAP = 8.0          # space between the code box and the output box
OUT_PAD_Y = 8.0
OUT_PAD_X = 12.0
OUT_HEAD_H = 20.0      # "OUTPUT . SUCCESS . 0.02s"
OUT_HEAD_GAP = 6.0     # between that header and the output text
STREAM_GAP = 4.0       # between stdout and stderr when both are present
CAPTION_GAP = 6.0

# Widest monospace face this might fall back to (JetBrains Mono 0.60em,
# DejaVu Sans Mono 0.602em, Consolas 0.55em): used to decide where long lines
# wrap. Slightly high on purpose - an estimate that wraps early reserves a
# little too much, one that wraps late lets the next block be painted over.
MONO_CHAR_W = FONT * 0.62

# A PDF page is finite and a learner needs the gist, not 40 KB of log. The
# editor still shows the full (separately capped) output; this bounds print.
MAX_OUTPUT_LINES = 40

# The two states a fragment of a split cell can be in. Only ever set on the
# throwaway copy made for printing (see layout.split_code_cell) - a stored
# document always holds whole cells.
MIN_CODE_LINES_PER_FRAGMENT = 3

# Display names for the PDF header. The editor gets labels from the executor,
# but a PDF must render even when the executor is down, so this is static.
LANGUAGE_LABELS: dict[str, str] = {
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "java": "Java",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "go": "Go",
    "rust": "Rust",
    "php": "PHP",
    "ruby": "Ruby",
    "sql": "SQL",
    "bash": "Bash",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "r": "R",
}

# Names a model (or a person) commonly uses for a language, mapped to the id
# the executor and the editor use. The executor resolves aliases too, but a
# stored cell should already carry the canonical id so the editor's language
# dropdown can show it as selected.
LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "nodejs": "javascript", "node.js": "javascript",
    "ts": "typescript",
    "c++": "cpp", "cxx": "cpp", "cplusplus": "cpp",
    "c#": "csharp", "cs": "csharp", "c sharp": "csharp",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "sh": "bash", "shell": "bash", "zsh": "bash",
    "kt": "kotlin",
}


def canonical_language(language: str) -> str:
    key = " ".join((language or "").strip().lower().split())
    return LANGUAGE_ALIASES.get(key, key)


def language_label(language: str) -> str:
    key = canonical_language(language)
    return LANGUAGE_LABELS.get(key) or (language or "Code").strip() or "Code"


# --- code ---------------------------------------------------------------------


def code_lines(code: str) -> list[str]:
    """The lines a cell shows, one numbered row each.

    A trailing newline does not add a phantom empty row, and an empty cell
    still has one (empty) row so it keeps a sensible height.
    """
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def _rows(lines: list[str], text_width: float) -> int:
    """Rendered rows for `lines` at `text_width` px, counting long-line wrap."""
    chars = max(int(text_width / MONO_CHAR_W), 8)
    return sum(max(1, math.ceil(len(line.expandtabs(4)) / chars)) for line in lines)


def code_text_width(block_width: float) -> float:
    return block_width - 2 * BORDER - GUTTER_W - TEXT_PAD_R


def code_box_height(lines: list[str], block_width: float) -> float:
    rows = _rows(lines, code_text_width(block_width))
    return 2 * BORDER + 2 * CODE_PAD_Y + rows * ROW_H


# --- output -------------------------------------------------------------------

_STATUS_WORD = {"success": "SUCCESS", "error": "ERROR", "timeout": "TIMEOUT"}


@dataclass(frozen=True)
class OutputView:
    """How a cell's output area reads, derived from the stored result.

    `state` is one of success / error / timeout (current output, shown),
    stale (ran, but the code has changed since - output withheld) and none
    (never run). Everything a renderer needs is here, so the PDF and the
    editor cannot disagree about it.
    """

    state: str
    label: str
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    message: str = ""

    @property
    def has_body(self) -> bool:
        return self.state in _STATUS_WORD

    @property
    def is_empty(self) -> bool:
        return not self.stdout.strip() and not self.stderr.strip()


def _clip(text: str) -> str:
    lines = (text or "").rstrip("\n").split("\n") if text else []
    if len(lines) <= MAX_OUTPUT_LINES:
        return "\n".join(lines)
    hidden = len(lines) - MAX_OUTPUT_LINES
    return "\n".join(lines[:MAX_OUTPUT_LINES] + [f"... ({hidden} more lines not shown)"])


def output_view(content: dict[str, Any]) -> OutputView:
    if content.get("execution") is None:
        return OutputView("none", "OUTPUT · NOT EXECUTED")
    if not execution_is_current(content):
        return OutputView(
            "stale",
            "OUTPUT · STALE",
            message="Code changed since the last run; output not shown.",
        )
    execution: dict[str, Any] = content["execution"]
    status = str(execution.get("status") or "success")
    seconds = float(execution.get("execution_time") or 0.0)
    return OutputView(
        state=status if status in _STATUS_WORD else "error",
        label=f"OUTPUT · {_STATUS_WORD.get(status, status.upper())} · {seconds:.2f}s",
        stdout=_clip(str(execution.get("stdout") or "")),
        stderr=_clip(str(execution.get("stderr") or "")),
        truncated=bool(execution.get("truncated")),
    )


def output_box_height(view: OutputView, block_width: float) -> float:
    base = 2 * BORDER + 2 * OUT_PAD_Y + OUT_HEAD_H
    if not view.has_body:
        return base  # a one-line status strip: never run, or stale

    text_width = block_width - 2 * BORDER - 2 * OUT_PAD_X
    rows = 0
    streams = 0
    for stream in (view.stdout, view.stderr):
        if stream.strip():
            rows += _rows(stream.split("\n"), text_width)
            streams += 1
    if rows == 0:
        rows = 1  # "(no output)"
    if view.truncated:
        rows += 1  # "Output was cut short."
    return base + OUT_HEAD_GAP + rows * ROW_H + (STREAM_GAP if streams > 1 else 0.0)


# --- what the PDF actually shows ---------------------------------------------


@dataclass(frozen=True)
class CellParts:
    """Which pieces of a cell are drawn, and with what numbering.

    Whole cells show everything. A cell too tall for any page is split into
    fragments *for printing only* (layout.split_code_cell): each fragment
    shows its slice of the code, and only the last one shows the output.
    """

    lines: list[str]
    first_line_number: int
    show_code: bool
    show_output: bool
    continued: bool


def cell_parts(content: dict[str, Any]) -> CellParts:
    return CellParts(
        lines=code_lines(str(content.get("code") or "")),
        first_line_number=int(content.get("cell_line_offset") or 0) + 1,
        show_code=not content.get("cell_hide_code"),
        show_output=not content.get("cell_hide_output"),
        continued=bool(content.get("cell_continued")),
    )


def static_code_height(content: dict[str, Any], block_width: float) -> float:
    """A static `code` block: head row + code box, the same box a cell draws.

    Reuses the cell's row/padding numbers, so the height follows the real
    number of rendered lines (long lines wrap) and nothing is padded for show.
    The caption is added by the caller, which owns text measurement.
    """
    return HEAD_H + code_box_height(code_lines(str(content.get("code") or "")), block_width)


def cell_body_height(content: dict[str, Any], block_width: float) -> float:
    """Head + code box + output box. The caption is added by the caller,
    which owns text measurement."""
    parts = cell_parts(content)
    height = HEAD_H
    if parts.show_code:
        height += code_box_height(parts.lines, block_width)
    if parts.show_output:
        gap = OUT_GAP if parts.show_code else 0.0
        height += gap + output_box_height(output_view(content), block_width)
    return height


# --- compatibility ------------------------------------------------------------


@dataclass(frozen=True)
class CellOutput:
    status: str
    stdout: str
    stderr: str
    seconds: float
    phase: str | None

    @property
    def is_empty(self) -> bool:
        return not self.stdout.strip() and not self.stderr.strip()


def visible_output(content: dict[str, Any]) -> CellOutput | None:
    """The current output, or None when there is none to show (never run, or
    run but the code has changed since)."""
    if not execution_is_current(content):
        return None
    execution: dict[str, Any] = content["execution"]
    return CellOutput(
        status=str(execution.get("status") or "success"),
        stdout=_clip(str(execution.get("stdout") or "")),
        stderr=_clip(str(execution.get("stderr") or "")),
        seconds=float(execution.get("execution_time") or 0.0),
        phase=execution.get("phase"),
    )
