"""One common Reviewer for both templates.

Deterministic template checks run locally (cheap, reliable); judgement calls -
accuracy, clarity, tone, repetition - go to the model. Both results are merged
into a single ChapterReview.

Two performance decisions live here:
* the model receives a text-only projection of the chapter rather than the full
  block JSON, because input size is latency;
* after a parallel run, one extra call checks the whole course for the failure
  mode parallel writing can introduce (repetition and broken transitions).
"""

from __future__ import annotations

import re

from app.agents import prompts
from app.agents.prompts import ContinuityContext
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.design_models import design_issues
from app.course.pedagogy import BANNED_OBJECTIVE_VERBS, BLOOM_VERBS
from app.course.style_guide import style_issues
from app.course.templates.blueprint.checks import blueprint_review
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, VISUAL_BLOCK_TYPES, BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.design import DesignModels
from app.schemas.document import Block
from app.schemas.learner import LearnerProfile
from app.schemas.research import ChapterResearch
from app.schemas.review import ChapterReview, ContinuityReport, ReviewIssue, SectionCheck
from app.schemas.style import StyleGuide
from app.schemas.template import CourseTemplate
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)

# VISUAL_BLOCK_TYPES (app.schemas.blocks): a diagram/illustration/concept
# visual, a table, or a code sample (always paired with its own explanation,
# per WRITER_SYSTEM) - deliberately not every non-text type: a heading,
# callout, quiz or divider adds structure but not something a learner
# actually *looks at*, so none of them reset the "wall of text" run below -
# only these do.
# Roughly one rendered page's worth of running paragraph text (see
# app.course.document.layout's own page-content-height accounting) - a
# stretch of text-only blocks longer than this with no visual between them
# reads as a page the learner just scrolls through, which is exactly the
# "wall of text" the 60/40 text-to-visual balance requirement exists to
# prevent (see the approved content-density requirement).
_MAX_TEXT_ONLY_RUN_WORDS = 500
# A chapter's overall visual density floor: roughly one visual per this many
# words of running text, averaged across the whole chapter - deliberately
# stricter than _MAX_TEXT_ONLY_RUN_WORDS above, so the two checks catch
# genuinely different failure modes rather than one subsuming the other: a
# chapter whose every individual gap is "fine" (under the wall-of-text
# limit) can still be consistently thinner than this average, e.g. several
# ~480-word gaps in a row - never one egregious run, but still not enough
# visuals overall for how much text there is.
_WORDS_PER_EXPECTED_VISUAL = 400


class ReviewerAgent:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()

    async def review_chapter(
        self,
        *,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        course_input: CourseInput,
        blocks: list[Block],
        continuity: ContinuityContext,
        research: ChapterResearch | None = None,
    ) -> ChapterReview:
        log.info(
            "TEMPLATE_SENT_TO_REVIEWER template_id=%s chapter=%s required_sections=%s",
            template.template_id,
            chapter.id,
            len(template.required_sections()),
        )
        payload = [block.model_dump(mode="json") for block in blocks]
        memory = await self.memory.build_context(
            stage="reviewer",
            course_title=blueprint.course_title,
            chapter_title=chapter.title,
            template=template,
        )
        review = await self.ai.structured(
            schema=ChapterReview,
            system=prompts.REVIEWER_SYSTEM,
            user=prompts.reviewer_user(
                blueprint,
                chapter,
                template,
                payload,
                course_input=course_input,
                continuity=continuity,
                limit=self.settings.reviewer_context_chars,
                memory=memory.render(),
                research=research,
            ),
            model=self.settings.reviewer_model,
            purpose=f"reviewer:{chapter.id}",
            phase="reviewer",
        )
        review.reviewed_at = utc_now_iso()
        apply_section_checks(review, blocks)
        review.issues.extend(_duplicate_text_issues(blocks))
        self._apply_structural_checks(
            review,
            template,
            blocks,
            course_input.learner_profile,
            course_input.design_models,
            course_input.style_guide,
        )
        # Keep the reviewer honest: structural gaps always block approval.
        if review.missing_required_blocks:
            review.approved = False
        log.info(
            "Reviewed %s: approved=%s score=%.1f issues=%s",
            chapter.id,
            review.approved,
            review.scores.overall(),
            len(review.issues),
        )
        return review

    async def review_continuity(
        self,
        *,
        blueprint: CourseBlueprint,
        summaries: list[tuple[str, str, str]],
    ) -> ContinuityReport:
        """One call over the whole course after the chapters are written."""
        report = await self.ai.structured(
            schema=ContinuityReport,
            system=prompts.CONTINUITY_SYSTEM,
            user=prompts.continuity_user(blueprint, summaries),
            model=self.settings.reviewer_model,
            purpose="continuity",
            phase="reviewer",
        )
        report.reviewed_at = utc_now_iso()
        log.info(
            "Continuity pass: approved=%s issues=%s", report.approved, len(report.issues)
        )
        return report

    @staticmethod
    def _apply_structural_checks(
        review: ChapterReview,
        template: CourseTemplate,
        blocks: list[Block],
        profile: LearnerProfile | None = None,
        design: DesignModels | None = None,
        style: StyleGuide | None = None,
    ) -> None:
        present_types = {block.type for block in blocks}
        present_sections = {block.meta.section_key for block in blocks if block.meta.section_key}

        missing: list[str] = list(review.missing_required_blocks)
        for block_type in template.required_block_types:
            if block_type not in present_types:
                missing.append(f"block type '{block_type.value}'")
        for section in template.required_sections():
            allowed = set(section.block_types) or set(BlockType)
            if section.key not in present_sections and not (allowed & present_types):
                missing.append(f"section '{section.key}'")
        missing.extend(_visual_balance_issues(blocks))
        # Template blueprint rules: required visuals, their placement and
        # captions, copied example wording, unresolved placeholders. A no-op
        # for templates without a blueprint.
        blueprint_missing, blueprint_issues = blueprint_review(blocks, template)
        missing.extend(blueprint_missing)
        review.issues.extend(blueprint_issues)
        # Deduplicate while preserving order.
        review.missing_required_blocks = list(dict.fromkeys(missing))

        # Pedagogy/style violations carry a block_index, so they route to the
        # surgical revision path (writer.revise_chapter) rather than forcing a
        # whole-chapter rewrite the way `missing_required_blocks` does.
        review.issues.extend(_pedagogy_issues(blocks, profile, style))
        review.issues.extend(style_issues(blocks, style))
        review.issues.extend(design_issues(blocks, design))
        review.issues.extend(_standards_issues(blocks, profile, design))


def _block_word_count(block: Block) -> int:
    if block.type not in TEXTUAL_BLOCK_TYPES:
        return 0
    return len(str(block.content.get("text", "")).split())


# An objective's verb is the word the learner is measured on, so a banned verb
# only counts in that position - "understand" inside explanatory prose is
# perfectly fine. Matches "...will be able to understand..." and an item that
# opens with the verb outright.
_BANNED_VERB_RE = re.compile(
    r"(?:\b(?:able to|will|shall|can|should|to)\s+|^)"
    r"(" + "|".join(re.escape(verb) for verb in BANNED_OBJECTIVE_VERBS) + r")\b",
    re.IGNORECASE,
)

# House-style slips worth reporting but not worth a rewrite (severity minor,
# so they never trigger `needs_revision` on their own).
_STYLE_SLIPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:e\.g\.|i\.e\.)", re.IGNORECASE), 'write "for example" or "that is"'),
    (re.compile(r"\bclick on\b", re.IGNORECASE), 'write "click", not "click on"'),
    (re.compile(r"\bplease\b", re.IGNORECASE), 'drop "please" from instructional copy'),
    (re.compile(r"\bthank you\b", re.IGNORECASE), 'drop "thank you" from instructional copy'),
    (
        re.compile(r"\b(?:you|users?|learners?|students?)\s+should\b", re.IGNORECASE),
        'use "must" for a required action; keep "should" for optional advice only',
    ),
)


def _pedagogy_issues(
    blocks: list[Block], profile: LearnerProfile | None, style: StyleGuide | None = None
) -> list[ReviewIssue]:
    """Deterministic checks for the rules the pedagogy brief states.

    Only runs when the course carries a learner profile: courses created
    before that feature never asked for these rules, so holding their content
    to them would raise issues the author never agreed to.
    """
    if profile is None:
        return []

    issues: list[ReviewIssue] = []
    for index, block in enumerate(blocks):
        if block.type is BlockType.LEARNING_OBJECTIVES:
            for item in block.content.get("items", []) or []:
                match = _BANNED_VERB_RE.search(str(item).strip())
                if match is None:
                    continue
                issues.append(
                    ReviewIssue(
                        severity="major",
                        category="pedagogy",
                        block_index=index,
                        description=(
                            f'Learning objective uses the unmeasurable verb '
                            f'"{match.group(1)}": {str(item).strip()[:160]}'
                        ),
                        suggestion=(
                            "Rewrite it with one verb from the approved Bloom's bank that "
                            "matches the cognitive level the task actually requires."
                        ),
                    )
                )

        words = _block_word_count(block)
        if words > profile.chunk_word_cap:
            issues.append(
                ReviewIssue(
                    severity="major",
                    category="chunk_size",
                    block_index=index,
                    description=(
                        f"This {block.type.value} block runs to {words} words, over the "
                        f"{profile.chunk_word_cap}-word delivery chunk cap."
                    ),
                    suggestion=(
                        "Split it into separate blocks, each with its own descriptive "
                        "heading, so no single block exceeds the cap."
                    ),
                )
            )

        # The MSTP slips are that guide's rules; a CMOS course has its own
        # checks (app/course/style_guide.py) and e.g. "e.g." is acceptable there.
        if block.type in TEXTUAL_BLOCK_TYPES and (style is None or style.base == "mstp"):
            text = str(block.content.get("text", ""))
            remedies = [remedy for pattern, remedy in _STYLE_SLIPS if pattern.search(text)]
            if remedies:
                issues.append(
                    ReviewIssue(
                        severity="minor",
                        category="style",
                        block_index=index,
                        description="House style (Microsoft Writing Style Guide) slip.",
                        suggestion="; ".join(remedies) + ".",
                    )
                )
    return issues


# Verbs allowed in a learning objective (Bloom's approved bank).
_APPROVED_OBJECTIVE_VERBS = {verb for verbs in BLOOM_VERBS.values() for verb in verbs}
_ABLE_TO_VERB_RE = re.compile(r"\bable to\s+([a-z']+)", re.IGNORECASE)
# Levels whose assessment may never be a multiple-choice quiz (Methodologies,
# Bloom rule 3: "never MCQ for Evaluate/Create").
_HIGH_LEVELS = ("Evaluate", "Create")
_NON_QUIZ_ASSESSMENTS = {
    BlockType.EXERCISE, BlockType.CHALLENGE, BlockType.CASE_STUDY, BlockType.REFLECTION,
}


def _objective_verb(item: str) -> str:
    match = _ABLE_TO_VERB_RE.search(item)
    if match:
        return match.group(1).casefold()
    words = item.strip().split(" ")
    return words[0].casefold().strip(".,:;") if words and words[0] else ""


def _standards_issues(
    blocks: list[Block], profile: LearnerProfile | None, design: DesignModels | None
) -> list[ReviewIssue]:
    """Accessibility and assessment-matching rules from the client's standards.

    Like `_pedagogy_issues`, this runs only for a course that opted in (a
    learner profile or a design model): older courses never agreed to these.
    """
    chosen = design is not None and bool(design.models)
    if profile is None and not chosen:
        return []
    bloom_checked_elsewhere = design is not None and design.has("bloom")

    issues: list[ReviewIssue] = []

    def add(severity: str, category: str, index: int, description: str, suggestion: str) -> None:
        issues.append(
            ReviewIssue(
                severity=severity,
                category=category,
                block_index=index,
                description=description,
                suggestion=suggestion,
            )
        )

    # Images need alt text, or an explicit "decorative" mark.
    for index, block in enumerate(blocks):
        if block.type is not BlockType.IMAGE or block.content.get("kind") == "toc":
            continue
        if block.content.get("decorative") or str(block.content.get("alt") or "").strip():
            continue
        add("major", "accessibility", index, "Image has no alt text.",
            "Write a short alt text describing what the image shows, or mark it decorative.")

    # Heading levels may not skip (H2 straight to H4).
    previous = 0
    for index, block in enumerate(blocks):
        if block.type is not BlockType.HEADING:
            continue
        level = int(block.content.get("level", 2) or 2)
        if previous and level > previous + 1:
            add("major", "accessibility", index,
                f"Heading level jumps from H{previous} to H{level}.",
                f"Use H{previous + 1} here so the heading hierarchy has no skipped level.")
        previous = level

    # Objectives: every verb must come from the approved bank (Bloom's own
    # check covers this when Bloom is selected, so it is not reported twice).
    levels: list[str] = []
    for index, block in enumerate(blocks):
        if block.type is not BlockType.LEARNING_OBJECTIVES:
            continue
        for item in block.content.get("items", []) or []:
            text = str(item)
            verb = _objective_verb(text)
            if _BANNED_VERB_RE.search(text) or not verb:
                continue  # unmeasurable verbs are reported by _pedagogy_issues
            if verb in _APPROVED_OBJECTIVE_VERBS:
                levels.extend(level for level, verbs in BLOOM_VERBS.items() if verb in verbs)
            elif profile is not None and not bloom_checked_elsewhere:
                add("minor", "pedagogy", index,
                    f'Objective verb "{verb}" is not in the approved Bloom bank: {text[:140]}',
                    "Use one verb from the approved bank that matches the task's cognitive level.")

    # A Create/Evaluate objective cannot be assessed by a quiz alone.
    if any(level in _HIGH_LEVELS for level in levels):
        kinds = {b.type for b in blocks}
        if BlockType.QUIZ in kinds and not (kinds & _NON_QUIZ_ASSESSMENTS):
            add("major", "assessment", -1,
                "An Evaluate or Create objective is assessed only by a multiple-choice quiz.",
                "Add a case study with justification (Evaluate) or an open-ended project or "
                "exercise (Create).")
    return issues


def _visual_balance_issues(blocks: list[Block]) -> list[str]:
    """Deterministic text-to-visual pacing check (approved content-density
    requirement: a chapter should read as roughly 60% text / 40% visual,
    never a long unbroken stretch of prose). Two independent checks:

    1. The longest run of text-only blocks with no visual breaking it up -
       catches one egregious "wall of text" section even in an otherwise
       well-paced chapter.
    2. The chapter's overall visual count against its total text length -
       catches a chapter that's thin on visuals throughout without any one
       run being long enough to trip check 1.

    Both are counted from the real written blocks, not guessed from
    `estimated_words` - this runs after the chapter exists, so there's no
    reason to estimate what can be measured directly. Treated exactly like
    a missing required block (blocks approval, drives a revision) because a
    text-heavy chapter is a structural problem, not a matter of taste the
    model should be trusted to self-police via prompt wording alone."""
    issues: list[str] = []
    total_text_words = 0
    visual_count = 0
    running_words = 0
    worst_run = 0

    for block in blocks:
        if block.type in VISUAL_BLOCK_TYPES:
            visual_count += 1
            running_words = 0
            continue
        words = _block_word_count(block)
        total_text_words += words
        if block.type in TEXTUAL_BLOCK_TYPES:
            running_words += words
            worst_run = max(worst_run, running_words)
        # Any other block type (heading, callout, quiz, summary, divider,
        # ...) neither resets nor grows the run - it adds structure, not
        # something a learner looks at, so it doesn't count as visual relief.

    if worst_run > _MAX_TEXT_ONLY_RUN_WORDS:
        issues.append(
            f"visual balance: a stretch of about {worst_run} words of running text has no "
            "diagram, illustration, table or code example breaking it up - roughly a full page "
            "of text-only reading. Add a visual (or split the section and place one between the "
            "halves) so this chapter doesn't read as a wall of text."
        )

    expected_visuals = total_text_words // _WORDS_PER_EXPECTED_VISUAL
    if expected_visuals >= 2 and visual_count < expected_visuals - 1:
        issues.append(
            f"visual balance: this chapter has about {total_text_words} words of text but only "
            f"{visual_count} visual block(s) (diagram/illustration/table/code) - a chapter this "
            f"long needs roughly {expected_visuals} spread through it, not clustered in one "
            "place, to keep a roughly 60/40 text-to-visual balance."
        )
    return issues


# ---------------------------------------------------------------------------
# teacher checks: the reviewer's per-section verdicts, made actionable
# ---------------------------------------------------------------------------

_SECTION_FAILURES: tuple[tuple[str, str, str, str], ...] = (
    # (field, category, severity, what a failure means)
    ("on_topic", "relevance", "major", "is not relevant to this course and chapter"),
    ("explains_heading", "clarity", "major", "does not actually explain what its heading promises"),
    ("meets_objective", "objective", "major", "does not serve the chapter objective"),
    ("accurate", "accuracy", "blocker", "contains information that is wrong"),
)


def _sections(blocks: list[Block]) -> list[tuple[int, int]]:
    """(heading index, end index exclusive) for every heading-led section."""
    starts = [index for index, block in enumerate(blocks) if block.type is BlockType.HEADING]
    return [(start, starts[n + 1] if n + 1 < len(starts) else len(blocks)) for n, start in enumerate(starts)]


def _target_block(blocks: list[Block], start: int, end: int, wanted: int) -> int:
    """The block an issue points at: the reviewer's choice when it lies inside
    the section, else the section's first block of prose."""
    if start < wanted < end:
        return wanted
    for index in range(start + 1, end):
        if blocks[index].type in TEXTUAL_BLOCK_TYPES:
            return index
    return start + 1 if start + 1 < end else start


def apply_section_checks(review: ChapterReview, blocks: list[Block]) -> None:
    """Turn the reviewer's per-section verdicts into issues on exact blocks.

    A failed check becomes a major (or, for accuracy, blocker) issue carrying
    the index of the block to rewrite, so the surgical revision changes only
    that block. A section the reviewer returned no verdict for is recorded in
    `unchecked_sections` and reported as a minor issue - it is never counted
    as having passed.
    """
    checks: dict[int, SectionCheck] = {}
    by_heading = {
        str(block.content.get("text") or "").strip().lower(): index
        for index, block in enumerate(blocks)
        if block.type is BlockType.HEADING
    }
    for check in review.section_checks:
        index = check.heading_index
        if not (0 <= index < len(blocks) and blocks[index].type is BlockType.HEADING):
            index = by_heading.get(check.heading.strip().lower(), -1)
        if index >= 0:
            checks.setdefault(index, check)

    for start, end in _sections(blocks):
        if end - start <= 1:
            continue  # a heading with nothing under it is the structure check's business
        heading = str(blocks[start].content.get("text") or "").strip()
        check = checks.get(start)
        answered = check is not None and any(
            getattr(check, field) is not None for field, *_ in _SECTION_FAILURES
        )
        if check is None or not answered:
            review.unchecked_sections.append(heading)
            review.issues.append(
                ReviewIssue(
                    severity="minor",
                    category="unchecked",
                    block_index=start,
                    description=f'The reviewer returned no verdict for section "{heading}", so it is unchecked.',
                    suggestion="Review this section by hand.",
                )
            )
            continue
        target = _target_block(blocks, start, end, check.problem_block)
        detail = f": {check.problem}" if check.problem else "."
        for field, category, severity, meaning in _SECTION_FAILURES:
            if getattr(check, field) is False:
                review.issues.append(
                    ReviewIssue(
                        severity=severity,
                        category=category,
                        block_index=target,
                        description=f'Section "{heading}" {meaning}{detail}',
                        suggestion=check.fix or "Rewrite this block so the section does its job.",
                    )
                )
        if check.unsupported is True:
            review.issues.append(
                ReviewIssue(
                    severity="major",
                    category="unsupported",
                    block_index=target,
                    description=f'Section "{heading}" states facts the research does not cover, so they are unverified{detail}',
                    suggestion=check.fix or "Remove the unverified specifics or state only what the research supports.",
                )
            )
        repeated = check.repeats_block
        if repeated is not None and 0 <= repeated < start:
            review.issues.append(
                ReviewIssue(
                    severity="major",
                    category="repetition",
                    block_index=target,
                    description=f'Section "{heading}" re-teaches what block {repeated} already covered{detail}',
                    suggestion=check.fix or "Replace the repeated material with something the learner has not seen yet.",
                )
            )


_DUP_RUN = 5
_DUP_SHARE = 0.85
_DUP_WORD_RE = re.compile(r"[a-z0-9']+")


def _runs_of(text: str) -> set[tuple[str, ...]]:
    words = _DUP_WORD_RE.findall(text.lower())
    return {tuple(words[i : i + _DUP_RUN]) for i in range(max(0, len(words) - _DUP_RUN + 1))}


def _duplicate_text_issues(blocks: list[Block]) -> list[ReviewIssue]:
    """A floor under the reviewer's meaning-based repetition check: a block
    that is almost word for word an earlier one is a duplicate whatever the
    model said. Paraphrased repetition is judged by the reviewer itself."""
    seen: list[tuple[int, set[tuple[str, ...]]]] = []
    issues: list[ReviewIssue] = []
    for index, block in enumerate(blocks):
        if block.type not in TEXTUAL_BLOCK_TYPES:
            continue
        runs = _runs_of(str(block.content.get("text") or ""))
        if len(runs) < 6:
            continue
        for earlier, earlier_runs in seen:
            if len(runs & earlier_runs) / len(runs) >= _DUP_SHARE:
                issues.append(
                    ReviewIssue(
                        severity="major",
                        category="repetition",
                        block_index=index,
                        description=f"This block repeats block {earlier} almost word for word.",
                        suggestion="Replace it with the next step of the explanation instead of saying it again.",
                    )
                )
                break
        seen.append((index, runs))
    return issues
