"""End-of-course compliance report: QA checklist, accessibility flags, metadata.

Client requirement (Methodologies section 3, "append to every deliverable"):
  * 3.3 Quality Assurance - pass/fail with a note for each of 8 checks, plus
        any unresolved flags needing human input;
  * 3.2 Accessibility    - compliance target, flags needing human/technical
        review, and alt-text requirements per asset;
  * 3.4 Metadata         - a structured LOM (default) or Dublin Core block.

Everything here is a pure function of the stored document and the course
input, computed with no AI call, so the same course always yields the same
report. It reports honestly: a check that code can only partly verify is
marked "review" (a person must confirm) rather than "pass".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.course.pedagogy import ARCHETYPES, BANNED_OBJECTIVE_VERBS
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument
from app.schemas.learner import AGE_GROUP_LABELS, LearnerProfile

Status = Literal["pass", "fail", "review"]
MetadataSchema = Literal["lom", "dublin_core"]

COMPLIANCE_TARGET = "WCAG 2.1/2.2 AA"
READING_WORDS_PER_MINUTE = 200

# Blocks where the learner does something, not just reads.
_PRACTICE = {
    BlockType.QUIZ,
    BlockType.EXERCISE,
    BlockType.CHALLENGE,
    BlockType.CASE_STUDY,
    BlockType.REFLECTION,
    BlockType.CODE_CELL,
}
_ASSESSMENT = {BlockType.QUIZ, BlockType.EXERCISE, BlockType.CHALLENGE, BlockType.CASE_STUDY}
_TEACHING_TEXT = {BlockType.PARAGRAPH, BlockType.CALLOUT, BlockType.TIP, BlockType.WARNING, BlockType.STORY}

# A chapter that explains less than this is more likely a stub than a lesson.
MIN_CHAPTER_WORDS = 150

_SUPPORT_RE = re.compile(r"\b(help|support|faq|contact|job aid|quick[- ]reference)\b", re.IGNORECASE)
_BANNED_RE = re.compile(
    r"\b(" + "|".join(re.escape(verb) for verb in BANNED_OBJECTIVE_VERBS) + r")\b", re.IGNORECASE
)
_TAG_RE = re.compile(r"</?jargon[^>]*>", re.IGNORECASE)


@dataclass
class Check:
    id: str
    name: str
    pass_criterion: str
    status: Status
    note: str


@dataclass
class AccessibilityFlag:
    area: str
    severity: Literal["fail", "review"]
    message: str
    location: str = ""


@dataclass
class AssetRequirement:
    asset: str
    kind: str
    location: str
    requirement: str
    present: bool


@dataclass
class ComplianceReport:
    course_id: str
    document_id: str
    generated_at: str
    qa_checks: list[Check] = field(default_factory=list)
    unresolved_flags: list[str] = field(default_factory=list)
    accessibility_target: str = COMPLIANCE_TARGET
    accessibility_flags: list[AccessibilityFlag] = field(default_factory=list)
    asset_requirements: list[AssetRequirement] = field(default_factory=list)
    metadata_schema: MetadataSchema = "lom"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "review": 0}
        for check in self.qa_checks:
            counts[check.status] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            if hasattr(value, "__dataclass_fields__"):
                return {key: plain(getattr(value, key)) for key in value.__dataclass_fields__}
            if isinstance(value, list):
                return [plain(item) for item in value]
            return value

        data = plain(self)
        data["summary"] = self.summary
        return data


# --- helpers -----------------------------------------------------------------


def _text(block: Block) -> str:
    content = block.content
    parts: list[str] = []
    for key in ("text", "title", "intro", "caption", "instructions", "code"):
        value = content.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("items", "steps", "options"):
        for item in content.get(key, []) or []:
            if isinstance(item, str):
                parts.append(item)
    for question in content.get("questions", []) or []:
        if isinstance(question, dict):
            parts.append(str(question.get("question", "")))
    return _TAG_RE.sub("", " ".join(parts))


def _words(text: str) -> int:
    return len(text.split())


@dataclass
class _Chapter:
    title: str
    first_page: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f'"{self.title}" (page {self.first_page})'


def _chapters(document: CourseDocument) -> list[_Chapter]:
    """Content pages split at each level-1 heading."""
    chapters: list[_Chapter] = []
    for page in document.pages:
        if page.kind != "content":
            continue
        for block in page.blocks:
            is_chapter_title = block.type is BlockType.HEADING and int(block.content.get("level", 2) or 2) <= 1
            if is_chapter_title or not chapters:
                title = str(block.content.get("text", "")) if is_chapter_title else "Introduction"
                chapters.append(_Chapter(title=title.strip() or "Untitled", first_page=page.page_number))
                if is_chapter_title:
                    continue
            chapters[-1].blocks.append(block)
    return chapters


def _hex_to_rgb(value: str) -> tuple[float, float, float] | None:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float | None:
    """WCAG contrast ratio of two hex colours, or None if either is not hex."""
    fg, bg = _hex_to_rgb(foreground), _hex_to_rgb(background)
    if fg is None or bg is None:
        return None
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# --- accessibility -----------------------------------------------------------


def _accessibility(document: CourseDocument) -> tuple[list[AccessibilityFlag], list[AssetRequirement]]:
    flags: list[AccessibilityFlag] = []
    assets: list[AssetRequirement] = []

    for page, block in document.iter_blocks():
        where = f"page {page.page_number}"
        if block.type is BlockType.IMAGE and block.content.get("kind") != "toc":
            decorative = bool(block.content.get("decorative"))
            alt = str(block.content.get("alt") or "").strip()
            kind = str(block.content.get("kind") or "illustration")
            if decorative:
                requirement, present = 'Decorative: mark alt="" instead of describing it.', True
            elif alt:
                requirement, present = "Alt text present - confirm it describes the image.", True
            else:
                requirement, present = "Alt text is required and missing.", False
                flags.append(
                    AccessibilityFlag("Images", "fail", "Image has no alt text.", where)
                )
            assets.append(AssetRequirement(block.id, kind, where, requirement, present))
        if block.type is BlockType.CODE_CELL:
            flags.append(
                AccessibilityFlag(
                    "Interactions",
                    "review",
                    "Runnable code cell: confirm the Run control and output are keyboard-operable.",
                    where,
                )
            )

    # Heading hierarchy: no skipped levels, within the reading order.
    previous = 0
    for page, block in document.iter_blocks():
        if block.type is not BlockType.HEADING:
            continue
        level = int(block.content.get("level", 2) or 2)
        if previous and level > previous + 1:
            flags.append(
                AccessibilityFlag(
                    "Structure",
                    "fail",
                    f"Heading level jumps from H{previous} to H{level}.",
                    f"page {page.page_number}",
                )
            )
        previous = level

    interactive = [
        block for _, block in document.iter_blocks() if block.type in {BlockType.QUIZ, BlockType.EXERCISE}
    ]
    if interactive:
        flags.append(
            AccessibilityFlag(
                "Interactions",
                "review",
                f"{len(interactive)} quiz/exercise block(s): any interactive delivery of these "
                "needs a keyboard-operable equivalent.",
            )
        )

    theme = document.meta.theme if hasattr(document.meta, "theme") else {}
    theme = theme if isinstance(theme, dict) else getattr(theme, "__dict__", {})
    background = str(theme.get("page_background") or "#ffffff")
    for name, label, minimum in (("text_color", "Body text", 4.5), ("muted_color", "Muted text", 4.5)):
        colour = str(theme.get(name) or "")
        ratio = contrast_ratio(colour, background) if colour else None
        if ratio is not None and ratio < minimum:
            flags.append(
                AccessibilityFlag(
                    "Color/contrast",
                    "fail",
                    f"{label} {colour} on {background} is {ratio:.1f}:1; {minimum}:1 is required.",
                )
            )

    flags.append(
        AccessibilityFlag(
            "PDF",
            "review",
            "PDF/UA tagging (reading order, tagged headings) cannot be verified by content "
            "generation; flag for technical remediation before publishing.",
        )
    )
    return flags, assets


# --- QA checks ---------------------------------------------------------------


def _qa_checks(
    document: CourseDocument, chapters: list[_Chapter], flags: list[AccessibilityFlag]
) -> list[Check]:
    checks: list[Check] = []

    def add(index: int, name: str, criterion: str, status: Status, note: str) -> None:
        checks.append(Check(f"qa_{index}", name, criterion, status, note))

    def names(items: list[_Chapter]) -> str:
        return ", ".join(chapter.label for chapter in items[:4]) + (" ..." if len(items) > 4 else "")

    if not chapters:
        for index, (name, criterion) in enumerate(_QA_DEFINITIONS, start=1):
            add(index, name, criterion, "fail", "The course has no content pages to check.")
        return checks

    # 1. Course overview
    first = chapters[0]
    has_overview = any(b.type in _TEACHING_TEXT or b.type is BlockType.LEARNING_OBJECTIVES for b in first.blocks)
    add(1, *_QA_DEFINITIONS[0], "pass" if has_overview else "fail",
        "The opening chapter states what the course covers." if has_overview
        else "The opening chapter has no overview text or objectives.")

    # 2. Learning objectives: present, before the teaching, measurable.
    missing: list[_Chapter] = []
    late: list[_Chapter] = []
    banned: list[str] = []
    for chapter in chapters:
        objective_index = next(
            (i for i, b in enumerate(chapter.blocks) if b.type is BlockType.LEARNING_OBJECTIVES), None
        )
        if objective_index is None:
            missing.append(chapter)
            continue
        # Teaching text may introduce the objectives; practice may not come first.
        practice_index = next((i for i, b in enumerate(chapter.blocks) if b.type in _PRACTICE), None)
        if practice_index is not None and practice_index < objective_index:
            late.append(chapter)
        for item in chapter.blocks[objective_index].content.get("items", []) or []:
            match = _BANNED_RE.search(str(item))
            if match:
                banned.append(f'"{match.group(1)}" in {chapter.label}')
    problems = []
    if missing:
        problems.append(f"no objectives in {names(missing)}")
    if late:
        problems.append(f"objectives come after practice in {names(late)}")
    if banned:
        problems.append("unmeasurable verbs: " + "; ".join(banned[:3]))
    add(2, *_QA_DEFINITIONS[1], "fail" if problems else "pass",
        "; ".join(problems) if problems else "Every chapter states measurable objectives up front.")

    # 3. Assessment: exists per chapter; mapping/depth needs a person.
    unassessed = [c for c in chapters if not any(b.type in _ASSESSMENT for b in c.blocks)]
    if unassessed:
        add(3, *_QA_DEFINITIONS[2], "fail", f"No assessment item in {names(unassessed)}.")
    else:
        add(3, *_QA_DEFINITIONS[2], "review",
            "Every chapter has an assessment. Whether each item maps to a specific objective at "
            "the right cognitive depth needs a human check.")

    # 4. Instructional materials: enough explanation to teach, not just reference.
    thin = [
        c for c in chapters
        if sum(_words(_text(b)) for b in c.blocks if b.type in _TEACHING_TEXT) < MIN_CHAPTER_WORDS
    ]
    add(4, *_QA_DEFINITIONS[3], "fail" if thin else "pass",
        f"Under {MIN_CHAPTER_WORDS} words of explanation in {names(thin)}." if thin
        else f"Every chapter has at least {MIN_CHAPTER_WORDS} words of explanatory text.")

    # 5. Interaction / engagement
    passive = [c for c in chapters if not any(b.type in _PRACTICE for b in c.blocks)]
    add(5, *_QA_DEFINITIONS[4], "fail" if passive else "pass",
        f"Only passive reading in {names(passive)}." if passive
        else "Every chapter includes active practice.")

    # 6. Course technology: we can build it, but no packaging standard exists yet.
    add(6, *_QA_DEFINITIONS[5], "review",
        "Delivered as an editable course document and PDF. No LMS packaging standard "
        "(SCORM, xAPI, cmi5) is selected or exportable yet.")

    # 7. Learner support
    everything = " ".join(_text(b) for _, b in document.iter_blocks())
    has_support = bool(_SUPPORT_RE.search(everything))
    add(7, *_QA_DEFINITIONS[6], "pass" if has_support else "fail",
        "The course points learners to help." if has_support
        else "No help, FAQ, job aid or contact is mentioned. Support contact not provided - "
        "a placeholder and client input are needed.")

    # 8. Accessibility / usability
    failing = [flag for flag in flags if flag.severity == "fail"]
    pending = [flag for flag in flags if flag.severity == "review"]
    if failing:
        add(8, *_QA_DEFINITIONS[7], "fail",
            f"{len(failing)} accessibility problem(s), e.g. {failing[0].message}")
    else:
        add(8, *_QA_DEFINITIONS[7], "review",
            f"No automatic failures; {len(pending)} item(s) need human/technical review.")
    return checks


_QA_DEFINITIONS: list[tuple[str, str]] = [
    ("Course overview", "Learner knows what the course covers and why, up front"),
    ("Learning objectives", "Present, measurable, and stated before the content that teaches them"),
    ("Assessment", "Each item maps to an objective and matches its cognitive depth"),
    ("Instructional materials", "Sufficient to actually teach the objective, not just reference it"),
    ("Interaction/engagement", "Active practice, not only passive reading/watching"),
    ("Course technology", "Buildable in the specified tool/platform without unsupported features"),
    ("Learner support", "Clear place to get help (job aid, contact, FAQ)"),
    ("Accessibility/usability", "Passes the checks in the accessibility standard"),
]


# --- metadata ----------------------------------------------------------------


def _difficulty(profile: LearnerProfile | None) -> str:
    if profile is None:
        return "not specified"
    return {
        "PRIMARY": "beginner",
        "SECONDARY": "beginner",
        "BACHELORS": "intermediate",
        "MASTERS_PHD": "advanced",
    }[profile.edu_level]


def _metadata(
    document: CourseDocument,
    chapters: list[_Chapter],
    schema: MetadataSchema,
    *,
    owner: str | None,
    language: str,
    profile: LearnerProfile | None,
    now: str,
) -> dict[str, Any]:
    total_words = sum(_words(_text(b)) for _, b in document.iter_blocks())
    minutes = max(1, round(total_words / READING_WORDS_PER_MINUTE))
    keywords = [c.title for c in chapters if c.title and c.title != "Introduction"]
    images = [b for _, b in document.iter_blocks() if b.type is BlockType.IMAGE]
    generated = sum(1 for b in images if b.content.get("generated"))
    rights = (
        f"{generated} AI-generated image(s); confirm licensing terms for the generating service."
        if generated
        else "No third-party media detected."
    )
    model = ARCHETYPES[profile.age_group].instructional_model if profile else "not specified"
    source = f"Instructional model: {model}. Packaging standard: none (PDF / editable course document)."
    description = (
        f"{document.course_title}: a {len(chapters)}-chapter course"
        + (f" for {AGE_GROUP_LABELS[profile.age_group].lower()}." if profile else ".")
    )

    if schema == "dublin_core":
        core = {
            "Title": document.course_title,
            "Creator": owner or "not recorded",
            "Subject": ", ".join(keywords[:8]),
            "Description": description,
            "Publisher": "not recorded",
            "Date": document.updated_at,
            "Type": "Text",
            "Format": "application/pdf",
            "Identifier": document.document_id,
            "Source": source,
            "Language": language,
            "Relation": "",
            "Coverage": "",
            "Rights": rights,
            "Contributor": "",
        }
    else:
        core = {
            "Title": document.course_title,
            "Description": description,
            "Keywords": keywords[:12],
            "Learning resource type": "course",
            "Difficulty/level": _difficulty(profile),
            "Typical learning time": f"about {minutes} minutes of reading (estimate at "
            f"{READING_WORDS_PER_MINUTE} words per minute)",
            "Technical requirements/format": "PDF; editable Course Document JSON",
            "Rights/licensing": rights,
            "Language": language,
        }
    core.update(
        {
            "Version number": str(document.version),
            "Last generated": now,
            "Owner/requester": owner or "not recorded",
            "Source standard": source,
        }
    )
    return core


# --- entry point -------------------------------------------------------------


def build_report(
    document: CourseDocument,
    *,
    schema: MetadataSchema = "lom",
    owner: str | None = None,
    language: str = "en",
    profile: LearnerProfile | None = None,
    now: str | None = None,
) -> ComplianceReport:
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    chapters = _chapters(document)
    flags, assets = _accessibility(document)
    checks = _qa_checks(document, chapters, flags)

    unresolved = [f"{check.name}: {check.note}" for check in checks if check.status != "pass"]
    return ComplianceReport(
        course_id=document.course_id,
        document_id=document.document_id,
        generated_at=stamp,
        qa_checks=checks,
        unresolved_flags=unresolved,
        accessibility_flags=flags,
        asset_requirements=assets,
        metadata_schema=schema,
        metadata=_metadata(
            document, chapters, schema, owner=owner, language=language, profile=profile, now=stamp
        ),
    )


# Shared with app.course.design_models, which also reads a document chapter by
# chapter and needs the same notion of "a chapter" and of a block text.
split_chapters = _chapters
block_text = _text
word_count = _words
