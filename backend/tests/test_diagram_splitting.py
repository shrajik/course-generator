"""Document-level insertion half of multi-diagram splitting -
app.course.document.diagram_splitting. DiagramService already decided to
split and rendered every later part (see test_diagrams.py's own
`_linear_chain_split`/`pending_diagram_splits` coverage); this module's only
job is turning those transient payloads into real sibling blocks, in order,
and re-paginating."""

from __future__ import annotations

import base64

from app.course.document.diagram_splitting import insert_split_diagrams
from app.course.templates.registry import load_template
from app.render.diagram_renderer import render_diagram_svg
from app.schemas.blocks import BlockType
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec
from app.schemas.document import Block, BlockLayout, BlockMeta, CourseDocument, DocumentMeta, Page

_TEMPLATE = load_template("technical_v1")


def _part_spec(label_prefix: str, title: str) -> DiagramSpec:
    nodes = [DiagramNode(id=f"{label_prefix}{i}", label=f"Step {i}") for i in range(4)]
    edges = [DiagramEdge(source=f"{label_prefix}{i}", target=f"{label_prefix}{i + 1}") for i in range(3)]
    return DiagramSpec(kind="flow_chart", title=title, nodes=nodes, edges=edges)


def _block_with_pending_splits(*, pending: list[dict] | None) -> Block:
    content = {
        "kind": "diagram", "diagram_kind": "flow_chart", "path": "assets/first_part.svg",
        "asset_id": "first_part.svg", "width": 400, "height": 300,
        "spec": DiagramSpec(kind="flow_chart", nodes=[DiagramNode(id="a", label="A"), DiagramNode(id="b", label="B")],
                             edges=[DiagramEdge(source="a", target="b")]).model_dump(mode="json"),
    }
    if pending is not None:
        content["pending_diagram_splits"] = pending
    return Block(
        id="first_part_block",
        type=BlockType.IMAGE, content=content,
        layout=BlockLayout(x=64, y=135, width=600, height=300),
        meta=BlockMeta(chapter_id="ch1", chapter_number=1, section_key="s1"),
    )


def _document_with(block: Block) -> CourseDocument:
    page = Page(id="p1", page_number=1, kind="content", blocks=[block])
    return CourseDocument(
        document_id="d", course_id="course_split", course_title="T", template_id="technical_v1",
        meta=DocumentMeta(), pages=[page],
    )


def _pending_payload(spec: DiagramSpec) -> dict:
    svg_bytes, width, height = render_diagram_svg(spec, _TEMPLATE.theme)
    return {
        "spec": spec.model_dump(mode="json"),
        "svg_base64": base64.b64encode(svg_bytes).decode("ascii"),
        "width": width,
        "height": height,
    }


def test_no_pending_splits_is_a_complete_no_op(storage):
    document = _document_with(_block_with_pending_splits(pending=None))
    report = insert_split_diagrams(document, _TEMPLATE, storage=storage)
    assert report.inserted == []
    assert report.skipped == []
    assert len(document.pages[0].blocks) == 1


def test_a_single_pending_split_inserts_a_new_sibling_block_right_after_the_original(storage):
    part2 = _part_spec("n", "A procedure (Part 2 of 2)")
    original = _block_with_pending_splits(pending=[_pending_payload(part2)])
    document = _document_with(original)

    report = insert_split_diagrams(document, _TEMPLATE, storage=storage)

    assert len(report.inserted) == 1
    blocks = document.pages[0].blocks
    assert len(blocks) == 2
    assert blocks[0].id == "first_part_block"
    new_block = blocks[1]
    assert new_block.id != original.id
    assert new_block.content["kind"] == "diagram"
    assert new_block.content["diagram_kind"] == "flow_chart"
    assert new_block.content["spec"]["title"] == "A procedure (Part 2 of 2)"
    assert new_block.content["path"]
    assert new_block.content["generated"] is True
    assert new_block.content["error"] is None
    assert new_block.meta.chapter_id == "ch1"
    assert new_block.meta.chapter_number == 1
    assert new_block.meta.section_key == "s1"
    assert new_block.meta.origin == "inserted"
    # The actual SVG bytes were saved as a real asset, not just referenced.
    svg_text = storage.asset_abs_path(document.course_id, new_block.content["path"]).read_text(encoding="utf-8")
    assert svg_text.startswith("<svg")


def test_multiple_pending_splits_are_inserted_in_order_after_the_original(storage):
    """3 total blocks may land across more than one page once re-paginated
    (reflow_document's own job) - order across the whole document, not
    page[0] specifically, is what this checks."""
    part2 = _part_spec("m", "A long procedure (Part 2 of 3)")
    part3 = _part_spec("o", "A long procedure (Part 3 of 3)")
    original = _block_with_pending_splits(pending=[_pending_payload(part2), _pending_payload(part3)])
    document = _document_with(original)

    report = insert_split_diagrams(document, _TEMPLATE, storage=storage)

    assert len(report.inserted) == 2
    blocks = [block for _, block in document.iter_blocks()]
    assert len(blocks) == 3
    assert blocks[0].id == "first_part_block"
    assert blocks[1].content["spec"]["title"] == "A long procedure (Part 2 of 3)"
    assert blocks[2].content["spec"]["title"] == "A long procedure (Part 3 of 3)"


def test_pending_splits_are_cleared_from_the_original_block_after_insertion(storage):
    part2 = _part_spec("n", "A procedure (Part 2 of 2)")
    original = _block_with_pending_splits(pending=[_pending_payload(part2)])
    document = _document_with(original)

    insert_split_diagrams(document, _TEMPLATE, storage=storage)

    assert not document.pages[0].blocks[0].content.get("pending_diagram_splits")


def test_an_invalid_pending_split_payload_is_skipped_not_fatal(storage):
    original = _block_with_pending_splits(
        pending=[{"spec": {"nodes": "not a list"}, "svg_base64": "", "width": 1, "height": 1}]
    )
    document = _document_with(original)

    report = insert_split_diagrams(document, _TEMPLATE, storage=storage)

    assert report.inserted == []
    assert len(report.skipped) == 1
    assert len(document.pages[0].blocks) == 1
    # The malformed payload is still cleared - never retried forever.
    assert not document.pages[0].blocks[0].content.get("pending_diagram_splits")


def test_one_bad_part_among_several_does_not_block_the_others(storage):
    good_part = _part_spec("n", "A procedure (Part 2 of 3)")
    original = _block_with_pending_splits(pending=[
        {"spec": {"nodes": "not a list"}, "svg_base64": "", "width": 1, "height": 1},
        _pending_payload(good_part),
    ])
    document = _document_with(original)

    report = insert_split_diagrams(document, _TEMPLATE, storage=storage)

    assert len(report.inserted) == 1
    assert len(report.skipped) == 1
    assert len(document.pages[0].blocks) == 2


def test_document_is_reflowed_only_when_something_was_actually_inserted(storage, monkeypatch):
    calls: list[CourseDocument] = []
    import app.course.document.diagram_splitting as diagram_splitting_module

    def fake_reflow(document, template):
        calls.append(document)
        return document

    monkeypatch.setattr(diagram_splitting_module, "reflow_document", fake_reflow)

    no_op_document = _document_with(_block_with_pending_splits(pending=None))
    insert_split_diagrams(no_op_document, _TEMPLATE, storage=storage)
    assert calls == []

    split_document = _document_with(
        _block_with_pending_splits(pending=[_pending_payload(_part_spec("n", "A procedure (Part 2 of 2)"))])
    )
    insert_split_diagrams(split_document, _TEMPLATE, storage=storage)
    assert calls == [split_document]
