"""Course-level diagram-kind diversity - app.services.diagram_diversity.
Mirrors test_diagram_compaction.py's own shape: a pure function, a small
CourseDocument fixture, no real network/AI calls - a tiny stub AIClient
controls what the one possible structured call returns."""

from __future__ import annotations

from app.core.config import get_settings
from app.schemas.blocks import BlockType
from app.schemas.document import Block, BlockLayout, BlockMeta, CourseDocument, DocumentMeta, Page
from app.services.diagram_diversity import balance_diagram_kinds


def _diagram_block(block_id: str, *, kind: str, path: str | None = None) -> Block:
    content = {
        "kind": "diagram", "diagram_kind": kind, "purpose": f"Explains {kind} content",
        "width": 880, "height": 400,
    }
    if path:
        content["path"] = path
        content["asset_id"] = path.rsplit("/", 1)[-1]
    return Block(
        id=block_id, type=BlockType.IMAGE, content=content,
        layout=BlockLayout(x=64, y=100, width=666, height=300),
        meta=BlockMeta(chapter_id="ch1"),
    )


def _document(blocks: list[Block]) -> CourseDocument:
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[Page(blocks=blocks)],
    )


class _StubAI:
    """A minimal AIClient stub - only `.structured` is ever used by this
    module, so nothing else needs implementing."""

    def __init__(self, response=None, *, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    async def structured(self, *, schema, **kwargs):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response if self.response is not None else schema()


async def test_fewer_than_the_minimum_diagram_blocks_is_a_zero_call_noop():
    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(3)]
    document = _document(blocks)
    ai = _StubAI()
    notes = await balance_diagram_kinds(document, ai=ai)
    assert notes == []
    assert ai.calls == 0
    assert all(b.content["diagram_kind"] == "flow_chart" for b in blocks)


async def test_no_kind_over_the_overuse_threshold_is_a_zero_call_noop():
    # 3 of flow_chart (at the threshold, not over it) + 2 distinct others -
    # 5 total blocks (clears MIN_DIAGRAM_BLOCKS) but nothing is overused.
    blocks = [
        _diagram_block("b0", kind="flow_chart"),
        _diagram_block("b1", kind="flow_chart"),
        _diagram_block("b2", kind="flow_chart"),
        _diagram_block("b3", kind="pyramid"),
        _diagram_block("b4", kind="timeline"),
    ]
    document = _document(blocks)
    ai = _StubAI()
    notes = await balance_diagram_kinds(document, ai=ai)
    assert notes == []
    assert ai.calls == 0


async def test_an_overused_kind_triggers_exactly_one_batched_call():
    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    document = _document(blocks)
    ai = _StubAI()
    await balance_diagram_kinds(document, ai=ai)
    assert ai.calls == 1  # one call for the WHOLE course, never per-block


async def test_an_accepted_reassignment_is_applied_and_noted():
    from app.services.diagram_diversity import _DiversityPlan, _KindReassignment

    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    document = _document(blocks)
    plan = _DiversityPlan(
        reassignments=[_KindReassignment(block_id="b2", new_kind="donut_segments", reason="better fit")]
    )
    ai = _StubAI(response=plan)
    notes = await balance_diagram_kinds(document, ai=ai)
    assert blocks[2].content["diagram_kind"] == "donut_segments"
    assert any("b2" in note and "donut_segments" in note for note in notes)
    # Every OTHER block is untouched - this pass only relabels what the
    # plan explicitly names.
    assert blocks[0].content["diagram_kind"] == "flow_chart"


async def test_a_reassignment_to_an_unknown_kind_is_ignored():
    from app.services.diagram_diversity import _DiversityPlan, _KindReassignment

    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    document = _document(blocks)
    plan = _DiversityPlan(reassignments=[_KindReassignment(block_id="b2", new_kind="not_a_real_kind")])
    ai = _StubAI(response=plan)
    notes = await balance_diagram_kinds(document, ai=ai)
    assert blocks[2].content["diagram_kind"] == "flow_chart"
    assert notes == []


async def test_reassignments_are_capped_at_the_maximum_per_course():
    from app.services.diagram_diversity import _DiversityPlan, _KindReassignment, _MAX_REASSIGNMENTS

    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(10)]
    document = _document(blocks)
    plan = _DiversityPlan(
        reassignments=[
            _KindReassignment(block_id=f"b{i}", new_kind="donut_segments") for i in range(10)
        ]
    )
    ai = _StubAI(response=plan)
    notes = await balance_diagram_kinds(document, ai=ai)
    assert len(notes) == _MAX_REASSIGNMENTS
    reassigned = sum(1 for b in blocks if b.content["diagram_kind"] == "donut_segments")
    assert reassigned == _MAX_REASSIGNMENTS


async def test_an_already_generated_block_is_never_considered_even_if_its_kind_is_overused():
    """Resume-safety: a block with a `path` was already rendered in an
    earlier run - relabelling its diagram_kind now would desync the
    stored kind from the image already on disk without ever regenerating
    it. This must hold even when every OTHER condition for a reassignment
    is met."""
    from app.services.diagram_diversity import _DiversityPlan, _KindReassignment

    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(4)]
    blocks.append(_diagram_block("b_done", kind="flow_chart", path="assets/image_001.svg"))
    document = _document(blocks)
    ai = _StubAI()
    await balance_diagram_kinds(document, ai=ai)
    # Only the 4 not-yet-generated blocks counted toward the histogram/
    # overuse decision and the candidate list sent to the model - the
    # already-generated one was excluded from consideration entirely.
    assert ai.calls == 1
    assert blocks[-1].content["diagram_kind"] == "flow_chart"
    assert blocks[-1].content["path"] == "assets/image_001.svg"


async def test_a_failed_ai_call_leaves_every_block_unchanged():
    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    document = _document(blocks)
    ai = _StubAI(raises=RuntimeError("model unavailable"))
    notes = await balance_diagram_kinds(document, ai=ai)
    assert notes == []
    assert all(b.content["diagram_kind"] == "flow_chart" for b in blocks)


async def test_disabled_setting_is_a_zero_call_noop():
    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    document = _document(blocks)
    ai = _StubAI()
    settings = get_settings().model_copy(update={"enable_diagram_diversity_pass": False})
    notes = await balance_diagram_kinds(document, ai=ai, settings=settings)
    assert notes == []
    assert ai.calls == 0


async def test_concept_experience_blocks_are_never_touched():
    """This pass only ever reads/writes `diagram_kind` - a concept_
    experience block's own selection isn't diagram_kind-based at all (see
    the module's own docstring), so it must never be counted or touched."""
    blocks = [_diagram_block(f"b{i}", kind="flow_chart") for i in range(5)]
    concept_block = Block(
        id="cev", type=BlockType.IMAGE,
        content={"kind": "concept_experience", "purpose": "Explains a concept"},
        layout=BlockLayout(x=64, y=100, width=666, height=300),
        meta=BlockMeta(chapter_id="ch1"),
    )
    blocks.append(concept_block)
    document = _document(blocks)
    ai = _StubAI()
    await balance_diagram_kinds(document, ai=ai)
    assert not concept_block.content.get("diagram_kind")
