"""Turn the nodes of an uploaded template into a `CourseTemplate` + blueprint.

Deterministic on purpose: the same file always yields the same blueprint, it
runs once at upload (never per course), costs nothing, and every rule it
records can be traced to a line of the file. The trade-off is that it reads
what the file says plainly; it does not guess intentions the text does not
express. Templates work best when instructions are written as instructions
("Add a labelled flowchart after the explanation") or placed under a heading
such as "Writing rules" / "Visual rules".

How a file is read:
* Headings at the section level become the chapter's sections, in order.
  A heading such as "Writing rules", "Visual guidelines" or "Instructions"
  opens a *rules* area instead of a section.
* Inside a section, an instruction ("Explain ...", "Must include ...",
  "[Instruction: ...]") is a rule for that section; plain prose, code and
  tables are a worked *example* of the expected pattern; a sub-heading such
  as "Example" marks everything under it as example.
* A sentence about a diagram, chart, image, ... becomes a visual rule with
  its kind, placement, labels, caption, count and aspect ratio.
* A picture is an example of a visual pattern, unless its caption or the
  text beside it says it is a logo or is to be reused.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationFailedError
from app.course.templates.docx_parser.placeholders import SUPPORTED, names_in, unknown_names
from app.course.templates.docx_parser.structure import _block_types_for, _clean_label, _obligation, _slug
from app.schemas.blocks import BlockType
from app.schemas.template import CourseTemplate, TemplateSection, TemplateTheme
from app.schemas.template_blueprint import (
    DesignSpec,
    TemplateAsset,
    TemplateBlueprint,
    TemplateExample,
    TextRules,
    VisualRule,
)

from .reader import Node
from .safety import is_prompt_injection

RULE_CHARS = 300
GUIDANCE_CHARS = 700
EXAMPLE_CHARS = 900
MAX_RULES_PER_LIST = 25
MAX_VISUALS = 30
MAX_EXAMPLES = 12

# ---------------------------------------------------------------------------
# classification vocabulary
# ---------------------------------------------------------------------------

_RULES_HEADING_RE = re.compile(
    r"\b(rules?|guidelines?|instructions?|style guide|writing style|tone( of voice)?|voice|"
    r"how to (write|use)|notes? (to|for) (the )?(writer|author|ai)|requirements|conventions|standards|"
    r"specifications?|guidance)\b",
    re.IGNORECASE,
)
_EXAMPLE_HEADING_RE = re.compile(r"\b(example|sample|worked example|model answer|specimen)s?\b", re.IGNORECASE)
_VISUAL_TOPIC_RE = re.compile(r"\b(visual|image|diagram|illustration|figure|graphic|picture)s?\b", re.IGNORECASE)

_MARKER_RE = re.compile(
    r"^\s*(?:\[\s*)?(instruction|instructions|note to (?:the )?(?:writer|author)|guidance|rule|writer|ai|author note)\s*[:\-–]\s*",
    re.IGNORECASE,
)
_BRACKETED_RE = re.compile(r"^\s*[\[{<]\s*(.+?)\s*[\]}>]\s*$")
_EXAMPLE_PREFIX_RE = re.compile(
    r"^\s*(example|sample|e\.g\.|for example|for instance|worked example)\s*[:\-–]\s*", re.IGNORECASE
)
_IMPERATIVE_RE = re.compile(
    r"^\s*(use|write|include|add|provide|explain|start|begin|end|finish|close|open|keep|avoid|do not|don't|"
    r"never|always|make|ensure|give|list|show|place|insert|put|describe|define|present|follow|limit|aim|"
    r"target|focus|introduce|summari[sz]e|conclude|illustrate|highlight|cover|mention|cite|state|offer|pair|"
    r"accompany|break|split|structure|organi[sz]e|number|label|caption|prefer|favou?r|emphasi[sz]e|reinforce|"
    r"assess|ask|encourage|each|every|all|must|should|you must|you should|the (writer|author|ai) (must|should))\b",
    re.IGNORECASE,
)
_MODAL_RE = re.compile(
    r"\b(must|should|shall|needs? to|is required|are required|required to|make sure|be sure to|mandatory)\b",
    re.IGNORECASE,
)
_WRITING_CONTEXT_RE = re.compile(
    r"\b(sections?|chapters?|paragraphs?|content|examples?|diagrams?|images?|visuals?|illustrations?|figures?|"
    r"learners?|readers?|students?|explain\w*|writ\w*|tone|voice|style|words?|captions?|summar\w*|quiz\w*|"
    r"exercises?|assessments?|questions?|headings?|course|lessons?|modules?|analog\w*|case stud\w*|objectives?|"
    r"jargon|terms?|terminology|sentences?|bullets?|lists?|tables?|code|labels?|length|takeaways?|activit\w*|"
    r"scenarios?|definitions?|charts?|flow ?charts?|graphs?|pictures?|photos?|screenshots?|timelines?)\b",
    re.IGNORECASE,
)

_KV_RE = re.compile(
    r"^\s*(tone|voice|style|writing style|reading level|reading age|audience level|level|depth|level of detail|"
    r"detail|word count|words per chapter|chapter length|length|paragraph length|paragraphs|section length|"
    r"terminology|vocabulary|image style|visual style)\s*[:=\-–]\s*(.+)$",
    re.IGNORECASE,
)
_WORDS_RE = re.compile(r"(\d{2,5})\s*(?:[-–to]+\s*(\d{2,5}))?\s*words", re.IGNORECASE)

_VISUAL_RE = re.compile(
    r"\b(diagrams?|flow ?charts?|charts?|graphs?|illustrations?|images?|pictures?|figures?|infographics?|"
    r"screenshots?|screen shots?|photos?|photographs?|timelines?|mind ?maps?|concept maps?|visuals?|"
    r"schematics?|drawings?|plots?|swimlanes?)\b",
    re.IGNORECASE,
)
_OPTIONAL_RE = re.compile(
    r"\b(may|optional|optionally|if (helpful|useful|relevant|appropriate|needed)|where (helpful|useful|relevant|"
    r"appropriate|possible)|consider|can be|could|when it helps|nice to have)\b",
    re.IGNORECASE,
)
_REQUIRED_RE = re.compile(r"\b(must|required|always|every|each|needs? to|mandatory|shall|include|add|insert|place)\b", re.IGNORECASE)
_NEGATIVE_RE = re.compile(r"\b(do not|don't|never|avoid|no|without)\b", re.IGNORECASE)

_ASSESSMENT_RE = re.compile(r"\b(quiz\w*|assessments?|knowledge checks?|questions?|answer keys?|answers?|exercises?|activit\w*|practice|tests?|self-check)\b", re.IGNORECASE)
_EXAMPLE_RULE_RE = re.compile(r"\b(examples?|analog\w*|case stud\w*|scenarios?|real[- ]world|stories|story|metaphors?)\b", re.IGNORECASE)
_TERMS_RE = re.compile(r"\b(terms?|terminology|jargon|definitions?|define|glossary|acronyms?|vocabulary)\b", re.IGNORECASE)
_INTRO_RE = re.compile(r"\b(introduc\w*|intro|open(s|ing)? with|begin(s|ning)? with|start(s|ing)? with|hook)\b", re.IGNORECASE)
_OUTRO_RE = re.compile(r"\b(conclu\w*|end(s|ing)? with|close(s)? with|wrap(s)? up|summar\w*|key takeaways?|recap)\b", re.IGNORECASE)
_TONE_RE = re.compile(r"\b(tone|voice|friendly|formal|informal|conversational|professional|warm|encouraging|authoritative|plain language|plain english)\b", re.IGNORECASE)
_LEVEL_RE = re.compile(r"\b(reading level|reading age|grade \d+|beginner|intermediate|advanced|novice|expert|cefr|b1|b2|c1)\b", re.IGNORECASE)
_DEPTH_RE = re.compile(r"\b(depth|in depth|in-depth|detailed|detail|thorough|concise|brief|high[- ]level|deep dive|comprehensive)\b", re.IGNORECASE)
_LENGTH_RE = re.compile(r"\b(\d+\s*words?|short paragraphs?|long paragraphs?|sentences? per paragraph|paragraph length)\b", re.IGNORECASE)

_DESIGN_REQUEST_RE = re.compile(
    r"\b(use|apply|keep|match|follow|reproduce|copy|retain)\b.{0,30}\b(this|the|our|same)\b.{0,30}\b(design|layout|"
    r"styling|branding|brand colou?rs|colou?r scheme|colou?rs|fonts?|look and feel)\b",
    re.IGNORECASE,
)
_DECORATIVE_BAN_RE = re.compile(
    r"\b(no|avoid|never|without|don't use|do not use|not use)\b.{0,30}\b(decorative|stock|filler|ornamental|"
    r"section[- ]opening|clip ?art)\b",
    re.IGNORECASE,
)
_NO_VISUALS_RE = re.compile(r"\b(text[- ]only|no (images|visuals|pictures|diagrams|illustrations) at all|do not (use|include) any (images|visuals|pictures))\b", re.IGNORECASE)
_REUSABLE_RE = re.compile(
    r"\b(logo|brand mark|wordmark|reuse|re-use|use this (image|picture|logo|graphic)|in every (course|chapter|module)|"
    r"on every (page|chapter)|always include this)\b",
    re.IGNORECASE,
)

_REQUIRED_BLOCK_RES: tuple[tuple[re.Pattern[str], BlockType], ...] = (
    (re.compile(r"\b(summary|key takeaways?|recap)\b", re.IGNORECASE), BlockType.SUMMARY),
    (re.compile(r"\blearning objectives?\b", re.IGNORECASE), BlockType.LEARNING_OBJECTIVES),
    (re.compile(r"\b(quiz|knowledge check|multiple[- ]choice questions?)\b", re.IGNORECASE), BlockType.QUIZ),
    (re.compile(r"\b(exercise|hands-on activity|practice activity)\b", re.IGNORECASE), BlockType.EXERCISE),
    (re.compile(r"\bcase stud(y|ies)\b", re.IGNORECASE), BlockType.CASE_STUDY),
)
_EVERY_CHAPTER_RE = re.compile(r"\b(every|each|all)\s+(chapters?|modules?|lessons?|units?)\b|\bmust (end|close|finish|begin|start|open) with\b", re.IGNORECASE)

# Order matters: the first matching entry wins.
_KIND_TABLE: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), kind, diagram_kind)
    for pattern, kind, diagram_kind in (
        (r"\bscreen ?shots?\b", "screenshot", ""),
        (r"\b(photos?|photographs?|photographic)\b", "photo", ""),
        (r"\b(flow ?charts?|decision trees?)\b", "diagram", "flow_chart"),
        (r"\btimelines?\b", "diagram", "flow_chart"),
        (r"\bswim ?lanes?\b", "diagram", "swimlane"),
        (r"\b(er diagrams?|entity[- ]relationship)\b", "diagram", "er_diagram"),
        (r"\bdata[- ]flow diagrams?\b", "diagram", "data_flow_diagram"),
        (r"\b(life ?cycle|cycle) (diagrams?|charts?)\b|\bcycle\b.{0,20}\bdiagram", "diagram", "cycle"),
        (r"\bprocess (diagrams?|flows?|charts?)\b|\b(steps?|step-by-step|stages?) diagrams?\b|\bdiagram\b.{0,40}\b(process|steps|stages)\b", "diagram", "process"),
        (r"\b(hierarch\w*|tree diagrams?|org(ani[sz]ation(al)?)? charts?)\b", "diagram", "hierarchy"),
        (r"\b(comparison|side[- ]by[- ]side|compare|versus|vs\.?)\b", "diagram", "comparison"),
        (r"\b(concept|mind) ?maps?\b", "diagram", "concept_map"),
        (r"\binfographics?\b", "diagram", "smart_art"),
        (r"\b(bar|line|pie|column|scatter) (charts?|graphs?)\b|\b(charts?|graphs?|plots?)\b", "chart", ""),
        (r"\b(diagrams?|schematics?|labell?ed drawings?)\b", "diagram", ""),
        (r"\b(illustrations?|drawings?|pictures?)\b", "illustration", ""),
    )
)

_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_COUNT_RE = re.compile(
    r"\b(?:at least |up to |exactly |no more than |a maximum of |a minimum of )?(\d+|a|an|one|single|two|three|four|five|six)\s+"
    r"(?:[a-z\-]+\s+){0,2}(?:diagrams?|images?|visuals?|illustrations?|figures?|charts?|flow ?charts?|pictures?|photos?|graphs?)\b",
    re.IGNORECASE,
)
_ASPECT_RE = re.compile(r"\b(\d{1,2})\s*[:x×]\s*(\d{1,2})\b")
_LABELS_RE = re.compile(r"\blabel(?:l)?ed\s+(?:with\s+|as\s+)?(?:the\s+)?([^.;()]+)|\blabels?\s*[:=]\s*([^.;()]+)", re.IGNORECASE)
_GENERIC_LABEL_RE = re.compile(r"\b(each|all|every|clearly|its|their|parts|components|steps|stages|elements|key|main|relevant|important)\b", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])")

# Writing-pattern features, detected in worked examples.
_FEATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("definition", re.compile(r"\b(is (a|an|the)|refers to|is defined as|means|are (a|an)?\s?\w+ that)\b", re.IGNORECASE)),
    ("analogy", re.compile(r"\b(like an? |think of|imagine|similar to|just as|picture (a|an|this))\b", re.IGNORECASE)),
    ("real-world example", re.compile(r"\b(for example|for instance|in practice|in the real world|real[- ]world|e\.g\.|such as|consider (a|an|the))\b", re.IGNORECASE)),
    ("step-by-step walkthrough", re.compile(r"\b(first|then|next|finally|step \d)\b", re.IGNORECASE)),
    ("question to the reader", re.compile(r"\?")),
    ("recap", re.compile(r"\b(in short|in summary|to sum up|the key (point|idea|takeaway)|remember that)\b", re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = RULE_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _strip_marker(text: str) -> str:
    bracket = _BRACKETED_RE.match(text)
    if bracket:
        text = bracket.group(1)
    return _MARKER_RE.sub("", text).strip()


def is_instruction(text: str) -> bool:
    """Whether a line tells the writer what to do, rather than being sample content."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _MARKER_RE.match(stripped):
        return True
    bracket = _BRACKETED_RE.match(stripped)
    if bracket and len(bracket.group(1).split()) >= 3:
        return True
    if _EXAMPLE_PREFIX_RE.match(stripped):
        return False
    if len(stripped.split()) > 80:
        return False
    if _KV_RE.match(stripped):
        return True
    if _IMPERATIVE_RE.match(stripped) and _WRITING_CONTEXT_RE.search(stripped):
        return True
    return bool(_MODAL_RE.search(stripped) and _WRITING_CONTEXT_RE.search(stripped))


def _features(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for name, pattern in _FEATURES:
        if name == "step-by-step walkthrough":
            hits = list(pattern.finditer(text))
            if len(hits) >= 2:
                found.append((hits[0].start(), name))
            continue
        match = pattern.search(text)
        if match:
            found.append((match.start(), name))
    return [name for _, name in sorted(found)]


_LABEL_STOP_RE = re.compile(
    r"\s+(?:after|before|with|in|at|on|that|which|so|to|for|showing|under|near|beside|below|above|using)\b",
    re.IGNORECASE,
)


def _split_labels(raw: str) -> list[str]:
    # "Input, Processing and Output after the explanation" -> the names only.
    raw = _LABEL_STOP_RE.split(raw, maxsplit=1)[0]
    parts = re.split(r",|;|\band\b|\bor\b|/", raw)
    labels = []
    for part in parts:
        label = part.strip(" '\"“”‘’`-–")
        if not label or len(label) > 40 or len(label.split()) > 4:
            continue
        if _GENERIC_LABEL_RE.search(label) or _VISUAL_RE.search(label):
            continue
        labels.append(label)
    return labels[:10]


def _aspect(text: str) -> tuple[str, str]:
    match = _ASPECT_RE.search(text)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width and height:
            ratio = width / height
            kind = "landscape" if ratio > 1.15 else "portrait" if ratio < 0.87 else "square"
            return kind, match.group(0)
    lowered = text.lower()
    for word, kind in (("landscape", "landscape"), ("widescreen", "landscape"), ("wide", "landscape"),
                       ("portrait", "portrait"), ("square", "square")):
        if re.search(rf"\b{word}\b", lowered):
            return kind, word
    return "", ""


def _kind_of(text: str) -> tuple[str, str]:
    for pattern, kind, diagram_kind in _KIND_TABLE:
        if pattern.search(text):
            return kind, diagram_kind
    return "any", ""


def _support(kind: str) -> tuple[str, str]:
    if kind == "screenshot":
        return "none", (
            "Real screenshots cannot be captured or generated. The writer is told to describe the "
            "screen in text (or use an illustration) instead, and no screenshot is ever reported as made."
        )
    if kind == "chart":
        return "partial", (
            "Charts are drawn by the image model as an illustration; exact data values and scales "
            "are not guaranteed, so do not rely on them for precise figures."
        )
    if kind == "photo":
        return "partial", "Photos are AI-generated illustrations, not real photographs."
    return "full", ""


# A rule line names block types more loosely than a heading does ("open with
# a short scenario" is not a request for a case-study block), so only the
# unambiguous ones are taken from rule text.
_RULE_BLOCK_TYPES = {
    BlockType.QUIZ, BlockType.EXERCISE, BlockType.SUMMARY, BlockType.TABLE, BlockType.CODE,
    BlockType.LEARNING_OBJECTIVES,
}


def _rule_block_types(line: str) -> list[BlockType]:
    found = [block_type for block_type in _block_types_for(line) if block_type in _RULE_BLOCK_TYPES]
    if re.search(r"\bcase stud(y|ies)\b", line, re.IGNORECASE):
        found.append(BlockType.CASE_STUDY)
    return found


@dataclass
class _Section:
    section: TemplateSection
    rules: list[str] = field(default_factory=list)
    subheadings: list[str] = field(default_factory=list)


@dataclass
class _Image:
    node: Node
    section_key: str
    before: str  # nearest text before it in the same area
    after: str
    prev_kind: str  # kind of the node just before it in the same section
    next_kind: str


@dataclass
class BuildResult:
    template: CourseTemplate
    # Extracted pictures to store, paired with the asset that references them.
    asset_files: list[tuple[TemplateAsset, bytes]] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)

    def report(self, parser_version: int) -> dict[str, Any]:
        """Stored on the row as `parse_report_json`; keeps the keys the
        existing UI already reads."""
        blueprint = self.template.blueprint
        assert blueprint is not None
        return {
            "parser_version": parser_version,
            "detected": {
                "sections": len(self.template.sections),
                "visual_rules": len(blueprint.visuals),
                "examples": len(blueprint.examples),
                "assets": len(blueprint.assets),
                "design_applied": blueprint.design.applied,
            },
            "warnings": list(blueprint.warnings),
            "unsupported_placeholders": unknown_names(self.placeholders),
            "supported_placeholders": [name for name in self.placeholders if name in SUPPORTED],
            "sections": [
                {
                    "key": section.key,
                    "label": section.label,
                    "required": section.required,
                    "block_types": [block.value for block in section.block_types],
                }
                for section in self.template.sections
            ],
        }


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------


class _Builder:
    def __init__(self, nodes: list[Node], *, source_format: str) -> None:
        self.nodes = nodes
        self.source_format = source_format
        self.title = ""
        self.sections: list[_Section] = []
        self.global_rules: list[tuple[str, str]] = []  # (text, topic)
        self.example_nodes: list[tuple[str, Node, bool]] = []  # (section_key, node, explicit example)
        self.images: list[_Image] = []
        self.ignored: list[str] = []
        self.warnings: list[str] = []
        self.problems: list[str] = []

    # --- pass 1: zones -----------------------------------------------------
    def walk(self) -> None:
        headings = [node for node in self.nodes if node.kind == "heading"]
        has_title = any(node.level == 0 for node in headings)
        level_counts = Counter(node.level for node in headings if node.level >= 1)
        section_level = min(level_counts) if level_counts else 1
        if not has_title and level_counts.get(section_level) == 1 and level_counts.get(section_level + 1, 0) >= 1:
            # "# Template name" followed by "## Section" headings - the lone top
            # heading is the document title, not a one-section template.
            section_level += 1

        zone = "preamble"  # preamble | rules | section | example
        topic = "general"
        current: _Section | None = None
        sub = ""  # "" | rules | example
        previous_kind = ""

        for index, node in enumerate(self.nodes):
            if node.kind == "heading":
                label = _clean_label(node.text)
                if node.level < section_level or node.level == 0:
                    self.title = self.title or label
                    zone, current, sub = "preamble", None, ""
                elif node.level == section_level:
                    sub = ""
                    if _RULES_HEADING_RE.search(label) and not _EXAMPLE_HEADING_RE.search(label):
                        zone, current = "rules", None
                        topic = "visual" if _VISUAL_TOPIC_RE.search(label) else "general"
                    elif _EXAMPLE_HEADING_RE.search(label) and len(label.split()) <= 4:
                        zone, current = "example", None
                    else:
                        zone = "section"
                        required, _ = _obligation(node.text)
                        current = _Section(
                            TemplateSection(
                                key=_slug(label),
                                label=label,
                                required=required,
                                block_types=_block_types_for(label),
                            )
                        )
                        self.sections.append(current)
                else:
                    if zone == "rules":
                        topic = "visual" if _VISUAL_TOPIC_RE.search(label) else topic
                    elif zone == "section" and current is not None:
                        if _RULES_HEADING_RE.search(label) and not _EXAMPLE_HEADING_RE.search(label):
                            sub = "rules"
                        elif _EXAMPLE_HEADING_RE.search(label):
                            sub = "example"
                        else:
                            sub = ""
                            current.subheadings.append(label)
                            for block_type in _block_types_for(label):
                                if block_type not in current.section.block_types:
                                    current.section.block_types.append(block_type)
                previous_kind = "heading"
                continue

            key = current.section.key if current is not None else ""
            if node.kind == "image":
                self.images.append(
                    _Image(
                        node=node,
                        section_key=key if zone == "section" else "",
                        before=self._neighbour_text(index, -1),
                        after=self._neighbour_text(index, +1),
                        prev_kind=previous_kind,
                        next_kind=self.nodes[index + 1].kind if index + 1 < len(self.nodes) else "",
                    )
                )
                previous_kind = "image"
                continue

            text = node.text.strip()
            previous_kind = "text" if node.kind in ("para", "item", "quote") else node.kind
            if not text:
                continue

            if zone == "rules":
                if node.kind == "table":
                    for row in text.splitlines():
                        self._rule(row.replace(" | ", ": ", 1).replace(" | ", "; "), topic, None)
                elif node.kind != "code":
                    self._rule(text, topic, None)
            elif zone == "example":
                self.example_nodes.append(("", node, True))
            elif zone == "section" and current is not None:
                if sub == "rules" and node.kind not in ("code", "table"):
                    self._rule(text, topic, current)
                elif sub == "example":
                    self.example_nodes.append((key, node, True))
                elif node.kind in ("para", "item", "quote") and is_instruction(text):
                    self._rule(text, topic, current)
                else:
                    self.example_nodes.append((key, node, bool(_EXAMPLE_PREFIX_RE.match(text))))
            else:  # preamble: only explicit instructions count
                if node.kind in ("para", "item") and is_instruction(text):
                    self._rule(text, "general", None)

    def _neighbour_text(self, index: int, step: int) -> str:
        position = index + step
        while 0 <= position < len(self.nodes):
            node = self.nodes[position]
            if node.kind == "heading":
                return ""
            if node.kind in ("para", "item", "quote") and node.text.strip():
                return node.text.strip()
            position += step
        return ""

    def _rule(self, text: str, topic: str, section: _Section | None) -> None:
        cleaned = _strip_marker(text)
        if not cleaned:
            return
        if is_prompt_injection(cleaned):
            self.ignored.append(_clip(cleaned, 160))
            return
        if section is None:
            self.global_rules.append((cleaned, topic))
        else:
            section.rules.append(cleaned)

    # --- pass 2: rules -------------------------------------------------------
    def build(self, *, extracted_theme: TemplateTheme | None, apply_design: bool) -> tuple[TemplateBlueprint, list[BlockType]]:
        text = TextRules()
        visuals: list[VisualRule] = []
        required_blocks: list[BlockType] = []
        section_intro: bool | None = None
        design_requested = False
        no_visuals = False
        tones: list[str] = []

        def add(bucket: list[str], value: str) -> None:
            value = _clip(value)
            if value and value not in bucket and len(bucket) < MAX_RULES_PER_LIST:
                bucket.append(value)

        for raw, topic in self.global_rules:
            line = raw.strip()
            if _DESIGN_REQUEST_RE.search(line) and not _NEGATIVE_RE.search(line):
                design_requested = True
                continue
            if _DECORATIVE_BAN_RE.search(line):
                section_intro = False
            if _NO_VISUALS_RE.search(line):
                no_visuals = True
                section_intro = False
                add(text.avoid, line)
                continue
            if _EVERY_CHAPTER_RE.search(line) and not _NEGATIVE_RE.search(line):
                for pattern, block_type in _REQUIRED_BLOCK_RES:
                    if pattern.search(line) and block_type not in required_blocks:
                        required_blocks.append(block_type)

            kv = _KV_RE.match(line)
            if kv:
                key, value = kv.group(1).lower(), kv.group(2).strip()
                if key in ("tone", "voice", "style", "writing style"):
                    tones.append(value)
                    text.tone = text.tone or _clip(value, 160)
                elif key in ("reading level", "reading age", "audience level", "level"):
                    text.reading_level = text.reading_level or _clip(value, 160)
                elif key in ("depth", "level of detail", "detail"):
                    text.depth = text.depth or _clip(value, 200)
                elif key in ("word count", "words per chapter", "chapter length", "length"):
                    words = _WORDS_RE.search(value) or re.search(r"(\d{2,5})", value)
                    if words:
                        count = int(words.group(1))
                        if words.lastindex and words.lastindex >= 2 and words.group(2):
                            count = (count + int(words.group(2))) // 2
                        if 150 <= count <= 20000:
                            text.words_per_chapter = count
                        else:
                            self.problems.append(f'Ignored chapter length "{value}" - it is outside 150-20,000 words.')
                elif key in ("paragraph length", "paragraphs"):
                    text.paragraph_length = text.paragraph_length or _clip(value, 160)
                elif key == "section length":
                    text.section_length = text.section_length or _clip(value, 160)
                elif key in ("terminology", "vocabulary"):
                    add(text.terminology, value)
                elif key in ("image style", "visual style"):
                    add(text.other, f"Image style: {value}")
                continue

            if _VISUAL_RE.search(line):
                rule = self._visual_rule(line, len(visuals) + 1, scope_section=None, origin="stated")
                if rule is not None:
                    visuals.append(rule)
                    continue
            if topic == "visual":
                add(text.other, line)
                continue

            negative = bool(re.match(r"^\s*(do not|don't|never|avoid|no)\b", line, re.IGNORECASE))
            if _TONE_RE.search(line) and not _ASSESSMENT_RE.search(line):
                tones.append(line)
                text.tone = text.tone or _clip(line, 200)
            elif _LEVEL_RE.search(line):
                text.reading_level = text.reading_level or _clip(line, 200)
            elif _LENGTH_RE.search(line) and re.search(r"paragraph", line, re.IGNORECASE):
                text.paragraph_length = text.paragraph_length or _clip(line, 200)
            elif _LENGTH_RE.search(line) and re.search(r"\b(chapter|module|lesson)\b", line, re.IGNORECASE) and _WORDS_RE.search(line):
                words = _WORDS_RE.search(line)
                count = int(words.group(1))
                if words.group(2):
                    count = (count + int(words.group(2))) // 2
                if 150 <= count <= 20000:
                    text.words_per_chapter = count
                add(text.other, line)
            elif _LENGTH_RE.search(line) and re.search(r"\bsection", line, re.IGNORECASE):
                text.section_length = text.section_length or _clip(line, 200)
            elif negative:
                add(text.avoid, line)
            elif _ASSESSMENT_RE.search(line):
                add(text.assessments, line)
            elif _TERMS_RE.search(line):
                add(text.terminology, line)
            elif _EXAMPLE_RULE_RE.search(line):
                add(text.examples, line)
            elif _INTRO_RE.search(line):
                add(text.introductions, line)
            elif _OUTRO_RE.search(line):
                add(text.conclusions, line)
            elif _DEPTH_RE.search(line):
                text.depth = text.depth or _clip(line, 200)
            elif re.match(r"^\s*(always |must |include|add)\b", line, re.IGNORECASE):
                add(text.include, line)
            else:
                add(text.other, line)

        if len({tone.lower() for tone in tones}) > 1:
            self.problems.append(
                "The template states the tone more than once (" + "; ".join(_clip(t, 60) for t in tones[:3]) +
                f"). The first one is used: {text.tone}."
            )

        # Section-level rules: visuals become rules, the rest is guidance.
        for entry in self.sections:
            guidance: list[str] = []
            for line in entry.rules:
                if _DESIGN_REQUEST_RE.search(line) and not _NEGATIVE_RE.search(line):
                    design_requested = True
                    continue
                if _DECORATIVE_BAN_RE.search(line):
                    section_intro = False
                if _VISUAL_RE.search(line) and not _NO_VISUALS_RE.search(line):
                    rule = self._visual_rule(line, len(visuals) + 1, scope_section=entry.section.key, origin="stated")
                    if rule is not None:
                        visuals.append(rule)
                        guidance.append(line)
                        continue
                guidance.append(line)
                for block_type in _rule_block_types(line):
                    if block_type not in entry.section.block_types:
                        entry.section.block_types.append(block_type)
            parts: list[str] = []
            if guidance:
                parts.append(" ".join(_clip(line, 200) for line in guidance))
            if entry.subheadings:
                parts.append("Covers: " + "; ".join(entry.subheadings))
            entry.section.guidance = _clip(" ".join(parts), GUIDANCE_CHARS)
            entry.section.max_blocks = max(3, min(len(entry.subheadings) + len(guidance) or 1, 10))

        examples, asset_files_meta = self._examples_and_assets(visuals)
        self._infer_text_patterns(text, examples)

        if no_visuals and any(rule.required for rule in visuals):
            self.problems.append(
                "The template says to use no images, but also requires visuals. The required visuals are kept; "
                "remove one of the two instructions to make the template consistent."
            )
        known = {entry.section.key for entry in self.sections}
        for rule in visuals:
            if rule.scope == "section" and rule.section_key not in known:
                self.problems.append(
                    f'Visual rule {rule.id} names a section that does not exist ("{rule.section_key}"); '
                    "it now applies to the chapter as a whole."
                )
                rule.scope, rule.section_key = "chapter", ""
            if rule.support == "none" and rule.required:
                self.problems.append(f"Visual rule {rule.id} asks for something the system cannot make ({rule.kind}). {rule.note}")

        design = DesignSpec()
        if extracted_theme is not None:
            design.extracted = extracted_theme.model_dump(mode="json", exclude_none=True)
            if apply_design:
                design.applied, design.reason = True, "uploader"
            elif design_requested:
                design.applied, design.reason = True, "document"
        elif design_requested:
            self.warnings.append(
                "The template asks for its own design, but a Markdown file carries no design to apply."
            )

        blueprint = TemplateBlueprint(
            source_format=self.source_format,  # type: ignore[arg-type]
            text=text,
            visuals=visuals[:MAX_VISUALS],
            section_intro_images=section_intro,
            examples=examples,
            assets=[asset for asset, _ in asset_files_meta],
            design=design,
            warnings=self.warnings,
            ignored=self.ignored[:20],
            problems=self.problems,
        )
        self._asset_files = asset_files_meta
        return blueprint, required_blocks

    def _visual_rule(self, line: str, number: int, *, scope_section: str | None, origin: str) -> VisualRule | None:
        if _NO_VISUALS_RE.search(line):
            return None
        if _DECORATIVE_BAN_RE.search(line) and not _REQUIRED_RE.search(line.split(",")[0]):
            return None
        kind, diagram_kind = _kind_of(line)
        support, note = _support(kind)
        lowered = line.lower()

        if scope_section is not None:
            scope, section_key = "section", scope_section
        elif re.search(r"\b(each|every|per)\s+(sub-?)?sections?\b", lowered):
            scope, section_key = "each_section", ""
        else:
            scope, section_key = "chapter", ""
            match = self._section_named_in(lowered)
            if match:
                scope, section_key = "section", match

        if re.search(r"\bafter\b", lowered):
            placement = "after_text"
        elif re.search(r"\b(before|at the (start|beginning|top) of|opens? with|begins? with|starts? with)\b", lowered):
            placement = "before_text"
        elif re.search(r"\b(at the end|end of|ends? with|closes? with|close the)\b", lowered):
            placement = "end_of_section"
        else:
            placement = "anywhere"

        required = bool(_REQUIRED_RE.search(lowered) or _IMPERATIVE_RE.match(line)) and not _OPTIONAL_RE.search(lowered)

        labels: list[str] = []
        labelled = bool(re.search(r"\blabel", lowered))
        for match in _LABELS_RE.finditer(line):
            labels.extend(_split_labels(match.group(1) or match.group(2) or ""))

        count = None
        count_match = _COUNT_RE.search(line)
        if count_match:
            word = count_match.group(1).lower()
            count = int(word) if word.isdigit() else _NUMBER_WORDS.get(word)
            if count is not None and not 1 <= count <= 10:
                count = None

        aspect, aspect_text = _aspect(line)
        if aspect and kind == "diagram":
            note = (note + " " if note else "") + (
                "Aspect ratios apply to generated pictures; diagrams are sized to fit their content."
            )
            support = "partial" if support == "full" else support

        return VisualRule(
            id=f"V{number}",
            scope=scope,  # type: ignore[arg-type]
            section_key=section_key,
            kind=kind,  # type: ignore[arg-type]
            diagram_kind=diagram_kind,
            instruction=_clip(line),
            required=required,
            placement=placement,  # type: ignore[arg-type]
            labels=list(dict.fromkeys(labels)),
            labelled=labelled or bool(labels),
            caption_required=bool(re.search(r"\bcaption", lowered)) and not re.search(r"\b(no|without) (a )?captions?\b", lowered),
            count=count,
            aspect=aspect,  # type: ignore[arg-type]
            aspect_text=aspect_text,
            origin=origin,  # type: ignore[arg-type]
            support=support,  # type: ignore[arg-type]
            note=note.strip(),
        )

    def _section_named_in(self, lowered: str) -> str:
        best = ""
        best_len = 0
        for entry in self.sections:
            label = entry.section.label.lower()
            if len(label) >= 4 and label in lowered and len(label) > best_len:
                best, best_len = entry.section.key, len(label)
        return best

    # --- examples, pictures, inferred patterns ----------------------------------
    def _examples_and_assets(self, visuals: list[VisualRule]) -> tuple[list[TemplateExample], list[tuple[TemplateAsset, bytes | None]]]:
        examples: list[TemplateExample] = []

        # Consecutive text nodes of one section form one example.
        grouped: list[tuple[str, str, list[Node]]] = []
        for key, node, _explicit in self.example_nodes:
            kind = {"code": "code", "table": "table"}.get(node.kind, "text")
            if grouped and grouped[-1][0] == key and grouped[-1][1] == kind == "text":
                grouped[-1][2].append(node)
            else:
                grouped.append((key, kind, [node]))

        for key, kind, nodes in grouped:
            body = "\n".join(node.text.strip() for node in nodes if node.text.strip())
            body = _EXAMPLE_PREFIX_RE.sub("", body)
            if not body or len(body.split()) < 4:
                continue
            if is_prompt_injection(body):
                self.ignored.append(_clip(body, 160))
                continue
            if kind == "code":
                self._add_block_type(key, BlockType.CODE)
            if kind == "table":
                self._add_block_type(key, BlockType.TABLE)
            if len(examples) >= MAX_EXAMPLES:
                continue
            examples.append(
                TemplateExample(
                    id=f"E{len(examples) + 1}",
                    section_key=key,
                    kind=kind,  # type: ignore[arg-type]
                    text=_clip_block(body, EXAMPLE_CHARS),
                    pattern=_features(body) if kind == "text" else [],
                )
            )

        assets: list[tuple[TemplateAsset, bytes | None]] = []
        for image in self.images:
            caption = _clip(image.node.caption, 200)
            context = _clip(image.before or image.after, 300)
            reusable = bool(_REUSABLE_RE.search(f"{caption} {context}"))
            asset: TemplateAsset | None = None
            if image.node.data is not None:
                asset = TemplateAsset(
                    id=f"A{len(assets) + 1}",
                    content_type=image.node.content_type,
                    caption=caption,
                    context=context,
                    section_key=image.section_key,
                    role="reusable" if reusable else "example",
                )
                assets.append((asset, image.node.data))

            described = caption or context
            kind, diagram_kind = _kind_of(f"{caption} {context}")
            if reusable and asset is not None:
                every = bool(re.search(r"\bevery (chapter|course|module|page)\b", f"{caption} {context}", re.IGNORECASE))
                visuals.append(
                    VisualRule(
                        id=f"V{len(visuals) + 1}",
                        scope="chapter" if every or not image.section_key else "section",
                        section_key="" if every else image.section_key,
                        kind="illustration",
                        instruction=_clip(f"Reuse the template's picture {asset.id}: {described or 'no description'}"),
                        required=every or bool(_REQUIRED_RE.search(context.lower())),
                        placement="before_text" if image.prev_kind in ("heading", "") else "after_text",
                        caption_required=False,
                        asset_id=asset.id,
                        origin="stated",
                    )
                )
                continue

            example_id = f"E{len(examples) + 1}"
            if len(examples) < MAX_EXAMPLES:
                examples.append(
                    TemplateExample(
                        id=example_id,
                        section_key=image.section_key,
                        kind="image",
                        text=_clip(context, 300),
                        caption=caption,
                        asset_id=asset.id if asset is not None else "",
                    )
                )
            # The picture shows where a visual goes in this kind of section.
            # Recorded as an inferred, optional rule unless the template
            # already states a rule for the same place.
            stated_here = any(
                rule.origin == "stated" and (rule.section_key == image.section_key or rule.scope == "each_section")
                for rule in visuals
            )
            if stated_here:
                continue
            if image.prev_kind in ("text", "table", "code"):
                placement = "after_text"
            elif image.next_kind in ("para", "item", "quote"):
                placement = "before_text"
            else:
                placement = "anywhere"
            support, note = _support(kind)
            visuals.append(
                VisualRule(
                    id=f"V{len(visuals) + 1}",
                    scope="section" if image.section_key else "chapter",
                    section_key=image.section_key,
                    kind=kind,  # type: ignore[arg-type]
                    diagram_kind=diagram_kind,
                    instruction=_clip(
                        "The template shows a visual here"
                        + (f": {described}" if described else "")
                        + " - make one with the same purpose for this course's own topic."
                    ),
                    required=False,
                    placement=placement,  # type: ignore[arg-type]
                    caption_required=bool(caption),
                    origin="inferred",
                    support=support,  # type: ignore[arg-type]
                    note=note,
                )
            )
        return examples, assets

    def _add_block_type(self, key: str, block_type: BlockType) -> None:
        for entry in self.sections:
            if entry.section.key == key and block_type not in entry.section.block_types:
                entry.section.block_types.append(block_type)

    def _infer_text_patterns(self, text: TextRules, examples: list[TemplateExample]) -> None:
        prose = [example for example in examples if example.kind == "text"]
        if not prose:
            return
        if not text.paragraph_length:
            lengths = [
                len(paragraph.split())
                for example in prose
                for paragraph in example.text.split("\n")
                if len(paragraph.split()) >= 15
            ]
            # Too few samples say nothing reliable about a house style.
            if len(lengths) >= 3:
                average = round(sum(lengths) / len(lengths) / 10) * 10
                text.paragraph_length = f"about {average} words per paragraph, like the template's examples"
                text.inferred.append("paragraph_length")
        sequences = Counter(tuple(example.pattern) for example in prose if len(example.pattern) >= 2)
        if sequences and not text.explanation_patterns:
            pattern, _ = sequences.most_common(1)[0]
            text.explanation_patterns.append(
                "Explain concepts in the order the template's examples use: " + " → ".join(pattern) + "."
            )
            text.inferred.append("explanation_patterns")


def _clip_block(text: str, limit: int) -> str:
    """Like _clip but keeps line breaks, which matter for code and lists."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _writer_guidance(name: str, sections: list[TemplateSection], blueprint: TemplateBlueprint) -> str:
    lines = [
        f'Follow the uploaded template "{name}": use its sections in the order listed, with their headings, '
        "and apply its TEMPLATE BLUEPRINT rules."
    ]
    if not any(section.required for section in sections) and sections:
        lines.append(
            "The template marks no section as explicitly required, so treat every section as expected but "
            "omit any that genuinely does not apply."
        )
    return " ".join(lines)


def build_template(
    nodes: list[Node],
    *,
    template_id: str,
    name: str,
    kind: str,
    description: str = "",
    source_format: str,
    extracted_theme: TemplateTheme | None = None,
    apply_design: bool = False,
    reader_warnings: list[str] | None = None,
    placeholders: list[str] | None = None,
) -> BuildResult:
    """Build the template. Raises ValidationFailedError when the file holds
    nothing a course could be generated from - never a silent fallback."""
    builder = _Builder(nodes, source_format=source_format)
    builder.warnings.extend(reader_warnings or [])
    builder.walk()
    blueprint, required_blocks = builder.build(extracted_theme=extracted_theme, apply_design=apply_design)
    sections = [entry.section for entry in builder.sections]

    seen: dict[str, int] = {}
    for section in sections:
        if section.key in seen:
            seen[section.key] += 1
            section.key = f"{section.key}_{seen[section.key]}"
        else:
            seen[section.key] = 1

    if not sections and blueprint.text.is_empty() and not blueprint.visuals:
        raise ValidationFailedError(
            "No section headings or instructions were found in this file, so it cannot be used as a template. "
            "Use headings for the parts of a chapter (Heading 1 in Word, # or ## in Markdown) and write "
            "instructions such as \"Explain each concept with a real-world example\"."
        )
    if not sections:
        blueprint.warnings.append(
            "No section headings were found, so chapters keep the default structure; only the writing and "
            "visual rules apply."
        )
    if blueprint.ignored:
        blueprint.warnings.append(
            f"{len(blueprint.ignored)} line(s) were ignored because they try to change how the AI system behaves."
        )

    found_placeholders: list[str] = list(placeholders or [])
    for node in nodes:
        found_placeholders.extend(names_in(node.text))
    found_placeholders = list(dict.fromkeys(found_placeholders))

    image_style = [line[len("Image style: "):] for line in blueprint.text.other if line.startswith("Image style: ")]
    review_focus = ["Every template section is present, with its heading, in template order."]
    if any(rule.required for rule in blueprint.visuals):
        review_focus.append("Every required visual from the template blueprint is present, in its section and position.")
    review_focus.append("The chapter follows the template's patterns without copying its example wording.")

    theme = extracted_theme if (extracted_theme is not None and blueprint.design.applied) else TemplateTheme()
    template = CourseTemplate(
        template_id=template_id,
        kind=kind,
        name=name,
        description=description or f"Uploaded {source_format.upper()} template ({len(sections)} sections)",
        sections=sections,
        allowed_block_types=[],
        required_block_types=required_blocks,
        writer_guidance=_writer_guidance(name, sections, blueprint),
        research_guidance=(
            "Research the depth each template section implies; the template's own headings define the scope "
            "of the chapter."
        ),
        review_focus=review_focus,
        image_guidance="; ".join(image_style),
        theme=theme,
        block_styles={},
        blueprint=blueprint,
    )
    result = BuildResult(template=template, placeholders=found_placeholders)
    result.asset_files = [(asset, data) for asset, data in builder._asset_files if data is not None]
    return result
