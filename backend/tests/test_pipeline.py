"""Agent and storage behaviour at the service level."""

from __future__ import annotations

import json

import pytest

from app.agents.reviewer import ReviewerAgent
from app.agents.writer import WriterAgent
from app.core.errors import NotFoundError
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.course import GenerateRequest, ImproveTocRequest
from app.schemas.document import Block
from app.schemas.review import ChapterReview, ReviewIssue


async def test_planner_produces_one_chapter_per_toc_entry(service, technical_input):
    record = await service.create_course(technical_input)
    blueprint = service.storage.load_blueprint(record.course_id)
    assert len(blueprint.chapters) == len(technical_input.toc)
    assert blueprint.chapter_ids() == ["chapter_1", "chapter_2"]
    assert blueprint.dos == technical_input.dos
    assert blueprint.donts == technical_input.donts


async def test_planner_does_not_rewrite_the_user_toc(service, technical_input):
    record = await service.create_course(technical_input)
    blueprint = service.storage.load_blueprint(record.course_id)
    assert [c.title for c in blueprint.chapters] == technical_input.chapter_titles()
    # Suggestions are reported separately.
    assert blueprint.critique.suggested_toc or blueprint.critique.missing_concepts


async def test_improve_toc_never_returns_an_empty_toc(service):
    response = await service.improve_toc(
        ImproveTocRequest(course_title="X", toc=["A", "B"], template="technical")
    )
    assert len(response.suggested_toc) >= 2


async def test_storage_layout_matches_the_spec(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    root = service.storage.course_dir(record.course_id)

    assert (root / "course.json").exists()
    assert (root / "blueprint.json").exists()
    assert (root / "document.json").exists()
    assert (root / "research" / "chapter_01.json").exists()
    assert (root / "research" / "chapter_02.json").exists()
    assert (root / "chapters" / "chapter_01.json").exists()
    assert (root / "chapters" / "chapter_02.json").exists()
    assert list((root / "assets").glob("image_*.png"))

    # Artifacts are valid JSON documents, not pickles or prose.
    json.loads((root / "document.json").read_text())


async def test_research_is_cached_between_runs(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    calls_before = sum(1 for call in service.ai.calls if call["kind"] == "research")
    await service.generate(record.course_id, GenerateRequest(chapter_ids=["chapter_1"], mode="sync"))
    calls_after = sum(1 for call in service.ai.calls if call["kind"] == "research")
    assert calls_after == calls_before  # reused the cached chapter_01.json


async def test_force_regenerates_research(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    before = sum(1 for call in service.ai.calls if call["kind"] == "research")
    await service.generate(
        record.course_id, GenerateRequest(chapter_ids=["chapter_1"], force=True, mode="sync")
    )
    after = sum(1 for call in service.ai.calls if call["kind"] == "research")
    assert after > before


async def test_single_chapter_regeneration_keeps_the_others(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    first_before = service.storage.load_chapter(record.course_id, "chapter_1", 1)

    await service.generate(
        record.course_id, GenerateRequest(chapter_ids=["chapter_2"], force=True, mode="sync")
    )
    first_after = service.storage.load_chapter(record.course_id, "chapter_1", 1)
    assert first_after.generated_at == first_before.generated_at

    document = service.storage.load_document(record.course_id)
    assert document.meta.chapter_ids == ["chapter_1", "chapter_2"]


async def test_sequential_mode_passes_the_real_previous_summary(
    service, technical_input, monkeypatch
):
    """The original behaviour, kept for A/B comparison of continuity quality."""
    service.settings.writing_mode = "sequential"
    seen: list[tuple[list[tuple[str, str]], bool]] = []
    original = WriterAgent.write_chapter

    async def spy(self, **kwargs):
        continuity = kwargs["continuity"]
        seen.append((list(continuity.previous), continuity.planned))
        return await original(self, **kwargs)

    monkeypatch.setattr(WriterAgent, "write_chapter", spy)
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))

    assert seen[0][0] == []  # first chapter has no predecessor
    assert len(seen[1][0]) == 1
    assert seen[1][0][0][0] == "What is RAG"
    assert seen[1][0][0][1]


async def test_generated_chapters_carry_summaries(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    chapter = service.storage.load_chapter(record.course_id, "chapter_1", 1)
    assert chapter.summary
    assert chapter.blocks
    assert chapter.review is not None


async def test_document_blocks_are_traceable_to_chapters(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    document = service.storage.load_document(record.course_id)
    content_pages = [page for page in document.pages if page.kind == "content"]
    chapter_ids = {
        block.meta.chapter_id for page in content_pages for block in page.blocks
    }
    assert chapter_ids == {"chapter_1", "chapter_2"}


async def test_images_are_saved_and_referenced_relatively(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    document = service.storage.load_document(record.course_id)
    images = document.blocks_of_type(BlockType.IMAGE)
    assert images
    for block in images:
        path = block.content["path"]
        assert path.startswith("assets/")
        assert service.storage.asset_abs_path(record.course_id, path).exists()
        assert block.content["width"] and block.content["height"]


async def test_image_generation_can_be_disabled(service, technical_input):
    record = await service.create_course(technical_input)
    result = await service.generate(
        record.course_id, GenerateRequest(generate_images=False, mode="sync")
    )
    assert result.images_generated == 0
    document = service.storage.load_document(record.course_id)
    assert all(b.content.get("path") is None for b in document.blocks_of_type(BlockType.IMAGE))


async def test_a_failing_chapter_does_not_sink_the_run(service, technical_input, monkeypatch):
    original = WriterAgent.write_chapter

    async def flaky(self, **kwargs):
        if kwargs["chapter"].id == "chapter_2":
            raise RuntimeError("model unavailable")
        return await original(self, **kwargs)

    monkeypatch.setattr(WriterAgent, "write_chapter", flaky)
    record = await service.create_course(technical_input)
    result = await service.generate(record.course_id, GenerateRequest(mode="sync"))
    assert result.chapters_generated == ["chapter_1"]
    assert result.chapters_failed == ["chapter_2"]
    assert service.storage.has_document(record.course_id)


async def test_building_a_document_without_chapters_fails_clearly(service, technical_input):
    record = await service.create_course(technical_input)
    with pytest.raises(NotFoundError):
        service.build_course_document(record.course_id)


async def test_reviewer_detects_missing_required_blocks():
    template = load_template("technical_v1")
    review = ChapterReview(approved=True, issues=[])
    blocks = [Block(type=BlockType.PARAGRAPH, content={"text": "only prose"})]
    ReviewerAgent._apply_structural_checks(review, template, blocks)
    missing = " ".join(review.missing_required_blocks)
    assert "quiz" in missing and "summary" in missing and "exercise" in missing


async def test_reviewer_passes_a_complete_chapter(service, technical_input):
    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    chapter = service.storage.load_chapter(record.course_id, "chapter_1", 1)
    assert chapter.review["missing_required_blocks"] == []
    assert chapter.review["approved"] is True


def test_review_notes_prioritise_blockers():
    review = ChapterReview(
        approved=False,
        issues=[
            ReviewIssue(severity="minor", category="clarity", description="nit"),
            ReviewIssue(
                severity="blocker",
                category="accuracy",
                block_index=3,
                description="wrong claim",
                suggestion="remove it",
            ),
        ],
        donts_violations=["used marketing language"],
    )
    notes = WriterAgent.review_to_notes(review)
    assert "wrong claim" in notes
    assert "used marketing language" in notes
    assert "nit" not in notes
    assert review.needs_revision() is True
