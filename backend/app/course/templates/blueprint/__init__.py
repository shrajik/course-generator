"""Uploaded templates as content-and-visual generation blueprints.

`process_template_file` is the single entry point used at upload time:

    bytes -> safety checks -> nodes (reader) -> CourseTemplate + blueprint (builder)

The result is stored with the upload and reused for every course; nothing
here runs during generation. The prompt section (brief) and the reviewer
checks (checks) read the stored blueprint.
"""

from __future__ import annotations

import io

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger

from .builder import BuildResult, build_template
from .reader import read_docx, read_markdown
from .safety import check_docx_archive

log = get_logger(__name__)

# Bumped whenever extraction changes meaningfully. 1 = the structure/style
# parser (app.course.templates.docx_parser); 2 = blueprints.
PARSER_VERSION = 2

__all__ = ["PARSER_VERSION", "BuildResult", "process_template_file"]


def _decode_markdown(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("Markdown file must be UTF-8 encoded.") from exc


def process_template_file(
    data: bytes,
    *,
    source_format: str,
    template_id: str,
    name: str,
    kind: str,
    description: str = "",
    apply_design: bool = False,
) -> BuildResult:
    """Read an uploaded DOCX or Markdown template into a blueprint.

    Raises ValidationFailedError with a message the uploader can act on; never
    returns a half-usable result that would silently fall back to another
    template.
    """
    if source_format == "md":
        text = _decode_markdown(data)
        if not text.strip():
            raise ValidationFailedError("The Markdown file is empty.")
        read = read_markdown(text)
        return build_template(
            read.nodes,
            template_id=template_id,
            name=name,
            kind=kind,
            description=description,
            source_format="md",
            reader_warnings=read.warnings,
        )

    if source_format != "docx":
        raise ValidationFailedError("Only .docx and .md files are supported.")

    check_docx_archive(data)
    try:
        import docx

        from app.course.templates.docx_parser.parser import _detect_unsupported
        from app.course.templates.docx_parser.style import extract_theme

        read = read_docx(data)
        theme, _findings = extract_theme(docx.Document(io.BytesIO(data)))
        layout_warnings = [
            warning for warning in _detect_unsupported(data) if "Images embedded" not in warning
        ]
    except ValidationFailedError:
        raise
    except Exception as exc:  # noqa: BLE001 - python-docx raises many types for a bad file
        log.warning("Could not read uploaded DOCX template %s: %s", template_id, exc)
        raise ValidationFailedError(f"This .docx could not be read ({type(exc).__name__}). It may be corrupted.") from exc

    return build_template(
        read.nodes,
        template_id=template_id,
        name=name,
        kind=kind,
        description=description,
        source_format="docx",
        extracted_theme=theme,
        apply_design=apply_design,
        reader_warnings=read.warnings + [f"Design: {warning}" for warning in layout_warnings],
    )
