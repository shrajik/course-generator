"""QA gate for math/scientific notation rendering - KaTeX parse errors.

Math in the exported PDF is real client-side KaTeX typesetting (see
app.render.html_renderer's own module docstring and course.html.j2's inline
script) - there is no server-side AST to check deterministically the way
formula_qa.py checks a diagram's governing equation, because whether a
given LaTeX string parses is a property of KaTeX's own JS parser, not
something Python can decide in advance. The only honest way to verify it is
to ask the browser that actually rendered it: KaTeX marks anything it
couldn't parse with `class="katex-error"` (see PdfService.export_pdf, which
evaluates this against the live Playwright page right after typesetting
finishes, in the SAME browser session the PDF itself is captured from - no
extra browser launch). This module just turns that raw list of error texts
into a structured report, mirroring every other QA module's finding shape
in this codebase (see visual_review.py's VisualReviewReport).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MathRenderingFinding:
    # The raw text KaTeX couldn't parse (its own error message, read back
    # from the `.katex-error` element's textContent) - not a guess at what
    # was intended, since KaTeX itself is the only authority on why its own
    # parser rejected it.
    detail: str


@dataclass
class MathRenderingReport:
    course_id: str
    findings: list[MathRenderingFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def warnings(self) -> list[str]:
        if not self.findings:
            return []
        return [
            f"Math/scientific notation could not be rendered correctly ({len(self.findings)} "
            "issue(s)) - see the exported PDF for exactly where; a $...$/$$...$$ expression "
            "likely has invalid LaTeX syntax."
        ]


def build_report(course_id: str, katex_error_texts: list[str]) -> MathRenderingReport:
    """Pure: turns the raw list of `.katex-error` element texts KaTeX left
    in the DOM into a structured report. Deduplicated and stripped - the
    same broken expression can appear more than once on a page (e.g. an
    inline and a display form of the same mistyped command)."""
    seen: set[str] = set()
    findings: list[MathRenderingFinding] = []
    for text in katex_error_texts:
        cleaned = (text or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        findings.append(MathRenderingFinding(detail=cleaned))
    return MathRenderingReport(course_id=course_id, findings=findings)
