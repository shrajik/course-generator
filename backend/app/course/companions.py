"""Companion-deliverable content (Content Studio Phase 7).

Mirrors app/course/design_models.py's plan-document section: a pure function
of the saved course that computes - never generates - job aid / quick
reference / manager guide content from already-written chapter blocks, so it
can never invent a fact the course itself doesn't contain. {} when none of
the three deliverables were requested in the brief.

A course can only ever have one CourseDocument (the id scheme, the filesystem
path and a DB unique constraint all enforce a strict 1:1 with its course), so
this deliberately stops at a computed preview rather than a second downloadable
document - see this phase's plan for why.
"""

from __future__ import annotations

from typing import Any

from app.course.compliance import split_chapters
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import CourseDocument


def _job_aid(document: CourseDocument) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for chapter in split_chapters(document):
        checklist: list[str] = []
        watch_outs: list[str] = []
        for block in chapter.blocks:
            if block.type is BlockType.LEARNING_OBJECTIVES:
                checklist += [str(i).strip() for i in block.content.get("items", []) or [] if str(i).strip()]
            elif block.type in {BlockType.EXERCISE, BlockType.CHALLENGE}:
                checklist += [str(s).strip() for s in block.content.get("steps", []) or [] if str(s).strip()]
            elif block.type is BlockType.WARNING:
                text = str(block.content.get("text", "")).strip()
                if text:
                    watch_outs.append(text)
        if checklist or watch_outs:
            sections.append({"chapter": chapter.title, "checklist": checklist, "watch_outs": watch_outs})
    return {"title": "Job aid", "sections": sections}


def _quick_ref(document: CourseDocument) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for chapter in split_chapters(document):
        key_points: list[str] = []
        objectives: list[str] = []
        for block in chapter.blocks:
            if block.type is BlockType.SUMMARY:
                key_points += [
                    str(i).strip() for i in block.content.get("key_takeaways", []) or [] if str(i).strip()
                ]
            elif block.type is BlockType.LEARNING_OBJECTIVES:
                objectives += [str(i).strip() for i in block.content.get("items", []) or [] if str(i).strip()]
        if not key_points:
            key_points = objectives
        if key_points:
            sections.append({"chapter": chapter.title, "key_points": key_points})
    return {"title": "Quick-reference card", "sections": sections}


def _manager_guide(document: CourseDocument) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for chapter in split_chapters(document):
        coaching_prompts: list[str] = []
        scenario_notes: list[str] = []
        fallback_outcomes: list[str] = []
        for block in chapter.blocks:
            if block.type is BlockType.LEARNING_OBJECTIVES:
                coaching_prompts += [
                    f"Ask them to show you: {str(i).strip()}"
                    for i in block.content.get("items", []) or []
                    if str(i).strip()
                ]
            elif block.type is BlockType.CASE_STUDY:
                for key in ("context", "challenge", "outcome"):
                    text = str(block.content.get(key, "")).strip()
                    if text:
                        scenario_notes.append(text)
            elif block.type in {BlockType.EXERCISE, BlockType.CHALLENGE}:
                outcome = str(block.content.get("expected_outcome", "")).strip()
                if outcome:
                    fallback_outcomes.append(outcome)
        if not scenario_notes:
            scenario_notes = fallback_outcomes
        if coaching_prompts or scenario_notes:
            sections.append(
                {"chapter": chapter.title, "coaching_prompts": coaching_prompts, "scenario_notes": scenario_notes}
            )
    return {
        "title": "Manager coaching guide",
        "note": "For the 70-20-10 content better delivered on the job - use in a 1:1 or team huddle.",
        "sections": sections,
    }


def companion_deliverables(document: CourseDocument, input_: CourseInput) -> dict[str, Any]:
    """Companion-deliverable content for whatever was requested in the brief.
    Built from the saved course with no AI call; {} when none were requested."""
    out: dict[str, Any] = {}
    companions = input_.companions or []
    if "job_aid" in companions:
        out["job_aid"] = _job_aid(document)
    if "quick_ref" in companions:
        out["quick_ref"] = _quick_ref(document)
    if "manager_guide" in companions:
        out["manager_guide"] = _manager_guide(document)
    return out
