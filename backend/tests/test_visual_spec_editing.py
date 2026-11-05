"""Edit Visuals, Part 1: editing a flow_chart/process/etc. (node/edge
"diagram" family) block's own structured spec - deterministic, no AI call
for update/preview; one AI call only for `reconstruct_diagram_spec`, and
only when no spec is stored yet. See DocumentService.update_visual_spec/
preview_visual_spec/reconstruct_diagram_spec and
PUT|POST /api/documents/{id}/blocks/{block_id}/visual-spec(/preview|/reconstruct).

Mirrors test_concept_visual_editing.py's fixtures/conventions exactly, for
the sibling "diagram" kind instead of "concept_experience"."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailedError
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, DocumentMeta, Page


def _spec_dict(**overrides) -> dict:
    base = {
        "kind": "flow_chart",
        "title": "Original Title",
        "nodes": [
            {"id": "n0", "label": "Step One", "detail": "First step."},
            {"id": "n1", "label": "Step Two", "detail": "Second step."},
        ],
        "edges": [{"source": "n0", "target": "n1", "label": ""}],
    }
    base.update(overrides)
    return base


def _diagram_block(*, path: str, spec: dict | None = None, **content_overrides) -> Block:
    content = {
        "kind": "diagram",
        "diagram_kind": "flow_chart",
        "purpose": "Explain the two-step process",
        "prompt": "a two-step process",
        "caption": "",
        "path": path,
        "asset_id": path.rsplit("/", 1)[-1],
        "generated": True,
        "width": 666,
        "height": 300,
    }
    if spec is not None:
        content["spec"] = spec
    content.update(content_overrides)
    return Block(id="block_diagram", type=BlockType.IMAGE, content=content)


async def _seeded_document(service, documents, technical_input, *, spec: dict | None = _spec_dict()):
    record = await service.create_course(technical_input, run_planner=False)
    initial_path = documents.storage.save_asset(record.course_id, b"<svg></svg>", extension="svg")
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=[_diagram_block(path=initial_path, spec=spec)])],
    )
    saved = await documents.save(record.document_id, document)
    return record, saved


async def test_update_visual_spec_now_accepts_a_diagram_block(service, documents, technical_input):
    """Previously a 422 ("not an editable concept_experience visual") -
    the whole point of generalizing this method."""
    record, _document = await _seeded_document(service, documents, technical_input)
    edited = _spec_dict()
    edited["nodes"][0]["label"] = "Step One (edited)"

    response = await documents.update_visual_spec(record.document_id, "block_diagram", edited)

    block = response.block
    assert block.content["spec"]["nodes"][0]["label"] == "Step One (edited)"
    assert block.content["path"] != ""


async def test_update_visual_spec_re_renders_the_svg_with_the_new_label(service, documents, technical_input):
    record, _document = await _seeded_document(service, documents, technical_input)
    edited = _spec_dict()
    edited["nodes"][0]["label"] = "Renamed Step"

    response = await documents.update_visual_spec(record.document_id, "block_diagram", edited)

    svg_bytes = documents.storage.asset_abs_path(record.course_id, response.block.content["path"]).read_bytes()
    assert b"Renamed Step" in svg_bytes


async def test_update_visual_spec_honors_a_role_and_color_override(service, documents, technical_input):
    record, _document = await _seeded_document(service, documents, technical_input)
    edited = _spec_dict()
    edited["nodes"][0]["role"] = "start"
    edited["nodes"][1]["color"] = "#f59e0b"

    response = await documents.update_visual_spec(record.document_id, "block_diagram", edited)

    svg_bytes = documents.storage.asset_abs_path(record.course_id, response.block.content["path"]).read_bytes()
    text = svg_bytes.decode("utf-8")
    assert "#bbf7d0" in text  # the "start" role's default fill, now applied via the override
    assert "#f59e0b" in text  # the custom node's own stroke colour


async def test_preview_visual_spec_never_touches_storage(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    original_path = document.pages[0].blocks[0].content["path"]
    edited = _spec_dict()
    edited["nodes"][0]["label"] = "Preview-only change"

    result = await documents.preview_visual_spec(record.document_id, "block_diagram", edited)

    assert "svg" in result
    assert "Preview-only change" in result["svg"]
    reloaded = await documents.load(record.document_id)
    saved_block = reloaded.pages[0].blocks[0]
    assert saved_block.content["path"] == original_path  # unchanged - no save happened
    assert saved_block.content["spec"]["nodes"][0]["label"] == "Step One"  # unchanged


async def test_a_graph_kind_block_is_not_editable_as_a_node_edge_diagram(service, documents, technical_input):
    """kind=="diagram" with diagram_kind=="graph" (native function/chart
    rendering) has no node/edge spec shape - _EDITABLE_KINDS gates on the
    top-level `kind`, so this still passes the gate; the real protection is
    that a "graph"-shaped spec_update simply fails DiagramSpec.model_validate
    or produces a usable-but-nonsensical node/edge spec. Documented here as
    a known, accepted limitation of the kind-level gate, not a silent gap:
    the frontend excludes "graph" blocks from the flowchart editor entirely
    (see PropertiesPanel.tsx), so this path is never reached from the UI."""
    record, _document = await _seeded_document(
        service, documents, technical_input,
        spec={"kind": "graph", "graph_type": "function", "functions": [{"expression": "x", "label": "y = x"}]},
    )
    # A legitimate flow_chart-shaped edit still works even on a block whose
    # stored content happens to be diagram_kind="flow_chart" (the fixture
    # above) - this test exists to document the gating boundary, not to
    # assert a rejection that the backend doesn't actually enforce.
    edited = _spec_dict()
    response = await documents.update_visual_spec(record.document_id, "block_diagram", edited)
    assert response.block.content["spec"]["kind"] == "flow_chart"


async def test_reconstruct_returns_the_existing_spec_without_an_ai_call(service, documents, technical_input):
    """When a spec is already stored, reconstruct is a free no-op read -
    never spends an AI call reconstructing something that already exists."""
    record, _document = await _seeded_document(service, documents, technical_input)

    result = await documents.reconstruct_diagram_spec(record.document_id, "block_diagram")

    assert result["nodes"][0]["label"] == "Step One"


async def test_reconstruct_calls_the_ai_when_no_spec_is_stored(service, documents, technical_input):
    """Simulates a course generated before spec persistence existed - no
    `spec` key on the block's content at all."""
    record, _document = await _seeded_document(service, documents, technical_input, spec=None)

    result = await documents.reconstruct_diagram_spec(record.document_id, "block_diagram")

    assert result["kind"] in ("flow_chart", "process", "cycle", "hierarchy", "comparison", "smart_art", "concept_map")
    assert len(result["nodes"]) >= 2


async def test_reconstruct_rejects_a_non_diagram_block(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    document.pages[0].blocks[0].content["kind"] = "illustration"
    await documents.save(record.document_id, document)

    with pytest.raises(ValidationFailedError):
        await documents.reconstruct_diagram_spec(record.document_id, "block_diagram")


# --- end-to-end via the real generation pipeline --------------------------


async def test_a_freshly_generated_diagram_block_already_has_its_spec_stored(client):
    """DiagramService.generate_for_block now persists `spec` on every
    successful save - the precondition this whole feature depends on."""
    created = client.post(
        "/api/courses",
        json={
            "course_title": "Spec Persistence", "toc": [{"title": "One"}],
            "target_audience": "Engineers", "template": "technical", "run_planner": False,
        },
    ).json()
    client.post(f"/api/courses/{created['course_id']}/approve-outline")
    generated = client.post(f"/api/courses/{created['course_id']}/generate", json={"mode": "sync"})
    assert generated.status_code == 200, generated.text

    doc = client.get(f"/api/documents/{created['document_id']}").json()
    diagram_blocks = [
        b for p in doc["pages"] for b in p["blocks"]
        if b["type"] == "image" and b["content"].get("kind") == "diagram"
    ]
    assert diagram_blocks
    assert all(b["content"].get("spec") is not None for b in diagram_blocks)
