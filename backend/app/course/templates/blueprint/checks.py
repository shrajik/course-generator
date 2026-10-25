"""Deterministic checks that a written chapter honours its template blueprint.

Run by the reviewer (app.agents.reviewer._apply_structural_checks) on every
chapter of a course whose template has a blueprint; a no-op otherwise, so
courses on other templates are reviewed exactly as before.

* A missing REQUIRED visual goes into `missing_required_blocks`, which makes
  the writer redo the chapter (the same path a missing required section uses).
* A visual in the wrong place, a missing caption, wording copied from the
  template's examples and an unresolved `{{PLACEHOLDER}}` become issues on the
  offending block, which the surgical revision path fixes.

Rules the system cannot honour (`support == "none"`, such as screenshots) are
never checked - they are reported on the upload preview instead, and nothing
pretends they were produced.
"""

from __future__ import annotations

import re

from app.course.compliance import block_text
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, BlockType
from app.schemas.document import Block
from app.schemas.review import ReviewIssue
from app.schemas.template import CourseTemplate
from app.schemas.template_blueprint import VisualRule

COPY_RUN_WORDS = 10
_PLACEHOLDER_RE = re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}")
_WORD_RE = re.compile(r"[a-z0-9']+")


def _matches(rule: VisualRule, block: Block) -> bool:
    if block.type is not BlockType.IMAGE:
        return False
    content = block.content
    if content.get("blueprint_rule") == rule.id:
        return True
    if rule.asset_id:
        return content.get("template_asset") == rule.asset_id
    kind = content.get("kind") or "illustration"
    if (content.get("illustration_style") or "") == "section_intro":
        return False  # a decorative opener never stands in for a teaching visual
    if rule.kind == "any":
        return True
    if rule.kind in ("diagram", "concept_experience"):
        return kind in ("diagram", "concept_experience")
    return kind == "illustration"


def _section_of(block: Block) -> str:
    return block.meta.section_key or ""


def _is_text(block: Block) -> bool:
    return block.type in TEXTUAL_BLOCK_TYPES


def _placement_issue(rule: VisualRule, blocks: list[Block], index: int) -> str:
    section = _section_of(blocks[index])
    same = [i for i, block in enumerate(blocks) if _section_of(block) == section] if rule.scope != "chapter" else list(range(len(blocks)))
    before = [i for i in same if i < index and _is_text(blocks[i])]
    after = [i for i in same if i > index and blocks[i].type is BlockType.PARAGRAPH]
    if rule.placement == "after_text" and not before:
        return "must come after the text that explains it, but no explanation precedes it"
    if rule.placement == "before_text" and before:
        return "must open its section, before the text, but text comes first"
    if rule.placement == "end_of_section" and after:
        return "must close its section, but more paragraphs follow it"
    return ""


def place_template_visuals(blocks: list[Block], template: CourseTemplate) -> int:
    """Move each image the writer tagged for a blueprint rule to where that
    rule puts it, within its own section. Deterministic, so placement never
    depends on a revision pass the AI may or may not get right. Mutates
    `blocks`; returns how many images moved."""
    blueprint = template.blueprint
    if blueprint is None:
        return 0
    moved = 0
    for block in [b for b in blocks if b.type is BlockType.IMAGE]:
        rule = blueprint.visual(str(block.content.get("blueprint_rule") or ""))
        if rule is None or rule.placement == "anywhere":
            continue
        index = blocks.index(block)
        if not _placement_issue(rule, blocks, index):
            continue
        section = _section_of(block)
        blocks.pop(index)
        same = [i for i, b in enumerate(blocks) if _section_of(b) == section]
        texts = [i for i in same if _is_text(blocks[i])]
        if not same:
            blocks.insert(index, block)
            continue
        if rule.placement == "after_text" and texts:
            target = texts[0] + 1
            # Keep a multi-paragraph explanation together.
            while target < len(blocks) and _section_of(blocks[target]) == section and blocks[target].type is BlockType.PARAGRAPH:
                target += 1
        elif rule.placement == "before_text" and texts:
            target = texts[0]
        elif rule.placement == "end_of_section":
            target = same[-1] + 1
        else:
            blocks.insert(index, block)
            continue
        blocks.insert(target, block)
        moved += 1
    return moved


def _sections_present(blocks: list[Block], template: CourseTemplate) -> list[str]:
    known = {section.key for section in template.sections}
    present: list[str] = []
    for block in blocks:
        key = _section_of(block)
        if key and key in known and key not in present:
            present.append(key)
    return present


def blueprint_review(blocks: list[Block], template: CourseTemplate) -> tuple[list[str], list[ReviewIssue]]:
    """(missing required visuals, block issues) for one chapter."""
    blueprint = template.blueprint
    if blueprint is None:
        return [], []

    missing: list[str] = []
    issues: list[ReviewIssue] = []
    claimed: set[int] = set()

    for rule in blueprint.visuals:
        if rule.support == "none":
            continue
        if rule.scope == "section":
            if rule.section_key not in {_section_of(block) for block in blocks}:
                continue  # the section itself is absent; the section check reports that
            groups = [[i for i, b in enumerate(blocks) if _section_of(b) == rule.section_key]]
            where = [f'section "{rule.section_key}"']
        elif rule.scope == "each_section":
            keys = _sections_present(blocks, template)
            groups = [[i for i, b in enumerate(blocks) if _section_of(b) == key] for key in keys]
            where = [f'section "{key}"' for key in keys]
        else:
            groups = [list(range(len(blocks)))]
            where = ["the chapter"]

        wanted = rule.count or 1
        for group, label in zip(groups, where):
            found = [i for i in group if i not in claimed and _matches(rule, blocks[i])]
            # Prefer blocks the writer tagged with this rule.
            found.sort(key=lambda i: blocks[i].content.get("blueprint_rule") != rule.id)
            found = found[:wanted]
            claimed.update(found)
            if rule.required and len(found) < wanted:
                short = wanted - len(found)
                missing.append(
                    f"template visual {rule.id} in {label}: {short} more "
                    f"{rule.kind if rule.kind != 'any' else 'visual'}{'s' if short > 1 else ''} "
                    f"({rule.instruction[:120]})"
                )
            for index in found:
                problem = _placement_issue(rule, blocks, index)
                if problem:
                    issues.append(
                        ReviewIssue(
                            severity="major",
                            category="template",
                            block_index=index,
                            description=f"Template visual {rule.id} {problem}.",
                            suggestion=f"Move this image {_placement_hint(rule)} within its section.",
                        )
                    )
                content = blocks[index].content
                if rule.caption_required and not str(content.get("caption") or "").strip():
                    issues.append(
                        ReviewIssue(
                            severity="major",
                            category="template",
                            block_index=index,
                            description=f"Template visual {rule.id} needs a caption and has none.",
                            suggestion="Add a caption that says what the learner should notice.",
                        )
                    )
                if rule.labels and not rule.asset_id:
                    said = " ".join(
                        [str(content.get("prompt") or ""), str(content.get("caption") or "")]
                        + [str(label) for label in content.get("expected_labels") or []]
                    ).lower()
                    absent = [label for label in rule.labels if label.lower() not in said]
                    if absent:
                        issues.append(
                            ReviewIssue(
                                severity="minor",
                                category="template",
                                block_index=index,
                                description=f"Template visual {rule.id} should show the labels: {', '.join(absent)}.",
                                suggestion="Name these labels in the image prompt.",
                            )
                        )

    issues.extend(_copied_examples(blocks, template))
    issues.extend(_placeholders(blocks))
    return missing, issues


def _placement_hint(rule: VisualRule) -> str:
    return {
        "after_text": "to just after the paragraph that explains it",
        "before_text": "to the start of the section, before its first paragraph",
        "end_of_section": "to the end of the section",
    }.get(rule.placement, "next to the text it supports")


def _runs(text: str, size: int = COPY_RUN_WORDS) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    return {tuple(words[i : i + size]) for i in range(0, max(0, len(words) - size + 1))}


def _copied_examples(blocks: list[Block], template: CourseTemplate) -> list[ReviewIssue]:
    blueprint = template.blueprint
    assert blueprint is not None
    source: set[tuple[str, ...]] = set()
    for example in blueprint.examples:
        if example.kind == "text":
            source |= _runs(example.text)
    if not source:
        return []
    issues: list[ReviewIssue] = []
    for index, block in enumerate(blocks):
        if block.type is BlockType.IMAGE or block.type is BlockType.CODE:
            continue
        if _runs(block_text(block)) & source:
            issues.append(
                ReviewIssue(
                    severity="major",
                    category="template",
                    block_index=index,
                    description="This block copies wording from the template's example.",
                    suggestion="Rewrite it in new words about this course's own topic; follow the example's pattern only.",
                )
            )
    return issues


def _placeholders(blocks: list[Block]) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for index, block in enumerate(blocks):
        found = _PLACEHOLDER_RE.findall(block_text(block))
        if found:
            issues.append(
                ReviewIssue(
                    severity="major",
                    category="template",
                    block_index=index,
                    description=f"Unresolved template placeholder in the text: {', '.join(dict.fromkeys(found))}.",
                    suggestion="Replace the placeholder with real content for this course.",
                )
            )
    return issues
