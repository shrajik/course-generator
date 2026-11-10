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


def test_replace_image_on_an_illustration_with_an_existing_picture_preserves_it_for_editing(document, template):
    """Raster-illustration editing root cause fix: an illustration block
    that already has a real picture on disk gets `_pending_edit=True` and
    its `path` left UNTOUCHED, not cleared - that path is both the source
    image `ImageService._edit_illustration` reads and genuinely edits, AND
    the fallback that keeps showing the correct, already-good picture if
    that edit ever fails. Clearing it (the old behaviour) would have
    discarded the one thing a real image-to-image edit needs."""
    image = document.pages[0].blocks[2]
    assert image.content.get("kind", "illustration") == "illustration"  # the fixture's own default
    image.content["path"] = "assets/image_001.png"
    image.content["generated"] = True
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "fix the overlapping numbers",
                    "caption": "new caption",
                }
            ]
        }
    )
    result = apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["path"] == "assets/image_001.png"
    assert updated.content["generated"] is True
    assert updated.content["_pending_edit"] is True
    assert updated.content["prompt"] == "fix the overlapping numbers"
    assert updated.content["caption"] == "new caption"
    assert updated.content["error"] is None
    assert result.images_to_generate == [image.id]


def test_replace_image_on_a_block_with_no_existing_picture_has_nothing_to_edit(document, template):
    """The mirror case: a block that never had a real picture (no `path`
    yet) has nothing to edit from, so this is an ordinary fresh generation
    - `_pending_edit` stays False and there's nothing to clear either way."""
    image = document.pages[0].blocks[2]
    assert image.content.get("path") is None
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": image.id, "prompt": "a clearer diagram"}]}
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["path"] is None
    assert updated.content["_pending_edit"] is False


def test_replace_image_on_a_diagram_or_concept_experience_still_clears_the_asset(document, template):
    """A diagram/concept_experience's `path` is SVG/HTML, not an editable
    raster source - `_pending_edit` must never fire for these, and the old
    behaviour (clear `path` so the structured pipeline regenerates it) is
    unchanged. See test_replace_image_preserves_the_existing_spec_... below
    for the real editing mechanism these kinds use instead (their own
    `spec`, never cleared, read back as correction context)."""
    image = document.pages[0].blocks[2]
    image.content["kind"] = "diagram"
    image.content["diagram_kind"] = "concept_map"
    image.content["path"] = "assets/image_001.svg"
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": image.id, "prompt": "fix the overlapping label"}]}
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["path"] is None
    assert updated.content["_pending_edit"] is False


def test_replace_image_preserves_the_existing_spec_for_the_regeneration_step_to_reuse(document, template):
    """Replace Image root cause fix: `replace_image` clears `path` so the
    block regenerates, but must NEVER discard `content["spec"]` - it's the
    existing diagram/concept_experience structure DiagramService/
    ConceptVisualService.generate_for_block now reads back as correction
    context (see their own regression tests) so a correction preserves the
    existing nodes/entities instead of the model inventing a different
    visual. `merge_content`'s shallow merge already keeps any key `update`
    doesn't touch - this locks that contract in explicitly for `spec`."""
    image = document.pages[0].blocks[2]
    image.content["kind"] = "diagram"
    image.content["path"] = "assets/image_001.svg"
    image.content["spec"] = {"kind": "concept_map", "title": "t", "nodes": [{"id": "a", "label": "A"}]}
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": image.id, "prompt": "fix the overlapping label"}]}
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["path"] is None
    assert updated.content["spec"] == {"kind": "concept_map", "title": "t", "nodes": [{"id": "a", "label": "A"}]}


def test_replace_image_can_set_expected_labels_for_a_label_correction(document, template):
    image = document.pages[0].blocks[2]
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "fix the misspelled label",
                    "expected_labels": ["Nucleus", "Cell wall"],
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["expected_labels"] == ["Nucleus", "Cell wall"]


def test_replace_image_without_expected_labels_leaves_the_existing_ones_untouched(document, template):
    image = document.pages[0].blocks[2]
    image.content["expected_labels"] = ["Anode", "Cathode"]
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": image.id, "prompt": "improve the spacing"}]}
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["expected_labels"] == ["Anode", "Cathode"]


def test_replace_image_can_reclassify_a_stale_diagram_to_concept_experience(document, template):
    image = document.pages[0].blocks[2]
    image.content["kind"] = "diagram"
    image.content["diagram_kind"] = "concept_map"
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "context window as a token budget",
                    "kind": "concept_experience",
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["kind"] == "concept_experience"
    assert updated.content["diagram_kind"] == ""


def test_replace_image_without_kind_keeps_the_existing_classification(document, template):
    image = document.pages[0].blocks[2]
    image.content["kind"] = "diagram"
    image.content["diagram_kind"] = "concept_map"
    patch = DocumentPatch.model_validate(
        {"operations": [{"type": "replace_image", "block_id": image.id, "prompt": "clearer"}]}
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["kind"] == "diagram"
    assert updated.content["diagram_kind"] == "concept_map"


def test_replace_image_reclassifying_to_illustration_can_set_textbook_style(document, template):
    image = document.pages[0].blocks[2]
    image.content["kind"] = "diagram"
    image.content["diagram_kind"] = "concept_map"
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "an anatomically accurate heart",
                    "kind": "illustration",
                    "illustration_style": "textbook",
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["kind"] == "illustration"
    assert updated.content["diagram_kind"] == ""
    assert updated.content["illustration_style"] == "textbook"


def test_replace_image_reclassifying_off_illustration_clears_the_stale_style(document, template):
    image = document.pages[0].blocks[2]
    image.content["kind"] = "illustration"
    image.content["illustration_style"] = "textbook"
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "the RAG pipeline stages",
                    "kind": "concept_experience",
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["kind"] == "concept_experience"
    assert updated.content["illustration_style"] == ""


def test_replace_image_can_set_illustration_style_without_changing_kind(document, template):
    image = document.pages[0].blocks[2]
    image.content["kind"] = "illustration"
    patch = DocumentPatch.model_validate(
        {
            "operations": [
                {
                    "type": "replace_image",
                    "block_id": image.id,
                    "prompt": "the mitochondria",
                    "illustration_style": "textbook",
                }
            ]
        }
    )
    apply_patch(document, patch, template)
    updated = document.find_block(image.id)[1]
    assert updated.content["kind"] == "illustration"
    assert updated.content["illustration_style"] == "textbook"


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
