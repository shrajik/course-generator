"""Visual style extraction from a DOCX, into the existing `TemplateTheme`.

Why this exists: the upload path's DOCX -> HTML -> Markdown conversion
(`app.services.course_template_document_service`) is a *semantic* conversion -
mammoth deliberately discards direct formatting, so the Markdown it produces
carries no fonts, colours, margins or page size at all. Visual information can
therefore only come from the original DOCX, which is why the uploaded file is
kept on disk and re-opened here.

Everything is best-effort and defensive: a template whose styles cannot be
read still uploads and still works, it just falls back to the defaults in
`TemplateTheme`. Nothing here raises.

Scope is deliberately limited to what the HTML/Playwright renderer can
actually honour (see app/render/templates/course.html.j2). Features the
renderer cannot reproduce are reported as warnings by `parser.py` rather than
silently extracted and ignored.
"""

from __future__ import annotations

from typing import Any

from docx.document import Document as DocxDocument
from docx.shared import RGBColor

from app.core.logging import get_logger
from app.schemas.template import PageGeometry, TemplateTheme

log = get_logger(__name__)

# 914400 EMU = 1 inch; CSS renders at 96 dpi, so 9525 EMU = 1 px. The layout
# engine and the @page rule both work in these CSS pixels.
_EMU_PER_PX = 9525.0

_THEME_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
_DRAWING_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _px(value: Any) -> float | None:
    try:
        return round(float(value) / _EMU_PER_PX, 1)
    except (TypeError, ValueError):
        return None


def _hex(color: Any) -> str:
    """A `w:color`/RGBColor as `#rrggbb`, or "" when unset or automatic.

    Word's "automatic" means "let the consumer decide", not black, so it must
    not be turned into an explicit colour.
    """
    try:
        rgb = getattr(color, "rgb", None)
        if rgb is None or not isinstance(rgb, RGBColor):
            return ""
        text = str(rgb).lower()
        if not text or text == "auto":
            return ""
        return f"#{text}"
    except Exception:  # noqa: BLE001 - styling is advisory, never fatal
        return ""


def _theme_part(document: DocxDocument):
    try:
        return document.part.package.part_related_by(_THEME_REL)
    except Exception:  # noqa: BLE001 - no theme part is normal for some files
        return None


def _theme_fonts(document: DocxDocument) -> tuple[str, str]:
    """(major, minor) typefaces from theme1.xml.

    Word styles usually say "+Headings"/"+Body" rather than naming a font, so
    the real typeface lives in the theme part. Returns ("", "") when absent.
    """
    part = _theme_part(document)
    if part is None:
        return "", ""
    try:
        from lxml import etree

        root = etree.fromstring(part.blob)
        major = root.find(".//a:fontScheme/a:majorFont/a:latin", _DRAWING_NS)
        minor = root.find(".//a:fontScheme/a:minorFont/a:latin", _DRAWING_NS)
        return (
            (major.get("typeface") or "") if major is not None else "",
            (minor.get("typeface") or "") if minor is not None else "",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read theme fonts: %s", exc)
        return "", ""


def _theme_accent(document: DocxDocument) -> str:
    """accent1 from theme1.xml - Word's primary brand colour slot."""
    part = _theme_part(document)
    if part is None:
        return ""
    try:
        from lxml import etree

        root = etree.fromstring(part.blob)
        node = root.find(".//a:clrScheme/a:accent1/a:srgbClr", _DRAWING_NS)
        value = node.get("val") if node is not None else None
        return f"#{value.lower()}" if value else ""
    except Exception:  # noqa: BLE001
        return ""


def _style(document: DocxDocument, name: str):
    try:
        return document.styles[name]
    except KeyError:
        return None


def _safe_face(name: str) -> str:
    """A font name reduced to characters that are safe to emit unquoted into
    a stylesheet. Also the sanitiser for this value: the name comes from an
    uploaded file and is interpolated into CSS."""
    cleaned = "".join(
        char for char in (name or "").strip() if char.isalnum() or char in " -_"
    ).strip()
    return " ".join(cleaned.split())


def _font_stack(named: str, fallback: str, generic: str) -> str:
    """A CSS font stack. The extracted face goes first and a generic family
    follows, so a font Chromium cannot resolve degrades sensibly instead of
    falling back to Times New Roman."""
    face = _safe_face(named or fallback)
    if not face:
        return ""
    # Deliberately unquoted: CSS allows an unquoted multi-word family name,
    # and the stylesheet is emitted through Jinja's HTML autoescaping, which
    # would turn any quote character into &#34; inside the <style> block.
    # _safe_face guarantees the name is a valid unquoted identifier sequence.
    return f"{face}, {generic}"


def _table_look(document: DocxDocument) -> tuple[str, str]:
    """(header background, border colour) sampled from the first table.

    Sampling the document's own first table is more reliable than reading the
    table style definition, because Word stores much of a table's appearance
    as direct formatting on the cells.
    """
    header_bg = ""
    border = ""
    try:
        if not document.tables:
            return "", ""
        table = document.tables[0]
        first_row = table.rows[0] if table.rows else None
        if first_row is not None:
            for cell in first_row.cells:
                shd = cell._tc.find(f".//{_W_NS}shd")
                fill = shd.get(f"{_W_NS}fill") if shd is not None else None
                if fill and fill.lower() not in ("auto", "ffffff"):
                    header_bg = f"#{fill.lower()}"
                    break
        borders = table._tbl.find(f".//{_W_NS}tblBorders")
        if borders is not None:
            for edge in borders:
                color = edge.get(f"{_W_NS}color")
                if color and color.lower() != "auto":
                    border = f"#{color.lower()}"
                    break
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not sample table styling: %s", exc)
    return header_bg, border


def _first_text(paragraphs: Any) -> str:
    try:
        for paragraph in paragraphs:
            text = (paragraph.text or "").strip()
            if text:
                return text
    except Exception:  # noqa: BLE001
        pass
    return ""


def extract_theme(document: DocxDocument) -> tuple[TemplateTheme, dict[str, Any]]:
    """Build a `TemplateTheme` from a DOCX.

    Returns the theme plus a findings dict describing what was actually
    detected, which the upload validation report surfaces so the user can see
    what was picked up rather than having to guess.
    """
    findings: dict[str, Any] = {}
    defaults = TemplateTheme()
    theme = TemplateTheme()

    major, minor = _theme_fonts(document)

    normal = _style(document, "Normal")
    body_face = ""
    if normal is not None:
        body_face = (normal.font.name or "").strip()
        size = getattr(normal.font.size, "pt", None)
        if size:
            theme.base_font_size = float(size)
            findings["body_font_size_pt"] = float(size)
        ink = _hex(normal.font.color)
        if ink:
            theme.text_color = ink
            findings["text_color"] = ink
    stack = _font_stack(body_face, minor, "Helvetica, Arial, sans-serif")
    if stack:
        theme.font_family = stack
        findings["body_font"] = body_face or minor

    heading = _style(document, "Heading 1")
    if heading is not None:
        heading_face = (heading.font.name or "").strip()
        head_stack = _font_stack(heading_face, major, "Helvetica, Arial, sans-serif")
        if head_stack and head_stack != theme.font_family:
            theme.heading_font_family = head_stack
            findings["heading_font"] = heading_face or major
        head_ink = _hex(heading.font.color)
        if head_ink:
            theme.heading_color = head_ink
            theme.accent_color = head_ink
            findings["heading_color"] = head_ink

    accent = _theme_accent(document)
    if accent:
        findings["theme_accent"] = accent
        if not theme.heading_color:
            theme.accent_color = accent

    _extract_spacing(normal, theme, findings)

    header_bg, border = _table_look(document)
    if header_bg:
        theme.table_header_background = header_bg
        findings["table_header_background"] = header_bg
    if border:
        theme.table_border_color = border
        theme.border_color = border
        findings["table_border_color"] = border

    theme.page = _extract_geometry(document, findings)
    _extract_furniture(document, theme, findings)

    findings["changed_fields"] = sorted(
        name
        for name in TemplateTheme.model_fields
        if name != "page" and getattr(theme, name) != getattr(defaults, name)
    )
    return theme, findings


def _extract_spacing(normal: Any, theme: TemplateTheme, findings: dict[str, Any]) -> None:
    """Line spacing and paragraph spacing, as recorded on the Normal style."""
    try:
        fmt = normal.paragraph_format if normal is not None else None
        if fmt is None:
            return
        if fmt.line_spacing:
            theme.base_line_height = round(float(fmt.line_spacing), 2)
            findings["line_spacing"] = theme.base_line_height
        after = getattr(fmt.space_after, "pt", None)
        if after and theme.base_font_size:
            theme.paragraph_spacing_em = round(float(after) / theme.base_font_size, 2)
            findings["space_after_pt"] = float(after)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read Normal paragraph format: %s", exc)


def _extract_geometry(document: DocxDocument, findings: dict[str, Any]) -> PageGeometry | None:
    """The page box, or None when it matches the layout engine's default -
    so an A4-ish template does not gratuitously mark itself as custom."""
    try:
        section = document.sections[0]
    except (IndexError, AttributeError):
        return None

    geometry = PageGeometry()
    default = PageGeometry()
    width = _px(section.page_width)
    height = _px(section.page_height)
    if width and height:
        geometry.width, geometry.height = width, height
        findings["page_size_px"] = [width, height]
    left, right = _px(section.left_margin), _px(section.right_margin)
    if left and right:
        # One horizontal margin: the layout engine centres a single content
        # column and has no concept of asymmetric left/right margins.
        geometry.margin_x = round((left + right) / 2, 1)
        if abs(left - right) > 2:
            findings["asymmetric_side_margins"] = [left, right]
    top, bottom = _px(section.top_margin), _px(section.bottom_margin)
    if top:
        geometry.margin_top = top
    if bottom:
        geometry.margin_bottom = bottom
    findings["margins_px"] = [geometry.margin_x, geometry.margin_top, geometry.margin_bottom]
    try:
        findings["orientation"] = str(section.orientation).split(".")[-1].split(" ")[0]
    except Exception:  # noqa: BLE001
        pass

    return geometry if geometry != default else None


def _extract_furniture(
    document: DocxDocument, theme: TemplateTheme, findings: dict[str, Any]
) -> None:
    """Running header/footer text and page numbering."""
    try:
        section = document.sections[0]
    except (IndexError, AttributeError):
        return

    header = _first_text(section.header.paragraphs)
    if header:
        theme.header_text = header
        findings["header_text"] = header

    footer_paragraphs = list(getattr(section.footer, "paragraphs", []))
    footer = _first_text(footer_paragraphs)
    # Word stores a page number as a PAGE field, whose literal text is often
    # just "Page " - detect the field itself rather than trusting the text.
    has_page_field = False
    try:
        for paragraph in footer_paragraphs:
            xml = paragraph._p.xml
            if "PAGE" in xml and f"{_W_NS}instrText" in xml:
                has_page_field = True
                break
    except Exception:  # noqa: BLE001
        pass
    # Word renders a page-number field as the literal text "Page" (or ""),
    # which is field residue rather than footer copy - keeping it would
    # replace the document title in the footer with the word "Page".
    if footer and footer.strip().lower() not in ("page", "page of"):
        theme.footer_text = footer
        findings["footer_text"] = footer
    if has_page_field:
        theme.show_page_numbers = True
        findings["page_numbers"] = True
