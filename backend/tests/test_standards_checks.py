"""Reviewer checks from the client's accessibility, Bloom and style standards."""

from __future__ import annotations

from app.agents.reviewer import _pedagogy_issues, _standards_issues
from app.course.design_models import design_issues
from app.schemas.blocks import BlockType
from app.schemas.design import DesignModels
from app.schemas.document import Block
from app.schemas.learner import LearnerProfile

PROFILE = LearnerProfile()
BLOOM = DesignModels(models=["bloom"])


def _b(block_type: BlockType, **content) -> Block:
    return Block(type=block_type, content=content)


def _objectives(*items: str) -> Block:
    return _b(BlockType.LEARNING_OBJECTIVES, items=list(items))


def _descriptions(issues) -> str:
    return " | ".join(i.description for i in issues)


# --- opt-in -------------------------------------------------------------------


def test_nothing_is_checked_for_a_course_that_never_opted_in():
    blocks = [_b(BlockType.IMAGE, alt=""), _b(BlockType.HEADING, text="A", level=2),
              _b(BlockType.HEADING, text="B", level=4)]
    assert _standards_issues(blocks, None, None) == []
    assert _standards_issues(blocks, None, DesignModels()) == []


def test_a_learner_profile_or_a_design_model_switches_the_checks_on():
    blocks = [_b(BlockType.IMAGE, alt="")]
    assert _standards_issues(blocks, PROFILE, None)
    assert _standards_issues(blocks, None, DesignModels(models=["gagne"]))


# --- alt text -----------------------------------------------------------------


def test_an_image_without_alt_text_is_a_major_accessibility_issue():
    issues = _standards_issues([_b(BlockType.IMAGE, alt="")], PROFILE, None)
    assert issues[0].severity == "major" and issues[0].category == "accessibility"
    assert issues[0].block_index == 0


def test_alt_text_or_the_decorative_mark_satisfies_the_check():
    assert _standards_issues([_b(BlockType.IMAGE, alt="A diagram of the flow")], PROFILE, None) == []
    assert _standards_issues([_b(BlockType.IMAGE, alt="", decorative=True)], PROFILE, None) == []


def test_the_contents_page_image_is_exempt():
    assert _standards_issues([_b(BlockType.IMAGE, kind="toc", alt="")], PROFILE, None) == []


# --- heading hierarchy --------------------------------------------------------


def test_a_skipped_heading_level_is_reported_at_the_offending_heading():
    blocks = [_b(BlockType.HEADING, text="A", level=2), _b(BlockType.HEADING, text="B", level=4)]
    issues = _standards_issues(blocks, PROFILE, None)
    assert "H2 to H4" in issues[0].description and issues[0].block_index == 1


def test_going_back_up_or_down_one_level_is_fine():
    blocks = [_b(BlockType.HEADING, text="A", level=2), _b(BlockType.HEADING, text="B", level=3),
              _b(BlockType.HEADING, text="C", level=2)]
    assert _standards_issues(blocks, PROFILE, None) == []


# --- objectives and assessment ------------------------------------------------


def test_a_verb_outside_the_approved_bank_is_reported():
    issues = _standards_issues([_objectives("Learners will be able to ponder the design.")], PROFILE, None)
    assert "ponder" in _descriptions(issues)


def test_an_unmeasurable_verb_is_left_to_the_existing_check_not_reported_twice():
    blocks = [_objectives("Learners will be able to understand the design.")]
    assert _standards_issues(blocks, PROFILE, None) == []
    assert "understand" in _descriptions(_pedagogy_issues(blocks, PROFILE))


def test_the_verb_bank_is_not_reported_twice_when_bloom_is_selected():
    blocks = [_objectives("Learners will be able to ponder the design.")]
    assert _standards_issues(blocks, PROFILE, BLOOM) == []
    assert "ponder" in _descriptions(design_issues(blocks, BLOOM))


def test_a_create_objective_assessed_only_by_a_quiz_is_flagged():
    blocks = [_objectives("Learners will be able to design a pipeline."), _b(BlockType.QUIZ, questions=[])]
    issues = _standards_issues(blocks, PROFILE, None)
    assert any(i.category == "assessment" and i.severity == "major" for i in issues)


def test_an_evaluate_objective_assessed_only_by_a_quiz_is_flagged():
    blocks = [_objectives("Learners will be able to justify a choice."), _b(BlockType.QUIZ, questions=[])]
    assert any(i.category == "assessment" for i in _standards_issues(blocks, PROFILE, None))


def test_an_open_ended_task_alongside_the_quiz_satisfies_the_rule():
    blocks = [
        _objectives("Learners will be able to design a pipeline."),
        _b(BlockType.QUIZ, questions=[]),
        _b(BlockType.EXERCISE, instructions="Build one."),
    ]
    assert not any(i.category == "assessment" for i in _standards_issues(blocks, PROFILE, None))


def test_a_quiz_is_fine_for_a_remember_objective():
    blocks = [_objectives("Learners will be able to list the stages."), _b(BlockType.QUIZ, questions=[])]
    assert not any(i.category == "assessment" for i in _standards_issues(blocks, PROFILE, None))


def test_the_objective_template_is_checked_for_bloom_and_addie_only():
    plain = [_objectives("Describe the pipeline.")]
    assert any("Learners will be able to" in i.description for i in design_issues(plain, BLOOM))
    assert any("Learners will be able to" in i.description for i in design_issues(plain, DesignModels(models=["addie"])))
    assert not any("Learners will be able to" in i.description for i in design_issues(plain, DesignModels(models=["gagne"])))
    ok = [_objectives("Learners will be able to describe the pipeline.")]
    assert design_issues(ok, BLOOM) == []


# --- house style --------------------------------------------------------------


def _style(text: str) -> str:
    issues = _pedagogy_issues([_b(BlockType.PARAGRAPH, text=text)], PROFILE)
    return " | ".join(i.suggestion for i in issues)


def test_should_for_a_required_action_is_reported():
    assert "must" in _style("You should restart the service.")


def test_thank_you_is_reported():
    assert "thank you" in _style("Thank you for reading.")


def test_clean_text_has_no_style_issue():
    assert _style("You must restart the service.") == ""


# --- decorative images in the PDF --------------------------------------------


def test_a_decorative_image_is_exported_with_empty_alt_text():
    from app.course.templates.registry import load_template
    from app.render.html_renderer import render_document_html
    from app.schemas.document import CourseDocument, Page

    image = _b(BlockType.IMAGE, path="assets/x.png", alt="A pretty swirl", decorative=True, width=400, height=300)
    document = CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        pages=[Page(id="p1", page_number=1, kind="content", blocks=[image])],
    )
    html = render_document_html(document, load_template("technical_v1"), asset_prefix="../")
    assert 'alt=""' in html and "A pretty swirl" not in html
