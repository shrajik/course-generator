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

import ast
import re

from app.agents import prompts
from app.agents.prompts import ContinuityContext
from app.core.config import Settings, get_settings
from app.core.ids import utc_now_iso
from app.core.logging import get_logger
from app.course.pedagogy import BANNED_OBJECTIVE_VERBS
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, VISUAL_BLOCK_TYPES, BlockType
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block
from app.schemas.draft import StructuralFinding
from app.schemas.learner import LearnerProfile
from app.schemas.review import ChapterReview, ContinuityReport, ReviewIssue
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
    ) -> tuple[ChapterReview, list[StructuralFinding]]:
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
            ),
            model=self.settings.reviewer_model,
            purpose=f"reviewer:{chapter.id}",
            phase="reviewer",
        )
        review.reviewed_at = utc_now_iso()
        structural_findings = self._apply_structural_checks(
            review, template, blocks, course_input.learner_profile, course_input
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
        return review, structural_findings

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
        course_input: CourseInput | None = None,
    ) -> list[StructuralFinding]:
        """Mutates `review` exactly as before (unchanged contract: every
        deterministic gap still lands in `review.missing_required_blocks`,
        which is what drives the existing retry/auto-revision loop in
        CourseService._generate_chapter - untouched by this change) and
        additionally RETURNS the subset of those gaps that came from a real
        deterministic check here, as typed `StructuralFinding`s, so a
        caller can persist them separately (see GeneratedChapter.
        structural_findings) without having to guess which of
        `missing_required_blocks`' plain strings were deterministic versus
        the reviewer MODEL's own self-reported text (also merged into that
        same list, exactly as before) - that distinction can't be
        recovered from the flat list alone once merged."""
        present_types = {block.type for block in blocks}
        present_sections = {block.meta.section_key for block in blocks if block.meta.section_key}

        deterministic: list[StructuralFinding] = []
        missing: list[str] = list(review.missing_required_blocks)

        for block_type in template.required_block_types:
            if block_type not in present_types:
                text = f"block type '{block_type.value}'"
                missing.append(text)
                deterministic.append(
                    StructuralFinding(check="missing_block_type", description=text, dimension="completeness")
                )
        for section in template.required_sections():
            allowed = set(section.block_types) or set(BlockType)
            if section.key not in present_sections and not (allowed & present_types):
                text = f"section '{section.key}'"
                missing.append(text)
                deterministic.append(
                    StructuralFinding(check="missing_section", description=text, dimension="completeness")
                )
        for text in _visual_balance_issues(blocks):
            missing.append(text)
            deterministic.append(
                StructuralFinding(check="visual_balance", description=text, dimension="visual")
            )
        content_level = course_input.content_level if course_input is not None else 2
        for text in _content_level_3_issues(blocks, content_level):
            missing.append(text)
            deterministic.append(
                StructuralFinding(check="content_level_3", description=text, dimension="pedagogy")
            )
        for text in _section_completeness_issues(blocks):
            missing.append(text)
            deterministic.append(
                StructuralFinding(check="section_completeness", description=text, dimension="completeness")
            )
        # Deduplicate while preserving order.
        review.missing_required_blocks = list(dict.fromkeys(missing))

        # Pedagogy/style violations carry a block_index, so they route to the
        # surgical revision path (writer.revise_chapter) rather than forcing a
        # whole-chapter rewrite the way `missing_required_blocks` does.
        effective_profile = profile if profile is not None else (
            course_input.learner_profile if course_input is not None else None
        )
        review.issues.extend(_pedagogy_issues(blocks, effective_profile))

        return deterministic


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
)


def _pedagogy_issues(blocks: list[Block], profile: LearnerProfile | None) -> list[ReviewIssue]:
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

        if block.type in TEXTUAL_BLOCK_TYPES:
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


# MOM Phase 4 - Content Level 3 ("experiential") is a prompt-level framing
# shift (see content_level_brief), but a framing shift alone can be ignored
# by the model with nothing to catch it - this is the actual generation
# validation path the MOM requires: a deterministic, post-hoc check that the
# chapter really contains the experiential elements the level promises,
# reusing the EXISTING EXERCISE/CHALLENGE/CASE_STUDY/STORY schemas (no new
# block type, no new content field) and the EXISTING missing_required_blocks
# -> full-rewrite retry path (see CourseService._generate_chapter's
# `max_review_revisions` loop) rather than a parallel validation system.
#
# "Worked, hands-on practice" maps onto ExerciseContent's own fields:
# `expected_outcome` is the required expected output, and `steps`/`hints`
# are the required step-by-step/constraint guidance - a block with neither
# is just a labelled heading, exactly what the MOM says not to accept.
# "Realistic scenario with a decision and a reflection" maps onto
# CaseStudyContent's `context`+`challenge` (the situation and its decision
# point) plus `outcome`/`lessons` (the evaluation/reflection), or StoryContent's
# `text`+`takeaway` for a lighter-weight narrative scenario.
def _content_level_3_issues(blocks: list[Block], content_level: int) -> list[str]:
    if content_level != 3:
        return []

    has_worked_exercise = False
    for block in blocks:
        if block.type not in (BlockType.EXERCISE, BlockType.CHALLENGE):
            continue
        content = block.content
        if str(content.get("expected_outcome", "")).strip() and (
            content.get("steps") or content.get("hints")
        ):
            has_worked_exercise = True
            break

    has_realistic_scenario = False
    for block in blocks:
        if block.type is BlockType.CASE_STUDY:
            content = block.content
            if (
                str(content.get("context", "")).strip()
                and str(content.get("challenge", "")).strip()
                and (content.get("outcome") or content.get("lessons"))
            ):
                has_realistic_scenario = True
                break
        elif block.type is BlockType.STORY:
            content = block.content
            if str(content.get("text", "")).strip() and str(content.get("takeaway", "")).strip():
                has_realistic_scenario = True
                break

    issues: list[str] = []
    if not has_worked_exercise:
        issues.append(
            "content level 3 (experiential): this chapter needs at least one `exercise` or "
            "`challenge` block with a concrete `expected_outcome` plus `steps` or `hints` - a "
            "hands-on activity with real instructions and a worked, checkable result, not just "
            "a heading."
        )
    if not has_realistic_scenario:
        issues.append(
            "content level 3 (experiential): this chapter needs at least one `case_study` block "
            "(with `context`, `challenge`, and an `outcome` or `lessons`) or `story` block (with "
            "`text` and a `takeaway`) presenting a realistic scenario or situation-based decision, "
            "not purely abstract explanation."
        )
    return issues


# MOM Phase 7 - section completeness: the OBJECTIVE half of completeness
# checking (see chapter_contract_brief in app.agents.prompts for the
# meaning-dependent half, handled by the reviewer model's own judgement).
# These three failure modes are structural, not matters of interpretation -
# an empty heading, a chapter that just stops, or Python that doesn't parse
# are true regardless of what the chapter was supposed to be about, so they
# don't need - and shouldn't wait on - a model call to catch.
_PLACEHOLDER_HEADING_TEXT = {
    "",
    "todo",
    "tbd",
    "tba",
    "untitled",
    "untitled section",
    "section title",
    "placeholder",
    "[placeholder]",
    "heading",
    "title",
}


def _section_completeness_issues(blocks: list[Block]) -> list[str]:
    issues: list[str] = []

    for index, block in enumerate(blocks):
        if block.type is BlockType.HEADING:
            text = str(block.content.get("text", "")).strip()
            if text.lower() in _PLACEHOLDER_HEADING_TEXT:
                issues.append(
                    f"section completeness: block {index} is a heading with no real title "
                    f"({text!r}) - give it a specific, descriptive heading or remove it."
                )

    # A chapter that ends on a heading (or on a heading followed only by
    # dividers) started a section and never filled it in.
    for block in reversed(blocks):
        if block.type is BlockType.DIVIDER:
            continue
        if block.type is BlockType.HEADING:
            issues.append(
                "section completeness: the chapter ends immediately after a heading with no "
                "content following it - finish that section instead of leaving it empty."
            )
        break

    for index, block in enumerate(blocks):
        if block.type is not BlockType.CODE:
            continue
        language = str(block.content.get("language", "")).strip().lower()
        code = str(block.content.get("code", "")).strip()
        if not code:
            issues.append(
                f"section completeness: block {index} is a code block with no code in it."
            )
        elif language in ("python", "py"):
            try:
                ast.parse(code)
            except SyntaxError as exc:
                issues.append(
                    f"section completeness: block {index}'s Python code does not parse "
                    f"({exc.msg} at line {exc.lineno}) - fix the syntax error."
                )
    return issues
