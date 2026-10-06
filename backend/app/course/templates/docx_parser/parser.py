"""Turn an uploaded DOCX into a normalized `CourseTemplate` + a parse report.

This is the bridge between the upload feature and the template architecture
the rest of the app already speaks. The output is an ordinary
`CourseTemplate` - the same object `technical_v1.json` deserializes into - so
the planner, writer, reviewer, document builder and renderer need no special
case for uploaded templates.

Design rules (from the brief):
* Structure and style are extracted from the *original DOCX*, never from the
  Markdown. Markdown is kept alongside for semantic reference only.
* Nothing is invented. A rule that cannot be inferred is left unset, and the
  report says so.
* Features the HTML/Playwright renderer cannot reproduce are reported as
  warnings rather than quietly dropped.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import docx

from app.core.logging import get_logger
from app.schemas.blocks import BlockType
from app.schemas.template import CourseTemplate, TemplateTheme

from .placeholders import SUPPORTED, unknown_names
from .structure import ParsedStructure, extract_structure
from .style import extract_theme

log = get_logger(__name__)

# Bumped whenever extraction changes meaningfully, so stored parses can be
# identified as stale and re-run against the retained source DOCX.
PARSER_VERSION = 1

# OOXML markers for Word features the renderer has no equivalent for. Detected
# so the report can say "this exists and will not survive", which is the
# honest alternative to silently dropping it.
_UNSUPPORTED_MARKERS: tuple[tuple[str, str], ...] = (
    ("w:cols w:num=\"2\"", "Multi-column layout is not reproduced; content renders in one column."),
    ("w:txbxContent", "Text boxes are not reproduced."),
    ("w:pgBorders", "Page borders are not reproduced."),
    ("w:footnoteReference", "Footnotes are not reproduced."),
    ("w:drawing", "Images embedded in the template are not carried into generated courses."),
)


@dataclass
class TemplateParseResult:
    """A parsed template plus everything the UI and logs need to explain it."""

    template: CourseTemplate
    theme: TemplateTheme
    structure: ParsedStructure
    detected: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unsupported_placeholders: list[str] = field(default_factory=list)
    parser_version: int = PARSER_VERSION

    def report(self) -> dict[str, Any]:
        """The JSON blob stored on the row and returned by the upload API."""
        return {
            "parser_version": self.parser_version,
            "detected": self.detected,
            "warnings": self.warnings,
            "unsupported_placeholders": self.unsupported_placeholders,
            "supported_placeholders": [
                name for name in self.structure.placeholders if name in SUPPORTED
            ],
            "sections": [
                {
                    "key": section.key,
                    "label": section.label,
                    "required": section.required,
                    "block_types": [block.value for block in section.block_types],
                }
                for section in self.template.sections
            ],
        }


def _writer_guidance(structure: ParsedStructure, name: str) -> str:
    """Prose the writer agent receives, derived only from what was found.

    The section list itself already reaches the prompt through
    `CourseTemplate.outline_for_prompt()`; this adds the framing that the
    outline came from a specific client document and must be honoured in
    order.
    """
    lines = [
        f'Follow the structure of the uploaded template "{name}" exactly: '
        "use its sections, in the order listed, with their stated headings.",
    ]
    if structure.table_count:
        lines.append(
            f"The template uses {structure.table_count} tables - prefer table blocks "
            "for comparisons, reference data and rubrics rather than prose."
        )
    if structure.list_count:
        lines.append(
            "The template makes heavy use of numbered and bulleted lists; favour "
            "them over long paragraphs where the content is genuinely a list."
        )
    if not structure.has_explicit_requirements:
        lines.append(
            "The template marks no section as explicitly required, so treat every "
            "section as expected but omit any that genuinely does not apply."
        )
    return " ".join(lines)


def _detect_unsupported(raw: bytes) -> list[str]:
    """Scan document.xml for features the renderer cannot reproduce."""
    warnings: list[str] = []
    try:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            body = archive.read("word/document.xml").decode("utf-8", "ignore")
            for marker, message in _UNSUPPORTED_MARKERS:
                if marker in body and message not in warnings:
                    warnings.append(message)
            if any(name.startswith("word/media/") for name in names):
                message = _UNSUPPORTED_MARKERS[-1][1]
                if message not in warnings:
                    warnings.append(message)
    except Exception as exc:  # noqa: BLE001 - reporting only, never fatal
        log.debug("Could not scan DOCX for unsupported features: %s", exc)
    return warnings


def parse_docx_template(
    raw: bytes,
    *,
    template_id: str,
    name: str,
    kind: str,
    description: str = "",
) -> TemplateParseResult:
    """Parse `raw` DOCX bytes into a `CourseTemplate`.

    Raises nothing for styling problems; only a genuinely unreadable file
    raises, and the caller turns that into a validation error.
    """
    document = docx.Document(io.BytesIO(raw))

    structure = extract_structure(document)
    theme, style_findings = extract_theme(document)

    warnings = _detect_unsupported(raw)
    if style_findings.get("asymmetric_side_margins"):
        warnings.append(
            "Left and right margins differ; the renderer uses a single centred "
            "content column, so their average is applied."
        )
    if not structure.sections:
        warnings.append(
            "No Heading 1 paragraphs were found, so no sections could be extracted. "
            "The template contributes styling only."
        )

    unsupported = unknown_names(structure.placeholders)

    # Only constrain block types the document actually implies. An empty
    # `allowed_block_types` means "anything goes" (CourseTemplate.is_allowed),
    # which is the correct default when the DOCX says nothing about it.
    implied: list[BlockType] = []
    for section in structure.sections:
        for block_type in section.block_types:
            if block_type not in implied:
                implied.append(block_type)

    template = CourseTemplate(
        template_id=template_id,
        kind=kind,
        name=name,
        description=description or f"Parsed from an uploaded DOCX ({len(structure.sections)} sections)",
        sections=structure.sections,
        allowed_block_types=[],
        # Nothing is forced: the DOCX carries no obligation markers, and the
        # reviewer treats required_block_types as a hard gate.
        required_block_types=[],
        writer_guidance=_writer_guidance(structure, name),
        research_guidance=(
            "Research the depth each template section implies; the template's own "
            "headings define the scope of the chapter."
        ),
        review_focus=[
            "Every template section is present, with its heading, in template order.",
            "Tables are used where the template uses tables.",
        ],
        image_guidance="",
        theme=theme,
        block_styles={},
    )

    detected = {
        **structure.findings,
        "style": style_findings,
        "implied_block_types": [block.value for block in implied],
        "title": structure.title,
    }

    log.info(
        "TEMPLATE_PARSED template_id=%s sections=%s placeholders=%s warnings=%s parser_version=%s",
        template_id,
        len(structure.sections),
        len(structure.placeholders),
        len(warnings),
        PARSER_VERSION,
    )
    return TemplateParseResult(
        template=template,
        theme=theme,
        structure=structure,
        detected=detected,
        warnings=warnings,
        unsupported_placeholders=unsupported,
    )
