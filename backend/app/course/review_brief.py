"""The Review Assistant: what a human reviewer needs to know before deciding.

HLD "Review Assistant": it prepares generated content and QA results for human
reviewers - summarises, highlights potential issues, organises what needs a
decision - and supports the reviewer rather than replacing the approval. Like
the rest of the quality layer it is built from the saved course and the saved
fact-check by code, with no model call, so it is the same every time and cannot
invent an issue.

`approval_blockers` is the one rule that is enforced rather than advised: a
course that was written from source documents may not be given final approval
while the fact check has an unresolved high-severity finding (a made-up source,
or a statement the sources contradict), or while no fact check exists at all.
"""

from __future__ import annotations

from typing import Any

from app.course.compliance import ComplianceReport, split_chapters
from app.schemas.audit import AuditReport
from app.schemas.blocks import BlockType
from app.schemas.document import CourseDocument


def approval_blockers(*, has_sources: bool, audit: AuditReport | None) -> list[str]:
    """Reasons final approval must wait. [] means the course may be approved."""
    if not has_sources:
        return []
    if audit is None:
        return ["The fact check has not been run for this course."]
    open_high = sum(1 for f in audit.findings if f.severity == "high" and f.status == "open")
    if open_high:
        noun = "finding" if open_high == 1 else "findings"
        return [f"{open_high} high-severity fact-check {noun} must be accepted or dismissed first."]
    return []


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def build_review_brief(
    *,
    document: CourseDocument,
    audit: AuditReport | None,
    compliance: ComplianceReport,
    has_sources: bool,
    review_status: str,
) -> dict[str, Any]:
    blockers = approval_blockers(has_sources=has_sources, audit=audit)
    advisories: list[str] = []

    open_findings = [f for f in (audit.findings if audit else []) if f.status == "open"]
    medium = sum(1 for f in open_findings if f.severity == "medium")
    low = sum(1 for f in open_findings if f.severity == "low")
    if medium:
        advisories.append(f"{_plural(medium, 'medium-severity finding')} still open: statements the sources do not clearly support.")
    if low:
        advisories.append(f"{_plural(low, 'assumption')} the sources do not cover. Confirm they are accurate.")

    failed = [c.name for c in compliance.qa_checks if c.status == "fail"]
    if failed:
        advisories.append("Quality checks failing: " + ", ".join(failed) + ".")
    needs_review = [c.name for c in compliance.qa_checks if c.status == "review"]
    if needs_review:
        advisories.append("A person must confirm: " + ", ".join(needs_review) + ".")

    generated_images = sum(
        1
        for _page, block in document.iter_blocks()
        if block.type is BlockType.IMAGE and block.content.get("generated") and block.content.get("kind") != "toc"
    )
    if generated_images:
        advisories.append(f"{_plural(generated_images, 'AI-generated image')}: check each against the text it illustrates.")

    if not has_sources:
        advisories.append("This course was not written from source documents, so there is nothing to fact-check it against.")

    # Per chapter: where the open findings are.
    titles = {chapter.title: chapter for chapter in split_chapters(document)}
    chapters: list[dict[str, Any]] = []
    for title in titles:
        mine = [f for f in open_findings if f.chapter == title]
        chapters.append({
            "chapter": title,
            "high": sum(1 for f in mine if f.severity == "high"),
            "medium": sum(1 for f in mine if f.severity == "medium"),
            "low": sum(1 for f in mine if f.severity == "low"),
        })

    if blockers:
        headline = blockers[0]
    elif review_status == "approved":
        headline = "This course has been approved."
    elif medium or failed:
        headline = "No blocking issues, but some statements and checks still deserve a look before approving."
    else:
        headline = "Nothing is blocking final approval."

    return {
        "headline": headline,
        "ready_to_approve": not blockers,
        "blockers": blockers,
        "advisories": advisories,
        "chapters": chapters,
        "stats": {
            "sentences_checked": audit.sentences_checked if audit else 0,
            "open_findings": len(open_findings),
            "qa_pass": compliance.summary["pass"],
            "qa_fail": compliance.summary["fail"],
            "qa_review": compliance.summary["review"],
            "fact_checked": audit is not None,
        },
    }
