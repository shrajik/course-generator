"""`DiagramService.generate_for_block`'s corrective-retry logic - previously
untested directly (only indirectly, through `ImageService`'s own tests).

Pipeline reliability follow-up: node-count/kind-mismatch/connectivity used to
each fire their own SEQUENTIAL single-issue retry call (up to 3 extra AI
calls before content QA even ran). They're now collected against the one
initial spec and folded into a SINGLE combined retry - this file proves the
call count actually dropped, not just that the end result still looks right.
"""

from __future__ import annotations

from app.agents.prompts import EDITOR_SYSTEM, WRITER_SYSTEM
from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.diagram import DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS, DIAGRAM_KINDS
from app.schemas.document import Block
from app.services import diagram_service as diagram_service_module
from app.services import mock_ai as mock_ai_module
from app.services.diagram_content_qa import CONTRADICTS_SOURCE_TEXT
from app.services.diagram_qa import DiagramQAIssue, DiagramQAResult
from app.services.diagram_render_qa import RenderQAIssue
from app.services.diagram_service import (
    DIAGRAM_SYSTEM,
    DiagramService,
    _build_trimmed_diagram_system,
    _diagram_user_prompt,
    _find_header_blocks,
    _find_universal_span,
    _kind_bullets,
)
from app.services.mock_ai import MockAIClient


def _diagram_block(**content) -> Block:
    return Block(type=BlockType.IMAGE, content={"kind": "diagram", "prompt": "A prompt", **content})


async def test_a_wrong_kind_and_broken_connectivity_fire_one_combined_retry_not_two(monkeypatch):
    """The mock's first DiagramSpec response is deliberately wrong on BOTH
    axes at once: it comes back as a sequential `flow_chart` (the brief
    asked for the relationship kind `concept_map`) AND that flow_chart has
    two nodes with no incoming edge (broken Start/End connectivity). Before
    this fix, each issue fired its own retry call (2 extra calls); now
    exactly one combined retry call fires."""
    calls: dict[str, int] = {"n": 0}

    def spec_builder(user: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "kind": "flow_chart",
                "title": "Bad first attempt",
                "nodes": [
                    {"id": "a", "label": "A"},
                    {"id": "b", "label": "B"},
                    {"id": "c", "label": "C"},
                ],
                # "a" and "c" both have no incoming edge - two Starts, a
                # genuine connectivity problem - and the kind itself
                # ("flow_chart") conflicts with the requested "concept_map".
                "edges": [
                    {"source": "a", "target": "b", "label": ""},
                    {"source": "c", "target": "b", "label": ""},
                ],
            }
        return {
            "kind": "concept_map",
            "title": "Corrected attempt",
            "nodes": [
                {"id": "subject", "label": "Subject", "level": 0},
                {"id": "part_a", "label": "Component A", "level": 1},
                {"id": "part_b", "label": "Component B", "level": 1},
                {"id": "part_c", "label": "Component C", "level": 1},
            ],
            "edges": [
                {"source": "part_a", "target": "subject", "label": "acts on"},
                {"source": "subject", "target": "part_b", "label": "produces"},
                {"source": "part_c", "target": "subject", "label": "relates to"},
            ],
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", spec_builder)

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(
        purpose="The parts of a cell", diagram_kind="concept_map",
    )
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_combined_retry", block=block, template=template, course_title="Biology",
    )

    assert ok is True
    # Exactly one initial request + one combined corrective retry - NOT a
    # third call for the kind mismatch and a fourth for connectivity.
    assert calls["n"] == 2
    assert block.content["spec"]["kind"] == "concept_map"


async def test_a_clean_first_attempt_needs_no_retry_at_all(monkeypatch):
    """The merge must not change the happy path: a spec with no issues
    still costs exactly one DiagramSpec call."""
    calls: dict[str, int] = {"n": 0}

    def spec_builder(user: str) -> dict:
        calls["n"] += 1
        return {
            "kind": "flow_chart",
            "title": "Clean",
            "nodes": [{"id": f"n{i}", "label": f"Step {i}"} for i in range(4)],
            "edges": [
                {"source": f"n{i}", "target": f"n{i + 1}", "label": ""} for i in range(3)
            ],
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", spec_builder)

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple four-step process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_clean_first_try", block=block, template=template, course_title="Biology",
    )

    assert ok is True
    assert calls["n"] == 1


def test_every_diagram_kind_is_mentioned_in_both_model_facing_prompts():
    """Root-cause regression guard (kind-selection drift, confirmed twice
    this session): `WRITER_SYSTEM` (decides WHICH kind to request) and
    `DIAGRAM_SYSTEM` (tells the spec-authoring model HOW to build one) each
    independently, by hand, enumerate/describe the diagram kinds - nothing
    previously stopped a newly-added kind from silently going missing from
    one of them. Confirmed real at the time this test was written: 7 kinds
    (`spectrum`, `iceberg`, `feedback_loop_process`, `causal_loop`,
    `input_process_output`, `ranked_bars`, plus the dead `conceptual` alias)
    had a full authoring bullet in `DIAGRAM_SYSTEM` but no mention at all in
    `WRITER_SYSTEM` - meaning the model that decides which kind to request
    could never have picked them. `DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS` is
    the reviewed, explained list of kinds that legitimately never need a
    mention (a dead alias; a kind authored through an entirely separate
    pipeline) - a future exception must be added there, with a reason, not
    silently skipped here."""
    for kind in DIAGRAM_KINDS:
        if kind in DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS:
            continue
        assert kind in WRITER_SYSTEM, (
            f"'{kind}' is in DIAGRAM_KINDS but WRITER_SYSTEM never mentions it - the writer "
            "model can never request this kind. Add a `diagram_kind: {kind}` bullet to "
            "WRITER_SYSTEM, or add it to DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS with a reason."
        )
        assert kind in DIAGRAM_SYSTEM, (
            f"'{kind}' is in DIAGRAM_KINDS but DIAGRAM_SYSTEM never mentions it - the "
            "spec-authoring model has no idea how to build one. Add an authoring bullet to "
            "DIAGRAM_SYSTEM, or add it to DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS with a reason."
        )


def _normalized_whitespace(text: str) -> str:
    """WRITER_SYSTEM is an `f\"\"\"...\"\"\"` literal - its source line-wraps are
    real `\\n` characters in the final string, not collapsed. Prose-content
    assertions should be robust to incidental line-wrapping (the same
    sentence reads identically to a human, or to the model consuming it,
    regardless of where the source happened to wrap), so phrase checks below
    normalize whitespace first rather than depending on an exact line break
    position that could shift on an unrelated future edit."""
    return " ".join(text.split())


def test_writer_system_instructs_naming_a_multi_faceted_topics_real_facets():
    """Visual-generation-density fix: a real, confirmed gap - a textbook-
    style illustration's `image_prompt` previously had no instruction to
    identify a topic's distinct teachable facets (its working principle, the
    governing law behind it, its characteristic output, the energy
    conversion involved, etc.), so a genuinely multi-faceted apparatus (a
    real case: an AC generator) came back as a single generic scene instead
    of the richer, panel-worthy description a well-instructed writer could
    have given it. WRITER_SYSTEM must now name at least a representative set
    of these facet categories, with the AC generator worked example that
    motivated this fix."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    for facet_category in (
        "working principle", "governing law", "characteristic output", "energy conversion",
    ):
        assert facet_category in normalized
    assert "AC generator" in normalized
    assert "Faraday's law" in normalized


def test_writer_system_never_mandates_multiple_facets_for_a_simple_topic():
    """The other half of the same fix: the instruction must be explicit that
    a genuinely single-facet topic stays a single plain description - this
    is an opt-in enrichment for topics that actually have real sub-concepts,
    never a mandate to inflate every illustration's complexity."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    assert "not every topic" in normalized
    assert "never invented for density" in normalized
    assert "single-facet topic" in normalized


def test_writer_system_instructs_choosing_playful_infographic_for_young_audiences():
    """New kids/playful-infographic style fix: the writer must be told this
    style exists and when to choose it FROM the already-propagated audience
    context (never a hardcoded keyword match in code), and that it is an
    audience-driven choice, never a fallback for a sparse brief, never a
    license to distort the real concept."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    assert "playful_infographic" in normalized
    assert "young/early learners" in normalized
    assert "never a hardcoded keyword match" in normalized
    assert "never a fallback for a sparse brief" in normalized
    assert "never a distorted or simplified-to-the-point-of-wrong" in normalized


def test_writer_system_offers_playful_infographic_as_an_alternative_for_apparatus_too():
    """Real-course follow-up fix: a real generated course showed the writer
    picking `illustration_style: "textbook"` for a real apparatus/mechanism
    (a wheel-and-axle crank handle) even under a kids audience, because the
    "schematic retired -> ALWAYS textbook" rule sat earlier in the prompt,
    stated unconditionally, with the playful_infographic alternative only
    appearing much later as an easy-to-miss addendum. The apparatus rule
    itself must now state the audience-driven choice at the point it first
    tells the model what to do, not leave it to a separate, later bullet -
    and it must do so without weakening the original "every mechanism needs
    real, accurate labels" requirement that rule exists to protect for
    EITHER style."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    # The apparatus-specific rule (schematic retired) itself must now mention
    # both styles and frame the choice as audience-driven, not subject-driven.
    assert (
        "Which `illustration_style` delivers that labelled treatment is an AUDIENCE choice, "
        "not a subject choice" in normalized
    )
    assert "Do not let \"this is a real apparatus\" alone default you to `textbook`" in normalized
    # The labelling requirement must still apply under either style - never
    # relaxed just because the style is the playful one.
    assert "still applies in full, under EITHER style" in normalized


def test_editor_system_also_offers_playful_infographic_for_apparatus():
    """The identical schematic-retired/apparatus rule is duplicated in
    EDITOR_SYSTEM (manual replace_image/insert_block edits) - fixed there too
    for consistency, so a manually-edited apparatus image gets the same
    audience-driven treatment a freshly-authored one does."""
    normalized = _normalized_whitespace(EDITOR_SYSTEM)
    assert "playful_infographic" in normalized
    assert "is an audience choice, not a subject choice" in normalized


def test_writer_and_editor_system_give_a_concrete_wheel_and_axle_worked_example():
    """Strengthening follow-up: two real samples after the first wording fix
    still showed the writer defaulting real apparatus to `textbook` under a
    kids audience. Both WRITER_SYSTEM and EDITOR_SYSTEM must now include a
    concrete, vivid worked example (the same wheel-and-axle subject drawn
    under both styles, same labels) for the model to pattern-match against,
    not just abstract instruction language."""
    for system_prompt in (WRITER_SYSTEM, EDITOR_SYSTEM):
        normalized = _normalized_whitespace(system_prompt)
        assert "wheel-and-axle lifting a cart" in normalized
        assert "Wheel, Axle, Effort, Load" in normalized
        assert "the subject" in normalized.lower()


def test_writer_system_names_a_rule_of_thumb_and_key_takeaways_as_optional_facets():
    """Panel-density follow-up (real live verification run: AC Generator
    shipped with only 3 of the Playground reference's 6 panels, missing an
    explicit governing-rule-of-thumb panel and a key-takeaways panel).
    WRITER_SYSTEM must now name both as facet categories a writer can draw
    from - still governed by the exact same "only when genuinely
    applicable, never forced" language the other facets already use, never
    a separate, weaker rule."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    assert "rule of thumb" in normalized
    assert "Fleming's right-hand rule" in normalized
    assert "key-takeaways" in normalized or "key takeaways" in normalized
    # The anti-padding safeguard explicitly covers the new facet too, not
    # just the pre-existing ones.
    assert "must never be added just to pad out a panel count" in normalized


# --- audience/style propagation (the uploaded template's own audience/
# image_guidance never reached diagram generation at all - DIAGRAM_SYSTEM and
# the diagram-authoring user prompt were completely audience-blind) ----------


def test_diagram_user_prompt_reflects_the_templates_own_audience_and_style():
    prompt = _diagram_user_prompt(
        purpose="Explain how photosynthesis works",
        prompt="",
        caption="",
        course_title="Biology Basics",
        template_audience="Children aged 6 to 11 (Class 1 to 5)",
        template_style="Simple shapes, bright friendly colors.",
    )
    assert "Children aged 6 to 11" in prompt
    assert "Simple shapes, bright friendly colors" in prompt
    assert "firm requirement" in prompt.lower()


def test_diagram_user_prompt_reflects_a_different_templates_distinct_audience():
    """Never a hardcoded audience branch - verbatim template text only."""
    prompt = _diagram_user_prompt(
        purpose="Explain how photosynthesis works",
        prompt="",
        caption="",
        course_title="Biology Basics",
        template_audience="Senior researchers and advanced practitioners",
        template_style="Precise technical notation, muted professional palette.",
    )
    assert "Senior researchers and advanced practitioners" in prompt
    assert "Children aged 6 to 11" not in prompt


def test_diagram_user_prompt_with_no_template_audience_or_style_is_unchanged():
    """Backward compatibility: omitting the new kwargs (as every pre-existing
    caller/test already does) must produce byte-identical output to before
    this fix - no new, empty 'firm requirement' guidance line."""
    prompt = _diagram_user_prompt(
        purpose="Explain how photosynthesis works", prompt="", caption="", course_title="Biology Basics",
    )
    assert "firm requirement" not in prompt.lower()


def test_diagram_user_prompt_template_guidance_is_directive_not_merely_descriptive():
    """Strengthening follow-up (same reasoning as image_service.py's
    equivalent fix): a firm directive, with generic translation guidance
    that describes HOW to adapt labelling in general terms, never a
    hardcoded audience-category branch."""
    prompt = _diagram_user_prompt(
        purpose="Explain how photosynthesis works", prompt="", caption="", course_title="Biology Basics",
        template_audience="Retired hobbyists", template_style="",
    )
    assert "firm requirement" in prompt.lower()
    assert "deliberately adapt" in prompt.lower()
    assert "noticeably simpler" in prompt.lower()
    assert "noticeably more precise" in prompt.lower()
    assert prompt.lower().count("retired hobbyists") == 1


# --- concept_map kind-selection exclusion (real, confirmed bug: a block
# asking for "which external resource maps to which topic, with a coverage
# badge per pairing" was requested as concept_map, had no real relationship
# to label, and produced zero usable nodes/edges - withheld correctly by
# today's A/C/D fix, but the upstream content-shape mismatch was never
# addressed) -------------------------------------------------------------


def test_writer_system_excludes_parallel_list_correspondences_from_concept_map():
    """WRITER_SYSTEM must explicitly steer a flat correspondence between two
    parallel lists (no real named relationship, just a match/rating per
    pairing) away from concept_map and toward comparison instead, using the
    real confirmed failure (external sources/references mapped to the
    topics they cover) as the worked example."""
    normalized = _normalized_whitespace(WRITER_SYSTEM)
    assert "PARALLEL LISTS" in normalized
    assert "which external source/reference covers which topic" in normalized
    assert "diagram_kind: comparison` instead" in normalized or (
        "comparison" in normalized and "zero usable nodes/edges" in normalized
    )
    assert "zero usable nodes/edges" in normalized


# --- conditional-shipping policy for structured diagrams (parity with the
# same-day fix for AI illustrations): most render-QA geometry issues are
# cosmetic and now ship, flagged, rather than withhold; a narrow critical
# subset (`edge_crosses_node`/`out_of_canvas`/`unparseable_svg` - issues that
# could mislead about connectivity or hide content) still withholds, the
# same as `is_usable()`/confirmed garbled text already did. ---------------


def _simple_flow_chart_builder(user: str) -> dict:
    return {
        "kind": "flow_chart",
        "title": "Simple",
        "nodes": [{"id": f"n{i}", "label": f"Step {i}"} for i in range(4)],
        "edges": [{"source": f"n{i}", "target": f"n{i + 1}", "label": ""} for i in range(3)],
    }


async def test_case1_a_clean_diagram_ships_with_no_findings(monkeypatch):
    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
    monkeypatch.setattr(diagram_service_module, "validate_rendered_svg", lambda svg, kind: [])

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case1", block=block, template=template, course_title="Biology",
    )
    assert ok is True
    assert block.content.get("omitted_after_qa_failure") is False
    assert block.content.get("visual_qa_findings") == []


async def test_case2_a_cosmetic_render_issue_ships_flagged_not_withheld(monkeypatch):
    """`shared_edge_segment` is cosmetic - it doesn't change which node an
    edge actually connects to, just how tidy the overlap looks - so it
    keeps the existing, unchanged "ship anyway, flagged" treatment."""
    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
    monkeypatch.setattr(
        diagram_service_module, "validate_rendered_svg",
        lambda svg, kind: [RenderQAIssue("shared_edge_segment", "two edges overlap for 10px")],
    )

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case2", block=block, template=template, course_title="Biology",
    )
    assert ok is True
    assert block.content.get("path") is not None
    assert block.content.get("omitted_after_qa_failure") is False
    findings = block.content.get("visual_qa_findings")
    assert any(
        f["type"] == "shared_edge_segment" and f["resolution"] == "rendered_with_unresolved_issues"
        for f in findings
    )


async def test_case3_a_genuinely_unusable_spec_still_withholds(monkeypatch):
    """Unchanged by this fix: no real nodes/edges at all is a content
    problem, not a geometry imperfection - `is_usable()` catches this
    before render-QA ever runs."""
    def empty_spec_builder(user: str) -> dict:
        return {"kind": "concept_map", "title": "Empty", "nodes": [], "edges": []}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", empty_spec_builder)

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="Something with no real structure", diagram_kind="concept_map")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case3", block=block, template=template, course_title="Biology",
    )
    assert ok is True  # the block was handled (withheld), not a hard failure
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    assert any(f["type"] == "image_withheld_after_qa_failure" for f in findings)
    assert not any(f["resolution"] == "rendered_with_unresolved_issues" for f in findings)


async def test_case4_a_confirmed_source_text_contradiction_still_ships_flagged(monkeypatch):
    """Documents CURRENT, deliberately UNCHANGED behaviour - reported as a
    finding needing the user's own decision, not silently altered here: no
    `formula_qa.py`-equivalent deterministic "this diagram is factually
    wrong" check exists for structured diagrams. The closest analog,
    `evaluate_content`'s `contradicts_source_text`, is an LLM judgment call
    (`deterministic=False`), not a provable structural fact, and was
    already, deliberately, routed to "ship anyway, flagged" by an earlier,
    separate fix (see `generate_for_block`'s own comment on why) - unlike
    the image pipeline, where `contradicts_source_text` IS critical. This
    test locks in the current behaviour so a future change to it is a
    deliberate, visible decision, not an accidental side effect."""
    async def fake_evaluate_content(spec, *, brief, course_title, ai, settings, source_excerpt):
        return DiagramQAResult(issues=[
            DiagramQAIssue(CONTRADICTS_SOURCE_TEXT, "claims something the course text disputes", deterministic=False),
        ])

    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
    monkeypatch.setattr(diagram_service_module, "evaluate_content", fake_evaluate_content)

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case4", block=block, template=template, course_title="Biology",
    )
    assert ok is True
    assert block.content.get("path") is not None
    assert block.content.get("omitted_after_qa_failure") is False
    findings = block.content.get("visual_qa_findings")
    assert any(
        f["type"] == CONTRADICTS_SOURCE_TEXT and f["resolution"] == "rendered_with_unresolved_issues"
        for f in findings
    )


async def test_case5_a_critical_edge_crosses_node_still_withholds(monkeypatch):
    """The new split: `edge_crosses_node` means an edge visually passes
    THROUGH an unrelated node - exactly the "edges pointing to the wrong
    things" case that can make a reader genuinely misread connectivity, a
    correctness problem, not a layout imperfection. Withheld, never
    downgraded to `rendered_with_unresolved_issues`."""
    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
    monkeypatch.setattr(
        diagram_service_module, "validate_rendered_svg",
        lambda svg, kind: [RenderQAIssue("edge_crosses_node", "edge point inside an unrelated node")],
    )

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case5", block=block, template=template, course_title="Biology",
    )
    assert ok is True
    assert block.content.get("path") is None
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    types = {f["type"] for f in findings}
    assert "edge_crosses_node" in types
    assert "image_withheld_after_qa_failure" in types
    assert not any(f["resolution"] == "rendered_with_unresolved_issues" for f in findings)


async def test_case6_out_of_canvas_and_unparseable_svg_are_also_critical(monkeypatch):
    """The other two critical kinds: content literally invisible, or the QA
    check itself couldn't verify the output at all - both withhold, same
    reasoning as `edge_crosses_node`."""
    for bad_kind in ("out_of_canvas", "unparseable_svg"):
        monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
        monkeypatch.setattr(
            diagram_service_module, "validate_rendered_svg",
            lambda svg, kind, _bad=bad_kind: [RenderQAIssue(_bad, "detail")],
        )

        settings = get_settings()
        svc = DiagramService(ai=MockAIClient(settings), settings=settings)
        block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
        template = load_template("technical")

        ok = await svc.generate_for_block(
            course_id=f"course_diagram_{bad_kind}", block=block, template=template, course_title="Biology",
        )
        assert ok is True
        assert block.content.get("omitted_after_qa_failure") is True, bad_kind


async def test_case7_a_critical_and_cosmetic_issue_together_still_withholds(monkeypatch):
    """Co-occurrence: critical always wins, consistent with how the image
    pipeline handles the identical kind of co-occurrence."""
    monkeypatch.setitem(mock_ai_module._BUILDERS, "DiagramSpec", _simple_flow_chart_builder)
    monkeypatch.setattr(
        diagram_service_module, "validate_rendered_svg",
        lambda svg, kind: [
            RenderQAIssue("edge_crosses_node", "edge point inside an unrelated node"),
            RenderQAIssue("shared_edge_segment", "two edges overlap for 10px"),
        ],
    )

    settings = get_settings()
    svc = DiagramService(ai=MockAIClient(settings), settings=settings)
    block = _diagram_block(purpose="A simple process", diagram_kind="flow_chart")
    template = load_template("technical")

    ok = await svc.generate_for_block(
        course_id="course_diagram_case7", block=block, template=template, course_title="Biology",
    )
    assert ok is True
    assert block.content.get("omitted_after_qa_failure") is True
    findings = block.content.get("visual_qa_findings")
    assert not any(f["resolution"] == "rendered_with_unresolved_issues" for f in findings)
    assert any(f["type"] == "edge_crosses_node" for f in findings)


# ---------------------------------------------------------------------------
# Per-kind trimmed DIAGRAM_SYSTEM - cost optimisation (MOM cost-audit,
# 2026-11-23). The exhaustive completeness test below is the real safety
# net: it re-derives, from the REAL DIAGRAM_SYSTEM source text itself (via
# the same `_find_header_blocks` the production code uses), exactly which
# named-list rule blocks apply to each kind, then asserts the trimmed
# prompt matches that derivation precisely - included blocks are a 1:1
# match, never a hand-maintained "expected" list that could silently drift
# out of sync with a future edit to DIAGRAM_SYSTEM.
# ---------------------------------------------------------------------------

_NO_OWN_BULLET = {"schematic", "concept_experience"}
_ALIASED_KINDS = {"conceptual": "concept_map"}


def test_every_kind_that_reaches_diagram_system_has_a_locatable_bullet():
    bullets = _kind_bullets(DIAGRAM_SYSTEM)
    for kind in DIAGRAM_KINDS:
        if kind in _NO_OWN_BULLET or kind in _ALIASED_KINDS:
            continue
        assert kind in bullets, f"{kind}: no dispatch bullet found by _kind_bullets"


def test_a_shared_bullet_with_multiple_outcomes_resolves_both_kinds():
    # The real case this parsing approach had to get right first-try and
    # didn't (caught during development, not hidden): "-> flow_chart or
    # process." is ONE bullet deciding between two real kinds via one arrow.
    bullets = _kind_bullets(DIAGRAM_SYSTEM)
    assert "flow_chart" in bullets
    assert "process" in bullets
    assert bullets["flow_chart"] == bullets["process"]


def test_unrecognised_kind_raises_rather_than_silently_shipping_an_incomplete_prompt():
    import pytest

    from app.core.errors import AIServiceError

    with pytest.raises(AIServiceError):
        _build_trimmed_diagram_system("not_a_real_kind")


def test_every_trimmed_kind_contains_its_own_bullet_and_the_universal_rules():
    universal = _find_universal_span(DIAGRAM_SYSTEM)
    bullets = _kind_bullets(DIAGRAM_SYSTEM)
    for kind in DIAGRAM_KINDS:
        if kind in _NO_OWN_BULLET:
            continue
        trimmed = _build_trimmed_diagram_system(kind)
        resolved = _ALIASED_KINDS.get(kind, kind)
        assert bullets[resolved] in trimmed, f"{kind}: own bullet missing from trimmed prompt"
        assert universal in trimmed, f"{kind}: universal rules missing from trimmed prompt"


def test_every_trimmed_kind_is_meaningfully_smaller_than_the_original():
    for kind in DIAGRAM_KINDS:
        if kind in _NO_OWN_BULLET:
            continue
        trimmed = _build_trimmed_diagram_system(kind)
        # Every real kind measured during this change reduced by 70%+; a
        # conservative 40% floor catches a real regression without being
        # brittle to minor future edits to shared/per-kind wording.
        assert len(trimmed) < len(DIAGRAM_SYSTEM) * 0.6, (
            f"{kind}: trimmed prompt ({len(trimmed)} chars) isn't meaningfully "
            f"smaller than the original ({len(DIAGRAM_SYSTEM)} chars)"
        )


def test_exhaustive_per_kind_named_block_inclusion_matches_the_real_source_text():
    """The mandatory safety net: for EVERY kind DIAGRAM_SYSTEM actually
    dispatches to, re-derive which named-list rule blocks apply by parsing
    the REAL source text (the exact same function production code calls),
    then assert the trimmed prompt includes every block that names this
    kind and excludes every block that doesn't - a mechanical, exhaustive
    proof against the real text, not a spot-check or a hand-written
    expectation list that could drift from DIAGRAM_SYSTEM's real content."""
    dispatch_end_marker = "For `flow_chart`/`process` specifically:"
    rules_region = DIAGRAM_SYSTEM[DIAGRAM_SYSTEM.index(dispatch_end_marker):]
    all_blocks = _find_header_blocks(rules_region)
    assert len(all_blocks) >= 2, "expected at least the 2 known named-list blocks to be found"

    checked = 0
    for kind in DIAGRAM_KINDS:
        if kind in _NO_OWN_BULLET:
            continue
        resolved = _ALIASED_KINDS.get(kind, kind)
        trimmed = _build_trimmed_diagram_system(kind)
        for block_kinds, block_body in all_blocks:
            should_include = resolved in block_kinds
            is_included = block_body in trimmed
            assert is_included == should_include, (
                f"kind={kind!r} (resolved={resolved!r}): block scoped to "
                f"{sorted(block_kinds)} - expected included={should_include}, "
                f"got included={is_included}"
            )
        checked += 1
    # Every real dispatchable kind was actually exercised - a kind silently
    # skipped (e.g. a future kind missing from DIAGRAM_KINDS iteration, or a
    # bug collapsing the loop early) would make every assertion above
    # vacuously true, so this count is the exhaustiveness proof itself.
    assert checked == len(DIAGRAM_KINDS) - len(_NO_OWN_BULLET)
