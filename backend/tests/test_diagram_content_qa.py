"""Semantic/content-relevance QA for the generic node+edge diagram family -
`app.services.diagram_content_qa`. New, previously-untested module (see the
visual pipeline audit at C:\\Users\\Dell\\.claude\\plans\\linear-wobbling-puppy.md):
closes a real regression-coverage gap, exercised before only indirectly
through other modules' tests."""

from __future__ import annotations

from app.services import mock_ai as mock_ai_module
from app.services.diagram_content_qa import (
    AUDIENCE_MISMATCH,
    CITATION_LEAKED_INTO_LABEL,
    CONTRADICTS_SOURCE_TEXT,
    GENERIC_CONTENT,
    MISSING_KEY_CONCEPT,
    MISSPELLED_OR_GARBLED_TEXT,
    NUMERIC_CONTRADICTS_SOURCE_TEXT,
    OFF_TOPIC,
    SOURCE_GROUNDING_UNSUPPORTED,
    STRUCTURALLY_GARBLED_TEXT,
    UNNECESSARILY_COMPLEX,
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


async def test_citation_ref_tag_in_node_label_short_circuits_before_any_ai_call():
    """Layer 0 (deterministic, free) catches this app's own reserved
    citation-tag syntax leaking into a node label - the real, confirmed
    production case (a live course's diagram literally carried a
    knowledge-library reference tag inside a node label) - and never spends
    the LLM critique call to confirm it."""
    spec = _spec(nodes=[
        DiagramNode(id="a", label="Mapping requires SME review of rubrics and thresholds [G2.7]"),
        DiagramNode(id="b", label="Apply the mapping"),
    ])
    result = await evaluate_content(spec, brief="Explain the mapping review process.", course_title="T", ai=_ExplodingAI(None))
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {CITATION_LEAKED_INTO_LABEL}
    assert all(issue.deterministic for issue in result.issues)


async def test_source_filename_in_edge_label_short_circuits_before_any_ai_call():
    """Same Layer 0 check, the other real shape this takes: a bare source
    filename with a document extension (as `render_source_excerpts` writes
    it) instead of a bracketed ref tag - the shape behind the screenshot
    evidence this check was built from (a document title/filename used as
    diagram content)."""
    spec = _spec(edges=[DiagramEdge(source="a", target="b", label="GSA_guides_4_Alagents.pdf")])
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=_ExplodingAI(None))
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {CITATION_LEAKED_INTO_LABEL}
    assert all(issue.deterministic for issue in result.issues)


async def test_cited_source_title_copied_as_node_label_short_circuits_before_any_ai_call():
    """Layer 0 (deterministic, free): a node label that IS one of this
    chapter's own real cited source titles - the free-text shape the
    bracketed-ref-tag check above cannot see (the original real production
    screenshot: a concept_map's satellite nodes were literally the
    chapter's cited document titles, e.g. "Nanopublications: The anatomy of
    a nanopublication (Groth et al.)"). Matches even with a trailing
    author/org parenthetical the writer or model added on either side."""
    cited_titles = [
        "Nanopublications: The anatomy of a nanopublication",
        "GSA_guides_4_Alagents_Provenance_and_Evidence_Tracking.pdf",
    ]
    spec = _spec(kind="concept_map", nodes=[
        DiagramNode(id="a", label="Provenance and graph store resources"),
        DiagramNode(id="b", label="Nanopublications: The anatomy of a nanopublication (Groth et al.)"),
    ], edges=[])
    result = await evaluate_content(
        spec, brief="Map the key resources for this topic.", course_title="T", ai=_ExplodingAI(None),
        cited_source_titles=cited_titles,
    )
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {CITATION_LEAKED_INTO_LABEL}
    assert all(issue.deterministic for issue in result.issues)


async def test_concept_sharing_a_word_with_a_cited_title_is_not_flagged():
    """No false positive: a legitimate, on-topic concept node that happens
    to share a word or two with a cited source's title must NOT match - only
    a label that reuses the source's title/filename itself (as a
    contiguous, near-complete match) counts. Conservative by design, per
    this session's "never guess, never flag without real ground truth"
    standard."""
    cited_titles = ["Nanopublications: The anatomy of a nanopublication"]
    spec = _spec(nodes=[
        DiagramNode(id="a", label="Nanopublication formats in modern data pipelines"),
        DiagramNode(id="b", label="How publishers version their datasets"),
    ])
    result = await evaluate_content(
        spec, brief="Explain nanopublication formats.", course_title="T", ai=MockAIClient(None),
        cited_source_titles=cited_titles,
    )
    assert CITATION_LEAKED_INTO_LABEL not in {issue.reason for issue in result.issues}


async def test_no_cited_source_titles_is_a_safe_no_op():
    """The common case: a call site with no real cited-source list for this
    chapter (the default) must never flag anything from this check - it is
    a pure addition, never a new way for an unrelated diagram to fail."""
    spec = _spec(nodes=[
        DiagramNode(id="a", label="Nanopublications: The anatomy of a nanopublication (Groth et al.)"),
    ])
    result = await evaluate_content(
        spec, brief="Map the key resources for this topic.", course_title="T", ai=MockAIClient(None),
    )
    assert CITATION_LEAKED_INTO_LABEL not in {issue.reason for issue in result.issues}


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


# Canonical Visual Spec milestone: source-grounding fields on ContentCritique.


async def test_no_source_excerpt_never_asks_the_grounding_question(monkeypatch):
    """The default `source_excerpt=""` must never trip either new field,
    even if a careless critique builder set them - the grounding question is
    never even asked without a real excerpt to check against (see
    `evaluate_content`'s own docstring)."""
    def over_eager_critique(user: str) -> dict:
        assert "SOURCE TEXT" not in user, "grounding block must not appear in the prompt with no excerpt"
        return {}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", over_eager_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert result.passed


async def test_contradicts_source_text_produces_a_non_deterministic_issue(monkeypatch):
    def contradiction_critique(user: str) -> dict:
        assert "SOURCE TEXT" in user
        return {
            "contradicts_source_text": True,
            "contradicts_source_text_detail": "The diagram shows the client retrying 5 times; the text says 3.",
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", contradiction_critique)
    spec = _spec()
    result = await evaluate_content(
        spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None),
        source_excerpt="The client retries up to 3 times before giving up.",
    )
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {CONTRADICTS_SOURCE_TEXT}
    assert all(issue.deterministic is False for issue in result.issues)
    assert "retrying 5 times" in result.feedback()


async def test_unsupported_claims_produces_a_separate_issue_type(monkeypatch):
    def unsupported_critique(user: str) -> dict:
        return {"unsupported_claims": ["the server caches responses for 60 seconds"]}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", unsupported_critique)
    spec = _spec()
    result = await evaluate_content(
        spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None),
        source_excerpt="The server processes the request and returns a response.",
    )
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {SOURCE_GROUNDING_UNSUPPORTED}
    assert "caches responses" in result.feedback()


async def test_contradiction_and_unsupported_claim_both_reported_independently(monkeypatch):
    """Genuinely different cases (a confirmed conflict vs. an unaddressed
    claim) must never collapse into one finding type - each is persisted
    and routed separately downstream (quality_framework.py)."""
    def both_critique(user: str) -> dict:
        return {
            "contradicts_source_text": True,
            "contradicts_source_text_detail": "Conflict.",
            "unsupported_claims": ["Something else unaddressed."],
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", both_critique)
    spec = _spec()
    result = await evaluate_content(
        spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None),
        source_excerpt="Some real course text.",
    )
    assert {issue.reason for issue in result.issues} == {CONTRADICTS_SOURCE_TEXT, SOURCE_GROUNDING_UNSUPPORTED}


async def test_numeric_contradiction_short_circuits_before_any_ai_call():
    """Layer 0 (deterministic numeric check) catches a high-confidence
    mismatch against the source excerpt and never spends the LLM critique
    call - same short-circuit contract as the garbled-text check."""
    spec = _spec(nodes=[
        DiagramNode(id="a", label="Reaction yield", detail="75% under these conditions"),
        DiagramNode(id="b", label="Result"),
    ])
    result = await evaluate_content(
        spec, brief="Explain the reaction yield.", course_title="T", ai=_ExplodingAI(None),
        source_excerpt="Under these exact conditions, the reaction yield was only 25%.",
    )
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {NUMERIC_CONTRADICTS_SOURCE_TEXT}
    assert all(issue.deterministic for issue in result.issues)


async def test_no_template_audience_never_asks_the_audience_question(monkeypatch):
    """The default `template_audience=""` must never trip `audience_match`,
    even if a careless critique builder set it false - the audience
    question is never even asked without a real audience to check against
    (see `evaluate_content`'s own docstring), mirroring `test_no_source_
    excerpt_never_asks_the_grounding_question`'s own shape."""
    def over_eager_critique(user: str) -> dict:
        assert "TARGET AUDIENCE" not in user, "audience block must not appear in the prompt with no audience"
        return {}

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", over_eager_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert result.passed


async def test_audience_mismatch_produces_a_non_deterministic_issue(monkeypatch):
    def mismatch_critique(user: str) -> dict:
        assert "TARGET AUDIENCE: young children" in user
        return {
            "audience_match": False,
            "audience_match_detail": "Uses unexplained jargon like 'idempotent' for young children.",
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", mismatch_critique)
    spec = _spec()
    result = await evaluate_content(
        spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None),
        template_audience="young children",
    )
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {AUDIENCE_MISMATCH}
    assert all(issue.deterministic is False for issue in result.issues)
    assert "idempotent" in result.feedback()


async def test_not_appropriately_scoped_produces_an_unnecessarily_complex_issue(monkeypatch):
    def scope_critique(user: str) -> dict:
        return {
            "appropriately_scoped": False,
            "appropriately_scoped_detail": "Three nodes describe retry/backoff detail the brief never asked for.",
        }

    monkeypatch.setitem(mock_ai_module._BUILDERS, "ContentCritique", scope_critique)
    spec = _spec()
    result = await evaluate_content(spec, brief="Explain the request flow.", course_title="T", ai=MockAIClient(None))
    assert not result.passed
    assert {issue.reason for issue in result.issues} == {UNNECESSARILY_COMPLEX}
    assert "retry/backoff" in result.feedback()


def test_content_critique_system_prompt_names_the_new_learning_effectiveness_judgment():
    from app.services.diagram_content_qa import CONTENT_CRITIQUE_SYSTEM, _AUDIENCE_QUESTION

    assert "appropriately_scoped" in CONTENT_CRITIQUE_SYSTEM
    assert "audience_match" in _AUDIENCE_QUESTION


async def test_numeric_equivalent_representation_does_not_short_circuit(monkeypatch):
    """0.5 vs 50% vs 1/2 are the SAME value - must reach the (happy-path)
    LLM critique rather than being misflagged as a numeric contradiction."""
    spec = _spec(nodes=[
        DiagramNode(id="a", label="Success rate", detail="50% in this scenario"),
        DiagramNode(id="b", label="Result"),
    ])
    result = await evaluate_content(
        spec, brief="Explain the success rate.", course_title="T", ai=MockAIClient(None),
        source_excerpt="Research shows the success probability is 0.5 for this exact scenario.",
    )
    assert result.passed
