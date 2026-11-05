"""Companion-deliverable content: job aid, quick-reference card, manager guide."""

from __future__ import annotations

from app.course.companions import companion_deliverables
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import Block, CourseDocument, Page


def _block(block_type: BlockType, **content) -> Block:
    return Block(type=block_type, content=content)


def _heading(text: str, level: int = 2) -> Block:
    return _block(BlockType.HEADING, text=text, level=level)


def _input(companions: list[str] | None) -> CourseInput:
    kwargs = dict(course_title="T", toc=[{"title": "One"}], target_audience="Engineers", template="technical")
    if companions is not None:
        kwargs["companions"] = companions
    return CourseInput(**kwargs)


def _document() -> CourseDocument:
    blocks = [
        _heading("Chapter One", 1),
        _block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to describe a chain."]),
        _block(BlockType.EXERCISE, instructions="Do it.", steps=["Open the console.", "Run the command."]),
        _block(BlockType.WARNING, text="Don't skip the backup step."),
        _block(BlockType.SUMMARY, key_takeaways=["Chains link calls together."]),
        _block(BlockType.CASE_STUDY, context="A team shipped without review.", challenge="Bugs reached prod.",
               outcome="They added a review gate."),
        _heading("Chapter Two", 1),
        _block(BlockType.LEARNING_OBJECTIVES, items=["Learners will be able to design a prompt."]),
        _block(BlockType.EXERCISE, instructions="Try it.", expected_outcome="A working prompt template."),
    ]
    return CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        pages=[Page(id="p1", page_number=1, kind="content", blocks=blocks)],
    )


def test_no_companions_requested_produces_nothing():
    assert companion_deliverables(_document(), _input(None)) == {}
    assert companion_deliverables(_document(), _input([])) == {}


def test_job_aid_combines_objectives_exercise_steps_and_warnings():
    out = companion_deliverables(_document(), _input(["job_aid"]))["job_aid"]
    sections = {s["chapter"]: s for s in out["sections"]}
    assert sections["Chapter One"]["checklist"] == [
        "Learners will be able to describe a chain.",
        "Open the console.",
        "Run the command.",
    ]
    assert sections["Chapter One"]["watch_outs"] == ["Don't skip the backup step."]
    # Chapter Two has an objective but no steps/warnings of its own.
    assert sections["Chapter Two"]["checklist"] == ["Learners will be able to design a prompt."]
    assert sections["Chapter Two"]["watch_outs"] == []


def test_quick_ref_uses_summary_and_falls_back_to_objectives():
    out = companion_deliverables(_document(), _input(["quick_ref"]))["quick_ref"]
    sections = {s["chapter"]: s for s in out["sections"]}
    assert sections["Chapter One"]["key_points"] == ["Chains link calls together."]
    # No SUMMARY block in Chapter Two - falls back to its objectives.
    assert sections["Chapter Two"]["key_points"] == ["Learners will be able to design a prompt."]


def test_manager_guide_turns_objectives_into_prompts_and_uses_case_study():
    out = companion_deliverables(_document(), _input(["manager_guide"]))["manager_guide"]
    assert "70-20-10" in out["note"]
    sections = {s["chapter"]: s for s in out["sections"]}
    assert sections["Chapter One"]["coaching_prompts"] == [
        "Ask them to show you: Learners will be able to describe a chain."
    ]
    assert sections["Chapter One"]["scenario_notes"] == [
        "A team shipped without review.",
        "Bugs reached prod.",
        "They added a review gate.",
    ]
    # No CASE_STUDY in Chapter Two - falls back to the exercise's expected outcome.
    assert sections["Chapter Two"]["scenario_notes"] == ["A working prompt template."]


def test_all_three_can_be_requested_together():
    out = companion_deliverables(_document(), _input(["job_aid", "quick_ref", "manager_guide"]))
    assert set(out) == {"job_aid", "quick_ref", "manager_guide"}


def test_companions_endpoint(client):
    response = client.post(
        "/api/courses",
        json={
            "course_title": "Intro to RAG",
            "toc": [{"title": "One"}, {"title": "Two"}],
            "target_audience": "Engineers",
            "template": "technical",
            "run_planner": False,
            "companions": ["job_aid"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    generated = client.post(f"/api/courses/{created['course_id']}/generate", json={"mode": "sync"})
    assert generated.status_code == 200, generated.text

    body = client.get(f"/api/documents/{created['document_id']}/companions").json()
    assert set(body) == {"job_aid"}
    assert body["job_aid"]["title"] == "Job aid"
