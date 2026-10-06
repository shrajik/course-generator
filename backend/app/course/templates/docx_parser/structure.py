"""Semantic structure extraction from a DOCX, into `TemplateSection`s.

Maps a Word document's own outline onto the existing `CourseTemplate`
vocabulary: each Heading 1 becomes a section, in document order, and its
Heading 2 children become that section's guidance. Ordering is the strongest
signal a DOCX actually carries, so it is preserved exactly.

Two deliberate limits, both because the brief forbids inventing rules that
cannot be inferred from the file:

* `required` is only set when the document *says so* - a literal
  "(required)" / "(optional)" marker next to the heading. Word has no
  standard way to express obligation, so in its absence every section is
  optional and the parse report says no markers were found.
* Block types are inferred from an explicit keyword table below and left
  empty when nothing matches, rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from docx.document import Document as DocxDocument

from app.schemas.blocks import BlockType
from app.schemas.template import TemplateSection

from .placeholders import PLACEHOLDER_RE, names_in

# Explicit obligation markers, e.g. "Glossary (optional)".
_REQUIRED_RE = re.compile(r"[\(\[\-–]\s*(required|mandatory)\s*[\)\]]?", re.IGNORECASE)
_OPTIONAL_RE = re.compile(r"[\(\[\-–]\s*(optional)\s*[\)\]]?", re.IGNORECASE)

# Keyword -> block type. Only mappings where the word genuinely determines the
# content type; ambiguous words ("example", "overview") are left out on
# purpose so a section simply gets no type constraint.
_BLOCK_KEYWORDS: tuple[tuple[str, BlockType], ...] = (
    ("objective", BlockType.LEARNING_OBJECTIVES),
    ("knowledge check", BlockType.QUIZ),
    ("quiz", BlockType.QUIZ),
    ("multiple choice", BlockType.QUIZ),
    ("assessment", BlockType.QUIZ),
    ("worksheet", BlockType.EXERCISE),
    ("practice", BlockType.EXERCISE),
    ("activity", BlockType.EXERCISE),
    ("exercise", BlockType.EXERCISE),
    ("challenge", BlockType.CHALLENGE),
    ("self-check", BlockType.REFLECTION),
    ("reflection", BlockType.REFLECTION),
    ("summary", BlockType.SUMMARY),
    ("takeaway", BlockType.SUMMARY),
    ("scenario", BlockType.CASE_STUDY),
    ("case study", BlockType.CASE_STUDY),
    ("glossary", BlockType.TABLE),
    ("rubric", BlockType.TABLE),
    ("comparison", BlockType.TABLE),
    ("quick reference", BlockType.TABLE),
    ("code", BlockType.CODE),
    ("diagram", BlockType.IMAGE),
    ("visual", BlockType.IMAGE),
    ("illustration", BlockType.IMAGE),
    ("best practice", BlockType.TIP),
    ("common mistake", BlockType.WARNING),
    ("pitfall", BlockType.WARNING),
    ("story", BlockType.STORY),
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ParsedStructure:
    """Everything `parser.py` needs to assemble a CourseTemplate."""

    title: str = ""
    sections: list[TemplateSection] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    heading_levels: int = 0
    table_count: int = 0
    list_count: int = 0
    has_explicit_requirements: bool = False
    findings: dict[str, Any] = field(default_factory=dict)


def _slug(text: str) -> str:
    """A stable section key. Placeholders are stripped first so
    "{{SECTION_1_TITLE}}" becomes "section_1_title" rather than braces."""
    cleaned = PLACEHOLDER_RE.sub(lambda m: m.group(1), text or "")
    cleaned = _SLUG_RE.sub("_", cleaned.lower()).strip("_")
    return cleaned[:60] or "section"


def _heading_level(style_name: str) -> int | None:
    """1..9 for a Word heading style, else None. 'Title' counts as level 0."""
    name = (style_name or "").strip().lower()
    if name == "title":
        return 0
    if not name.startswith("heading"):
        return None
    try:
        return int(name.split()[-1])
    except (ValueError, IndexError):
        return None


# Whole-word matching: a plain substring test makes "History" match "story"
# and "Practicalities" match "practice".
_KEYWORD_RES: tuple[tuple[object, BlockType], ...] = tuple(
    (re.compile(rf"\b{re.escape(keyword)}s?\b", re.IGNORECASE), block_type)
    for keyword, block_type in _BLOCK_KEYWORDS
)


def _block_types_for(label: str) -> list[BlockType]:
    found: list[BlockType] = []
    for pattern, block_type in _KEYWORD_RES:
        if pattern.search(label or "") and block_type not in found:
            found.append(block_type)
    return found


def _obligation(text: str) -> tuple[bool, bool]:
    """(required, explicitly_marked) for one heading."""
    if _REQUIRED_RE.search(text):
        return True, True
    if _OPTIONAL_RE.search(text):
        return False, True
    return False, False


def _clean_label(text: str) -> str:
    label = _REQUIRED_RE.sub("", _OPTIONAL_RE.sub("", text or ""))
    return re.sub(r"\s+", " ", label).strip(" -–:")


def extract_structure(document: DocxDocument) -> ParsedStructure:
    """Walk the document body once, in order, collecting the outline."""
    parsed = ParsedStructure()
    levels: set[int] = set()
    placeholders: list[str] = []
    current: TemplateSection | None = None
    subheadings: list[str] = []
    body_runs: list[str] = []

    def flush() -> None:
        """Attach accumulated Heading 2s and intro prose to the open section."""
        nonlocal current, subheadings, body_runs
        if current is None:
            return
        parts: list[str] = []
        lead = " ".join(body_runs).strip()
        if lead:
            parts.append(lead[:300])
        if subheadings:
            parts.append("Covers: " + "; ".join(subheadings))
        current.guidance = " ".join(parts)[:600]
        # max_blocks defaults to 3 on TemplateSection; a section with many
        # subheadings genuinely needs more room than one with none.
        current.max_blocks = max(3, min(len(subheadings) or 1, 10))
        parsed.sections.append(current)
        current, subheadings, body_runs = None, [], []

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if text:
            placeholders.extend(names_in(text))
        try:
            level = _heading_level(paragraph.style.name)
        except Exception:  # noqa: BLE001 - a broken style must not stop the walk
            level = None

        if level is None:
            if text and current is not None and not subheadings and len(body_runs) < 3:
                body_runs.append(text)
            continue

        levels.add(level)
        if level == 0:
            parsed.title = parsed.title or _clean_label(text)
            continue
        if level == 1:
            flush()
            label = _clean_label(text)
            if not label:
                continue
            required, marked = _obligation(text)
            parsed.has_explicit_requirements = parsed.has_explicit_requirements or marked
            current = TemplateSection(
                key=_slug(label),
                label=label,
                required=required,
                block_types=_block_types_for(label),
                guidance="",
            )
        elif current is not None:
            sub = _clean_label(text)
            if sub:
                subheadings.append(sub)
                required, marked = _obligation(text)
                parsed.has_explicit_requirements = parsed.has_explicit_requirements or marked
                for block_type in _block_types_for(sub):
                    if block_type not in current.block_types:
                        current.block_types.append(block_type)

    flush()

    # Word can repeat a heading text; keys must stay unique for the reviewer's
    # section checks (app.agents.reviewer._apply_structural_checks).
    seen: dict[str, int] = {}
    for section in parsed.sections:
        if section.key in seen:
            seen[section.key] += 1
            section.key = f"{section.key}_{seen[section.key]}"
        else:
            seen[section.key] = 1

    for table in document.tables:
        placeholders.extend(names_in("\n".join(cell.text for row in table.rows for cell in row.cells)))
    for section in document.sections:
        for part in (section.header, section.footer):
            placeholders.extend(names_in("\n".join(p.text for p in part.paragraphs)))

    parsed.placeholders = list(dict.fromkeys(placeholders))
    parsed.heading_levels = len([level for level in levels if level >= 1])
    parsed.table_count = len(document.tables)
    parsed.list_count = sum(
        1
        for paragraph in document.paragraphs
        if (paragraph.style.name or "").lower().startswith("list")
    )
    parsed.findings = {
        "sections": len(parsed.sections),
        "heading_levels": parsed.heading_levels,
        "tables": parsed.table_count,
        "list_paragraphs": parsed.list_count,
        "placeholders": len(parsed.placeholders),
        "explicit_required_markers": parsed.has_explicit_requirements,
    }
    return parsed
