"""Bulk visual regeneration for an EXISTING document - the missing
mechanism behind "old courses still show the old chart styling": a
rendered diagram/image is a static file saved once at generation time, so
a later renderer/prompt change never touches it on its own. See
DocumentService.regenerate_all_visuals and
POST /api/documents/{id}/regenerate-visuals.
"""

from __future__ import annotations

from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, DocumentMeta, Page


def _image_block(block_id: str, *, kind: str, path: str) -> Block:
    return Block(
        id=block_id,
        type=BlockType.IMAGE,
        content={
            "kind": kind,
            "diagram_kind": "flow_chart" if kind == "diagram" else "",
            "purpose": "Explain the process",
            "prompt": "a short process",
            "caption": "",
            "path": path,
            "asset_id": path.rsplit("/", 1)[-1],
            "generated": True,
        },
    )


async def _seeded_document(service, documents, technical_input):
    record = await service.create_course(technical_input, run_planner=False)
    diagram_path = documents.storage.save_asset(record.course_id, b"<svg>old</svg>", extension="svg")
    illustration_path = documents.storage.save_asset(record.course_id, b"old-bytes", extension="png")
    blocks = [
        _image_block("block_diagram", kind="diagram", path=diagram_path),
        _image_block("block_illustration", kind="illustration", path=illustration_path),
    ]
    document = CourseDocument(
        document_id=record.document_id,
        course_id=record.course_id,
        course_title=technical_input.course_title,
        template_id="technical_v1",
        meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )
    saved = await documents.save(record.document_id, document)
    return record, saved


async def test_regenerate_all_visuals_replaces_every_already_generated_block(
    service, documents, technical_input
):
    record, document = await _seeded_document(service, documents, technical_input)
    old_diagram_path = document.pages[0].blocks[0].content["path"]
    old_illustration_path = document.pages[0].blocks[1].content["path"]

    response = await documents.regenerate_all_visuals(record.document_id)

    assert response.regenerated == 2
    assert response.failed == []
    updated = await documents.load(record.document_id)
    new_diagram = updated.find_block("block_diagram")[1]
    new_illustration = updated.find_block("block_illustration")[1]
    assert new_diagram.content["path"] != old_diagram_path
    assert new_illustration.content["path"] != old_illustration_path


async def test_regenerate_all_visuals_can_be_scoped_to_one_kind(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    old_diagram_path = document.pages[0].blocks[0].content["path"]
    old_illustration_path = document.pages[0].blocks[1].content["path"]

    response = await documents.regenerate_all_visuals(record.document_id, kinds=["diagram"])

    assert response.regenerated == 1
    updated = await documents.load(record.document_id)
    new_diagram = updated.find_block("block_diagram")[1]
    untouched_illustration = updated.find_block("block_illustration")[1]
    assert new_diagram.content["path"] != old_diagram_path
    assert untouched_illustration.content["path"] == old_illustration_path


async def test_regenerate_all_visuals_via_api(client, service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    old_diagram_path = document.pages[0].blocks[0].content["path"]

    resp = client.post(f"/api/documents/{record.document_id}/regenerate-visuals", json={"kinds": []})

    assert resp.status_code == 200
    body = resp.json()
    assert body["regenerated"] == 2
    updated = await documents.load(record.document_id)
    assert updated.find_block("block_diagram")[1].content["path"] != old_diagram_path
