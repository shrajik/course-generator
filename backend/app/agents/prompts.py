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
        "RELEVANT MEMORY (background only, from past templates/courses/visuals - "
        "use only if genuinely helpful; never treat as required or as fact about "
        "this specific course):\n"
        f"{memory}"
    )


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
"""


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
TEMPLATE WRITING GUIDANCE: {template.writer_guidance}
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
- A diagram must always illustrate the SUBJECT MATTER being taught (the
  phenomenon, mechanism, organism, system, process or comparison the chapter
  is actually about) - never the course itself. Do not diagram the syllabus,
  the lesson plan, the learning workflow, prerequisites, labs or assessment
  steps; that is meta-content about the course, not the topic, and teaches
  the reader nothing about the subject. A diagram about "how this chapter is
  organised" is almost always the wrong diagram.
- Every chapter that describes a SEQUENCE - steps that happen in order, a
  procedure, a decision path - needs at least one `image` block with
  `image_kind: diagram` and `diagram_kind: flow_chart` (or `process`/`cycle`
  for a repeating sequence) right in the section it explains.
- Every chapter that describes a THING made of parts, or a phenomenon/
  mechanism whose components act on or relate to each other (what physically
  happens, what causes what, which direction something moves or acts) needs
  a SEPARATE `image` block with `image_kind: diagram` - a labelled diagram of
  the subject itself, showing every relevant component, the direction of
  motion/force/current/flow, and what causes what. Prose describing a
  mechanism is not enough on its own; a reader needs to see the components
  and how they relate. Choose which shape fits:
  * `diagram_kind: schematic` when the subject has a real, recognisable
    physical shape and arrangement best shown as a simplified textbook
    illustration - this is a general-purpose choice, not a physics-only one:
    a magnet and a coil, a titration setup, a cell and its organelles, a
    plant's parts, an organ, a molecule forming from its atoms, an orbit, a
    mechanical assembly - use whichever domain the chapter is actually about.
    Especially good when there's a natural before/after, at-rest/in-motion
    or reactant/product comparison to show.
  * `diagram_kind: concept_map` when a labelled-boxes-and-arrows diagram of
    the relationships is enough (an abstract system, an organisation, a
    reaction pathway) and there's no real physical arrangement to draw.
  * `diagram_kind: hierarchy` for a strict parent/child breakdown, and
    `diagram_kind: comparison` for two or more things compared side by side.
- A chapter with BOTH a process to walk through AND a phenomenon/structure to
  show needs BOTH diagrams (a flow_chart plus a schematic or concept_map) -
  one does not replace the other, and this is normal and expected, not
  redundant. This applies to every subject: a chemistry chapter gets both a
  reaction-mechanism flow_chart and a schematic of the molecules involved; a
  biology chapter gets both a process flow_chart and a schematic of the
  organism/structure; a physics chapter gets both a problem-solving
  flow_chart and a schematic of the apparatus - do not stop at just the
  flowchart because it feels like "enough".
- Set `diagram_kind` whenever you can tell which shape fits (flow_chart,
  process, cycle, schematic, concept_map, hierarchy, comparison, smart_art) -
  this is what the renderer actually builds, so getting it right here is
  what makes the diagram useful. Leave it blank only when you genuinely
  cannot tell.
- For an `image` block, do not produce the image. Provide `image_purpose`, a
  precise `image_prompt` (every component/label/direction the diagram needs,
  not just a style description) and a `caption`. Set `image_kind: diagram`
  for anything structured, labelled or relational (see above - this is
  rendered from labelled shapes, never drawn, so labels always come out
  exact). Use `image_kind: illustration` (the default) only for a genuinely
  photographic or artistic scene that has no components or relationships to
  label.
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
"""


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

Set `technical_correctness` for technical courses (also judging code quality) and
`practical_relevance` for non-technical courses. Leave the other one null.

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

REQUIRED TEMPLATE SECTIONS: {", ".join(s.key for s in template.required_sections())}
BLOCK TYPES THAT MUST APPEAR: {", ".join(bt.value for bt in template.required_block_types)}
TEMPLATE REVIEW FOCUS:
{_bullets(template.review_focus)}

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
  Set content `kind` to "diagram" for a flow chart, process, hierarchy, comparison,
  concept map/labelled relationship diagram, physical schematic or SmartArt-style
  list (rendered from labelled shapes, so labels stay exact); also set
  `diagram_kind` to the specific shape (flow_chart/process/cycle for a sequence,
  schematic for a physical apparatus/mechanism best shown as a real illustration,
  concept_map for an abstract structure's components and how they relate,
  hierarchy, comparison, smart_art) -
  never diagram the course/lesson itself, only the subject matter. Leave `kind` as
  "illustration" (the default) for a photographic/artistic scene. `replace_image`
  always keeps the block's existing `kind`/`diagram_kind` unless you also send a
  content update for them.
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
