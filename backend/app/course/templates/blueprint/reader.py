"""Read a DOCX or Markdown template into one flat, ordered list of nodes.

Both formats end up in the same shape so a single builder
(app.course.templates.blueprint.builder) turns either into a blueprint. The
reader only reports what is in the file; deciding what is a rule, an example
or an asset is the builder's job.

What is deliberately not read, and reported instead of guessed:
* DOCX floating-object positions, text boxes and columns - python-docx
  exposes the anchor, not where a picture visually sits, so an image's place
  is taken from where it sits in the text flow.
* Images in DOCX headers/footers and linked (not embedded) pictures.
* Remote images in Markdown - only the reference and its alt text are kept;
  nothing is downloaded.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from app.core.logging import get_logger

log = get_logger(__name__)

# Image types the renderer and the image pipeline can actually use.
_IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass
class Node:
    kind: str  # heading | para | item | table | code | image | quote
    text: str = ""
    level: int = 0  # heading level; 0 = document title
    # image only
    data: bytes | None = None
    content_type: str = ""
    caption: str = ""
    ref: str = ""  # Markdown image reference, never fetched


@dataclass
class ReadResult:
    nodes: list[Node] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

_CODE_FONTS = ("consolas", "courier", "menlo", "monaco", "source code", "jetbrains", "lucida console")
_CAPTION_RE = re.compile(r"^(figure|fig\.|image|diagram|illustration|chart)\s*\d*\s*[:.\-–]", re.IGNORECASE)


def _heading_level(style_name: str) -> int | None:
    name = (style_name or "").strip().lower()
    if name == "title":
        return 0
    if name.startswith("heading"):
        try:
            return int(name.split()[-1])
        except (ValueError, IndexError):
            return None
    return None


def _is_code_paragraph(paragraph) -> bool:
    style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
    if "code" in style or "source" in style or "preformatted" in style:
        return True
    runs = [run for run in paragraph.runs if run.text.strip()]
    if not runs:
        return False
    fonts = [(run.font.name or "").lower() for run in runs]
    return all(any(code in font for code in _CODE_FONTS) for font in fonts)


def read_docx(raw: bytes) -> ReadResult:
    """Walk the DOCX body in order: paragraphs and tables interleaved, with
    each embedded picture placed where its anchor sits in the text flow."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(raw))
    result = ReadResult()
    images_seen = 0
    skipped_types: set[str] = set()
    too_big = 0

    def images_in(paragraph: Paragraph) -> list[Node]:
        nonlocal images_seen, too_big
        found: list[Node] = []
        try:
            rel_ids = paragraph._p.xpath(".//a:blip/@r:embed")
        except Exception:  # noqa: BLE001 - an odd drawing must not stop the walk
            return found
        for rel_id in rel_ids:
            part = document.part.related_parts.get(rel_id)
            if part is None:
                continue
            content_type = getattr(part, "content_type", "")
            if content_type not in _IMAGE_TYPES:
                skipped_types.add(content_type or "unknown")
                continue
            blob = part.blob
            if len(blob) > MAX_IMAGE_BYTES:
                too_big += 1
                continue
            images_seen += 1
            if images_seen > MAX_IMAGES:
                continue
            found.append(Node(kind="image", data=blob, content_type=content_type))
        return found

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            table = Table(child, document)
            rows = []
            for row in table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                rows.append(" | ".join(cells))
            text = "\n".join(row for row in rows if row.strip(" |"))
            if text:
                result.nodes.append(Node(kind="table", text=text))
            continue
        if tag != "p":
            continue
        paragraph = Paragraph(child, document)
        text = (paragraph.text or "").strip()
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        level = _heading_level(style)
        pictures = images_in(paragraph)

        if level is not None and text:
            result.nodes.append(Node(kind="heading", text=text, level=level))
        elif style.lower() == "caption" or (text and pictures == [] and _CAPTION_RE.match(text) and _last_is_image(result.nodes)):
            _attach_caption(result.nodes, text)
        elif text:
            if _is_code_paragraph(paragraph):
                kind = "code"
            elif style.lower().startswith("list") or paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None:
                kind = "item"
            elif "quote" in style.lower():
                kind = "quote"
            else:
                kind = "para"
            # Consecutive code lines are one code sample.
            if kind == "code" and result.nodes and result.nodes[-1].kind == "code":
                result.nodes[-1].text += "\n" + paragraph.text
            else:
                result.nodes.append(Node(kind=kind, text=text if kind != "code" else paragraph.text))
        result.nodes.extend(pictures)

    if images_seen > MAX_IMAGES:
        result.warnings.append(f"Only the first {MAX_IMAGES} of {images_seen} images were read.")
    if too_big:
        result.warnings.append(f"{too_big} image(s) larger than 5 MB were skipped.")
    if skipped_types:
        result.warnings.append(
            "Some images use a format that cannot be reused (" + ", ".join(sorted(skipped_types)) + "); "
            "they were skipped. PNG, JPEG, GIF and WebP are supported."
        )
    body = document.element.body.xml
    if "w:txbxContent" in body:
        result.warnings.append("Text inside text boxes was not read.")
    if "wp:anchor" in body:
        result.warnings.append(
            "Floating pictures are read where they are anchored in the text, not where they "
            "visually sit on the page."
        )
    return result


def _last_is_image(nodes: list[Node]) -> bool:
    return bool(nodes) and nodes[-1].kind == "image"


def _attach_caption(nodes: list[Node], text: str) -> None:
    """A caption belongs to the picture right before it; Word captions above
    a picture are rare, and a caption with no picture is kept as text."""
    if nodes and nodes[-1].kind == "image" and not nodes[-1].caption:
        nodes[-1].caption = text
    else:
        nodes.append(Node(kind="para", text=text))


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")


def read_markdown(text: str) -> ReadResult:
    """Headings, paragraphs, list items, tables, code fences, block quotes
    and image references, in document order (markdown-it tokens)."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(text)
    result = ReadResult()
    stack: list[str] = []  # open container types: list / quote / table
    row: list[str] = []
    table_rows: list[str] = []
    remote_images = 0

    for index, token in enumerate(tokens):
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            stack.append("list")
        elif kind in ("bullet_list_close", "ordered_list_close", "blockquote_close"):
            if stack:
                stack.pop()
        elif kind == "blockquote_open":
            stack.append("quote")
        elif kind == "table_open":
            table_rows = []
        elif kind == "tr_open":
            row = []
        elif kind == "tr_close":
            table_rows.append(" | ".join(row))
        elif kind == "table_close":
            if table_rows:
                result.nodes.append(Node(kind="table", text="\n".join(table_rows)))
        elif kind == "heading_open":
            level = int(token.tag[1:])
            inline = tokens[index + 1]
            result.nodes.append(Node(kind="heading", text=inline.content.strip(), level=level))
        elif kind in ("fence", "code_block"):
            result.nodes.append(Node(kind="code", text=token.content.rstrip("\n")))
        elif kind == "inline":
            parent = tokens[index - 1].type if index else ""
            if parent == "heading_open":
                continue
            if parent in ("th_open", "td_open"):
                row.append(token.content.strip())
                continue
            content = token.content.strip()
            images = list(_MD_IMAGE_RE.finditer(content))
            remainder = _MD_IMAGE_RE.sub("", content).strip()
            if remainder:
                node_kind = "item" if "list" in stack else "quote" if "quote" in stack else "para"
                if remainder and images == [] and _CAPTION_RE.match(remainder) and _last_is_image(result.nodes):
                    _attach_caption(result.nodes, remainder)
                else:
                    result.nodes.append(Node(kind=node_kind, text=remainder))
            for match in images:
                alt, ref, title = match.group(1), match.group(2), match.group(3) or ""
                if ref.startswith(("http://", "https://", "//")):
                    remote_images += 1
                result.nodes.append(Node(kind="image", caption=(title or alt).strip(), ref=ref))

    if remote_images:
        result.warnings.append(
            f"{remote_images} image reference(s) point to web addresses; they were not downloaded, "
            "so only their description is used."
        )
    if any(node.kind == "image" for node in result.nodes):
        result.warnings.append(
            "Markdown image files are not uploaded with the template, so they serve as "
            "examples of where visuals go rather than as reusable pictures."
        )
    return result
