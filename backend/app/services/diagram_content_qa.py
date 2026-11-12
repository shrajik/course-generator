"""Semantic/content-relevance QA for the generic node+edge diagram family
(flow_chart, process, cycle, hierarchy, comparison, smart_art, concept_map,
data_flow_diagram, er_diagram, swimlane).

Two other QA layers already exist for diagrams - `app.services.diagram_qa`
(schematic's geometry/blueprint checks) and `app.services.diagram_render_qa`
(this family's rendered-SVG geometry: overlap, clipping, font size) - but
neither one ever asks whether the diagram's actual CONTENT is right: a spec
can pass `DiagramSpec.is_usable()` (the right number of labelled nodes and
edges) while those labels are generic placeholders, off-topic, or missing a
concept the brief explicitly asked for. This is the gap behind "a flowchart
with boxes but no meaningful information" - nothing in the pipeline could
previously catch it.

RELEASE-READINESS AUDIT FINDING (live production preview, real screenshots):
this family's labels can ALSO ship plain-Latin garbled/misspelled text (e.g.
"Triggerted probe", "Ambigeous or missing signal", "Ferereberelan",
"auemtcatctor/release", entire sentences scrambled into nonsense) with
NOTHING in the pipeline able to catch it. `diagram_qa._looks_garbled` already
exists and is already wired into `concept_qa` (concept_experience) and
`evaluate_schematic` (schematic) - but it was never wired into THIS module,
and even where it IS wired, it deliberately only catches a narrow signal
(the Unicode replacement character, or a word mixing Latin with Cyrillic/
Greek homoglyphs) - see its own docstring. It cannot and does not catch an
ordinary English misspelling or a scrambled-but-still-all-Latin phrase,
which is exactly what the real screenshots show. Two layers close this gap
for this family, mirroring `concept_qa`'s own two-layer shape:

1. Layer 0 (deterministic, free, reused unchanged from `diagram_qa`): every
   node/edge label and detail is run through `_looks_garbled` BEFORE the LLM
   critique is even requested - same short-circuit contract as `concept_qa`'s
   own structural-then-semantic ordering. Catches the homoglyph/replacement-
   character case for this family too, at zero marginal cost.
2. Layer 1 (the existing `ContentCritique` LLM call, now also asked to judge
   `text_legible`): since no deterministic, dependency-free check can tell
   "Triggerted" from "Triggered" or recognise a scrambled English sentence,
   catching THAT defect class requires real language understanding. This
   piggybacks on the content-critique call this module already makes once
   per graded diagram - no new API call, no new dependency, no duplicate QA
   framework. Like every other issue this module raises, it is an AI
   judgment call (`deterministic=False`), never a proven fact - the same
   honest provenance distinction `app.services.quality_framework` already
   enforces for `off_topic`/`generic_content`/`missing_key_concept`.

One GPT-5-mini structured call, only run once the spec is already
`is_usable()` AND Layer 0 found nothing - a structurally broken or already-
garbled spec is never worth grading semantically, the same short-circuit
contract `app.services.concept_qa` already uses for `concept_experience`.
Reuses `DiagramQAIssue`/`DiagramQAResult` from `diagram_qa` rather than
duplicating them, same reasoning as `concept_qa`'s own docstring: both are
already generic (reason/detail strings), not kind-specific."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.schemas.diagram import DiagramSpec
from app.services.diagram_qa import DiagramQAIssue, DiagramQAResult, _looks_garbled
from app.services.numeric_consistency_qa import (
    IDENTIFIER_VALUE_NEEDS_REVIEW,
    NUMERIC_CONTRADICTS_SOURCE_TEXT,
    find_identifier_value_issues,
    find_numeric_contradictions,
)
from app.services.openai_service import AIClient, get_ai_client

OFF_TOPIC = "off_topic"
GENERIC_CONTENT = "generic_content"
MISSING_KEY_CONCEPT = "missing_key_concept"
MISSPELLED_OR_GARBLED_TEXT = "misspelled_or_garbled_text"
# Deliberately NOT `diagram_qa.GARBLED_TEXT` ("garbled_text") - that exact
# string is already used by `concept_qa`, and `quality_framework`'s source
# dispatch is keyed by `type` alone (see `_visual_qa_source`'s own docstring:
# source must never be guessed from `resolution` alone). Reusing the same
# string here, persisted with a DIFFERENT resolution
# ("fell_back_to_illustration" vs concept_qa's "rendered_with_unresolved_
# issues"), would make that dispatch ambiguous without a resolution-based
# special case. A distinct reason code keeps every reason code's source
# unambiguous by construction - the check itself (`_looks_garbled`) is still
# the exact same reused function, only the persisted label differs.
STRUCTURALLY_GARBLED_TEXT = "structurally_garbled_text"
# Canonical Visual Spec milestone: a diagram that actively CONTRADICTS the
# course's own written text is treated exactly like garbled text above - a
# confirmed defect that falls back to the raster illustration path rather
# than shipping (see `evaluate_content`'s caller, `DiagramService.
# generate_for_block`'s `garbled_types` set), never the "ship anyway, flagged
# for review" treatment `off_topic`/`generic_content`/`missing_key_concept`
# get. `source_grounding_unsupported` is the deliberately DIFFERENT, less
# severe case - a claim the source excerpt simply doesn't address either way
# (neither confirms nor contradicts) - which DOES still ship, flagged for a
# human reviewer (reuses `app.services.quality_framework`'s existing
# `review_findings` bucket, the same `severity="blocker"`+
# `verification_status="unverified"` combination that field already defines
# - see that module's own docstring). These two resolution strings are
# shared with `ImageService._verify_image_relevance`'s matching raster-side
# fields - one unified category across both visual-generation paths, not a
# diagram-specific duplicate.
CONTRADICTS_SOURCE_TEXT = "contradicts_source_text"
SOURCE_GROUNDING_UNSUPPORTED = "source_grounding_unsupported"

# Kinds this module ever grades - every other normalised_kind() (schematic,
# concept_experience) already has its own dedicated semantic check and is a
# deliberate no-op here, never a silent double-charge of two LLM calls for
# the same spec. `periodic_table` joins that deliberate-no-op set for a
# different reason: its grid is real, hardcoded ground truth (app.render.
# periodic_table_data), never model-authored - there is nothing for an LLM
# to grade, and asking one to "verify" a fixed, already-correct dataset only
# risks a false content-QA complaint about something that was never wrong.
_GRADED_KINDS = frozenset(
    {
        "flow_chart", "process", "cycle", "hierarchy", "comparison", "smart_art",
        "concept_map", "data_flow_diagram", "er_diagram", "swimlane",
        "pyramid", "matrix", "radial_mindmap", "venn", "chevron_process", "timeline", "staircase_process",
        "balance_comparison", "nested_circles", "network_mesh", "decision_tree", "fishbone",
        "layered_diagram", "gantt_chart", "funnel", "honeycomb", "puzzle_pieces", "gears_process",
        "donut_segments", "pie_wedge", "wavy_journey_path", "progress_rings", "orbit_diagram",
        "bridge_process", "chain_links", "flower_petal", "hourglass_diagram", "org_chart",
        "circuit_schematic",
    }
)


class ContentCritique(BaseModel):
    """Structured grading of a diagram spec's LABELS (never rendered
    pixels, never layout) against the brief it was supposed to teach."""

    model_config = ConfigDict(extra="ignore")

    on_topic: bool = True
    on_topic_detail: str = ""
    specific_enough: bool = True
    specific_detail: str = ""
    missing_concepts: list[str] = Field(default_factory=list)
    text_legible: bool = True
    text_legible_detail: str = ""
    # Canonical Visual Spec milestone: these two mirror `ImageRelevanceCheck`'s
    # matching fields exactly (see that model's own docstring for the full
    # reasoning) - only ever asked about when a non-blank `source_excerpt` is
    # given (see `_critique_user_prompt`'s own grounding_question), so they
    # stay at their safe defaults (no claim) whenever no excerpt was
    # available to check against. IMPORTANT: this judges CONSISTENCY with
    # the course's own text, never independent scientific/factual truth - a
    # diagram can be `contradicts_source_text=False` while still being
    # objectively wrong about the real world, if the course's own source
    # text happens to share that same error; that is a different problem
    # this field does not and must never claim to catch.
    contradicts_source_text: bool = False
    contradicts_source_text_detail: str = ""
    unsupported_claims: list[str] = Field(default_factory=list)


CONTENT_CRITIQUE_SYSTEM = """\
You are a strict subject-matter reviewer checking whether a diagram's text
is genuinely informative and specific to what it is supposed to teach - you
are never judging layout, spacing or visual style (a separate check already
does that). You see only the diagram's text (title, node labels, edge
labels) - never a rendered picture.

Judge these things:
- `on_topic`: do the labels actually describe THIS brief's subject - not a
  generic, unrelated, or placeholder topic a model might default to when it
  didn't really engage with the brief?
- `specific_enough`: are the labels concrete, specific terms, steps or
  entities a real subject-matter expert would recognise and stand behind -
  not vague filler like "Step 1", "Component A", "Process", "Input"/
  "Output" with no real meaning attached to this exact topic?
- `missing_concepts`: any concept the brief explicitly named that is
  conspicuously absent from the labels (empty list if nothing important is
  missing - do not invent a gap that isn't there).
- `text_legible`: is every label/edge label spelled correctly and readable as
  real words - not a typo a proofreader would immediately flag (e.g.
  "Triggerted" instead of "Triggered", "Ambigeous" instead of "Ambiguous"),
  and not a scrambled or garbled string of letters that doesn't form a real
  word or sentence at all. This is purely about whether the text is
  correctly spelled, readable English (or the brief's working language) -
  never about whether the content is on-topic or well-chosen, which the
  other two judgments above already cover.

Be concrete and specific in every detail field: name exactly what's wrong,
in one sentence, never a bare "could be better". If the diagram is
genuinely good, say so plainly - do not manufacture a problem."""

# Canonical Visual Spec milestone: appended to CONTENT_CRITIQUE_SYSTEM only
# when a source excerpt is actually available (see `_critique_user_prompt`'s
# own conditional) - mirrors ImageService._verify_image_relevance's
# `grounding_question` exactly, same wording/intent, applied to a diagram's
# labels instead of a rendered picture.
_GROUNDING_QUESTION = """

Also judge source-grounding: the course's own written text (given below as
SOURCE TEXT) describes this subject. Does this diagram's labels/edges state
any entity, relationship, direction, or value that DIRECTLY CONTRADICTS
something the source text says (not merely absent from it - a reasonable
illustrative detail a textbook author would add is NOT a contradiction)?
Set `contradicts_source_text` true only for a genuine, specific conflict,
and name it exactly in `contradicts_source_text_detail`. Separately, list in
`unsupported_claims` any specific thing a label/edge clearly states as fact
that the source text simply does not address either way (neither confirms
nor denies) - leave this empty if every claim is either confirmed by the
source text or is ordinary, reasonable illustrative elaboration. IMPORTANT:
this is judging CONSISTENCY with the course's own text, never independent
scientific truth - if the source text itself were wrong, that is a
different problem this judgment does not cover."""


def _critique_user_prompt(spec: DiagramSpec, *, brief: str, course_title: str, source_excerpt: str = "") -> str:
    nodes = "\n".join(
        f"- {node.label}" + (f": {node.detail}" if node.detail.strip() else "")
        for node in spec.nodes
        if node.label.strip()
    ) or "(none)"
    edges = "\n".join(
        f"- {edge.source} -> {edge.target}" + (f" ({edge.label})" if edge.label.strip() else "")
        for edge in spec.edges
    ) or "(none)"
    source_block = (
        f"\n\nSOURCE TEXT (the course's own ground truth to check consistency against - "
        f"not an external authority, just what this course itself already says):\n{source_excerpt}"
        if source_excerpt.strip() else ""
    )
    return f"""\
COURSE: {course_title}

VISUAL BRIEF (what this diagram is supposed to teach):
{brief or "(not specified)"}

DIAGRAM KIND: {spec.normalised_kind()}
TITLE: {spec.title or "(none)"}

NODE LABELS:
{nodes}

EDGES:
{edges}
{source_block}

Judge whether this diagram's labels genuinely, specifically teach the brief above.
"""


def _garbled_label_issues(spec: DiagramSpec) -> list[DiagramQAIssue]:
    """Layer 0: the same deterministic, free `_looks_garbled` check already
    used by `concept_qa`/`evaluate_schematic`, applied here to this family's
    own node/edge text. Narrow by design (homoglyph swaps, the Unicode
    replacement character) - see `_looks_garbled`'s own docstring - so an
    ordinary misspelling is NOT expected to be caught here; that is Layer 1's
    (the LLM critique's `text_legible` judgment) job below."""
    texts = {
        value for node in spec.nodes
        for value in (node.label, node.detail)
        if value and _looks_garbled(value)
    } | {
        value for edge in spec.edges
        for value in (edge.label,)
        if value and _looks_garbled(value)
    }
    if spec.title and _looks_garbled(spec.title):
        texts.add(spec.title)
    if not texts:
        return []
    return [DiagramQAIssue(
        STRUCTURALLY_GARBLED_TEXT,
        "These contain corrupted, garbled text rather than real words: "
        f"{', '.join(repr(t) for t in sorted(texts)[:3])}. Rewrite them as plain, "
        "correctly spelled English text.",
    )]


def _numeric_contradiction_issues(spec: DiagramSpec, source_excerpt: str) -> tuple[list[DiagramQAIssue], list[DiagramQAIssue]]:
    """Layer 0 (Canonical Visual Spec milestone): the same deterministic,
    zero-API-call reasoning `_garbled_label_issues` already uses above,
    applied to `app.services.numeric_consistency_qa`'s own conservative
    number-vs-source-text and identifier-vs-source-text checks instead of
    garbled-text detection. A no-op whenever there's no `source_excerpt` to
    check against (the common case for a block whose section has no real
    prose - see `chapter_section_context`'s own docstring) - absence of an
    excerpt is never itself a reason to flag anything here.

    Returns `(confirmed, review)` - two SEPARATE lists, never one merged
    list, because the two have different downstream treatment (see this
    module's own `evaluate_content`): a CONFIRMED mismatch
    (`NUMERIC_CONTRADICTS_SOURCE_TEXT`/`IDENTIFIER_VALUE_CONTRADICTS_SOURCE_
    TEXT`, `deterministic=True`) is at least as disqualifying as garbled
    text and short-circuits the LLM call exactly like that does. A short-
    identifier case Layer 0 could only resolve as AMBIGUOUS
    (`IDENTIFIER_VALUE_NEEDS_REVIEW`, `deterministic=False` - see that
    reason's own docstring on why this field is deliberately set to False
    for a pure-Python finding: `quality_framework`'s dispatch reads
    `deterministic` as "is this CONFIRMED", not literally "was a model
    involved", and an ambiguous finding must route to `review_findings`,
    never a hard block) must NOT short-circuit - the whole point of the
    hard requirement "use the existing LLM call as a backstop for cases
    that cannot be confidently resolved" is that Layer 1 still gets to run
    and add its own judgment; this finding is merged into whatever Layer 1
    separately decides, never a replacement for it."""
    if not source_excerpt.strip():
        return [], []
    claim_text = "\n".join(
        part for part in (
            spec.title,
            *(f"{n.label} {n.detail}" for n in spec.nodes),
            *(e.label for e in spec.edges),
        ) if part and part.strip()
    )
    confirmed = [
        DiagramQAIssue(issue.reason, issue.detail, deterministic=True)
        for issue in find_numeric_contradictions(claim_text, source_excerpt)
    ]
    review: list[DiagramQAIssue] = []
    for issue in find_identifier_value_issues(claim_text, source_excerpt):
        if issue.reason == IDENTIFIER_VALUE_NEEDS_REVIEW:
            review.append(DiagramQAIssue(issue.reason, issue.detail, deterministic=False))
        else:
            confirmed.append(DiagramQAIssue(issue.reason, issue.detail, deterministic=True))
    return confirmed, review


async def evaluate_content(
    spec: DiagramSpec,
    *,
    brief: str,
    course_title: str,
    ai: AIClient | None = None,
    settings: Settings | None = None,
    source_excerpt: str = "",
) -> DiagramQAResult:
    """A no-op for schematic/concept_experience (each already has its own
    dedicated semantic check elsewhere) and for a blank brief (nothing to
    grade the content against). Only meaningful AFTER the caller's own
    `spec.is_usable()` check already passed - callers own that ordering,
    the same short-circuit contract `concept_qa.evaluate_concept_experience`
    already uses.

    Layer 0 (deterministic) runs first and short-circuits on its own, the
    same structural-then-semantic ordering `concept_qa` uses - a spec with
    already-confirmed garbled text is never worth spending an LLM call to
    grade on top of.

    `source_excerpt` (Canonical Visual Spec milestone), when given, folds the
    source-grounding judgment (`contradicts_source_text`/`unsupported_
    claims`) into this SAME existing content-critique call - zero new API
    calls for the common case, exactly the reasoning `ImageService.
    _verify_image_relevance` documents for its own matching fields. Blank
    (the default) skips asking the grounding question at all, same as an
    empty `brief` already skips the whole call."""
    if spec.normalised_kind() not in _GRADED_KINDS or not brief.strip():
        return DiagramQAResult()

    garbled = _garbled_label_issues(spec)
    if garbled:
        return DiagramQAResult(issues=garbled)

    # Layer 0 continued: a deterministic, high-confidence numeric or
    # identifier-value mismatch against the course's own text is at least as
    # disqualifying as garbled text - stronger evidence than the LLM's own
    # `contradicts_source_text` judgment below, not weaker - so a CONFIRMED
    # one gets the exact same short-circuit (never worth an LLM call on top
    # of an already-confirmed defect). An AMBIGUOUS short-identifier finding
    # is different (see `_numeric_contradiction_issues`'s own docstring) -
    # it does NOT short-circuit, it rides along into the LLM critique below
    # and gets merged with whatever that call separately finds.
    numeric_confirmed, numeric_review = _numeric_contradiction_issues(spec, source_excerpt)
    if numeric_confirmed:
        return DiagramQAResult(issues=numeric_confirmed)

    critique = await (ai or get_ai_client()).structured(
        schema=ContentCritique,
        system=CONTENT_CRITIQUE_SYSTEM + (_GROUNDING_QUESTION if source_excerpt.strip() else ""),
        user=_critique_user_prompt(spec, brief=brief, course_title=course_title, source_excerpt=source_excerpt),
        model=(settings or get_settings()).diagram_model,
        purpose=f"diagram_content_qa:{spec.normalised_kind()}",
        phase="image",
    )
    issues: list[DiagramQAIssue] = []
    # Every issue built here explicitly sets deterministic=False, never the
    # class default - see DiagramQAIssue.deterministic's own docstring: this
    # is an AI model's judgment call about content, not a proven Python
    # structural fact, and must never be mistaken downstream for one.
    if critique.contradicts_source_text:
        issues.append(DiagramQAIssue(
            CONTRADICTS_SOURCE_TEXT,
            critique.contradicts_source_text_detail
            or "This diagram depicts something that contradicts the course's own text.",
            deterministic=False,
        ))
    if critique.unsupported_claims:
        issues.append(DiagramQAIssue(
            SOURCE_GROUNDING_UNSUPPORTED,
            "The course's own text doesn't address: " + "; ".join(critique.unsupported_claims[:5]),
            deterministic=False,
        ))
    if not critique.on_topic:
        issues.append(DiagramQAIssue(
            OFF_TOPIC,
            critique.on_topic_detail
            or "This diagram's labels don't actually describe the requested topic.",
            deterministic=False,
        ))
    if not critique.specific_enough:
        issues.append(DiagramQAIssue(
            GENERIC_CONTENT,
            critique.specific_detail
            or "This diagram's labels are too generic/vague to teach anything specific.",
            deterministic=False,
        ))
    if critique.missing_concepts:
        issues.append(DiagramQAIssue(
            MISSING_KEY_CONCEPT,
            "The brief named these concepts, which are missing from the diagram's labels: "
            + ", ".join(critique.missing_concepts[:5]),
            deterministic=False,
        ))
    if not critique.text_legible:
        issues.append(DiagramQAIssue(
            MISSPELLED_OR_GARBLED_TEXT,
            critique.text_legible_detail
            or "This diagram's labels contain misspelled or garbled text.",
            deterministic=False,
        ))
    # Merged in, never short-circuited - see `_numeric_contradiction_
    # issues`'s own docstring on why an ambiguous short-identifier finding
    # must still let this LLM critique run, rather than replacing it.
    issues.extend(numeric_review)
    return DiagramQAResult(issues=issues)
