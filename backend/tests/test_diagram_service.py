"""`DiagramService.generate_for_block`'s corrective-retry logic - previously
untested directly (only indirectly, through `ImageService`'s own tests).

Pipeline reliability follow-up: node-count/kind-mismatch/connectivity used to
each fire their own SEQUENTIAL single-issue retry call (up to 3 extra AI
calls before content QA even ran). They're now collected against the one
initial spec and folded into a SINGLE combined retry - this file proves the
call count actually dropped, not just that the end result still looks right.
"""

from __future__ import annotations

from app.agents.prompts import WRITER_SYSTEM
from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.diagram import DIAGRAM_KIND_PROMPT_DRIFT_EXCEPTIONS, DIAGRAM_KINDS
from app.schemas.document import Block
from app.services import mock_ai as mock_ai_module
from app.services.diagram_service import DIAGRAM_SYSTEM, DiagramService
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
