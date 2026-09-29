"""Editing the text inside a concept_experience visual - deterministic,
no AI call (a typo/wording fix, not a new concept). See
DocumentService.update_concept_visual_spec and
PUT /api/documents/{id}/blocks/{block_id}/visual-spec.
"""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError, ValidationFailedError
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, DocumentMeta, Page


def _spec_dict(**overrides) -> dict:
    base = {
        "kind": "concept_experience",
        "representation": "process",
        "title": "Original Title",
        "steps": [
            {"id": "s1", "label": "Step One", "description": "First step.", "order": 0},
            {"id": "s2", "label": "Step Two", "description": "Second step.", "order": 1},
        ],
    }
    base.update(overrides)
    return base


def _concept_block(*, path: str, **content_overrides) -> Block:
    content = {
        "kind": "concept_experience",
        "purpose": "Explain the process",
        "prompt": "a two-step process",
        "caption": "",
        "path": path,
        "asset_id": path.rsplit("/", 1)[-1],
        "generated": True,
        "width": 666,
        "height": 300,
        "spec": _spec_dict(),
    }
    content.update(content_overrides)
    return Block(id="block_visual", type=BlockType.IMAGE, content=content)


async def _seeded_document(service, documents, technical_input, *, extra_blocks: list[Block] | None = None):
    record = await service.create_course(technical_input, run_planner=False)
    # A real placeholder asset on disk, not just a path string - so
    # `save_asset`'s own next-filename counter (it counts real files) makes
    # the very first edit allocate a genuinely new name, the same way it
    # would for a course that actually went through generation first.
    initial_path = documents.storage.save_asset(record.course_id, b"<div>placeholder</div>", extension="html")
    blocks = [_concept_block(path=initial_path)]
    if extra_blocks:
        blocks.extend(extra_blocks)
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


async def test_editing_a_label_re_renders_with_the_new_text(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    edited = _spec_dict()
    edited["steps"][0]["label"] = "Step One (edited)"

    response = await documents.update_concept_visual_spec(
        record.document_id, "block_visual", edited
    )

    assert response.block.content["spec"]["steps"][0]["label"] == "Step One (edited)"
    abs_path = documents.storage.asset_abs_path(record.course_id, response.block.content["path"])
    assert abs_path.exists()
    html = abs_path.read_text(encoding="utf-8")
    assert "Step One (edited)" in html


async def test_a_new_asset_path_is_written_not_overwritten_in_place(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    original_path = document.pages[0].blocks[0].content["path"]
    edited = _spec_dict()
    edited["steps"][0]["label"] = "Changed"

    response = await documents.update_concept_visual_spec(record.document_id, "block_visual", edited)

    assert response.block.content["path"] != original_path


async def test_reserved_height_grows_for_a_longer_description(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    original_height = document.pages[0].blocks[0].content["height"]
    edited = _spec_dict()
    edited["steps"][0]["description"] = (
        "A much longer description of this step, long enough that it should "
        "genuinely need more vertical room once it wraps onto extra lines "
        "inside the card, the same way any other content-driven estimate "
        "in this renderer grows with real content length."
    )

    response = await documents.update_concept_visual_spec(record.document_id, "block_visual", edited)

    assert response.block.content["height"] >= original_height


async def test_version_bumps_and_the_change_is_retrievable_after_reload(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    starting_version = document.version
    edited = _spec_dict()
    edited["title"] = "Edited Title"

    response = await documents.update_concept_visual_spec(record.document_id, "block_visual", edited)
    assert response.version == starting_version + 1

    reloaded = await documents.load(record.document_id)
    assert reloaded.version == response.version
    assert reloaded.find_block("block_visual")[1].content["spec"]["title"] == "Edited Title"


async def test_editing_reflows_later_blocks_if_height_changed(service, documents, technical_input):
    later = Block(
        id="block_after",
        type=BlockType.PARAGRAPH,
        content={"text": "Comes after the visual."},
    )
    record, document = await _seeded_document(
        service, documents, technical_input, extra_blocks=[later]
    )
    y_before = document.find_block("block_after")[1].layout.y

    edited = _spec_dict()
    edited["steps"].append(
        {"id": "s3", "label": "Step Three", "description": "A third step, taller overall.", "order": 2}
    )
    await documents.update_concept_visual_spec(record.document_id, "block_visual", edited)

    reloaded = await documents.load(record.document_id)
    y_after = reloaded.find_block("block_after")[1].layout.y
    assert y_after >= y_before


async def test_unknown_block_id_is_a_404(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    with pytest.raises(NotFoundError):
        await documents.update_concept_visual_spec(record.document_id, "no_such_block", _spec_dict())


async def test_a_non_concept_experience_block_is_rejected(service, documents, technical_input):
    plain_paragraph = Block(id="block_plain", type=BlockType.PARAGRAPH, content={"text": "hi"})
    record, document = await _seeded_document(
        service, documents, technical_input, extra_blocks=[plain_paragraph]
    )
    with pytest.raises(ValidationFailedError):
        await documents.update_concept_visual_spec(record.document_id, "block_plain", _spec_dict())


async def test_an_invalid_spec_is_rejected(service, documents, technical_input):
    record, document = await _seeded_document(service, documents, technical_input)
    with pytest.raises(ValidationFailedError):
        await documents.update_concept_visual_spec(
            record.document_id, "block_visual", {"steps": "not a list"}
        )
