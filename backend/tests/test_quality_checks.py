"""Course quality checks done the way a teacher and a student would.

Text: the reviewer's per-section verdicts (on topic, explains its heading,
meets the objective, accurate, not repeated) become issues on the exact block,
and the surgical revision rewrites only that block.

Visuals: app.services.visual_review looks at the actual picture files and the
recorded content of diagrams, compares them with the text beside them,
detects duplicates from the pixels, regenerates only what fails, and never
reports a visual as passed without an explicit verdict.

The AI is replaced by fakes that decide from the real input they are given
(the colour of the attached picture, the arrows of the drawn diagram), so the
tests prove the checks look at the actual visual rather than at its prompt.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.agents import prompts
from app.agents.prompts import ContinuityContext
from app.agents.reviewer import ReviewerAgent, _duplicate_text_issues, apply_section_checks
from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.blueprint import BlueprintChapter, ChapterSection, CourseBlueprint
from app.schemas.course import CourseInput
from app.schemas.document import Block, BlockMeta, CourseDocument, Page
from app.schemas.research import ChapterResearch
from app.schemas.review import ChapterReview, SectionCheck
from app.schemas.visual_review import DiagramAssessment, ImageAssessment
from app.services.image_service import ImageService
from app.services.mock_ai import MockAIClient
from app.services.visual_review import (
    VisualReviewService,
    diagram_verdict,
    image_fingerprint,
    image_verdict,
    load_visual_review,
)

GREEN, RED, BLUE = (40, 160, 60), (200, 30, 30), (30, 60, 200)


def _png(color, size=(320, 240), stripe=40) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, color)
    # A little structure so the fingerprint is not trivially flat.
    for x in range(0, size[0], stripe):
        for y in range(size[1]):
            image.putpixel((x, y), (255, 255, 255))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _colour_of(png: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.convert("RGB").getpixel((image.width // 2 + 5, image.height // 2))


def _b(block_type: BlockType, chapter: str = "chapter_1", **content) -> Block:
    return Block(type=block_type, content=content, meta=BlockMeta(chapter_id=chapter))


# ---------------------------------------------------------------------------
# text: section verdicts
# ---------------------------------------------------------------------------


def _chapter_blocks() -> list[Block]:
    return [
        _b(BlockType.HEADING, text="How Plants Make Food", level=1),  # 0
        _b(BlockType.PARAGRAPH, text="Plants are living things that grow in soil."),  # 1
        _b(BlockType.HEADING, text="What photosynthesis is", level=2),  # 2
        _b(BlockType.PARAGRAPH, text="Photosynthesis is how a plant turns light, water and carbon dioxide into sugar."),  # 3
        _b(BlockType.PARAGRAPH, text="It happens in chloroplasts, which contain chlorophyll."),  # 4
        _b(BlockType.HEADING, text="Why leaves are green", level=2),  # 5
        _b(BlockType.PARAGRAPH, text="Leaves are green because they absorb green light."),  # 6
    ]


def test_a_section_that_does_not_explain_its_heading_becomes_an_issue_on_that_block():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=0, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=2, on_topic=True, explains_heading=False, meets_objective=True, accurate=True,
                     problem_block=4, problem="never says what the sugar is used for", fix="Say the plant uses the sugar as food and energy."),
        SectionCheck(heading_index=5, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
    ])
    apply_section_checks(review, _chapter_blocks())
    issue = next(issue for issue in review.issues if issue.category == "clarity")
    assert (issue.severity, issue.block_index) == ("major", 4)
    assert "never says what the sugar is used for" in issue.description
    assert issue.suggestion.startswith("Say the plant uses the sugar")
    assert review.needs_revision() and review.offending_indices() == [4]
    assert review.unchecked_sections == []


def test_inaccurate_content_is_a_blocker_and_points_at_the_wrong_block():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=0, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=2, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=5, on_topic=True, explains_heading=True, meets_objective=True, accurate=False,
                     problem_block=6, problem="leaves reflect green light; they absorb red and blue",
                     fix="Leaves look green because chlorophyll reflects green light."),
    ])
    apply_section_checks(review, _chapter_blocks())
    issue = next(issue for issue in review.issues if issue.category == "accuracy")
    assert (issue.severity, issue.block_index) == ("blocker", 6)
    assert review.offending_indices() == [6]


def test_off_topic_and_repeated_sections_are_reported_and_a_bad_index_falls_back_inside_the_section():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=0, on_topic=False, explains_heading=True, meets_objective=False, accurate=True,
                     problem_block=99, problem="talks about soil, not how plants make food"),
        SectionCheck(heading_index=2, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=5, on_topic=True, explains_heading=True, meets_objective=True, accurate=True,
                     repeats_block=3, problem="re-explains photosynthesis"),
    ])
    apply_section_checks(review, _chapter_blocks())
    categories = {(issue.category, issue.block_index) for issue in review.issues}
    # Index 99 is outside the section, so the section's own paragraph is used.
    assert ("relevance", 1) in categories and ("objective", 1) in categories
    assert ("repetition", 6) in categories


def test_a_section_without_a_verdict_is_unchecked_never_passed():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=2, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        # An entry with every judgement left empty is not a verdict.
        SectionCheck(heading_index=5),
    ])
    apply_section_checks(review, _chapter_blocks())
    assert review.unchecked_sections == ["How Plants Make Food", "Why leaves are green"]
    unchecked = [issue for issue in review.issues if issue.category == "unchecked"]
    assert [issue.block_index for issue in unchecked] == [0, 5]
    assert all(issue.severity == "minor" for issue in unchecked)  # reported, not a rewrite loop


def test_a_verdict_matched_by_heading_text_when_the_index_is_wrong():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=42, heading="Why leaves are green", on_topic=True, explains_heading=False,
                     meets_objective=True, accurate=True, problem_block=6),
    ])
    apply_section_checks(review, _chapter_blocks())
    assert any(issue.category == "clarity" and issue.block_index == 6 for issue in review.issues)


def test_a_near_copy_of_an_earlier_block_is_a_duplicate_but_a_paraphrase_is_left_to_the_reviewer():
    original = (
        "Photosynthesis turns light, water and carbon dioxide into glucose and oxygen inside the chloroplast. "
        "The green pigment chlorophyll captures the energy of sunlight, and the plant uses that energy to join "
        "carbon dioxide from the air with water drawn up from the roots."
    )
    blocks = [
        _b(BlockType.PARAGRAPH, text=original),
        _b(BlockType.PARAGRAPH, text="Inside the chloroplast, a plant uses sunlight to build sugar from water and air."),
        _b(BlockType.PARAGRAPH, text=original + " That is all."),
    ]
    issues = _duplicate_text_issues(blocks)
    assert [(issue.block_index, issue.category) for issue in issues] == [(2, "repetition")]


def test_the_reviewer_is_given_the_objective_key_concepts_and_research_to_judge_against():
    chapter = BlueprintChapter(
        id="chapter_1", order=1, title="How Plants Make Food",
        objective="Explain how a leaf turns light into stored food.",
        key_concepts=["chlorophyll", "glucose"], sections=[ChapterSection(title="What photosynthesis is")],
    )
    research = ChapterResearch(
        chapter_id="chapter_1", chapter_title="How Plants Make Food",
        payload={"overview": "Chlorophyll absorbs red and blue light and reflects green."},
    )
    user = prompts.reviewer_user(
        CourseBlueprint(course_title="Plant Biology", learning_objectives=["Describe photosynthesis"]),
        chapter, load_template("non_technical_v1"), [b.model_dump(mode="json") for b in _chapter_blocks()],
        course_input=CourseInput(course_title="Plant Biology", toc=[{"title": "How Plants Make Food"}],
                                 target_audience="Students", template="non_technical"),
        continuity=ContinuityContext(), research=research,
    )
    assert "CHAPTER OBJECTIVE: Explain how a leaf turns light into stored food." in user
    assert "KEY CONCEPTS: chlorophyll, glucose" in user
    assert "Describe photosynthesis" in user
    assert "reflects green" in user  # the evidence for the accuracy check
    assert "section_checks" in prompts.REVIEWER_SYSTEM and "by meaning" in prompts.REVIEWER_SYSTEM


async def test_a_failed_section_is_fixed_by_rewriting_only_that_block(settings):
    """The reviewer's verdict reaches the existing surgical revision, which
    rewrites the one wrong block and leaves every other block untouched."""
    from app.agents.writer import WriterAgent

    blocks = _chapter_blocks()
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=0, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=2, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=5, on_topic=True, explains_heading=True, meets_objective=True, accurate=False,
                     problem_block=6, problem="leaves reflect green light", fix="Say chlorophyll reflects green light."),
    ])
    apply_section_checks(review, blocks)
    seen: dict = {}

    class Reviser(MockAIClient):
        async def structured(self, *, schema, system, user, **kwargs):
            seen["user"] = user
            return await super().structured(schema=schema, system=system, user=user, **kwargs)

    chapter = BlueprintChapter(id="chapter_1", order=1, title="How Plants Make Food")
    revised, _ = await WriterAgent(ai=Reviser()).revise_chapter(
        blueprint=CourseBlueprint(course_title="Plant Biology"), chapter=chapter,
        template=load_template("non_technical_v1"),
        course_input=CourseInput(course_title="Plant Biology", toc=[{"title": "How Plants Make Food"}],
                                 target_audience="Students", template="non_technical"),
        research=None, continuity=ContinuityContext(), review=review, blocks=blocks, summary="s",
    )
    assert "Say chlorophyll reflects green light." in seen["user"]  # the teacher's fix reached the writer
    changed = [i for i, (before, after) in enumerate(zip(blocks, revised)) if before.content != after.content]
    assert changed == [6]
    assert [b.id for b in revised] == [b.id for b in blocks]


# ---------------------------------------------------------------------------
# images and diagrams: verdict rules
# ---------------------------------------------------------------------------


def _good(**overrides) -> ImageAssessment:
    values = dict(relevant_to_topic=True, matches_text=True, misleading=False, necessary=True)
    values.update(overrides)
    return ImageAssessment(**values)


def test_a_picture_passes_only_when_every_judgement_is_explicitly_good():
    assert image_verdict(_good(), decorative_opener=False, meant_as_diagram=False)[0] == "passed"
    status, problems, regenerate = image_verdict(_good(misleading=True), decorative_opener=False, meant_as_diagram=False)
    assert (status, regenerate) == ("failed", True) and "mislead" in problems[0]
    assert image_verdict(_good(matches_text=False), decorative_opener=False, meant_as_diagram=False)[2] is True
    assert image_verdict(_good(text_in_image_correct=False), decorative_opener=False, meant_as_diagram=False)[0] == "failed"
    # Unnecessary but correct: reported, but regenerating would not help.
    assert image_verdict(_good(necessary=False), decorative_opener=False, meant_as_diagram=False)[::2] == ("failed", False)


def test_a_missing_judgement_is_unchecked_not_passed():
    assert image_verdict(ImageAssessment(), decorative_opener=False, meant_as_diagram=False)[0] == "unchecked"
    assert image_verdict(_good(necessary=None), decorative_opener=False, meant_as_diagram=False)[0] == "unchecked"
    # A section opener need not add information, so `necessary` is not required of it.
    assert image_verdict(_good(necessary=None), decorative_opener=True, meant_as_diagram=False)[0] == "passed"
    # A picture standing in for a diagram must have its labels and arrows judged.
    assert image_verdict(_good(), decorative_opener=False, meant_as_diagram=True)[0] == "unchecked"


def test_diagram_verdicts():
    good = DiagramAssessment(represents_text=True, labels_correct=True, arrows_correct=True)
    assert diagram_verdict(good, arrows_known=True, readability=[], missing_labels=[])[0] == "passed"
    assert diagram_verdict(good.model_copy(update={"arrows_correct": False}), arrows_known=True, readability=[], missing_labels=[])[0] == "failed"
    assert diagram_verdict(good, arrows_known=True, readability=["text too small"], missing_labels=[])[0] == "failed"
    assert diagram_verdict(good, arrows_known=True, readability=[], missing_labels=["Oxygen"])[0] == "failed"
    no_arrows = DiagramAssessment(represents_text=True, labels_correct=True)
    assert diagram_verdict(no_arrows, arrows_known=True, readability=[], missing_labels=[])[0] == "unchecked"
    assert diagram_verdict(no_arrows, arrows_known=False, readability=[], missing_labels=[])[0] == "passed"


def test_duplicates_are_found_from_the_pixels():
    assert image_fingerprint(_png(GREEN)) == image_fingerprint(_png(GREEN))
    assert image_fingerprint(_png(GREEN, size=(640, 480))) is not None
    from app.services.visual_review import _distance

    assert _distance(image_fingerprint(_png(GREEN)), image_fingerprint(_png(GREEN, size=(640, 480)))) <= 6


# ---------------------------------------------------------------------------
# images and diagrams: the whole review against real files
# ---------------------------------------------------------------------------

LEAF_TEXT = "Chlorophyll in the leaf captures sunlight, which powers the making of glucose."


class TeacherAI(MockAIClient):
    """Judges a picture by what is actually in it (its colour) and a diagram
    by the arrows actually drawn. New pictures it generates are green leaves."""

    def __init__(self):
        super().__init__()
        self.vision_calls: list[dict] = []
        self.diagram_calls: list[str] = []
        self.generated_prompts: list[str] = []
        self.silent_on: set[tuple[int, int, int]] = set()

    async def structured(self, *, schema, system, user, image=None, **kwargs):
        if schema is ImageAssessment:
            assert image is not None, "the picture itself must be sent"
            colour = _colour_of(image)
            self.vision_calls.append({"colour": colour, "user": user})
            if colour in self.silent_on:
                return ImageAssessment()  # the model said nothing useful
            if colour == GREEN:
                return ImageAssessment(depicts="a green leaf with sunlight", relevant_to_topic=True, matches_text=True,
                                       misleading=False, necessary=True)
            return ImageAssessment(depicts="a red sports car", relevant_to_topic=False, matches_text=False,
                                   misleading=False, necessary=False, problems=["Shows a car, not a leaf."],
                                   better_prompt="A green leaf in sunlight with chloroplasts visible.")
        if schema is DiagramAssessment:
            self.diagram_calls.append(user)
            if "Glucose -> Sunlight" in user:
                return DiagramAssessment(represents_text=True, labels_correct=True, arrows_correct=False,
                                         wrong=["arrow points from Glucose to Sunlight"],
                                         correction="Sunlight must point to Glucose.")
            return DiagramAssessment(represents_text=True, labels_correct=True, arrows_correct=True)
        return await super().structured(schema=schema, system=system, user=user, image=image, **kwargs)

    async def image(self, *, prompt, size=None, phase="image"):
        self.generated_prompts.append(prompt)
        return _png(GREEN, stripe=17)  # a new, different leaf picture


SVG_REVERSED = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
    b'<rect class="diagram-box" x="10" y="10" width="120" height="40"/><text x="20" y="35">Sunlight</text>'
    b'<rect class="diagram-box" x="250" y="10" width="120" height="40"/><text x="260" y="35">Glucose</text>'
    b"</svg>"
)


def _document(storage, assets: dict[str, tuple[bytes, str]], extra_blocks=()) -> CourseDocument:
    course_id = "course_quality"
    storage.ensure_course_dirs(course_id)
    paths = {key: storage.save_asset(course_id, data, extension=ext) for key, (data, ext) in assets.items()}
    blocks = [
        _b(BlockType.HEADING, text="How Plants Make Food", level=1),
        _b(BlockType.HEADING, text="Inside the leaf", level=2),
        _b(BlockType.PARAGRAPH, text=LEAF_TEXT),
    ]
    for key, path in paths.items():
        content = {"kind": "illustration", "prompt": f"brief for {key}", "caption": f"Caption {key}", "path": path, "generated": True}
        if path.endswith(".svg"):
            content.update(kind="diagram", diagram_kind="flow_chart", prompt="Flow from sunlight to glucose",
                           diagram_outline={"kind": "flow_chart", "parts": [{"label": "Sunlight"}, {"label": "Glucose"}],
                                            "arrows": [{"from": "Glucose", "to": "Sunlight", "label": ""}]})
        blocks.append(Block(id=f"img_{key}", type=BlockType.IMAGE, content=content, meta=BlockMeta(chapter_id="chapter_1")))
    blocks.extend(extra_blocks)
    blocks.append(_b(BlockType.PARAGRAPH, text="The glucose is stored as starch."))
    return CourseDocument(document_id="doc_quality", course_id=course_id, course_title="Plant Biology",
                          template_id="non_technical_v1", pages=[Page(page_number=3, blocks=blocks)])


def _service(storage, ai, settings):
    return VisualReviewService(ai=ai, settings=settings, storage=storage, images=ImageService(ai=ai, storage=storage, settings=settings))


async def test_the_actual_picture_is_inspected_with_its_text_and_an_irrelevant_one_is_regenerated(storage, settings):
    ai = TeacherAI()
    document = _document(storage, {"good": (_png(GREEN), "png"), "car": (_png(RED), "png")})
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    by_id = {check.block_id: check for check in report.checks}

    # The vision model saw the real file and what the reader sees around it.
    first = ai.vision_calls[0]["user"]
    assert "SECTION HEADING: Inside the leaf" in first and LEAF_TEXT in first and "starch" in first
    assert by_id["img_good"].status == "passed" and by_id["img_good"].action == "none"

    car = by_id["img_car"]
    assert car.action == "regenerated" and car.fixed is True and car.status == "passed"
    assert car.depicts == "a green leaf with sunlight"
    assert any("chloroplasts" in prompt for prompt in ai.generated_prompts)  # the corrected brief was used
    block = next(b for _, b in document.iter_blocks() if b.id == "img_car")
    assert _colour_of(storage.asset_abs_path(document.course_id, block.content["path"]).read_bytes()) == GREEN
    assert block.content["quality_review"]["status"] == "passed"
    # Only the failing picture was regenerated.
    assert len(ai.generated_prompts) == 1
    saved = load_visual_review(storage, document.course_id)
    assert saved is not None and saved.counts()["fixed"] == 1


async def test_a_duplicated_picture_is_caught_even_when_each_copy_looks_fine(storage, settings):
    ai = TeacherAI()
    document = _document(storage, {"one": (_png(GREEN), "png"), "two": (_png(GREEN, size=(640, 480)), "png")})
    settings.visual_review_max_fixes = 0  # report only
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    two = next(check for check in report.checks if check.block_id == "img_two")
    assert two.status == "failed" and two.duplicate_of == "img_one"
    assert "Looks the same" in two.problems[0]
    assert two.action == "flagged" and "fix limit" in two.note


async def test_a_diagram_with_a_reversed_arrow_is_regenerated_with_the_correction(storage, settings):
    ai = TeacherAI()
    document = _document(storage, {"flow": (SVG_REVERSED, "svg")})
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    assert "ARROWS (from -> to, as drawn):\n- Glucose -> Sunlight" in ai.diagram_calls[0]
    assert check.action == "regenerated"
    block = next(b for _, b in document.iter_blocks() if b.id == "img_flow")
    assert "CORRECTION FROM REVIEW" in block.content["prompt"] and "Sunlight must point to Glucose" in block.content["prompt"]
    assert block.content["original_prompt"] == "Flow from sunlight to glucose"
    # Checked, confirmed by a second independent check, then the regenerated
    # diagram was itself checked again.
    assert len(ai.diagram_calls) == 3 and check.status == "passed" and check.fixed is True


async def test_a_label_the_outline_names_but_the_svg_never_draws_fails_the_diagram(storage, settings):
    ai = TeacherAI()
    document = _document(storage, {"flow": (SVG_REVERSED, "svg")})
    block = next(b for _, b in document.iter_blocks() if b.id == "img_flow")
    block.content["diagram_outline"] = {"kind": "flow_chart", "parts": [{"label": "Sunlight"}, {"label": "Oxygen"}],
                                        "arrows": [{"from": "Sunlight", "to": "Oxygen"}]}
    settings.visual_review_max_fixes = 0
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    assert report.checks[0].status == "failed"
    assert any('"Oxygen" is not actually drawn' in problem for problem in report.checks[0].problems)


async def test_no_verdict_means_unchecked_and_nothing_is_regenerated(storage, settings):
    ai = TeacherAI()
    ai.silent_on = {RED}
    document = _document(storage, {"car": (_png(RED), "png")})
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    assert check.status == "unchecked" and check.action == "none"
    assert ai.generated_prompts == []
    assert any("could not be checked" in warning for warning in report.warnings())


async def test_a_failed_regeneration_keeps_the_original_picture(storage, settings):
    class BrokenGenerator(TeacherAI):
        async def image(self, *, prompt, size=None, phase="image"):
            raise RuntimeError("image provider down")

    ai = BrokenGenerator()
    document = _document(storage, {"car": (_png(RED), "png")})
    block = next(b for _, b in document.iter_blocks() if b.id == "img_car")
    original_path = block.content["path"]
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    assert (check.status, check.action, check.fixed) == ("failed", "flagged", False)
    assert block.content["path"] == original_path and not block.content.get("error")
    assert "original visual was kept" in check.note


async def test_a_regeneration_that_is_no_better_is_rolled_back(storage, settings):
    class StubbornGenerator(TeacherAI):
        async def image(self, *, prompt, size=None, phase="image"):
            self.generated_prompts.append(prompt)
            return _png(RED, size=(330, 250))  # another wrong picture

    ai = StubbornGenerator()
    document = _document(storage, {"car": (_png(RED), "png")})
    block = next(b for _, b in document.iter_blocks() if b.id == "img_car")
    original_path = block.content["path"]
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    assert check.fixed is False and check.status == "failed"
    assert block.content["path"] == original_path
    assert "no better" in check.note


async def test_skipped_visuals_say_why(storage, settings):
    ai = TeacherAI()
    extra = [
        Block(id="img_concept", type=BlockType.IMAGE, content={"kind": "concept_experience", "path": "assets/x.html"}, meta=BlockMeta(chapter_id="chapter_1")),
        Block(id="img_missing", type=BlockType.IMAGE, content={"kind": "illustration", "prompt": "p", "error": "provider down"}, meta=BlockMeta(chapter_id="chapter_1")),
    ]
    document = _document(storage, {}, extra_blocks=extra)
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    notes = {check.block_id: (check.status, check.note) for check in report.checks}
    assert notes["img_concept"][0] == "skipped" and "own QA" in notes["img_concept"][1]
    assert notes["img_missing"] == ("skipped", "No image was generated for this block.")


# ---------------------------------------------------------------------------
# wiring: the pipeline and the on-demand endpoint
# ---------------------------------------------------------------------------


async def test_generation_runs_the_visual_review_and_stores_the_report(service, technical_input):
    from app.schemas.course import GenerateRequest

    record = await service.create_course(technical_input)
    await service.generate(record.course_id, GenerateRequest(mode="sync"))
    report = load_visual_review(service.storage, record.course_id)
    assert report is not None and report.checks
    # The offline mock gives no verdicts, so nothing may count as passed...
    assert report.counts()["passed"] == 0
    # ...and the only failures are real ones: the mock draws the same diagram
    # and the same placeholder picture for both chapters.
    for check in report.checks:
        assert check.status in {"unchecked", "skipped"} or check.duplicate_of, check


def test_the_visual_review_endpoint_checks_an_existing_document(client, technical_input):
    created = client.post("/api/courses", json={**technical_input.model_dump(mode="json"), "run_planner": True})
    assert created.status_code == 201, created.text
    course_id = created.json()["course_id"]
    client.post(f"/api/courses/{course_id}/approve-outline")
    generated = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert generated.status_code == 200, generated.text
    from app.core.ids import document_id_for_course

    document_id = document_id_for_course(course_id)

    response = client.post(f"/api/documents/{document_id}/visual-review", params={"fix": "false"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document_id and body["checks"]
    assert client.get(f"/api/documents/{document_id}/visual-review").json()["checked_at"] == body["checked_at"]


def test_unverified_specifics_are_reported_separately_from_wrong_facts():
    review = ChapterReview(section_checks=[
        SectionCheck(heading_index=0, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=2, on_topic=True, explains_heading=True, meets_objective=True, accurate=True),
        SectionCheck(heading_index=5, on_topic=True, explains_heading=True, meets_objective=True, accurate=True,
                     unsupported=True, problem_block=6, problem="gives a statistic the research does not cover"),
    ])
    apply_section_checks(review, _chapter_blocks())
    issue = next(issue for issue in review.issues if issue.category == "unsupported")
    assert (issue.severity, issue.block_index) == ("major", 6)
    assert not any(issue.category == "accuracy" for issue in review.issues)
    assert "unverified" in issue.description


async def test_a_replacement_of_the_right_subject_is_kept_even_with_more_remarks(storage, settings):
    class PickyTeacher(TeacherAI):
        async def structured(self, *, schema, system, user, image=None, **kwargs):
            if schema is ImageAssessment and _colour_of(image) == GREEN:
                # Right subject, nothing seriously wrong, but a long list of quibbles
                # and an unanswered `necessary`: unchecked, not passed.
                return ImageAssessment(depicts="a leaf", relevant_to_topic=True, matches_text=True, misleading=False,
                                       problems=[f"minor detail {n}" for n in range(9)])
            return await super().structured(schema=schema, system=system, user=user, image=image, **kwargs)

    ai = PickyTeacher()
    document = _document(storage, {"car": (_png(RED), "png")})
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    block = next(b for _, b in document.iter_blocks() if b.id == "img_car")
    assert _colour_of(storage.asset_abs_path(document.course_id, block.content["path"]).read_bytes()) == GREEN
    assert check.action == "regenerated" and check.status == "unchecked" and check.depicts == "a leaf"


async def test_when_two_checks_disagree_a_visual_is_left_unchanged_for_a_person(storage, settings):
    class Inconsistent(TeacherAI):
        async def structured(self, *, schema, system, user, image=None, **kwargs):
            if schema is ImageAssessment:
                self.vision_calls.append({"colour": _colour_of(image), "user": user})
                if len(self.vision_calls) == 1:  # one stray verdict
                    return ImageAssessment(depicts="a leaf", relevant_to_topic=True, matches_text=True, misleading=True,
                                           necessary=True, problems=["stomata drawn like hairs"])
                return ImageAssessment(depicts="a leaf", relevant_to_topic=True, matches_text=True, misleading=False, necessary=True)
            return await super().structured(schema=schema, system=system, user=user, image=image, **kwargs)

    ai = Inconsistent()
    document = _document(storage, {"leaf": (_png(GREEN), "png")})
    block = next(b for _, b in document.iter_blocks() if b.id == "img_leaf")
    path = block.content["path"]
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    assert (check.status, check.action) == ("unchecked", "none")
    assert "disagreed" in check.note and ai.generated_prompts == []
    assert block.content["path"] == path


async def test_a_flawed_but_on_topic_replacement_beats_an_unrelated_original(storage, settings):
    class GarbledLabels(TeacherAI):
        async def structured(self, *, schema, system, user, image=None, **kwargs):
            if schema is ImageAssessment and _colour_of(image) == GREEN:
                # The right subject, but a misspelled label makes it misleading.
                return ImageAssessment(depicts="a leaf with a garbled CO2 label", relevant_to_topic=True, matches_text=True,
                                       misleading=True, necessary=True, text_in_image_correct=False,
                                       problems=["the CO2 label reads 'C2'"])
            return await super().structured(schema=schema, system=system, user=user, image=image, **kwargs)

    ai = GarbledLabels()
    document = _document(storage, {"car": (_png(RED), "png")})
    report = await _service(storage, ai, settings).review_document(document, load_template("non_technical_v1"))
    check = report.checks[0]
    block = next(b for _, b in document.iter_blocks() if b.id == "img_car")
    assert _colour_of(storage.asset_abs_path(document.course_id, block.content["path"]).read_bytes()) == GREEN
    assert check.status == "failed" and check.fixed is False  # still flagged for its label
    assert check.depicts == "a leaf with a garbled CO2 label"
