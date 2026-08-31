"""Convert flat writer draft blocks into validated Course Document blocks."""

from __future__ import annotations

from typing import Any

from app.core.ids import block_id as new_block_id
from app.core.logging import get_logger
from app.schemas.blocks import BlockType, validate_content
from app.schemas.document import Block, BlockLayout, BlockMeta, BlockStyle
from app.schemas.draft import DraftBlock
from app.schemas.template import CourseTemplate

log = get_logger(__name__)


def _first(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def draft_to_content(draft: DraftBlock) -> dict[str, Any]:
    """Map the superset draft fields onto the content model for its type."""
    t = draft.type

    if t is BlockType.HEADING:
        return {"text": _first(draft.text, draft.title), "level": draft.level or 2}

    if t is BlockType.PARAGRAPH:
        return {"text": _first(draft.text, draft.intro)}

    if t is BlockType.IMAGE:
        return {
            "purpose": _first(draft.image_purpose, draft.title, draft.text),
            "prompt": _first(draft.image_prompt, draft.text),
            "caption": _first(draft.caption, draft.title),
            "alt": _first(draft.alt_text, draft.caption, draft.title),
        }

    if t is BlockType.QUOTE:
        return {"text": draft.text, "attribution": draft.attribution}

    if t is BlockType.CALLOUT:
        return {
            "title": draft.title,
            "text": _first(draft.text, draft.intro),
            "variant": draft.variant or "info",
        }

    if t is BlockType.CODE:
        return {
            "language": draft.language or "text",
            "code": _first(draft.code, draft.text),
            "caption": _first(draft.caption, draft.title),
        }

    if t is BlockType.TABLE:
        return {
            "caption": _first(draft.caption, draft.title),
            "columns": draft.columns,
            "rows": [{"cells": row.cells} for row in draft.rows],
        }

    if t is BlockType.QUIZ:
        return {
            "title": draft.title or "Knowledge Check",
            "questions": [q.model_dump() for q in draft.questions],
        }

    if t in (BlockType.EXERCISE, BlockType.CHALLENGE):
        default_title = "Challenge" if t is BlockType.CHALLENGE else "Exercise"
        return {
            "title": draft.title or default_title,
            "instructions": _first(draft.instructions, draft.text, draft.intro),
            "steps": draft.steps or draft.items,
            "hints": draft.hints,
            "expected_outcome": draft.expected_outcome,
            "difficulty": draft.difficulty or ("hard" if t is BlockType.CHALLENGE else "medium"),
        }

    if t is BlockType.CASE_STUDY:
        return {
            "title": draft.title or "Case Study",
            "context": _first(draft.context, draft.text),
            "challenge": draft.challenge,
            "actions": draft.actions or draft.steps or draft.items,
            "outcome": draft.outcome,
            "lessons": draft.lessons or draft.key_takeaways,
        }

    if t is BlockType.STORY:
        return {
            "title": draft.title,
            "text": _first(draft.text, draft.context),
            "takeaway": _first(draft.takeaway, draft.outcome),
        }

    if t is BlockType.TIP:
        return {"title": draft.title or "Pro Tip", "text": _first(draft.text, draft.intro)}

    if t is BlockType.WARNING:
        return {
            "title": draft.title or "Common Mistake",
            "text": _first(draft.text, draft.intro),
        }

    if t is BlockType.SUMMARY:
        return {
            "title": draft.title or "Summary",
            "key_takeaways": draft.key_takeaways or draft.items,
            "next_steps": draft.next_steps,
        }

    if t is BlockType.DIVIDER:
        return {}

    if t in (BlockType.LEARNING_OBJECTIVES, BlockType.REFLECTION):
        default_title = (
            "Learning Objectives" if t is BlockType.LEARNING_OBJECTIVES else "Reflection"
        )
        return {
            "title": draft.title or default_title,
            "items": draft.items or draft.key_takeaways or draft.steps,
            "intro": _first(draft.intro, draft.text),
        }

    # Unknown-but-registered type: pass the obvious fields through.
    return {"text": draft.text, "title": draft.title, "items": draft.items}


def is_empty_content(block_type: BlockType, content: dict[str, Any]) -> bool:
    """Drop blocks the model left blank instead of rendering empty boxes."""
    if block_type is BlockType.DIVIDER:
        return False
    meaningful = ("text", "code", "instructions", "prompt", "context", "purpose")
    if any(str(content.get(key, "")).strip() for key in meaningful):
        return False
    list_keys = (
        "items",
        "key_takeaways",
        "questions",
        "rows",
        "steps",
        "lessons",
        "actions",
        "next_steps",
    )
    return not any(content.get(key) for key in list_keys)


def build_block(
    draft: DraftBlock,
    *,
    template: CourseTemplate,
    chapter_id: str,
    chapter_number: int,
    origin: str = "generated",
) -> Block | None:
    content = draft_to_content(draft)
    try:
        content = validate_content(draft.type, content)
    except Exception as exc:
        log.warning("Dropping invalid %s block in %s: %s", draft.type, chapter_id, exc)
        return None
    if is_empty_content(draft.type, content):
        return None
    return Block(
        id=new_block_id(),
        type=draft.type,
        content=content,
        style=BlockStyle.model_validate(template.style_for(draft.type)),
        layout=BlockLayout(),
        meta=BlockMeta(
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            section_key=draft.section_key or None,
            origin=origin,
        ),
    )


def normalize_draft_blocks(
    drafts: list[DraftBlock],
    *,
    template: CourseTemplate,
    chapter_id: str,
    chapter_number: int,
) -> list[Block]:
    blocks: list[Block] = []
    for draft in drafts:
        if not template.is_allowed(draft.type):
            log.info(
                "Block type %s not allowed by template %s - keeping as paragraph fallback",
                draft.type,
                template.template_id,
            )
            draft = draft.model_copy(update={"type": BlockType.PARAGRAPH})
        block = build_block(
            draft,
            template=template,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
        )
        if block is not None:
            blocks.append(block)
    return blocks
