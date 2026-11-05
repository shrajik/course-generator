"""Semantic/content-relevance QA for the generic node+edge diagram family -
`app.services.diagram_content_qa`. New, previously-untested module (see the
visual pipeline audit at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md):
closes a real regression-coverage gap, exercised before only indirectly
through other modules' tests."""

from __future__ import annotations

from app.services import mock_ai as mock_ai_module
from app.services.diagram_content_qa import (
    GENERIC_CONTENT,
    MISSING_KEY_CONCEPT,
    MISSPELLED_OR_GARBLED_TEXT,
    OFF_TOPIC,
    STRUCTURALLY_GARBLED_TEXT,
    evaluate_content,
)
from app.services.mock_ai import MockAIClient
from app.schemas.diagram import DiagramEdge, DiagramNode, DiagramSpec


class _ExplodingAI(MockAIClient):
    async def structured(self, **kwargs):  # noqa: ANN003
        raise AssertionError("Layer 1 (the LLM critique) must not run when Layer 0 already found an issue")


def _spec(kind: str = "flow_chart", **overrides) -> DiagramSpec:
    base = dict(
        kind=kind,
        title="How a request is handled",
        nodes=[
            DiagramNode(id="a", label="Client sends request"),
            DiagramNode(id="b", label="Server processes it"),
        ],
        edges=[DiagramEdge(source="a", target="b", label="HTTP")],
    )
    base.update(overrides)
    return DiagramSpec(**base)


async def test_garbled_label_short_circuits_before_any_ai_call():
    """Layer 0 (deterministic, free) catches the Unicode replacement
    character and never spends the LLM critique call to confirm it - proven
    by an AI client whose .structured() would raise if ever invoked."""
    spec = _spec(nodes=[DiagramNode(id="a", label="Node \ufffd label")])
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=_ExplodingAI(None))
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {STRUCTURALLY_GARBLED_TEXT}
    assert all(issue.deterministic for issue in result.issues)


async def test_ungraded_kinds_are_a_no_op():
    """schematic and concept_experience each have their own dedicated
    semantic check elsewhere - this module must never double-charge an LLM
    call for either, even with content that would otherwise fail every
    critique question."""
    for kind in ("schematic", "concept_experience"):
        spec = _spec(kind=kind)
        result = await evaluate_content(
            spec, brief="Explain the request flow.", course_title="T", ai=_ExplodingAI(None)
        )
        assert result.passed


async def test_blank_brief_is_a_no_op():
    """Nothing to grade the content against - must not spend an AI call."""
    spec = _spec()
    result = await evaluate_content(spec, brief="", course_title="T", ai=_ExplodingAI(None))
    assert result.passed


async def test_happy_path_produces_no_issues():
    """With no builder registered for ContentCritique, MockAIClient returns
    the schema's own defaults (every judgment True/empty) - the same
    "nothing wrong" result a genuinely good diagram would get."""
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert result.passed


async def test_off_topic_critique_produces_a_non_deterministic_issue(monkeypatch):
    def off_topic_critique(user: str) -> dict:
        return {"on_topic": False, "on_topic_detail": "This describes a database schema, not a request flow."}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", off_topic_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {OFF_TOPIC}
    assert "database schema" in result.feedback()
    assert all(issue.deterministic is False for issue in result.issues)


async def test_generic_content_critique_produces_an_issue(monkeypatch):
    def generic_critique(user: str) -> dict:
        return {"specific_enough": False, "specific_detail": "Labels are just Step 1 / Step 2."}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", generic_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert {issue.reason for issue in result.issues} == {GENERIC_CONTENT}


async def test_missing_concepts_critique_names_them(monkeypatch):
    def missing_critique(user: str) -> dict:
        return {"missing_concepts": ["authentication", "rate limiting"]}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", missing_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert {issue.reason for issue in result.issues} == {MISSING_KEY_CONCEPT}
    assert "authentication" in result.issues[0].detail and "rate limiting" in result.issues[0].detail


async def test_text_legible_false_produces_an_issue(monkeypatch):
    def misspelled_critique(user: str) -> dict:
        return {"text_legible": False, "text_legible_detail": "'Recieve' is misspelled on the second node."}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", misspelled_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert {issue.reason for issue in result.issues} == {MISSPELLED_OR_GARBLED_TEXT}
