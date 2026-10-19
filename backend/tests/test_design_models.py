"""Instructional-design models: ADDIE, Bloom, SAM, Gagne, Kirkpatrick."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.agents import prompts
from app.course.design_models import (
    GAGNE_EVENTS,
    design_issues,
    design_model_brief,
    design_outputs,
)
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.design import DesignModels
from app.schemas.document import Block, CourseDocument, Page


def _block(block_type: BlockType, **content) -> Block:
    return Block(type=block_type, content=content)


def _heading(text: str, level: int = 2) -> Block:
    return _block(BlockType.HEADING, text=text, level=level)


def _gagne_blocks(order: list[str] | None = None) -> list[Block]:
    names = order or [name for name, _ in GAGNE_EVENTS]
    return [_heading(name) for name in names]


# --- schema -------------------------------------------------------------------


def test_sam_requires_a_loop_type():
    with pytest.raises(ValidationError, match="loop"):
        DesignModels(models=["sam"])
    assert DesignModels(models=["sam"], sam_loop="SAM1").has("sam")


def test_kirkpatrick_requires_a_business_goal():
    with pytest.raises(ValidationError, match="goal"):
        DesignModels(models=["kirkpatrick"])
    assert DesignModels(models=["kirkpatrick"], business_goal="Cut errors").has("kirkpatrick")


def test_duplicate_models_are_collapsed():
    assert DesignModels(models=["addie", "bloom", "addie"]).models == ["addie", "bloom"]


def test_unknown_models_are_rejected():
    with pytest.raises(ValidationError):
        DesignModels(models=["waterfall"])


# --- prompt brief -------------------------------------------------------------


def test_no_selection_adds_nothing_to_any_prompt():
    assert design_model_brief(None) == ""
    assert design_model_brief(DesignModels()) == ""


def test_the_brief_never_contains_a_line_the_mock_ai_would_misread():
    design = DesignModels(
        models=["addie", "bloom", "sam", "gagne", "kirkpatrick"], sam_loop="SAM2", business_goal="Goal"
    )
    brief = design_model_brief(design)
    assert not re.search(r"^\s*\d+\. ", brief, re.MULTILINE)
    for label in ("COURSE TITLE:", "CHAPTER TITLE:", "TEMPLATE KIND:"):
        assert label not in brief


def test_gagne_brief_names_all_nine_events_in_order():
    brief = design_model_brief(DesignModels(models=["gagne"]))
    positions = [brief.index(name) for name, _ in GAGNE_EVENTS]
    assert positions == sorted(positions)


def test_microlearning_keeps_only_events_2_4_6_8_and_asks_for_a_note():
    brief = design_model_brief(DesignModels(models=["gagne"], microlearning=True))
    kept = [name for name, _ in GAGNE_EVENTS if f"Section heading {name}:" in brief]
    assert kept == ["Inform objectives", "Present content", "Elicit performance", "Assess performance"]
    assert "compressed" in brief


def test_addie_only_applies_bloom_verb_rules_when_bloom_is_selected():
    without = design_model_brief(DesignModels(models=["addie"]))
    with_bloom = design_model_brief(DesignModels(models=["addie", "bloom"]))
    assert "not selected" in without and "apply because" in with_bloom


def test_bloom_verb_bank_is_not_repeated_when_a_learner_profile_already_has_it():
    design = DesignModels(models=["bloom"])
    assert "approved Bloom" not in design_model_brief(design, bloom_already_stated=True)
    assert "Remember - define" in design_model_brief(design)


def test_sam_brief_limits_the_pass_and_demands_draft_labels_and_questions():
    brief = design_model_brief(DesignModels(models=["sam"], sam_loop="SAM1", sam_review_ready="A clean outline"))
    assert "Draft - Iteration" in brief and "Open questions" in brief and "A clean outline" in brief


def test_kirkpatrick_brief_forbids_invented_targets():
    brief = design_model_brief(DesignModels(models=["kirkpatrick"], business_goal="Reduce refunds"))
    assert "Reduce refunds" in brief and "Never invent numeric" in brief


def _input(design: DesignModels | None) -> CourseInput:
    return CourseInput(
        course_title="T", toc=[{"title": "One"}], target_audience="Engineers", template="technical",
        design_models=design,
    )


def test_prompts_are_byte_identical_without_a_selection():
    from app.course.templates.registry import load_template

    template = load_template("technical_v1")
    a, b, c = _input(None), _input(DesignModels()), _input(DesignModels(models=["gagne"]))
    assert prompts._course_context(a, template) == prompts._course_context(b, template)
    assert "GAGNE" in prompts._course_context(c, template)


# --- reviewer checks ----------------------------------------------------------


def test_no_selection_means_no_design_issues():
    assert design_issues(_gagne_blocks()[:2], None) == []


def test_a_complete_gagne_lesson_has_no_issues():
    assert design_issues(_gagne_blocks(), DesignModels(models=["gagne"])) == []


def test_missing_gagne_events_are_reported_by_name():
    blocks = [b for b in _gagne_blocks() if "Stimulate" not in b.content["text"]]
    issues = design_issues(blocks, DesignModels(models=["gagne"]))
    assert issues and "Stimulate recall" in issues[0].description


def test_out_of_order_gagne_events_are_reported():
    names = [name for name, _ in GAGNE_EVENTS]
    names[0], names[1] = names[1], names[0]
    issues = design_issues(_gagne_blocks(names), DesignModels(models=["gagne"]))
    assert any("out of order" in i.description for i in issues)


def test_numbered_headings_still_match_their_event():
    blocks = [_heading(f"Event {n}: {name}") for n, (name, _) in enumerate(GAGNE_EVENTS, start=1)]
    assert design_issues(blocks, DesignModels(models=["gagne"])) == []


def test_microlearning_needs_the_four_events_and_a_compression_note():
    design = DesignModels(models=["gagne"], microlearning=True)
    four = _gagne_blocks(["Inform objectives", "Present content", "Elicit performance", "Assess performance"])
    assert any("compressed" in i.description for i in design_issues(four, design))
    four.append(_block(BlockType.PARAGRAPH, text="Events 1, 3, 5, 7 and 9 were compressed to fit five minutes."))
    assert design_issues(four, design) == []


def test_sam_content_needs_a_draft_label_and_open_questions():
    design = DesignModels(models=["sam"], sam_loop="SAM1")
    bare = [_block(BlockType.PARAGRAPH, text="Some content.")]
    assert len(design_issues(bare, design)) == 2
    good = [
        _block(BlockType.CALLOUT, title="Draft - Iteration 1", text="Prototype."),
        _block(BlockType.CALLOUT, title="Open questions for the reviewer", text="Is the depth right?"),
    ]
    assert design_issues(good, design) == []


def test_addie_wants_code_flagged_for_sme_verification():
    design = DesignModels(models=["addie"])
    code = [_block(BlockType.CODE, code="print(1)", language="python")]
    assert design_issues(code, design)
    code.append(_block(BlockType.CALLOUT, title="SME verification", text="Verify the sample."))
    assert design_issues(code, design) == []


def test_bloom_flags_verbs_outside_the_approved_bank():
    design = DesignModels(models=["bloom"])
    bad = [_block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to ponder the pipeline."])]
    assert any("ponder" in i.description for i in design_issues(bad, design))
    good = [_block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to describe the pipeline."])]
    assert design_issues(good, design) == []


# --- plan documents -----------------------------------------------------------


def _document() -> CourseDocument:
    blocks = [
        _heading("Chapter One", 1),
        _block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to describe a chain."]),
        _block(BlockType.QUIZ, questions=[{"question": "Q"}]),
        _heading("Chapter Two", 1),
        _block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to design a prompt."]),
        _block(BlockType.EXERCISE, instructions="Do it."),
    ]
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        pages=[Page(id="p1", page_number=1, kind="content", blocks=blocks)],
    )


def test_no_selection_produces_no_plan_documents():
    assert design_outputs(_document(), _input(None)) == {}
    assert design_outputs(_document(), _input(DesignModels(models=["gagne"]))) == {}


def test_addie_outputs_sequencing_tracking_and_an_evaluation_summary():
    out = design_outputs(_document(), _input(DesignModels(models=["addie"])))["addie"]
    modules = out["implementation"]["modules"]
    assert [m["module"] for m in modules] == ["Chapter One", "Chapter Two"]
    assert modules[0]["prerequisite"] == "none" and modules[1]["prerequisite"] == "Chapter One"
    assert modules[0]["tracking"] == "scored (pass/fail)" and modules[1]["tracking"] == "scored"
    assert "High-level" in out["evaluation_summary"]["detail"]


def test_kirkpatrick_plan_covers_four_levels_and_maps_objectives_to_assessments():
    design = DesignModels(models=["kirkpatrick"], business_goal="Cut refund errors")
    plan = design_outputs(_document(), _input(design))["kirkpatrick"]
    rows = plan["level_2_learning"]["items"]
    assert rows[0]["bloom_level"] == "Understand" and "short answer" in rows[0]["assessment"]
    assert rows[1]["bloom_level"] == "Create" and "open-ended" in rows[1]["assessment"]
    assert len(plan["level_3_behavior"]["checklist"]) == 2
    assert "Cut refund errors" in plan["level_4_results"]["measure"]


def test_kirkpatrick_never_invents_a_numeric_target():
    design = DesignModels(models=["kirkpatrick"], business_goal="Cut refund errors")
    plan = design_outputs(_document(), _input(design))["kirkpatrick"]
    assert "stakeholder" in plan["level_4_results"]["note"]
    assert not re.search(r"\d+\s*%", str(plan["level_4_results"]))


def test_lms_only_data_flags_that_levels_3_and_4_need_external_data():
    lms = design_outputs(_document(), _input(DesignModels(models=["kirkpatrick"], business_goal="g")))
    both = design_outputs(
        _document(), _input(DesignModels(models=["kirkpatrick"], business_goal="g", data_sources="lms_and_kpis"))
    )
    assert lms["kirkpatrick"]["flags"] and not both["kirkpatrick"]["flags"]


# --- SAM iterations through the API ------------------------------------------


def _create(client, design):
    response = client.post("/api/courses", json={
        "course_title": "Intro to RAG", "toc": [{"title": "One"}, {"title": "Two"}, {"title": "Three"}],
        "target_audience": "Engineers", "template": "technical", "run_planner": False,
        "design_models": design,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _written(client, course_id) -> int:
    chapters = client.get(f"/api/courses/{course_id}").json()["course"]["chapters"]
    return sum(1 for c in chapters if c["written"])


def test_sam_builds_one_module_per_iteration_and_waits_for_feedback(client):
    course_id = _create(client, {"models": ["sam"], "sam_loop": "SAM1"})["course_id"]
    assert client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"}).status_code == 200
    assert _written(client, course_id) == 1

    refused = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert refused.status_code == 409 and "feedback" in refused.json()["error"]["message"].lower()

    ok = client.post(
        f"/api/courses/{course_id}/generate",
        json={"mode": "sync", "sam_feedback": "Simpler examples please."},
    )
    assert ok.status_code == 200, ok.text
    assert _written(client, course_id) == 2


def test_a_course_without_sam_still_builds_everything_at_once(client):
    course_id = _create(client, None)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    assert _written(client, course_id) == 3


def test_design_outputs_endpoint(client):
    created = _create(client, {"models": ["kirkpatrick"], "business_goal": "Cut refund errors"})
    client.post(f"/api/courses/{created['course_id']}/generate", json={"mode": "sync"})
    body = client.get(f"/api/documents/{created['document_id']}/design-outputs").json()
    assert set(body) == {"kirkpatrick"} and body["kirkpatrick"]["business_goal"] == "Cut refund errors"


def test_the_writer_prompt_states_the_design_models_exactly_once():
    from app.schemas.blueprint import CourseBlueprint
    from app.course.templates.registry import load_template

    prompt = prompts.writer_shared_prefix(
        CourseBlueprint(course_title="T"), _input(DesignModels(models=["gagne"])), load_template("technical_v1")
    )
    assert prompt.count("INSTRUCTIONAL DESIGN MODELS SELECTED") == 1
