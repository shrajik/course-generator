"""Prompt construction.

All prompts are plain functions so they can be unit tested and diffed. Keeping
them here (rather than inline) also makes it obvious that the two templates share
one pipeline: the template configuration is *data* inside the prompt, not a
separate code path.

Convention: prompts include machine-readable markers (`COURSE TITLE:`,
`CHAPTER TITLE:`, `TEMPLATE KIND:`) which the offline mock client uses to produce
context-appropriate placeholder content.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import CourseInput, ImproveTocRequest
from app.schemas.document import Block
from app.schemas.research import ChapterResearch
from app.schemas.review import ChapterReview
from app.course.design_models import design_model_brief
from app.course.pedagogy import learner_brief, pedagogy_rules_brief
from app.course.style_guide import style_guide_brief
from app.course.templates.blueprint.brief import blueprint_brief
from app.schemas.template import CourseTemplate


@dataclass
class ContinuityContext:
    """What a chapter is told about its neighbours.

    In parallel mode the summaries are the *planned* ones from the blueprint
    (`planned=True`), which is what lets every chapter be written at once. The
    boundaries below are what stop chapters from covering each other's material.
    """

    previous: list[tuple[str, str]] = field(default_factory=list)
    upcoming: list[tuple[str, str]] = field(default_factory=list)
    planned: bool = False

    def render(self) -> str:
        lines: list[str] = []
        if self.previous:
            lines.append(
                "ALREADY COVERED BY EARLIER CHAPTERS (build on this, never repeat it):"
            )
            lines.extend(f"- {title}: {summary}" for title, summary in self.previous)
        else:
            lines.append("This is the first chapter; assume no prior course content.")
        if self.upcoming:
            lines.append("")
            lines.append(
                "RESERVED FOR LATER CHAPTERS (do not teach these here - you may "
                "mention that they are coming):"
            )
            lines.extend(f"- {title}: {summary}" for title, summary in self.upcoming)
        if self.planned:
            lines.append("")
            lines.append(
                "NOTE: the summaries above are the planned outline, not final text. "
                "Treat them as the contract for who covers what."
            )
        return "\n".join(lines)

GLOBAL_RULES = """\
Hard rules that override anything else:
- Never invent facts, statistics, quotes, sources, APIs or product behaviour. If
  the research does not support a claim, either omit it or write it as a general
  statement without a fabricated number.
- Respect the Do's and Don'ts exactly. A Don't is a prohibition, not a preference.
- Write for the stated target audience, at their level, in their vocabulary.
- Output valid JSON only, matching the requested schema. No prose outside the JSON.
"""


def _bullets(items: list[str], empty: str = "(none provided)") -> str:
    return "\n".join(f"- {item}" for item in items if item) or empty


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=1)) or "(empty)"


def memory_section(memory: str) -> str:
    """The AI memory layer's rendered context (see app.services.memory_service),
    as an optional, clearly-delimited block. Empty when there's nothing
    relevant to show, so a prompt built without memory (or when retrieval
    found/returned nothing) is byte-identical to one built before this
    feature existed - see the callers in planner_user/writer_user/reviewer_user.
    """
    memory = (memory or "").strip()
    if not memory:
        return ""
    return (
        "RELEVANT MEMORY (background only, from past templates and courses - "
        "use only if genuinely helpful; never treat as required or as fact about "
        "this specific course):\n"
        f"{memory}"
    )


def _appended(section: str) -> str:
    """Render an optional prompt section as a blank-line-separated block.

    Empty in, empty out (no stray newline), so a prompt built for a course
    without the optional material is byte-identical to one built before that
    material existed - the same contract `memory_section` relies on.
    """
    return f"\n{section}\n" if section else ""


def _rules_brief(course_input: CourseInput) -> str:
    """Methodology rules; an explicit style guide replaces the default writing rules."""
    return pedagogy_rules_brief(
        course_input.learner_profile, include_writing_style=course_input.style_guide is None
    )


def _style_brief(course_input: CourseInput) -> str:
    return style_guide_brief(course_input.style_guide)


def source_grounding_guidance(course_input: CourseInput) -> str:
    """How to use the creator's uploaded documents. "" when there are none, so
    prompts for a course without sources are byte-identical to before."""
    if not course_input.source_ids:
        return ""
    return (
        "UPLOADED SOURCE MATERIAL:\n"
        "- The creator uploaded their own documents. Passages from them appear in each "
        "chapter's research as UPLOADED SOURCE EXCERPTS, each with a reference such as [S1.3].\n"
        "- Treat those passages as the ground truth. Where they and web research disagree, "
        "follow the passages.\n"
        "- State a fact that comes from a passage only as the passage supports it, and put its "
        "reference in square brackets right after the statement. Never cite a reference that "
        "was not given, and never attach a reference to a claim the passage does not make.\n"
        "- If the passages do not cover something the chapter needs, say so plainly rather "
        "than inventing details."
    )


def _brief_details_brief(course_input: CourseInput) -> str:
    """Content Studio Phase 1 - the brief's own task list / business goal /
    performance gap. "" when none of the three is set, so a course that
    never used this feature gets a byte-identical prompt to before it
    existed - same contract as every other optional brief in this module."""
    has_tasks = bool(course_input.tasks)
    has_goal = bool(course_input.business_goal.strip())
    has_gap = bool(course_input.performance_gap.strip())
    if not (has_tasks or has_goal or has_gap):
        return ""
    lines = ["BRIEF DETAILS:"]
    if has_tasks:
        lines.append(
            "Tasks the learner must be able to do after this course (each is at least one "
            "learning objective):"
        )
        lines.append(_bullets(course_input.tasks))
    if has_goal:
        lines.append(f"Business/performance goal: {course_input.business_goal.strip()}")
    if has_gap:
        lines.append(f"Performance gap: {course_input.performance_gap.strip()}")
    else:
        lines.append(
            "Performance gap: not stated by the creator - draft a short, plausible gap "
            "statement from the brief and tasks above for the Analysis summary, and say "
            "plainly that it is drafted/inferred, never presented as a fact the creator "
            "confirmed."
        )
    return "\n".join(lines)


def _design_brief(course_input: CourseInput) -> str:
    """Rules for the selected instructional-design models ("" when none is)."""
    return design_model_brief(
        course_input.design_models,
        bloom_already_stated=course_input.learner_profile is not None,
    )


def code_cell_guidance(template: CourseTemplate) -> str:
    """When to emit a runnable `code_cell` instead of a static `code` block.

    Empty unless the template allows code cells, so prompts for templates that
    do not (the non-technical one, any uploaded template that restricts block
    types) are byte-identical to what they were before the feature existed.
    """
    from app.core.config import get_settings
    from app.schemas.blocks import BlockType

    if BlockType.CODE_CELL not in template.allowed_block_types:
        return ""

    languages = ", ".join(get_settings().code_cell_languages)
    return f"""EXECUTABLE CODE CELLS:
- Two block types carry code. `code` is a static sample the learner reads: use
  it for fragments, signatures, snippets that need surrounding context, and
  anything that cannot run on its own. `code_cell` is a runnable cell the
  learner executes in the editor with a Run button.
- Use a `code_cell` only when seeing the program's output teaches something. It
  must be a short, complete, self-contained program that runs as written with
  no input, no files, no network and no installed packages (standard library
  only), finishes within a couple of seconds, and prints its result to
  standard output.
- NEVER write the output yourself and never describe it as a block of its own
  inside the cell: leave it out entirely. The learner's own run produces it,
  and an output you invent could be wrong.
- Set `language` to the language this COURSE teaches - never convert an example
  to Python to make it runnable. Runnable languages: {languages}. For any other
  language use a static `code` block instead.
- C++ needs a complete `int main()`. Java needs one public class containing
  `public static void main`, ideally named `Main`. Print with the language's
  normal output call so there is something to show.
- Follow every `code_cell` with an explanation of what it does and what the
  learner should observe when they run it.
"""


def content_level_brief(content_level: int) -> str:
    """MOM Phase 4 - "" at level 2 (the existing default/only behaviour
    before this feature existed), which is what keeps every pre-existing
    course's prompts byte-identical, same contract as `learner_brief`
    returning "" for `profile=None`.

    Level 1 is enforced in TWO places, not just here: this prompt line
    tells the writer not to plan/request any image block in the first
    place (so the document never contains an empty/ungenerated visual
    placeholder - a real, confirmed failure mode this avoids), and
    CourseService.generate separately forces `generate_images=False`
    regardless of the request's own override, as a structural backstop in
    case the model asks for one anyway.

    Level 3 is explicitly the MOM's own PROVISIONAL interpretation, not a
    finalised spec - this is a prompt-level framing shift only (ask for
    realistic scenarios/decisions/hands-on activities where the topic
    supports them), not a separate generation pipeline or a new block type;
    it still goes through the exact same block types (exercise/challenge/
    case_study/story) the writer already has, just asked for more
    deliberately and more often."""
    if content_level == 1:
        return (
            "CONTENT LEVEL 1 - TEXT ONLY: do not plan or request any `image` "
            "block of any kind (no diagrams, no flowcharts, no illustrations, "
            "no section-intro icons) for this course. Every concept must be "
            "explained in text alone - use headings, definitions, examples, "
            "tables and code blocks as needed, but never an image block."
        )
    if content_level == 3:
        return (
            "CONTENT LEVEL 3 - EXPERIENTIAL LEARNING: beyond the course's "
            "normal explanatory content, prioritise realistic scenarios, "
            "situation-based decisions and hands-on practice wherever the "
            "topic genuinely supports them - use `story`/`case_study` blocks "
            "for realistic scenarios, `exercise`/`challenge` blocks for "
            "hands-on activities with clear instructions and expected "
            "outputs, and favour decisions made under realistic, sometimes "
            "incomplete information over purely abstract explanation. This "
            "is additive to the course's normal content, not a replacement "
            "for explaining the underlying concepts clearly."
        )
    return ""


# MOM Phase 5 - learning approaches: HOW a concept is taught. Each entry
# names the EXISTING block type(s) it reuses (never invents a new one, per
# the MOM's own "reuse existing content primitives" instruction) and states
# the concrete content shape, not just a label - the MOM explicitly warns
# against "merely inserting a heading such as 'Case Study' without
# meaningful content", so every instruction below ties the approach to a
# real structural requirement the reviewer can actually check for.
_LEARNING_APPROACH_GUIDANCE: dict[str, str] = {
    # The pre-existing default behaviour (conceptual explanation via
    # paragraphs/definitions) - included for completeness and for when it's
    # combined with other approaches, but adds no new instruction on its own.
    "explanatory": (
        "Explanatory: focus on conceptual understanding - what the concept is, "
        "why it works, and how its parts relate. This is the baseline the "
        "course already provides; when combined with other approaches below, "
        "keep the conceptual explanation intact rather than replacing it."
    ),
    "example_driven": (
        "Example-driven: include genuinely worked examples, not just a labelled "
        "heading - show the concrete input, the step-by-step working, and the "
        "resulting output (a `code`/`code_cell` block for a technical worked "
        "example, a `callout` or `paragraph` walkthrough otherwise). Include at "
        "least one common mistake and its correction where that is realistic "
        "for the topic."
    ),
    "scenario_based": (
        "Scenario-based: use `story`/`case_study` blocks to set up a realistic, "
        "specific context with an explicit objective, real constraints, a "
        "decision actually made, and the plausible consequences of that "
        "decision - not a generic, interchangeable anecdote. State what was "
        "decided and why, not just what happened."
    ),
    "situation_based": (
        "Situation-based: use a `case_study` or `exercise` block to present ONE "
        "specific situation that requires judgment, then evaluate at least two "
        "plausible responses to it - explain concretely why each response is "
        "suitable or unsuitable in this situation, not in the abstract."
    ),
    "hands_on": (
        "Hands-on: use `exercise`/`challenge` blocks with everything the "
        "learner actually needs to DO the activity - clear step-by-step "
        "instructions, any required inputs/prerequisites, exactly what "
        "deliverable is expected, and explicit criteria for judging whether "
        "it was done correctly. For a technical topic, prefer a `code_cell` "
        "the learner can actually run over a purely descriptive exercise."
    ),
    "conversational": (
        "Conversational: present the explanation as a realistic dialogue or a "
        "sequence of guided questions-and-answers (a learner's likely question, "
        "followed by a direct answer, building progressively) rather than a "
        "lecture-style block of prose. This is a STATIC, written dialogue "
        "format (this application does not provide live interactive tutoring) "
        "- never claim or imply the reader can actually ask a follow-up "
        "question."
    ),
}


def learning_approaches_brief(approaches: list[str]) -> str:
    """"" for an empty list - the pre-existing default (the writer judges
    which approach fits each section on its own) - same contract as
    `content_level_brief` returning "" at level 2. For 2+ approaches, asks
    the writer to combine them coherently within the same content rather
    than generating one repetitive section per approach - a real, MOM-
    flagged failure mode ("combine coherently, not repetitive content") this
    line exists specifically to prevent."""
    valid = [a for a in approaches if a in _LEARNING_APPROACH_GUIDANCE]
    if not valid:
        return ""
    lines = ["LEARNING APPROACH(ES) FOR THIS COURSE - apply throughout, not just in one section:"]
    lines.extend(f"- {_LEARNING_APPROACH_GUIDANCE[a]}" for a in valid)
    if len(valid) > 1:
        lines.append(
            "- When combining the approaches above, weave them into the SAME "
            "content coherently (e.g. a scenario that then poses a hands-on "
            "exercise) rather than generating one repetitive, separate section "
            "per approach."
        )
    return "\n".join(lines)


def approach_review_brief(course_input: CourseInput) -> str:
    """Quality-governance follow-up (Phase 3.10a) - gives the REVIEWER the
    same facts the writer already received about which content level and
    learning approach(es) were requested, so it can judge adherence instead
    of being blind to the request entirely (previously `reviewer_user` never
    passed this through at all, even though `course_input` was already a
    parameter - confirmed by inspection, not assumed).

    Deliberately reuses `content_level_brief`/`learning_approaches_brief`
    verbatim rather than maintaining a second, review-framed copy of the
    same guidance that could drift out of sync - this function only adds a
    short header (reframing "what to write" as "what to check for") and one
    instruction line, never new facts about what each approach requires.

    "" when both underlying briefs are themselves "" (content_level == 2,
    no approaches selected - the pre-existing default) - same contract as
    every other `_brief` function in this module, so a course that never
    opted into either feature gets a byte-identical reviewer prompt to
    before this function existed.

    This does NOT change how the resulting judgement is classified: like
    every other reviewer opinion, whatever the model reports lands in
    `review.issues` and app.services.quality_framework marks it
    `verification_status="unverified"` unconditionally, regardless of the
    severity text used - this prompt only gives the model something to
    form an (still unverified) opinion about. Severity is explicitly
    capped at major/minor below, mirroring `chapter_contract_brief`'s own
    existing instruction, so this introduces no new severity-policy
    precedent of its own."""
    level = content_level_brief(course_input.content_level)
    approaches = learning_approaches_brief(course_input.learning_approaches)
    if not level and not approaches:
        return ""
    lines = [
        "REQUESTED FOR THIS CHAPTER (the writer was asked for this - judge whether "
        "the chapter's actual content delivers it, not just whether a matching block "
        "type exists):"
    ]
    if level:
        lines.append(level)
    if approaches:
        lines.append(approaches)
    lines.append(
        "- If a requested content level or learning approach above is not genuinely "
        "reflected in this chapter's actual content, report it as an issue with "
        "category 'approach_adherence' - severity 'major' if it is essentially "
        "absent, 'minor' if it is present but weak. Never report this as 'blocker'."
    )
    return "\n".join(lines)


def _course_context(course_input: CourseInput, template: CourseTemplate) -> str:
    toc_lines = []
    for index, item in enumerate(course_input.toc, start=1):
        toc_lines.append(f"{index}. {item.title}")
        for section in item.sections:
            toc_lines.append(f"   - {section}")
        if item.notes:
            toc_lines.append(f"   (note: {item.notes})")
    return f"""\
COURSE TITLE: {course_input.course_title}
TARGET AUDIENCE: {course_input.target_audience}
TEMPLATE KIND: {template.kind}
TEMPLATE: {template.name} ({template.template_id}) - {template.description}
LANGUAGE: {course_input.language}
REQUESTED TONE: {course_input.tone or "(not specified)"}

TABLE OF CONTENTS (as provided by the user):
{chr(10).join(toc_lines)}

DO'S:
{_bullets(course_input.dos)}

DON'TS:
{_bullets(course_input.donts)}
{_appended(learner_brief(course_input.learner_profile))}{_appended(content_level_brief(course_input.content_level))}{_appended(learning_approaches_brief(course_input.learning_approaches))}{_appended(_design_brief(course_input))}{_appended(_style_brief(course_input))}{_appended(_brief_details_brief(course_input))}"""


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = f"""\
You are a senior instructional designer building a course blueprint.

{GLOBAL_RULES}

Additional rules for planning:
- Keep the user's chapter titles and their order. Report problems in `critique`
  instead of silently rewriting the table of contents.
- Give every chapter a stable id of the form `chapter_1`, `chapter_2`, ... in the
  order the user supplied, and set `order` to the same number.
- `required_blocks` / `optional_blocks` must use only the block types listed for
  the selected template.
- Research questions should be specific enough to search for.
"""


def planner_user(course_input: CourseInput, template: CourseTemplate, *, memory: str = "") -> str:
    return f"""\
{_course_context(course_input, template)}

TEMPLATE CHAPTER STRUCTURE (each chapter should be able to satisfy this):
{template.outline_for_prompt()}

ALLOWED BLOCK TYPES: {", ".join(bt.value for bt in template.allowed_block_types)}
TEMPLATE WRITING GUIDANCE: {template.writer_guidance}{_appended(blueprint_brief(template, planner=True))}
{memory_section(memory)}
Produce the course blueprint:
1. A one-paragraph course summary.
2. Course-level learning objectives and prerequisites.
3. One entry per chapter in the table of contents above, in the same order, with
   sections, key concepts, required/optional blocks and research questions.
4. A critique listing missing concepts, duplicate topics, ordering problems and
   notes on the learning progression. Put any table-of-contents changes you would
   recommend in `critique.suggested_toc` - do not apply them to `chapters`.
"""


# ---------------------------------------------------------------------------
# TOC improvement
# ---------------------------------------------------------------------------

TOC_SYSTEM = f"""\
You are a senior curriculum reviewer improving a course table of contents.

{GLOBAL_RULES}

Rules:
- Return the full proposed table of contents in `suggested_toc`, including
  chapters you kept unchanged, in the order you recommend.
- Every difference from the original must appear in `changes` with an action of
  add, remove, rename, reorder, split or merge, plus a concrete reason.
- Be conservative: propose changes that measurably improve learning progression,
  not cosmetic rewording.
"""


def toc_user(request: ImproveTocRequest, template: CourseTemplate) -> str:
    return f"""\
COURSE TITLE: {request.course_title}
TARGET AUDIENCE: {request.audience or "(not specified)"}
TEMPLATE KIND: {template.kind}
TEMPLATE: {template.name} - {template.description}

CURRENT TABLE OF CONTENTS:
{_numbered([item.title for item in request.toc])}

DO'S:
{_bullets(request.dos)}

DON'TS:
{_bullets(request.donts)}

Review the table of contents for: missing foundational concepts, duplicate or
overlapping chapters, poor ordering, and gaps in the learning progression.
Then return the improved table of contents, the list of changes and your reasoning.
"""


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM = """\
You are a meticulous research assistant preparing source material for one chapter
of a course. Gather accurate, current, specific material. Prefer authoritative
primary sources. Never fabricate a source, a statistic or a quote - if you cannot
verify something, leave it out and say so. Note the URL of anything you rely on.
"""


def research_user(
    blueprint: CourseBlueprint,
    chapter: BlueprintChapter,
    template: CourseTemplate,
    *,
    course_input: CourseInput,
) -> str:
    return f"""\
COURSE TITLE: {blueprint.course_title}
CHAPTER TITLE: {chapter.title}
TEMPLATE KIND: {template.kind}
TARGET AUDIENCE: {blueprint.audience or course_input.target_audience}

CHAPTER OBJECTIVE: {chapter.objective}
KEY CONCEPTS: {", ".join(chapter.key_concepts) or "(none listed)"}
CHAPTER SECTIONS:
{_bullets([s.title for s in chapter.sections])}

RESEARCH QUESTIONS TO ANSWER:
{_numbered(chapter.research_questions or [f"What must a learner know about {chapter.title}?"])}

DO'S:
{_bullets(course_input.dos)}

DON'TS:
{_bullets(course_input.donts)}

TEMPLATE RESEARCH GUIDANCE: {template.research_guidance}

Research THIS CHAPTER ONLY. Collect: definitions, core concepts, examples,
real-world examples, case studies, important facts, statistics (only with a
source), common mistakes, best practices, references with URLs, FAQs, visual
opportunities, exercise ideas and quiz ideas.
"""


RESEARCH_STRUCTURE_SYSTEM = f"""\
You convert research notes into a structured research artifact.

{GLOBAL_RULES}

Rules:
- Use only what is present in the notes. Do not add new facts.
- Copy source URLs exactly as they appear in the notes. Never guess a URL.
- Leave a list empty rather than padding it with generic filler.
- For each reference, fill `author`, `source_type` (e.g. "official
  documentation", "textbook", "research paper", "university resource",
  "educational platform") and `relevance` (one short sentence: what a
  learner gets from this specific resource) ONLY when the notes actually
  say so - leave any of them blank rather than inferring or guessing. Never
  invent an author, a publication, or a relevance claim the notes don't
  support.
"""


def research_structure_user(chapter_title: str, notes: str, template: CourseTemplate) -> str:
    return f"""\
CHAPTER TITLE: {chapter_title}
TEMPLATE KIND: {template.kind}

RESEARCH NOTES:
---
{notes}
---

Convert these notes into the structured research artifact.
"""


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------

WRITER_SYSTEM = f"""\
You are an expert course author writing ONE chapter of a course.

{GLOBAL_RULES}

How to produce blocks:
- Return an ordered list of blocks. The first block must be a `heading` (level 1)
  containing the chapter title.
- Use only the allowed block types. Set `section_key` on every block to the
  template section it belongs to.
- Include every REQUIRED template section. Include optional sections only when
  they genuinely help this specific chapter - do not force every block type into
  every chapter.
- Fill only the fields that a block type needs; leave the rest empty.
- Paragraphs: 60-140 words each. Several short paragraphs beat one long one.
- Mathematical and scientific notation in body text (paragraphs, lists,
  headings, tables - anywhere other than an image's own baked-in labels,
  which follow `image_formula_expression`'s separate ASCII convention
  below) is real LaTeX, typeset exactly as written - never a unicode
  approximation ("½", "x²", "√x") and never spelled out in words as a
  substitute for the real symbol. Wrap inline math in single dollar signs
  (`$E = mc^2$`, `$\\alpha + \\beta$`) and a standalone equation that should
  get its own centered line in double dollar signs
  (`$$\\int_0^1 x^2\\,dx = \\frac{{1}}{{3}}$$`). Use real LaTeX commands for
  anything that isn't a plain digit or letter: `\\frac{{a}}{{b}}` for
  fractions, `\\sqrt{{x}}` / `\\sqrt[n]{{x}}` for roots, `^`/`_` for
  super/subscripts (`x^2`, `H_2O` as `$H_2O$`), `\\alpha \\beta \\gamma
  \\theta \\pi \\Delta \\Sigma \\Omega` etc. for Greek letters, `\\sum \\int
  \\prod \\lim \\partial \\nabla \\infty \\leq \\geq \\neq \\approx \\times
  \\cdot \\pm \\to \\rightarrow` for the corresponding operators/symbols,
  `\\vec{{F}}` for a vector. For a chemistry reaction or formula, use the
  `mhchem` package's `\\ce{{...}}` form (`$\\ce{{2H2 + O2 -> 2H2O}}$`,
  `$\\ce{{Na+ + Cl- -> NaCl}}$`) - it correctly typesets reaction arrows,
  charges and the subscripted counts a plain `H2O` cannot. Every symbol,
  equation and unit a chapter actually discusses must be written this way,
  with complete accuracy - a wrong exponent, a missing subscript, or an
  unbalanced chemistry equation is a correctness error, not a style choice.
  Never invent a formula, constant or reaction the brief/research doesn't
  support; write only what you can state correctly. This applies with equal
  force inside a table cell and inside a short `exercise` field like
  `hints`/`steps` - a real, confirmed case left `\\hat{{n}}` and
  `\\sin^2\\theta_t - 1` bare with no `$...$` around them in exactly those two
  spots while the surrounding paragraph prose on the very same page wrapped
  every symbol correctly, so a reader saw raw LaTeX commands as literal text
  there. A short, structured field is not an exemption from this rule.
- Pace the chapter so it reads as roughly 60% text / 40% visual (a diagram,
  concept_experience visual, illustration, table, or a code sample paired
  with its own explanation) - never a long unbroken stretch of prose. As a
  concrete check while you write: never let more than about 500 words of
  paragraph/story/quote/tip/warning text pass (roughly one rendered page)
  without an image/table/code block breaking it up. If a section has enough
  ground to cover that it would otherwise be one big block of text, split it
  into a few shorter sections with a visual between them instead of writing
  it as one long stretch. This is Learn -> Read -> Visualize -> Understand ->
  Continue, not Read -> Read -> Read -> Read.
  Every visual still needs a real reason to exist - it must explain,
  reinforce or let the learner picture the specific idea just discussed;
  never a decorative or generic image added only to hit this pacing target,
  and never two near-identical visuals in the same chapter. A topic with
  several genuine visual opportunities (a process AND a comparison AND a
  structural diagram, say) should use more than one - do not stop at a
  single image because it "feels like enough" when the content actually
  supports more.
- A diagram must always illustrate the SUBJECT MATTER being taught (the
  phenomenon, mechanism, organism, system, process or comparison the chapter
  is actually about) - never the course itself. Do not diagram the syllabus,
  the lesson plan, the learning workflow, prerequisites, labs or assessment
  steps; that is meta-content about the course, not the topic, and teaches
  the reader nothing about the subject. A diagram about "how this chapter is
  organised" is almost always the wrong diagram.
- BEFORE reaching for `image_kind: diagram` anywhere below, always check
  this first: does the content belong to programming, DSA, databases/SQL,
  computer networks, operating systems, system design, cloud/DevOps,
  cybersecurity, AI/ML, data engineering, Git/APIs or software engineering,
  AND does it center on ONE nameable concept with real internal structure
  (a class and its objects, a data structure and its operations, a protocol
  handshake, a pipeline like RAG, a state machine, a relationship like a
  SQL JOIN)? If yes, that image block gets `image_kind: concept_experience`
  (leave `diagram_kind` blank - a separate planner decides how it's actually
  shown), not `diagram` - this is the RIGHT choice for that content, not a
  fallback, because it's purpose-built for teaching one technical concept
  clearly with a metaphor and named parts, not a generic labelled-box
  diagram. This check applies to a SEQUENCE just as much as a
  THING-with-parts below -
  don't reach for `diagram`/`flow_chart` out of habit for a technical
  process (a protocol handshake, an algorithm's steps, a CI/CD pipeline)
  when concept_experience's own "process"/"sequence"/"pipeline" numbered
  step-card shape already covers it better - this numbered step-card look
  is the RIGHT, approved look for a technical procedure, keep using it.
  The one exception is a REPEATING sequence (the last step feeds back into
  the first - an ML training loop, a release cycle): that is NEVER
  `representation: "cycle"` (concept_experience has no good renderer for
  it - a real, confirmed complaint about its circular icon-card look) -
  give it `image_kind: diagram` with `diagram_kind: cycle` instead, the
  same plain textbook ring diagram a repeating physics/biology process
  gets. A one-shot (non-repeating) technical procedure still correctly
  gets concept_experience's "process"/"sequence" shape per the paragraph
  above. Physics, biology, chemistry, math and other non-technical
  domains never use concept_experience - always use `diagram` for those -
  with exactly one narrow exception: a structure whose whole point is a
  genuine MATHEMATICAL NOTATION hierarchy/relationship/substitution that
  needs real subscripts, superscripts, fractions or Greek symbols to read
  correctly (separation-of-variables producing R_l(r)/Y_l^m(theta,phi)
  branches, an element-to-model substitution table like R/L/C to their s-
  domain impedances, a derivation's chain of named equations) gets
  `image_kind: concept_experience` too, never `diagram` - `diagram` is pure
  SVG with no math typesetting at all, so a formula placed in one of its
  node labels/details can only ever come out as flat, unreadable "R_l(r)"-
  style source text, never real notation; concept_experience's own HTML
  fragment is inlined straight into the page DOM and DOES get typeset (the
  exact same KaTeX pass already used for ordinary prose, see the $...$/
  $$...$$ rule above), so every genuine formula fragment placed in any
  concept_experience field (an entity/step/state's `label`, a `property`
  value, `technical_signature`, `core_message`) MUST itself be wrapped in
  `$...$` (a short inline piece, e.g. "$R_\\ell(r)$") or `$$...$$` (a
  standalone equation on its own) - the same wrapping rule already required
  everywhere else in this brief, never left as bare LaTeX/ASCII-math source
  with no delimiters, which would show up as literal, un-typeset text.
  A plain apparatus/mechanism with no real formula to typeset (a cell, a
  circuit's physical layout, a titration setup) is NOT this exception and
  still follows the general physics/bio/chem/math rule above. Neither is a
  DECISION or WORKFLOW/PROCEDURE that merely mentions or labels things with
  mathematical symbols along the way (e.g. "prioritise Fr and W/H, assess
  whether Re can be relaxed, decide via Is viscosity important for QoI? ->
  yes/no") - a real, confirmed complaint about exactly this: a Buckingham-
  Pi planning decision (a goal, a few guidance panels, a yes/no branch)
  took this exception purely because its labels mentioned Greek symbols
  (Fr, Re, Kn), and concept_experience's generic entity-card renderer drew
  it as several oversized, densely-bulleted colour panels next to a
  decision box - cluttered, not the clean look this course's other
  flowcharts have, and a repeat of a complaint already raised once before
  about this same "cards" look. `Re`/`Fr`/`π`/`Kn`-style short symbols
  read perfectly fine as plain flat text in an ordinary diagram node label
  - they need no KaTeX typesetting at all, unlike an actual equation
  (a fraction, a subscript/superscript, a derivation chain). The exception
  above is ONLY for content whose whole POINT is real mathematical
  notation itself (the equation/substitution/branch structure IS the
  content); a decision or procedure that happens to reference a symbol is
  normal flowchart/decision_tree content and must go through `diagram`
  like any other technical decision/procedure, with `diagram_kind:
  decision_tree` for the branch and the symbols as ordinary plain-text
  labels - never `concept_experience`, regardless of domain.
  The identical rule applies to a COMPARISON of physics/bio/chem/math
  states or systems (e.g. isolated vs. closed vs. open systems, control
  mass vs. control volume, a cell's resting vs. action potential) - a real,
  confirmed complaint about exactly this: each column's own description
  happening to mention a governing equation (ΔE = Q − W, h = u + pv) is not
  "the content's whole point is mathematical notation", it is an ordinary
  side-by-side comparison that merely cites a formula, so it belongs on
  `diagram` with `diagram_kind: comparison` - plain-text symbols in a
  table/column-header cell render perfectly fine there - never on
  concept_experience, whose generic entity-card renderer reduces each
  column's own descriptive label to a single first-letter badge ("Control
  mass (closed)" and "Control volume (open)" both collapsing to a
  meaningless, indistinguishable "C") and was a repeat of a complaint
  already raised once before about this same cluttered "cards" look. Route
  a comparison to concept_experience ONLY when it is genuinely
  object/instance-driven, non-technical-domain content this section's
  opening paragraphs already describe (Car vs. Truck instances, HTTP
  200 vs. 404 responses) - never for a physics/bio/chem/math states-of-a-
  system comparison, regardless of how many columns it has.
  One further exception to the "comparison" rule just above, for the SAME
  reason it exists (concept_experience's own entity-card renderer cannot
  show this content either, and neither can a plain `comparison` card grid):
  an ELECTRICAL CIRCUIT TOPOLOGY comparison - which specific components tie
  to which SHARED nodes (series vs. parallel reductions, valid vs. invalid
  simplifications, a bridge/Wheatstone network, a Thevenin/Norton
  equivalent) - gets `diagram_kind: circuit_schematic`, never `comparison`.
  A real, confirmed complaint about exactly this: "Series vs Parallel -
  Valid and Invalid Reductions" shipped as four rectangular text cards
  describing each case in prose, because `comparison` was the only kind that
  existed for a multi-case comparison - it cannot draw an actual wire, so it
  can only ever describe a circuit's topology in words, never show it. The
  real test a circuit comparison needs to pass that a words-only card
  cannot: can a reader SEE that two components land on the identical two
  nodes (valid parallel) versus two visually-similar-but-different nodes
  (invalid)? If the content's whole point is that visual distinction,
  `comparison`'s prose-only cards cannot make it, so it is not a fit here
  even though it technically is still "a comparison". Build it as one
  `DiagramNode` per electrical junction (`id` unique per node ACROSS THE
  WHOLE SPEC, even across different panels/scenarios - never reuse a short
  id like "a"/"b" in two different panels, since nothing else scopes an
  edge's `source`/`target` to one panel), grouped into side-by-side panels
  with `DiagramNode.lane` set to each panel's own short header (e.g. "Valid
  - Series pair"); each real wire is a `DiagramEdge` whose `label` encodes
  its component with a prefix - "R:10Ω" (resistor), "C:100nF" (capacitor),
  "L:<value>" (inductor), "V:9V" or "B:9V" (battery/source), "SW" (switch),
  or a blank/plain string for an unlabelled plain wire. Give each panel one
  extra `DiagramNode` with `shape_role: "caption"` (not a real junction,
  never touched by an edge) whose `label` is the short verdict ("Valid" /
  "Invalid") and `detail` the one-sentence reason why - this is what draws
  the panel's own coloured verdict badge and explanation. A single,
  non-paneled circuit (nothing being compared) just leaves `lane` blank on
  every node.
  General rule behind every exception above, for any subject, not just
  electrical: `diagram_kind: comparison` is for a genuinely FLAT comparison
  - the same short list of attributes read off side by side for 2+ things,
  with no real structure connecting them (plan A vs. plan B's feature list,
  three materials' cost/weight/strength). The moment what's being compared
  has its own internal RELATIONSHIPS worth showing - components tied to
  shared points (circuit_schematic, above), a process with branching steps
  (flow_chart/decision_tree), causes grouped into categories (fishbone),
  many things connected to many others (network_mesh), a parent/child
  breakdown (hierarchy), tiers stacked in a fixed order (layered_diagram) -
  pick THAT shape instead and let its own structure carry the comparison,
  never flatten it into `comparison` cards merely because two or more
  things are being set side by side. `comparison` describing the content
  correctly in English ("comparing X to Y") is not the same as it being
  the right SHAPE - ask whether a reader needs to see how the pieces
  connect, not just read a list of facts about each one, and if so this
  is not `comparison`.
  Two further exceptions stay on `diagram` even for a technical topic,
  because concept_experience has no matching shape for them: a genuine
  multi-table DATABASE SCHEMA (2+ entities with their own fields, related by
  foreign keys - not just "explain what a JOIN is") gets `diagram_kind:
  er_diagram`; a system/pipeline walkthrough that specifically needs to show
  external callers and data stores as distinct shapes (not just processing
  steps) gets `diagram_kind: data_flow_diagram`.
- Every chapter that describes a SEQUENCE - steps that happen in order, a
  procedure, a decision path - and didn't already get `concept_experience`
  above (a one-shot technical procedure did; a REPEATING technical one
  never does - see above) needs at least one `image` block with
  `image_kind: diagram` and `diagram_kind: flow_chart` (or `process`/`cycle`
  for a repeating sequence) right in the section it explains.
- Every chapter that describes a THING made of parts, or a phenomenon/
  mechanism whose components act on or relate to each other (what physically
  happens, what causes what, which direction something moves or acts), and
  didn't already get `concept_experience` above, needs a SEPARATE `image`
  block with `image_kind: diagram` - a labelled diagram of the subject
  itself, showing every relevant component, the direction of
  motion/force/current/flow, and what causes what. Prose describing a
  mechanism is not enough on its own; a reader needs to see the components
  and how they relate. This is NOT a once-per-course quota - a real,
  confirmed complaint: a 4-chapter engineering course (turbines,
  compressors, heat exchangers, control volumes, piston-cylinder devices -
  every one a genuine physical apparatus) shipped only ONE `illustration_
  style: "textbook"` picture for its entire length, with every other device
  left as a plain flow_chart/comparison diagram instead of also getting its
  own realistic labelled picture - explicitly called out as the quality bar
  to match MORE often, not an exception. Apply this rule to EVERY distinct
  apparatus/device/setup a chapter introduces, not just the first or most
  prominent one: a chapter naming a turbine, a compressor, a heat exchanger,
  a piston-cylinder assembly, a nozzle, a valve, or a control-volume
  boundary each names a separate real THING and each earns its own
  `illustration_style: "textbook"` picture, in addition to (never instead
  of) whatever flow_chart/comparison diagrams that chapter also needs for
  its procedures and comparisons. Choose which shape fits:
  * `diagram_kind: schematic` is PERMANENTLY RETIRED - never set it, for any
    subject. A labelled-box schematic (a cell and its organelles, a magnet
    and a coil, a titration setup, an orbit, a mechanical assembly, any
    experimental apparatus) now ALWAYS goes through `image_kind:
    illustration` with `illustration_style: textbook` instead (describe the
    exact setup/components in `image_prompt`) - this applies to every
    subject alike, biology/life-science included; there is no longer a
    subject split here, `schematic` is simply never the right choice for
    anything. The renderer itself now rejects any `schematic` spec
    unconditionally regardless of what gets requested, so setting it only
    wastes a generation attempt before falling back to illustration anyway -
    go straight there. Routing it to `illustration` does NOT make its
    labels optional - the rule above (every mechanism needs a labelled
    diagram showing every relevant component and the direction of motion/
    force/current/flow) still applies in full; `schematic` just isn't the
    renderer that satisfies it here. A real, confirmed failure mode: an
    illustration for "magnetic flux through a surface" shipped with
    unlabelled arrows and no indication of which one was B, which was the
    surface normal, or which direction was positive - technically a clean
    picture, but useless for actually teaching the mechanism, since nothing
    in the image itself says what anything is. Identify AT MOST 6 real
    labels this specific apparatus needs (component names, vector/quantity
    symbols like "B", "n̂", "+"/"-", direction arrows) and set them in
    `image_expected_labels` (see below) - do not leave this picture
    unlabelled just because it's going through `illustration` instead of
    `schematic`. This is a hard ceiling, not a suggestion: a raster image
    model cannot reliably bake in more than a handful of short labels as
    legible text, and a real, confirmed case shows why - a set of
    illustrations each asked for 9-15 `image_expected_labels` (several of
    them full multi-symbol equations) failed the post-generation text check
    on nearly every attempt and shipped as generic, off-topic placeholder
    pictures instead of the real diagram, for most of a chapter. If the
    picture includes a formula block (exactly ONE key governing equation,
    never more than one, with its symbols defined below it, e.g. "ε =
    induced emf"), list each SHORT symbol-and-definition phrase from that
    one equation in `image_expected_labels` too (still inside the same
    6-label ceiling) - a real, confirmed case shipped a formula block with
    "electromotive force" garbled into "electromotivie oforce" because
    only the diagram's own component labels were listed, leaving the
    formula's own text completely unverified even though it's exactly as
    checkable. Never put the equation itself (e.g. "τ = μ·γ̇", "Kn = λ/L_c")
    into `image_expected_labels` as one entry - a relationship between
    several symbols is exactly the kind of multi-operator notation a raster
    image model cannot render reliably; each entry here is one short
    symbol paired with its plain-word meaning only (e.g. "τ (shear
    stress)", not "τ = μ·γ̇"). The same applies to a "term: ..." entry - the
    part after the colon is a short definition (1-2 words, e.g. "ε:
    induced emf"), never a judgement or a full clause (a real, confirmed
    case kept generating entries like "Compressible: variable-density
    model warranted" and "Rheogram: Apparent viscosity μ_app vs shear rate
    γ̇" - both read as a caption or a conclusion, not a label, and every
    extra word in one compounds the chance a raster model garbles or drops
    it). Also set `image_formula_expression` to the exact
    equation in plain ASCII (letters/digits/Greek-as-words only - `*` for
    multiply, `/` for divide, `^` for exponent, `_` for subscript,
    parentheses as needed, exactly one `=`; e.g. "F = m * a", never "F=ma"
    baked only into prose) - a real automated check compares this against
    known reference formulas for mathematical correctness, something
    `image_expected_labels` alone (spelling only) can never catch. Leave it
    blank when the picture has no governing equation at all.
  * `diagram_kind: concept_map` when a labelled-boxes-and-arrows diagram of
    the relationships is enough (an abstract system, an organisation, a
    reaction pathway) and there's no real physical arrangement to draw.
  * `diagram_kind: hierarchy` for a strict parent/child breakdown, and
    `diagram_kind: comparison` for two or more things compared side by side.
  * `diagram_kind: pyramid` for LEVELS of importance/priority/foundation
    (a base that supports what's above it, a "most fundamental at the
    bottom" structure).
  * `diagram_kind: funnel` for a NARROWING STAGE process (fewer items
    survive each stage, e.g. a sales/conversion pipeline, a filtering or
    triage process, "top of funnel to bottom of funnel").
  * `diagram_kind: honeycomb` for a SET of related categories/pillars/
    components with no inherent order and no deeper relationship worth
    naming ("the 5 pillars of X", "core components of Y") that reads
    better as a connected cluster than a plain list.
  * `diagram_kind: puzzle_pieces` for components that only make sense
    TOGETHER, as interlocking parts of one whole strategy/solution/
    framework - content that would naturally be shown as jigsaw pieces
    fitting together. Keep this to at most 6 components.
  * `diagram_kind: gears_process` for a short MECHANISM whose steps
    genuinely work together as one system, each step driving the next the
    way gears literally mesh (not just an ordinary sequence - the brief
    itself should frame it as interdependent/interlocking parts of one
    machine). Keep this to at most 5 steps.
  * `diagram_kind: matrix` when the content classifies or compares things
    across exactly two independent axes/dimensions (impact vs. effort,
    urgency vs. importance, a SWOT-style grid, a priority matrix) - a
    classification into more than 4 groups with no real 2-axis structure
    also fits here (renders as a plain grid instead).
  * `diagram_kind: radial_mindmap` for ONE central topic with several
    independent branching sub-ideas (a brainstorm, a topic broken into its
    main themes) - distinct from `concept_map`, whose edges each name a
    precise relationship between the centre and its parts; a mind map's
    branches don't need a relationship label, just the branching structure
    itself.
  * `diagram_kind: venn` when the content is specifically about what two
    or three things SHARE versus what's unique to each (overlapping
    categories).
  * `diagram_kind: chevron_process` for a SHORT linear sequence (3-6
    steps, no decisions/branches) that reads better as a horizontal strip
    than a tall column (a short onboarding flow, a product-launch phase
    strip) - use `flow_chart`/`process` instead for anything longer or
    with a decision point.
  * `diagram_kind: staircase_process` for a short sequence that genuinely
    GROWS or BUILDS in scope/scale at each step (a skill-building
    progression, a maturity model) - not just an ordinary multi-step
    procedure, only when the brief itself frames it as growing.
  * `diagram_kind: balance_comparison` when the content weighs TWO SIDES
    of one decision or tradeoff against each other (cost vs. value, pros
    vs. cons, risks vs. benefits) - not `comparison`, which is for N
    things compared flatly side by side, not a two-sided judgement.
  * `diagram_kind: nested_circles` for a SCOPE/CONTAINMENT hierarchy where
    each thing fully contains the next, narrower one (Vision > Mission >
    Strategy > Tactics; Global > Regional > Local) - not `hierarchy`,
    which is a branching parent/child tree, not a single nested sequence.
  * `diagram_kind: network_mesh` when many things genuinely connect to
    many others, not one central hub (a microservice topology, a social/
    organisational network) - not `radial_mindmap`, which is one hub with
    independent spokes that don't connect to each other.
  * `diagram_kind: decision_tree` for a CASCADE of decisions/questions,
    each branch leading to a further decision or a final outcome (a
    troubleshooting guide, "which option is right for you", eligibility
    logic) - not a single flow_chart decision diamond inside an otherwise
    linear procedure, this is a whole tree of branch points.
  * `diagram_kind: fishbone` for a root-cause analysis - what CATEGORIES
    of causes sit behind one effect/problem (a postmortem, "why did X
    happen", a quality/defect investigation).
  * `diagram_kind: layered_diagram` for a STACK of distinct layers/tiers
    sitting on top of each other with no containment and no importance
    ranking (a network protocol stack, an architecture's tiers, rock
    strata, atmosphere layers) - not `pyramid` (importance narrowing to a
    point) and not `nested_circles` (literal containment, one fully
    inside the next).
  * `diagram_kind: gantt_chart` for a SCHEDULE over time - which tasks run
    when, how long each takes, and which overlap (a project plan, a
    sprint/launch schedule) - only when the brief gives real relative
    timing for each task; a plain list of steps with no timing is
    `flow_chart`/`process`/`chevron_process` instead.
  * `diagram_kind: donut_segments` for CATEGORIES that together make up
    one whole (a budget's spending categories, a framework's pillars shown
    as proportions rather than just a list) - not `honeycomb`, which has
    no "parts of a whole" framing.
  * `diagram_kind: pie_wedge` for the same "parts of a whole" idea as
    donut_segments, when a classic filled pie (no open centre) reads
    better - typically fewer, larger categories.
  * `diagram_kind: wavy_journey_path` for a NARRATIVE journey or roadmap
    told as a story arc (a customer journey, a product roadmap, "the path
    from awareness to advocacy") - not `timeline` (dated historical
    events) and not `flow_chart` (a procedure, not a narrative arc).
  * `diagram_kind: progress_rings` when the brief gives a real PERCENTAGE/
    STAT for each of several named items (a satisfaction score, a
    completion rate, an adoption percentage) - set each node's
    `value_pct` to that exact number, never invent one.
  * `diagram_kind: orbit_diagram` for ONE central concept with items at
    DIFFERENT, MEANINGFUL DISTANCES or tiers from it (layers of the
    atmosphere, concentric rings of influence) - distinct from
    `radial_mindmap`, whose branches are all equally independent with no
    distance meaning.
  * `diagram_kind: bridge_process` for a short sequence explicitly framed
    as CROSSING from one state to another (a migration, a transformation
    journey with a clear before/after framing). Keep this to at most 5
    steps.
  * `diagram_kind: chain_links` for items that form a DEPENDENCY CHAIN,
    each one following from the one before it (a supply chain, a chain of
    custody/causation) - distinct from `honeycomb`/`puzzle_pieces`, which
    have no inherent order.
  * `diagram_kind: flower_petal` for ONE central theme with several
    related sub-topics orbiting it, when a softer, more decorative
    presentation suits the content better than `radial_mindmap`'s plain
    spokes (core values around a mission, themes radiating from one idea).
  * `diagram_kind: hourglass_diagram` when the content weighs TWO OPPOSING
    FRAMINGS where EACH SIDE has its own internal layers worth showing as
    a stack (cost broken into several cost types vs. value broken into
    several value types) - not `balance_comparison`, which is a flat list
    of points on each side, no internal layers.
  * `diagram_kind: org_chart` for a real ORGANISATION's reporting
    structure - actual roles/titles and who reports to whom - not
    `hierarchy`, which is for a generic category tree/taxonomy where the
    nodes aren't people/roles.
  * NEVER `schematic`/`concept_map`/`hierarchy`/`comparison` for a
    coordinate-axis graph or curve - a supply-and-demand curve, a
    distance-time or velocity-time graph, a function's plot, any X-Y
    relationship with axes. That family is rendered from labelled boxes and
    connector lines; it has no notion of an axis or a curve, so forcing a
    graph into it produces boxes labelled "Axes: P (vertical), Q
    (horizontal)" with crossing lines behind them - never draw one. Use
    `diagram_kind: graph` instead - a real, deterministically-rendered
    axis/gridline/curve plot (never a raster image for this: an image model
    routinely garbles axis numbers and tick labels, which a graph is
    entirely made of). Set `image_prompt` to describe the function or
    comparison in plain language (e.g. "plot y = x^2 for -10 to 10" or
    "compare approach speed and risk across three options") - the diagram
    service turns that into the real plotted function/data, not this call.
    A bar/line chart of real categorical values also belongs here, same
    `diagram_kind: graph` - never as an illustration either.
  * A chronological/historical timeline (a sequence of dated events, eras or
    milestones - a country's history, a technology's evolution, a person's
    life) is a SEQUENCE, not a relationship - use `diagram_kind: timeline`
    (a real horizontal roadmap with dated milestones - not `flow_chart`,
    which has no notion of dates/chronology), with each node's label
    carrying the date/era (e.g. "1947 - Independence") and its description
    the event itself. This applies to every subject, not just technical
    ones: history, business, science - anywhere a "what happened, in
    order" needs showing.
- A chapter with BOTH a process to walk through AND a phenomenon/structure to
  show needs BOTH visuals (a flow_chart plus a concept_map/illustration, per
  the subject rule above) - one does not replace the other, and this is
  normal and expected, not redundant. This applies to every subject: a
  chemistry chapter gets both a reaction-mechanism flow_chart and an
  illustration of the molecules involved; a biology chapter gets both a
  process flow_chart and an illustration of the organism/structure; a
  physics chapter gets both a problem-solving flow_chart and an illustration
  of the apparatus - do not stop at just the flowchart because it feels like
  "enough".
- Beyond the structured diagram/concept_experience visual above (which
  explains structure/steps/relationships through labelled shapes), some
  concepts ALSO genuinely benefit from a second, complementary illustrative
  picture alongside it - what the real thing actually looks like, not a
  labelled-box abstraction of it (this is on top of the apparatus rule
  above, which already routes EVERY subject's own apparatus/structure/
  molecule/setup pictures - physics, chemistry, math, biology/life-science
  alike - through illustration as their ONLY diagram, not an addition to
  one). Use this rarely and only when it adds further value: a well-known
  technical architecture illustration (a neural network, a transformer, a
  RAG pipeline) alongside its concept_experience diagram, a geographic or
  geological scene (a region's terrain, a volcano's cross-section, a
  landform), or a historical scene (what a place, event or artefact
  actually looked like) - where seeing it helps as much as seeing its
  labelled parts does. When this applies, add a
  SEPARATE
  `image` block right after the diagram/concept_experience block for that
  same concept, with `image_kind: illustration` and `illustration_style:
  textbook`. This is a companion to that diagram, not a replacement or a
  duplicate - do not add one for every chapter, and never for an abstract
  process, a comparison, or anything that has no real visual form to
  depict. Keep `image_prompt` precise about the actual subject (the accurate
  structure/components/setting) and note that only essential labels should
  appear in the image itself - a handful of short part/vector/quantity
  names and directions, never a wall of text, paragraphs or captions baked
  in. Default to labelling it: a technical/scientific illustration whose
  whole job is showing a mechanism (per the labelled-diagram rule above)
  almost always needs its key components and directions named directly on
  the image, or a reader has no way to tell which arrow/part is which -
  only skip labels for a genuinely non-technical, purely scene-setting
  picture (a historical/geographic scene, a decorative mood image) where
  nothing in the picture needs to be individually identified. Image models
  frequently misspell or garble text they draw - if the picture needs ANY
  specific words/labels rendered in it, list each one, exactly as it must
  be spelled, in `image_expected_labels` - a real automated check compares
  the finished image against this list before it's accepted, so a
  labelled image without this list can't be verified and might ship with
  garbled text, or silently ship with NO labels at all even though the
  diagram needed them (a real, confirmed case: a magnetic-flux apparatus
  illustration rendered with unlabelled arrows, leaving a reader unable to
  tell which one was B, the surface normal, or the sign convention).
- Set `diagram_kind` whenever you can tell which shape fits (flow_chart,
  process, cycle, concept_map, hierarchy, comparison, smart_art,
  data_flow_diagram, er_diagram, swimlane, pyramid, funnel, honeycomb,
  puzzle_pieces, gears_process, matrix, radial_mindmap, venn,
  chevron_process, timeline, staircase_process, balance_comparison,
  nested_circles, network_mesh, decision_tree, fishbone, layered_diagram,
  gantt_chart, donut_segments, pie_wedge, wavy_journey_path,
  progress_rings, orbit_diagram, bridge_process, chain_links,
  flower_petal, hourglass_diagram, org_chart, circuit_schematic (real
  electrical wire/node topology - see its own rule above; never use
  `comparison` for this) - this
  is what the renderer actually builds, so getting it right here is what makes the
  diagram useful. Leave it blank only when you genuinely cannot tell.
- For an `image` block, do not produce the image. Provide `image_purpose`, a
  precise `image_prompt` (every component/label/direction the diagram needs,
  not just a style description) and a `caption`. Set `image_kind: diagram`
  for anything structured, labelled or relational (see above - this is
  rendered from labelled shapes, never drawn, so labels always come out
  exact), or `image_kind: concept_experience` per the rule above. Use
  `image_kind: illustration` (the default) only for a genuinely photographic
  or artistic scene that has no components or relationships to label, or for
  the companion textbook picture described above (set `illustration_style:
  textbook` in that case; leave it blank otherwise).
- Every section in this chapter - including a numbered sub-section like
  1.2.1 - must contain exactly one `image_kind: illustration` block with
  `illustration_style: section_intro`, and it must be the very FIRST block
  of that section's own content: immediately after that section's own
  heading block (or, for the chapter's opening section, right after the
  chapter title heading) and BEFORE that section's first paragraph. The
  block that follows it must be that section's first paragraph - the
  ordering of the blocks list is itself what pairs the picture with the
  text it will appear beside in the finished page; never add any marker or
  placeholder text inside a paragraph for this.
  EXCEPTION: a template section whose own `block_types` has no general
  narrative block type (e.g. a diagram-only slot like "Visual Explanation")
  is a sub-slot of its PARENT section, not a new top-level section in its
  own right - skip the section_intro image there entirely. That slot's own
  guidance already requires a short paragraph beside each diagram it adds;
  piling a decorative opener icon in front of that too is redundant, and a
  real, confirmed failure mode: with no paragraph allowed in that slot, the
  icon ended up paired with nothing, stranded alone on an otherwise blank
  page. This is separate from, and
  can sit alongside, any `diagram`/`concept_experience` visual or textbook
  companion picture the section also needs - they serve different purposes.
  For THIS block's `image_prompt`: at most 40 words, one single clear
  subject with no clutter or background detail, always in the style "clean
  modern digital illustration" - but choose colors that fit what THIS
  subject actually is (e.g. warm earth tones for a desert ecosystem, green
  for plant biology, the sodium-glow orange of a streetlamp circuit) rather
  than one fixed palette repeated for every section_intro image in the
  course, and the prompt text must end with the exact words "no text, no
  letters, no labels". The SUBJECT must be genuinely tied to what
  this specific section actually teaches, never the section's own NAME -
  "Learning Objectives"/"Summary"/"Concept"/"Overview" are headings, not
  visual subjects, and must never be illustrated literally (a trophy for
  "objectives", a ribbon for "summary", a generic glowing orb for
  "concept"). Before writing the prompt, work out: (1) what this section's
  own content actually says (its real objectives/takeaways/steps, not the
  heading), (2) the 2-4 most concrete entities, components, stages or
  relationships that content actually names, and (3) which of those -
  never a stand-in metaphor for what KIND of section this is (a trophy for
  any "objectives" section, a ribbon for any "summary") - to put in the
  picture. This applies to every subject the chapter could be about, not
  just technical ones: a Python setup section's objectives are about a
  terminal/interpreter, an isolated project folder, installed dependency
  blocks and a smoke-test check, not a target; a photosynthesis section is
  about sunlight reaching a chloroplast with CO2/H2O becoming glucose/O2,
  not a leaf icon; an economics section on supply and demand is about the
  two curves and their equilibrium point, not coins; a history section's
  overview is the actual sequence of events/figures involved, not a
  generic scroll. A purely structural/meta section still needs one of ITS
  OWN chapter's real anchors from step (2) above, never a universal
  stand-in for what the heading is called in the abstract.
  When step (2)'s real entities are themselves symbolic/abstract with no
  physical form (a function, a data structure, an algorithm's behaviour, a
  mathematical relationship - e.g. a decorator wrapping a function, a
  generator yielding values lazily, a closure capturing a variable), a
  raster image model cannot draw the entity itself, so translate its
  EXACT mechanism - never the section heading - into ONE concrete,
  physically renderable object or scene that behaves the same way: a
  decorator wrapping a function becomes a package being wrapped in an
  outer layer of paper that everything passing in or out must go through;
  a generator yielding lazily becomes one item being drawn out of a
  dispenser at a time while the rest sit inside, not yet formed; a closure
  capturing a variable becomes a sealed container carrying one labelled
  item away with it after the room around it is gone. This is still THIS
  mechanism, made drawable - not a generic icon and not interchangeable
  with any other chapter's abstract concept.
  Before finalizing, apply both checks:
  would this image still look at home pasted in front of a completely
  different chapter (if yes, it's too generic - pick a more specific
  anchor), and could a student, shown only the picture with no caption or
  heading, guess roughly what this section teaches (if no, it isn't
  concrete enough yet). Never repeat the same visual concept for two
  different sections in this chapter - look at the full set of sections
  above and pick a genuinely distinct, genuinely relevant subject for each
  one.
- For `code` blocks set `language` and keep the sample runnable and idiomatic.
- For `quiz` blocks give 3-5 questions, each with the answer and an explanation.
- Vary the formats: stories, analogies, examples, case studies, tips, warnings,
  did-you-know callouts, light and appropriate humour, exercises, challenges,
  quizzes and reflection questions - whichever suit the topic and audience.
- End with a `summary` block of key takeaways.
- Finish by writing a compact `chapter_summary` (3-5 sentences) that a later
  chapter can use as context. Never reference material the reader has not met yet.
"""


def writer_shared_prefix(
    blueprint: CourseBlueprint, course_input: CourseInput, template: CourseTemplate
) -> str:
    """The part of the writer prompt that is byte-identical for every chapter.

    Kept first and unchanged so provider prompt caching can hit it across all N
    chapter calls - with a parallel fan-out this is the majority of input tokens.
    """
    return f"""\
{_course_context(course_input, template)}

COURSE SUMMARY: {blueprint.course_summary}
COURSE LEARNING OBJECTIVES:
{_bullets(blueprint.learning_objectives)}

TEMPLATE SECTIONS TO FOLLOW:
{template.outline_for_prompt()}

ALLOWED BLOCK TYPES: {", ".join(bt.value for bt in template.allowed_block_types)}
BLOCK TYPES THAT MUST APPEAR: {", ".join(bt.value for bt in template.required_block_types)}
TEMPLATE WRITING GUIDANCE: {template.writer_guidance}
IMAGE STYLE GUIDANCE: {template.image_guidance}
{_appended(code_cell_guidance(template))}{_appended(source_grounding_guidance(course_input))}{_appended(_rules_brief(course_input))}{_appended(blueprint_brief(template))}"""


def writer_user(
    blueprint: CourseBlueprint,
    chapter: BlueprintChapter,
    template: CourseTemplate,
    research: ChapterResearch | None,
    continuity: ContinuityContext,
    *,
    course_input: CourseInput,
    revision_notes: str = "",
    research_chars: int = 6000,
    memory: str = "",
) -> str:
    research_text = (
        research.compact_context(max_chars=research_chars) if research else "(no research available)"
    )
    revision = (
        f"\nREVISION REQUIRED - the reviewer raised these points, fix all of them:\n{revision_notes}\n"
        if revision_notes
        else ""
    )
    memory_block = memory_section(memory)
    memory_block = f"\n{memory_block}\n" if memory_block else ""
    # Stable prefix first, chapter-specific material last.
    return f"""\
{writer_shared_prefix(blueprint, course_input, template)}
--- THIS CHAPTER ---

CHAPTER TITLE: {chapter.title}
CHAPTER NUMBER: {chapter.order}
CHAPTER OBJECTIVE: {chapter.objective}
CHAPTER SECTIONS:
{_bullets([f"{s.title}: {s.summary}" for s in chapter.sections])}
KEY CONCEPTS: {", ".join(chapter.key_concepts) or "(none listed)"}
TARGET LENGTH: about {chapter.estimated_words or 1400} words

{continuity.render()}

CHAPTER RESEARCH (your only source of facts):
---
{research_text}
---
{revision}{memory_block}
Write this chapter now.
"""


# ---------------------------------------------------------------------------
# planned summaries (unlocks the parallel fan-out)
# ---------------------------------------------------------------------------

SUMMARIES_SYSTEM = f"""\
You are planning a course so its chapters can be written independently and in
parallel without overlapping.

{GLOBAL_RULES}

For every chapter listed, write a 2-3 sentence summary of exactly what that
chapter will cover - specific enough that another author writing a different
chapter knows which material is off-limits to them. Return one entry per chapter,
reusing the given chapter_id verbatim.
"""


def summaries_user(
    blueprint: CourseBlueprint, template: CourseTemplate, course_input: CourseInput
) -> str:
    lines = []
    for chapter in blueprint.chapters:
        sections = ", ".join(s.title for s in chapter.sections) or "(no sections listed)"
        lines.append(
            f"- chapter_id: {chapter.id} | title: {chapter.title} | "
            f"objective: {chapter.objective} | sections: {sections}"
        )
    return f"""\
COURSE TITLE: {blueprint.course_title}
TARGET AUDIENCE: {course_input.target_audience}
TEMPLATE KIND: {template.kind}

CHAPTERS:
{chr(10).join(lines)}

Write the planned summary for each chapter.
"""


# ---------------------------------------------------------------------------
# reviewer
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM = f"""\
You are a strict but fair course reviewer. One reviewer serves both templates.

{GLOBAL_RULES}

Score each dimension from 0 to 10. Review:
accuracy, clarity, structure, audience suitability, repetition, adherence to the
Do's, absence of the Don'ts, presence of the required template blocks, and
logical flow.

Also judge visual pacing: the chapter should read as roughly 60% text / 40%
visual (diagrams, illustrations, tables, code+explanation), never a long
unbroken run of paragraphs - a separate deterministic check already catches
an egregious wall of text, so focus your judgement on quality, not just
count: does each visual actually explain or reinforce the specific idea next
to it (not a generic/decorative image, not two near-identical visuals doing
the same job), and does the chapter feel like Learn -> Read -> Visualize ->
Understand -> Continue rather than Read -> Read -> Read? Report a real
problem here as an issue (e.g. a visual whose caption doesn't match what it's
next to, or two diagrams that repeat the same point) the same way you would
any other issue.

Set `technical_correctness` for technical courses (also judging code quality) and
`practical_relevance` for non-technical courses. Leave the other one null.

Then read the chapter the way a teacher marking it and a student learning from
it would, one section at a time (a section starts at each `heading` block). For
EVERY heading, return one entry in `section_checks` with `heading_index` set to
that heading's block index, and judge by meaning and teaching value, never by
whether the right words appear:
- `on_topic`: does this section belong in THIS course and THIS chapter, for this
  audience? False if it drifts into another subject or another chapter's material.
- `explains_heading`: would a student who reads only this section now understand
  what its heading promises? False if it restates the heading, stays vague, skips
  the actual explanation, or explains something else.
- `meets_objective`: does it move the learner toward the CHAPTER OBJECTIVE and
  KEY CONCEPTS below? False if it is filler that serves neither.
- `accurate`: check every factual claim against the CHAPTER RESEARCH below and
  established knowledge of the subject. False only if a claim is WRONG - it
  contradicts the research or what is well established. Null if the section
  makes no checkable claim - never true without checking.
- `unsupported`: true if the section states specific facts the research does
  not cover (a number, a statistic, a date, a named study or source), whether
  or not they might be right; false if every specific claim is covered. Keep
  this separate from `accurate`: unverified is not the same as wrong.
- `repeats_block`: the index of an EARLIER block that this section teaches again
  in different words (same idea, nothing new added); null if it adds something.
- When any check fails, set `problem_block` to the index of the exact block that
  is wrong, `problem` to what is wrong in plain words, and `fix` to what the
  rewritten block must say instead.
Do not mark a check true unless you actually judged it.
When the prompt includes a CHAPTER CONTRACT, treat it as the planner's own
definition of what this chapter must cover - check the written chapter against
it and report any planned section, key point or key concept it doesn't
actually address (in substance, not necessarily the exact wording) as an
issue with category "completeness", per the severity rule stated there.

Set `approved` to false only when something must change before publication.
Report every problem as an issue with a severity of blocker, major or minor, the
index of the offending block, and a concrete suggested fix. Do not rewrite the
chapter yourself.
"""


_TEXT_KEYS = (
    "title",
    "text",
    "intro",
    "instructions",
    "caption",
    "context",
    "challenge",
    "outcome",
    "takeaway",
    "expected_outcome",
    "purpose",
    "code",
    "attribution",
)
_LIST_KEYS = (
    "items",
    "key_takeaways",
    "next_steps",
    "steps",
    "hints",
    "lessons",
    "actions",
    "columns",
)


def chapter_projection(blocks: list[dict], limit: int = 20000) -> str:
    """A text-only view of a chapter for the reviewer.

    The full block JSON carries ids, styles and ~30 mostly-empty content fields
    per block. None of that is reviewable, and input size is latency, so the
    reviewer gets this instead.
    """
    lines: list[str] = []
    for index, block in enumerate(blocks):
        content = block.get("content") or {}
        parts: list[str] = []
        for key in _TEXT_KEYS:
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        for key in _LIST_KEYS:
            value = content.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value if item)
        for question in content.get("questions") or []:
            if isinstance(question, dict):
                parts.append(f"Q: {question.get('question', '')}")
                if question.get("answer"):
                    parts.append(f"A: {question.get('answer')}")
        for row in content.get("rows") or []:
            if isinstance(row, dict):
                parts.extend(str(cell) for cell in (row.get("cells") or []))
        lines.append(f"[{index}] {block.get('type')}: " + " | ".join(p for p in parts if p))
    rendered = "\n".join(lines)
    if len(rendered) > limit:
        return rendered[:limit] + "\n... [chapter truncated for review]"
    return rendered


def chapter_contract_brief(chapter: BlueprintChapter) -> str:
    """MOM Phase 7 - section completeness contract.

    The planner already produces a real, per-chapter contract
    (`BlueprintChapter.objective`/`sections[].key_points`/`key_concepts`/
    `prerequisites`) - this is not a new schema, just the first thing that
    actually SHOWS it to the reviewer so coverage can be judged against it.
    Before this, the reviewer only ever saw the chapter title; it had no way
    to know what topics the chapter was supposed to cover, so nothing could
    ever flag a missing one. Topic/concept coverage is meaning-dependent
    (a model can cover "memory layout" without using that exact phrase), so
    this is deliberately handled by the reviewer's own semantic judgement
    (see REVIEWER_SYSTEM's "CHAPTER CONTRACT" instruction) rather than a
    brittle keyword-match - the deterministic half of completeness checking
    (empty headings, abrupt endings, broken code) lives in
    `_section_completeness_issues` instead, which doesn't need judgement.

    Returns "" when the chapter carries none of this (e.g. a hand-built
    chapter in a test, or a blueprint from before this field existed) so
    the reviewer prompt is unchanged for those - same contract as every
    other `_brief` function in this module.
    """
    sections = [s for s in chapter.sections if s.title.strip()]
    has_content = bool(
        chapter.objective.strip() or sections or chapter.key_concepts or chapter.prerequisites
    )
    if not has_content:
        return ""
    lines = ["CHAPTER CONTRACT (what the planner intended this chapter to cover):"]
    if chapter.objective.strip():
        lines.append(f"- Objective: {chapter.objective.strip()}")
    if chapter.prerequisites:
        lines.append(f"- Assumes the reader already knows: {', '.join(chapter.prerequisites)}")
    for section in sections:
        points = ", ".join(section.key_points) if section.key_points else "(no key points listed)"
        lines.append(f"- Planned section \"{section.title}\": {points}")
    if chapter.key_concepts:
        lines.append(f"- Key concepts that must be covered: {', '.join(chapter.key_concepts)}")
    lines.append(
        "- Check the chapter against this contract: if a planned section, key point or key "
        "concept above is not actually covered anywhere in the chapter (not just absent from "
        "a heading - the content may cover it under different wording), report it as an issue "
        "with category 'completeness' - severity 'major' if an entire planned section or key "
        "concept is missing, 'minor' if it's present but only thinly covered."
    )
    return "\n".join(lines)


def reviewer_user(
    blueprint: CourseBlueprint,
    chapter: BlueprintChapter,
    template: CourseTemplate,
    blocks: list[dict],
    *,
    course_input: CourseInput,
    continuity: ContinuityContext,
    limit: int = 20000,
    memory: str = "",
    research: ChapterResearch | None = None,
    research_chars: int = 4000,
) -> str:
    neighbours = continuity.render()
    memory_block = memory_section(memory)
    memory_block = f"\n{memory_block}\n" if memory_block else ""
    return f"""\
COURSE TITLE: {blueprint.course_title}
CHAPTER TITLE: {chapter.title}
TEMPLATE KIND: {template.kind}
TARGET AUDIENCE: {course_input.target_audience}

DO'S:
{_bullets(course_input.dos)}

DON'TS:
{_bullets(course_input.donts)}
{_appended(learner_brief(course_input.learner_profile))}{_appended(chapter_contract_brief(chapter))}{_appended(approach_review_brief(course_input))}{_appended(_rules_brief(course_input))}{_appended(_design_brief(course_input))}{_appended(_style_brief(course_input))}
REQUIRED TEMPLATE SECTIONS: {", ".join(s.key for s in template.required_sections())}
BLOCK TYPES THAT MUST APPEAR: {", ".join(bt.value for bt in template.required_block_types)}
TEMPLATE REVIEW FOCUS:
{_bullets(template.review_focus)}

COURSE LEARNING OBJECTIVES:
{_bullets(blueprint.learning_objectives)}
CHAPTER OBJECTIVE: {chapter.objective or "(not stated)"}
KEY CONCEPTS: {", ".join(chapter.key_concepts) or "(none listed)"}
PLANNED SECTIONS:
{_bullets([f"{s.title}: {s.summary}" if s.summary else s.title for s in chapter.sections])}

CHAPTER RESEARCH (the evidence to check accuracy against):
---
{research.compact_context(max_chars=research_chars) if research else "(no research available - set `accurate` to null rather than true for claims you cannot verify)"}
---

NEIGHBOURING CHAPTERS (check this chapter does not stray into their material):
{neighbours}

CHAPTER BLOCKS (index: type: text):
{chapter_projection(blocks, limit)}
{memory_block}
Review this chapter now. Always set `block_index` to the index shown above so a
targeted fix is possible.
"""


# ---------------------------------------------------------------------------
# surgical revision
# ---------------------------------------------------------------------------

REVISION_SYSTEM = f"""\
You are fixing specific blocks of a course chapter that a reviewer rejected.

{GLOBAL_RULES}

Rules:
- Rewrite ONLY the blocks you are given. Do not rewrite the chapter.
- Return one replacement per block, with the same `index` and normally the same
  `type`. Change the type only if the reviewer explicitly asked for it.
- Fill only the fields that block type needs.
- Also return an updated 3-5 sentence `chapter_summary` for the whole chapter.
"""


def revision_user(
    chapter: BlueprintChapter,
    template: CourseTemplate,
    blocks: list[dict],
    indices: list[int],
    review: ChapterReview,
    *,
    course_input: CourseInput,
    research: ChapterResearch | None,
    research_chars: int = 4000,
) -> str:
    targeted = [
        {"index": index, "type": blocks[index].get("type"), "content": blocks[index].get("content")}
        for index in indices
        if 0 <= index < len(blocks)
    ]
    import json

    issues = [
        f"- [{issue.severity}/{issue.category}] block {issue.block_index}: "
        f"{issue.description} -> {issue.suggestion}"
        for issue in review.issues
        if issue.severity in {"blocker", "major"}
    ]
    research_text = (
        research.compact_context(max_chars=research_chars) if research else "(no research available)"
    )
    return f"""\
CHAPTER TITLE: {chapter.title}
TEMPLATE KIND: {template.kind}
TARGET AUDIENCE: {course_input.target_audience}

DO'S:
{_bullets(course_input.dos)}

DON'TS:
{_bullets(course_input.donts)}
{_appended(learner_brief(course_input.learner_profile))}{_appended(_rules_brief(course_input))}{_appended(_design_brief(course_input))}{_appended(_style_brief(course_input))}
REVIEWER ISSUES TO FIX:
{chr(10).join(issues) or "(see summary)"}
REVIEWER SUMMARY: {review.summary}

CHAPTER CONTEXT (for reference only - do not rewrite these):
{chapter_projection(blocks, 6000)}

BLOCKS TO REWRITE:
{json.dumps(targeted, ensure_ascii=False)[:20000]}

RESEARCH:
---
{research_text}
---

Return the replacements now.
"""


# ---------------------------------------------------------------------------
# cross-chapter continuity (one call after a parallel run)
# ---------------------------------------------------------------------------

CONTINUITY_SYSTEM = f"""\
You are checking a finished course for problems that only show up across
chapters, because the chapters were written independently.

{GLOBAL_RULES}

Look for: the same material taught twice, a concept used before it is
introduced, missing bridges between consecutive chapters, and ordering that
fights the learning progression. Report issues; do not rewrite anything. If the
course reads cleanly, approve it with an empty issue list.
"""


def continuity_user(
    blueprint: CourseBlueprint, summaries: list[tuple[str, str, str]]
) -> str:
    """`summaries` is (chapter_id, title, written summary), in course order."""
    lines = [f"- {chapter_id} | {title}: {summary}" for chapter_id, title, summary in summaries]
    return f"""\
COURSE TITLE: {blueprint.course_title}
AUDIENCE: {blueprint.audience}

COURSE LEARNING OBJECTIVES:
{_bullets(blueprint.learning_objectives)}

CHAPTER SUMMARIES AS ACTUALLY WRITTEN, IN ORDER:
{chr(10).join(lines)}

Review the course for repetition, gaps, transitions and ordering.
"""


# ---------------------------------------------------------------------------
# editor
# ---------------------------------------------------------------------------

EDITOR_SYSTEM = f"""\
You are the AI editor behind a visual course editor. The user selects one or more
blocks and gives an instruction. You return a PATCH - never a whole document.

{GLOBAL_RULES}

Rules:
- Emit the smallest set of operations that satisfies the instruction.
- Allowed operations: update_block, delete_block, insert_block, replace_block,
  replace_image, update_style.
- Only touch the selected blocks, plus blocks you insert next to them.
- `update_block.content` is merged into the existing content, so send only the
  keys that change. Keep the block's type unless the instruction requires a
  different one (then use replace_block).
- For a new image use replace_image (existing image block) or insert_block with an
  `image` block carrying `purpose`, `prompt` and `caption`. Never output image data.
  FIRST check: is this a programming/DSA/databases/networks/OS/system-design/
  cloud/DevOps/cybersecurity/AI-ML/data-engineering/Git/software-engineering
  topic centered on one nameable technical concept with real internal
  structure (a class and its objects, a data structure, a protocol
  handshake, a pipeline, a state machine, a SQL JOIN)? If yes, set `kind` to
  "concept_experience" and leave `diagram_kind` blank - this is the right
  choice for that content, not a fallback, including for a technical
  SEQUENCE (concept_experience's "process"/"sequence"/"pipeline" numbered
  step-card look is the right, approved look for a technical procedure).
  The one exception is a REPEATING sequence (the last step feeds back into
  the first): never set `representation: "cycle"` on a concept_experience
  (no good renderer for it - a real, confirmed complaint about its
  circular icon-card look) - give that `kind: "diagram"` with
  `diagram_kind: "cycle"` instead, the plain textbook ring diagram. Otherwise set `kind` to
  "diagram" for a flow chart, process, hierarchy, comparison, concept
  map/labelled relationship diagram or SmartArt-style list (rendered from
  labelled shapes, so labels stay exact); also set `diagram_kind` to the
  specific shape (flow_chart/process/cycle for a sequence - never
  `schematic`, permanently retired for every subject including biology/
  life-science: a cell, a plant, an organ, a magnet, an apparatus, a
  molecule, an orbit, a mechanical assembly all always get `kind:
  "illustration"` with `illustration_style: "textbook"` instead, even for a
  simple static object - this is a firm preference from real generated
  courses, not a style suggestion,
  concept_map for an abstract structure's components and how they relate,
  hierarchy, comparison, smart_art, data_flow_diagram for a system/pipeline
  walkthrough needing distinct external-caller/data-store shapes, er_diagram
  for a genuine multi-table database schema, swimlane for a process where
  which role/actor/system does each step is itself part of the point) -
  never diagram the course/lesson itself, only the subject matter. Leave `kind`
  as "illustration" (the default) for a photographic/artistic scene - this
  includes a coordinate-axis graph or curve (a supply-and-demand curve, a
  distance-time graph, a function's plot): that family is built from labelled
  boxes and connector lines, has no notion of an axis or a curve, and forcing
  a graph into it produces boxes labelled with the axis/curve description
  instead of an actual graph - always "illustration" for these, describing
  the exact axes/labels/curves in `prompt`.
  When the user specifically asks for an actual illustrative picture of the
  real subject (anatomy, an experiment/apparatus, a molecular structure, a
  known technical-architecture illustration) rather than another structured
  diagram - typically as a companion next to an existing diagram/
  concept_experience block for the same concept, via insert_block - set
  `kind: "illustration"` and also `illustration_style: "textbook"` (both
  directly on the operation for replace_image, or inside `content` for
  insert_block). Leave `illustration_style` unset/blank for every other
  illustration.
  `replace_image` (both fields are directly on the operation, not inside a
  content update) keeps the block's existing `kind`/`diagram_kind` when you
  leave them unset - but when the instruction is to regenerate/replace/redo an
  image, always re-run the check above against the block's actual topic first,
  even if the user didn't mention "kind" at all. A block sitting on `kind:
  "diagram"` whose topic clearly qualifies for `concept_experience`, whose
  topic is actually a coordinate-axis graph wrongly rendered as boxes-and-
  lines, or whose `diagram_kind` is "schematic" (permanently retired, for
  any subject), is a bug to fix while you're there, not a choice to
  preserve - set `kind` to "concept_experience" (diagram_kind clears
  itself), or to "illustration" (with `illustration_style: "textbook"` for
  the former-schematic case) instead of only swapping the prompt and
  reproducing the same wrong output.
  A `replace_image` instruction that describes a PROBLEM with the current
  picture (overlapping/cut-off text, a wrong symbol, garbled or missing
  labels, cramped spacing, a cluttered/unprofessional look, a wrong detail)
  is a CORRECTION, not a request for a different picture - the reader
  already approved what this image depicts and is asking you to fix
  specific named problems with it, not redesign it. When the block stays
  `kind: "diagram"` or `"concept_experience"`, the server automatically
  feeds the block's own existing structure back into the regeneration as
  correction context, so your `prompt` only needs to state the fix(es)
  clearly - you do not need to re-describe the whole diagram/visual
  yourself. When the block stays `kind: "illustration"` (no such server-
  side memory exists for a raster picture), you must do this preservation
  yourself: write `prompt` by taking the block's own existing `content.
  prompt` almost unchanged and inserting or adjusting only the specific
  correction the instruction describes - never write a fresh, unrelated
  scene description that discards what the picture already correctly
  showed. If a label/symbol itself is what's wrong, missing or needs
  adding, also set `expected_labels` to the corrected complete list
  (every exact part-label the regenerated image must show, spelled
  correctly) - leave it unset when the correction is purely visual
  (spacing, overlap, clutter) and the existing labels are already right.
  When the instruction genuinely asks for a DIFFERENT diagram (not a
  correction), a `diagram_kind: flow_chart`/`decision_tree` replacement
  must stay within the same node/branch-count discipline DIAGRAM_SYSTEM
  already holds fresh generations to - a real, confirmed case: a
  replacement description that spelled out 5 parallel branches all
  re-converging into one shared decision, then branching and re-converging
  again into one terminal node, failed the deterministic renderer's own
  geometry QA twice (both the first attempt and its one retry) before
  falling back through the ENTIRE raster-illustration chain behind it
  (text-free retries, relevance retries, a final generic fallback) - a
  long, multi-stage cascade any single step of which can fail under real
  network conditions, which is exactly how a "corrected" diagram can come
  back completely blank instead. `flow_chart`/`decision_tree` lay out
  cleanly as a mostly-linear spine with occasional SHORT side branches,
  not a web of several parallel paths that all merge back into shared
  nodes (a general graph, not a tree/spine) - when the instruction's own
  content has this shape, pick only the 1-2 branches that matter most
  rather than enumerating every one, or keep the merge implicit in prose
  instead of drawing every path back to a shared node explicitly.
- update_style may only set presentation keys: font_size, font_weight, color,
  background, align, italic, padding, border_radius, border_color, line_height.
- Never invent a block_id. Use only ids that appear in the context below.
- Explain what you changed in `reasoning`.
"""


def editor_user(
    document_title: str,
    template: CourseTemplate,
    selected: list[Block],
    context_blocks: list[Block],
    instruction: str,
    *,
    audience: str = "",
) -> str:
    import json

    def describe(block: Block) -> dict:
        return {
            "block_id": block.id,
            "type": block.type.value,
            "page": block.meta.chapter_number,
            "content": block.content,
            "style": block.style.model_dump(exclude_none=True),
        }

    return f"""\
COURSE TITLE: {document_title}
TEMPLATE KIND: {template.kind}
TARGET AUDIENCE: {audience or "(not specified)"}
ALLOWED BLOCK TYPES: {", ".join(bt.value for bt in template.allowed_block_types)}

USER INSTRUCTION:
{instruction}

SELECTED BLOCKS (these are what you may modify):
{json.dumps([describe(b) for b in selected], ensure_ascii=False)[:40000]}

SURROUNDING BLOCKS (read-only context):
{json.dumps([{"block_id": b.id, "type": b.type.value, "preview": b.text_preview(160)} for b in context_blocks], ensure_ascii=False)[:20000]}

Return the patch.
"""
