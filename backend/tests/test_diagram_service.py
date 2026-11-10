"""`DiagramService.generate_for_block`'s corrective-retry logic - previously
untested directly (only indirectly, through `ImageService`'s own tests).

Pipeline reliability follow-up: node-count/kind-mismatch/connectivity used to
each fire their own SEQUENTIAL single-issue retry call (up to 3 extra AI
calls before content QA even ran). They're now collected against the one
initial spec and folded into a SINGLE combined retry - this file proves the
call count actually dropped, not just that the end result still looks right.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.services import mock_ai as mock_ai_module
from app.services.diagram_service import DiagramService
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
