"""DOCX template parsing: original file -> normalized CourseTemplate.

Split so each concern is independently testable:
* structure.py   - headings/sections/placeholders (semantic)
* style.py       - fonts/colours/geometry/furniture (visual)
* placeholders.py- the controlled {{PLACEHOLDER}} allow-list
* parser.py      - assembles both into a CourseTemplate + parse report
"""

from .parser import PARSER_VERSION, TemplateParseResult, parse_docx_template
from .placeholders import SUPPORTED, context_from, names_in, resolve, unknown_names

__all__ = [
    "PARSER_VERSION",
    "SUPPORTED",
    "TemplateParseResult",
    "context_from",
    "names_in",
    "parse_docx_template",
    "resolve",
    "unknown_names",
]
