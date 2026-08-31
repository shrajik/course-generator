"""AI patch validation and application."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.course.document.patcher import apply_patch
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.schemas.patch import SUPPORTED_OPERATIONS, DocumentPatch


@pytest.fixture
def template():
    return load_template("technical_v1")


@pytest.fixture
def document() -> CourseDocument:
    blocks = [
        Block(type=BlockType.HEADING, content={"text": "Chapter One", "level": 1}),
        Block(type=BlockType.PARAGRAPH, content={"text": "First paragraph."}),
        Block(type=BlockType.IMAGE, content={"prompt": "old", "caption": "old caption"}),
        Block(type=BlockType.PARAGRAPH, content={"text": "Second paragraph."}),
    ]
    return CourseDocument(
        document_id="doc_test",
        course_id="crs_test",
        course_title="Test Course",
        template_id="technical_v1",
        pages=[Page(id="page_1", page_number=1, kind="content", blocks=blocks)],
    )


# --- validation -------------------------------------------------------------


def test_supported_operations_match_the_spec():
    assert set(SUPPORTED_OPERATIONS) == {
        "update_block",
        "delete_block",
        "insert_block",
        "replace_block",
        "replace_image",
        "update_style",
    }


def test_patch_parses_a_discriminated_union():
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {"type": "update_block", "block_id": "b1", "content": {"text": "x"}},
                {"type": "delete_block", "block_id": "b2"},
                {"type": "update_style", "block_id": "b3", "style": {"font_size": 20}},
            ]
        }
    )
    assert [op.type for op in patch.operations] == [
        "update_block",
        "delete_block",
        "update_style",
    ]


def test_unknown_operation_type_is_rejected():
    with pytest.raises(ValidationError):
        DocumentPatch.model_validate(
            {"operations": [{"type": "rewrite_everything", "block_id": "b"}]}
        )


def test_update_block_requires_a_payload():
    with pytest.raises(ValidationError):
        DocumentPatch.model_validate({"operations": [{"type": "update_block", "block_id": "b"}]})


def test_insert_block_requires_an_anchor():
    with pytest.raises(ValidationError):
        DocumentPatch.model_validate(
            {
                "operations": [
                    {"type": "insert_block", "block": {"type": "tip", "content": {"text": "x"}}}
                ]
            }
        )


def test_invalid_block_type_is_rejected():
    with pytest.raises(ValidationError):
        DocumentPatch.model_validate(
            {
                "operations": [
                    {
                        "type": "insert_block",
                        "after_block_id": "b",
                        "block": {"type": "hologram", "content": {}},
                    }
                ]
            }
        )


# --- application -----------------------------------------------------------


def test_update_block_merges_content_and_bumps_version(document, template):
    target = document.pages[0].blocks[1]
    version = document.version
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {"type": "update_block", "block_id": target.id, "content": {"text": "Simpler."}}
            ]
        }
    )
    result = apply_patch(document, patch, template)
    assert result.applied == [f"update_block:{target.id}"]
    assert document.find_block(target.id)[1].content["text"] == "Simpler."
    assert document.find_block(target.id)[1].meta.origin == "edited"
    assert document.version == version + 1


def test_insert_block_after_anchor(document, template):
    anchor = document.pages[0].blocks[1]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "insert_block",
                    "after_block_id": anchor.id,
                    "block": {"type": "tip", "content": {"text": "A useful tip."}},
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    ids = [block.id for _, block in document.iter_blocks()]
    inserted = next(b for _, b in document.iter_blocks() if b.type is BlockType.TIP)
    assert ids.index(inserted.id) == ids.index(anchor.id) + 1
    # Template styling is applied to inserted blocks.
    assert inserted.style.background is not None


def test_delete_block(document, template):
    target = document.pages[0].blocks[3]
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "delete_block", "block_id": target.id}]}
    )
    apply_patch(document, patch, template)
    assert document.find_block(target.id) is None


def test_replace_block_keeps_the_id(document, template):
    target = document.pages[0].blocks[1]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_block",
                    "block_id": target.id,
                    "block": {
                        "type": "callout",
                        "content": {"title": "Note", "text": "Replaced."},
                    },
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    replaced = document.find_block(target.id)[1]
    assert replaced.type is BlockType.CALLOUT
    assert replaced.content["text"] == "Replaced."


def test_replace_image_clears_the_asset_and_queues_regeneration(document, template):
    image = document.pages[0].blocks[2]
    image.content["path"] = "assets/image_001.png"
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "a clearer diagram",
                    "caption": "new caption",
                }
            ]
        }
    )
    result = apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["path"] is None
    assert updated.content["prompt"] == "a clearer diagram"
    assert updated.content["caption"] == "new caption"
    assert result.images_to_generate == [image.id]


def test_replace_image_on_a_non_image_block_is_rejected(document, template):
    paragraph = document.pages[0].blocks[1]
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": paragraph.id, "prompt": "x"}]}
    )
    result = apply_patch(document, patch, template)
    assert result.applied == []
    assert result.rejected and "not an image" in result.rejected[0]["reason"]


def test_update_style_touches_only_presentation(document, template):
    target = document.pages[0].blocks[1]
    before = target.content["text"]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {"type": "update_style", "block_id": target.id, "style": {"font_size": 19}}
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(target.id)[1]
    assert updated.style.font_size == 19
    assert updated.content["text"] == before


def test_unknown_block_is_reported_not_raised(document, template):
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "delete_block", "block_id": "block_missing"}]}
    )
    result = apply_patch(document, patch, template)
    assert result.applied == []
    assert result.rejected[0]["reason"] == "block not found"
    assert result.changed is False


def test_one_bad_operation_does_not_block_the_others(document, template):
    good = document.pages[0].blocks[1]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {"type": "delete_block", "block_id": "block_missing"},
                {"type": "update_block", "block_id": good.id, "content": {"text": "Kept."}},
            ]
        }
    )
    result = apply_patch(document, patch, template)
    assert len(result.applied) == 1 and len(result.rejected) == 1
    assert document.find_block(good.id)[1].content["text"] == "Kept."


def test_patch_application_reflows_layout(document, template):
    target = document.pages[0].blocks[1]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "update_block",
                    "block_id": target.id,
                    "content": {"text": "word " * 4000},
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    assert len(document.pages) > 1  # re-paginated after the block grew
