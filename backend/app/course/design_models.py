"""Instructional-design models as deterministic code.

Client requirement (Methodologies section 2): ADDIE, Bloom's Taxonomy, SAM,
Gagne's Nine Events and Kirkpatrick's Four Levels, each with required inputs
and a required output. Like the learner brief in app/course/pedagogy.py, every
function here is a pure function of the selected models - no AI call decides
which model applies or what its output must contain.

Three things come out of this module:

* `design_model_brief` - rules injected into the planner/writer/reviewer
  prompts (empty string when nothing is selected, which keeps prompts for
  courses created before this feature byte-identical);
* `design_issues` - deterministic reviewer checks for the rules a document can
  be checked against;
* `design_outputs` - the plan documents the models call for (ADDIE
  implementation notes, the Kirkpatrick evaluation plan), built from the saved
  course. They are computed, not generated, so they cannot invent facts - in
  particular no numeric business targets, which the client says must come from
  the business stakeholder.

Prompt-safety note (see app/course/pedagogy.py): the brief must not contain a
line starting with a number and a dot, or the literal labels `COURSE TITLE:`,
`CHAPTER TITLE:` or `TEMPLATE KIND:`.
"""

from __future__ import annotations

import re
from typing import Any

from app.course.compliance import block_text, split_chapters
from app.course.pedagogy import BLOOM_ASSESSMENTS, BLOOM_VERBS
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.design import DESIGN_MODEL_LABELS, DesignModels
from app.schemas.document import Block, CourseDocument
from app.schemas.review import ReviewIssue

# --- Gagne -------------------------------------------------------------------

GAGNE_EVENTS: list[tuple[str, str]] = [
    ("Gain attention", "An opening hook: a scenario, a question or a relevant problem."),
    ("Inform objectives", "A plain statement of what the learner will be able to do."),
    ("Stimulate recall", "A prompt connecting to prior knowledge."),
    ("Present content", "The core instructional content."),
    ("Provide guidance", "Worked examples, analogies, annotated diagrams, examples and non-examples."),
    ("Elicit performance", "A practice activity requiring the learner to apply the content."),
    ("Provide feedback", "Specific, corrective feedback tied to the practice activity."),
    ("Assess performance", "A formal, scored assessment item."),
    ("Enhance retention and transfer", "A job aid, summary or real-world application prompt."),
]
# Microlearning keeps these four events (1-based positions in GAGNE_EVENTS).
GAGNE_MICRO_EVENTS = (2, 4, 6, 8)

_SAM_LABEL_RE = re.compile(r"draft\s*[-–—]\s*iteration\s*\d+", re.IGNORECASE)


def gagne_events(design: DesignModels) -> list[tuple[int, str, str]]:
    keep = GAGNE_MICRO_EVENTS if design.microlearning else tuple(range(1, 10))
    return [(n, name, what) for n, (name, what) in enumerate(GAGNE_EVENTS, start=1) if n in keep]


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z]+", " ", text.casefold()).strip()


# --- prompt brief ------------------------------------------------------------


def design_model_brief(design: DesignModels | None, *, bloom_already_stated: bool = False) -> str:
    """Rules for the selected models, or "" when none is selected.

    `bloom_already_stated` is true when a learner profile is set: its brief
    already carries the Bloom verb bank, so it is not repeated.
    """
    if design is None or not design.models:
        return ""

    lines: list[str] = ["INSTRUCTIONAL DESIGN MODELS SELECTED FOR THIS COURSE:"]
    lines.append("- Apply: " + ", ".join(DESIGN_MODEL_LABELS[m] for m in design.models) + ".")

    if design.has("addie"):
        bloom_note = (
            "Bloom's verb rules apply because Bloom's Taxonomy is also selected."
            if design.has("bloom")
            else "Do not apply Bloom's verb rules, because Bloom's Taxonomy is not selected."
        )
        lines += [
            "",
            "ADDIE - work in order: analysis and design come before development content.",
            "- Analysis: the course summary must state the performance gap (what the audience "
            "cannot do now versus what they must do), an audience profile (role, prior "
            "knowledge, constraints) and the specific tasks or behaviours the course must produce.",
            '- Design: write each learning objective in the simple form "Learners will be able to '
            '[do X]", one per task, and map every chapter to the objectives it serves. ' + bloom_note,
            "- Development: whenever supporting media would teach better than text (video, "
            "diagram, simulation), flag that need instead of describing it in prose alone.",
            "- Development: flag every technical claim, code sample and procedure for "
            "subject-matter-expert verification, in a callout titled SME verification.",
        ]

    if design.has("bloom") and not bloom_already_stated:
        verbs = "; ".join(f"{level} - {', '.join(v)}" for level, v in BLOOM_VERBS.items())
        lines += [
            "",
            "BLOOM'S TAXONOMY - objectives and matched assessments:",
            "- Write each objective as: learners will be able to [approved verb] [specific task] "
            "[condition or context]. Use exactly one verb per objective from this bank: "
            + verbs
            + ".",
            "- Never use the unmeasurable verbs understand, know, learn, appreciate or be "
            "familiar with.",
            "- Pair every objective with the assessment its level requires: "
            + "; ".join(f"{level} - {kind}" for level, kind in BLOOM_ASSESSMENTS.items())
            + ". Never assess an Evaluate or Create objective with a multiple-choice quiz.",
            "- Put lower levels before the higher levels that depend on them, and state the "
            "domain used (cognitive by default; affective for soft skills, compliance or ethics; "
            "psychomotor for physical procedures).",
        ]
    if design.has("bloom") and design.bloom_target_level:
        lines.append(
            f"- Pitch the objectives at the {design.bloom_target_level} level of Bloom's Taxonomy."
        )

    if design.has("gagne"):
        lines += [
            "",
            "GAGNE'S EVENTS - build each chapter as one lesson with these labelled sections, "
            "in this order:",
        ]
        lines += [f"- Section heading {name}: {what}" for _, name, what in gagne_events(design)]
        lines.append(
            "- Use each event name above, exactly, as the heading of its section, so a reviewer "
            "can find it. A lesson that only presents and tests information is not acceptable."
        )
        if design.microlearning:
            lines.append(
                "- This is a microlearning format: only the four events above are kept. Add one "
                "short note saying which events were compressed and why."
            )

    if design.has("sam"):
        loop = "SAM2 (multi-loop)" if design.sam_loop == "SAM2" else "SAM1 (single loop)"
        lines += [
            "",
            f"SAM - iterative build, {loop}. Never deliver a complete polished course in one pass:",
            "- This pass covers one module or proof of concept only. Write real content, but "
            'label it clearly as a draft: start with a callout titled "Draft - Iteration N", where N '
            "is the chapter number.",
            "- End with a callout titled Open questions for the reviewer, asking about specific "
            "decisions: tone, depth, structure and activity type. Never just ask for thoughts.",
            "- Do not build further modules speculatively; the next iteration waits for feedback.",
        ]
        if design.sam_review_ready.strip():
            lines.append(f"- Good enough to review means: {design.sam_review_ready.strip()}")

    if design.has("kirkpatrick"):
        lines += [
            "",
            "KIRKPATRICK - the evaluation plan is a separate deliverable, not course content:",
            "- Write objectives that name an observable on-the-job behaviour, tied to this "
            f"business goal: {design.business_goal.strip()}",
            "- Never invent numeric business targets or KPI values anywhere in the content.",
        ]

    return "\n".join(lines)


# --- reviewer checks ---------------------------------------------------------

_ABLE_TO_RE = re.compile(r"\bable to\s+([a-z']+)", re.IGNORECASE)
_TEMPLATE_RE = re.compile(r"\b(?:learners?|the learner|you)\s+will be able to\b", re.IGNORECASE)
_VERB_TO_LEVEL = {verb: level for level, verbs in BLOOM_VERBS.items() for verb in verbs}


def _headings(blocks: list[Block]) -> list[tuple[int, str]]:
    return [
        (index, _normalise(str(block.content.get("text", ""))))
        for index, block in enumerate(blocks)
        if block.type is BlockType.HEADING
    ]


def _objective_verb(text: str) -> str:
    match = _ABLE_TO_RE.search(text)
    if match:
        return match.group(1).casefold()
    words = text.strip().split(" ")
    return words[0].casefold() if words and words[0] else ""


def design_issues(blocks: list[Block], design: DesignModels | None) -> list[ReviewIssue]:
    """Deterministic checks for the selected models. [] when none is selected."""
    if design is None or not design.models:
        return []
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

    if design.has("gagne"):
        wanted = gagne_events(design)
        found: dict[int, int] = {}
        for index, text in _headings(blocks):
            for number, name, _ in wanted:
                if number not in found and _normalise(name) in text:
                    found[number] = index
                    break
        missing = [name for number, name, _ in wanted if number not in found]
        if missing:
            add("major", "gagne", -1,
                "Gagne event section(s) missing: " + ", ".join(missing) + ".",
                "Add a section headed with each missing event name, in order.")
        else:
            positions = [found[number] for number, _, _ in wanted]
            if positions != sorted(positions):
                add("major", "gagne", -1, "Gagne events are out of order.",
                    "Reorder the sections to follow the event order exactly.")
        if design.microlearning and not any("compressed" in block_text(b).casefold() for b in blocks):
            add("minor", "gagne", -1,
                "A microlearning lesson must say which events were compressed and why.",
                "Add a short note naming the compressed events and the reason.")

    if design.has("sam"):
        joined = " ".join(block_text(b) for b in blocks)
        if not _SAM_LABEL_RE.search(joined):
            add("major", "sam", 0, 'SAM content must be labelled "Draft - Iteration N".',
                'Add a callout titled "Draft - Iteration N" at the start of the chapter.')
        if "open question" not in joined.casefold():
            add("major", "sam", -1, "No specific open questions for the reviewer.",
                "End with a callout of open questions about tone, depth, structure and activity type.")

    if design.has("addie"):
        has_code = any(b.type in {BlockType.CODE, BlockType.CODE_CELL} for b in blocks)
        flagged = any("sme verification" in block_text(b).casefold() for b in blocks)
        if has_code and not flagged:
            add("minor", "addie", -1,
                "Code or technical procedures are present with no SME verification flag.",
                "Add a callout titled SME verification listing the claims, code and procedures to verify.")

    if design.has("bloom") or design.has("addie"):
        for index, block in enumerate(blocks):
            if block.type is not BlockType.LEARNING_OBJECTIVES:
                continue
            for item in block.content.get("items", []) or []:
                if not _TEMPLATE_RE.search(str(item)):
                    add("minor", "bloom" if design.has("bloom") else "addie", index,
                        f'Objective does not follow "Learners will be able to ...": {str(item)[:140]}',
                        'Rewrite it as "Learners will be able to [verb] [specific task] [context]".')

    if design.has("bloom"):
        for index, block in enumerate(blocks):
            if block.type is not BlockType.LEARNING_OBJECTIVES:
                continue
            for item in block.content.get("items", []) or []:
                verb = _objective_verb(str(item))
                if verb and verb not in _VERB_TO_LEVEL:
                    add("major", "bloom", index,
                        f'Objective verb "{verb}" is not in the approved Bloom bank: {str(item)[:140]}',
                        "Rewrite it with one approved verb that matches the cognitive level.")
    return issues


# --- plan documents ----------------------------------------------------------


def _chapter_objectives(document: CourseDocument) -> list[tuple[str, list[str]]]:
    result: list[tuple[str, list[str]]] = []
    for chapter in split_chapters(document):
        items: list[str] = []
        for block in chapter.blocks:
            if block.type is BlockType.LEARNING_OBJECTIVES:
                items += [str(i).strip() for i in block.content.get("items", []) or [] if str(i).strip()]
        result.append((chapter.title, items))
    return result


def _implementation_notes(document: CourseDocument) -> dict[str, Any]:
    chapters = split_chapters(document)
    modules = []
    for index, chapter in enumerate(chapters):
        has_quiz = any(b.type is BlockType.QUIZ for b in chapter.blocks)
        has_task = any(
            b.type in {BlockType.EXERCISE, BlockType.CHALLENGE, BlockType.CASE_STUDY}
            for b in chapter.blocks
        )
        modules.append({
            "module": chapter.title,
            "prerequisite": chapters[index - 1].title if index else "none",
            "tracking": "scored (pass/fail)" if has_quiz else ("scored" if has_task else "complete on viewing"),
        })
    return {
        "sequencing": "Deliver the modules in the order listed; each assumes the previous one.",
        "modules": modules,
        "tracking_note": "Mark a module complete when it is viewed; scored modules also report a "
        "score and pass/fail. Bookmark/resume location is recommended.",
        "rollout_note": "Written for self-paced delivery. Add a facilitator note if the client "
        "runs it instructor-led or blended.",
    }


def _evaluation_summary(design: DesignModels) -> dict[str, str]:
    return {
        "reaction": "A short end-of-course survey on satisfaction, relevance and engagement.",
        "learning": "The assessments inside each module, mapped to the stated objectives.",
        "on_the_job": "Follow-up observation of the target behaviours, if the business goal needs it.",
        "detail": "See the Kirkpatrick plan for the full four-level plan."
        if design.has("kirkpatrick")
        else "High-level only. Select Kirkpatrick's Four Levels for a full plan.",
    }


def _kirkpatrick_plan(document: CourseDocument, design: DesignModels) -> dict[str, Any]:
    objective_rows: list[dict[str, str]] = []
    for title, items in _chapter_objectives(document):
        for item in items:
            level = _VERB_TO_LEVEL.get(_objective_verb(item))
            objective_rows.append({
                "module": title,
                "objective": item,
                "bloom_level": level or "not classified",
                "assessment": BLOOM_ASSESSMENTS.get(
                    level or "", "Choose an assessment that matches the objective's level."
                ),
            })
    behaviours = [row["objective"] for row in objective_rows] or [
        "(no objectives found in the saved course)"
    ]
    external = design.data_sources == "lms_and_kpis"
    return {
        "business_goal": design.business_goal.strip(),
        "level_1_reaction": {
            "timing": "End of course",
            "questions": [
                "How satisfied were you with this course overall?",
                "How relevant was the content to your work or goals?",
                "How engaging did you find the activities?",
                "What one change would make this course more useful?",
            ],
        },
        "level_2_learning": {"timing": "End of course", "items": objective_rows},
        "level_3_behavior": {
            "timing": "30, 60 and 90 days after the course",
            "checklist": [f"Observe whether the learner can: {b}" for b in behaviours],
        },
        "level_4_results": {
            "timing": "Ongoing, after launch",
            "measure": "Business KPI(s) this course should influence, related to: "
            + design.business_goal.strip(),
            "note": "Numeric targets are not set here. They must come from the business stakeholder.",
        },
        "data_sources": {
            "lms_measurable_now": ["Level 1 reaction", "Level 2 learning"],
            "external_collection": ["Level 3 behavior", "Level 4 results"],
            "note": "Manager observation and business KPIs are available for Levels 3-4."
            if external
            else "Only LMS data is available, so Levels 3-4 need a plan to collect external data "
            "(manager observation, business KPIs) before they can be measured.",
        },
        "flags": []
        if external
        else ["Only Levels 1-2 can be measured with LMS data alone; the business goal needs Levels 3-4."],
    }


def design_outputs(document: CourseDocument, input_: CourseInput) -> dict[str, Any]:
    """The plan documents for the selected models. {} when none applies."""
    design = input_.design_models
    out: dict[str, Any] = {}
    if design is None:
        return out
    if design.has("addie"):
        out["addie"] = {
            "implementation": _implementation_notes(document),
            "evaluation_summary": _evaluation_summary(design),
        }
    if design.has("kirkpatrick"):
        out["kirkpatrick"] = _kirkpatrick_plan(document, design)
    return out
