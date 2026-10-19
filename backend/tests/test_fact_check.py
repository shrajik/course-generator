"""The fact-check audit: rules, AI verdicts, the service and the endpoints."""

from __future__ import annotations

import time

import pytest

from app.agents.auditor import AuditorAgent
from app.core.errors import ConflictError, NotFoundError
from app.course.fact_check import (
    PassageIndex,
    AuditBlock,
    apply_verdicts,
    audit_blocks,
    collect_audit_blocks,
    content_tokens,
    extract_refs,
    has_specifics,
    is_candidate,
    normalise_numbers,
    split_sentences,
    strip_refs,
    verification_candidates,
)
from app.schemas.audit import AuditFinding, ClaimVerdict, ClaimVerdicts
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.schemas.source import SourceChunk, SourceRecord
from app.services.audit_service import AuditService, get_audit_service
from app.services.source_service import Candidate, SourceLibrary, get_source_service
from tests.sample_files import POLICY_TEXT

OWNER = "user-1"

PASSAGES = {
    "S1.1": "Refunds are issued within fourteen days of a valid request. Digital goods are never refundable.",
    "S1.2": "Disputed refunds are escalated to the billing team, who respond within two business days.",
    "S2.1": "Orders ship from the warehouse by courier within three business days.",
}


def _library(passages: dict[str, str] | None = None) -> SourceLibrary:
    items: dict[str, tuple[SourceRecord, SourceChunk]] = {}
    candidates: list[Candidate] = []
    for index, (ref, text) in enumerate((passages or PASSAGES).items()):
        source = int(ref[1:].split(".")[0])
        record = SourceRecord(
            source_id=f"src_{source:012d}", owner_key=OWNER, filename=f"doc{source}.pdf", kind="pdf",
            size_bytes=1, sha256="x", status="ready", created_at="t", updated_at="t",
        )
        chunk = SourceChunk(index=int(ref.split(".")[1]) - 1, heading_path=["Policy"], page=index + 1, text=text, words=len(text.split()))
        items[ref] = (record, chunk)
        candidates.append(Candidate(key=ref, text=chunk.embed_text()))
    return SourceLibrary(items, candidates)


def _audit(*sentences: str):
    blocks = [AuditBlock(f"block_{i}", i + 1, "Refunds", s) for i, s in enumerate(sentences)]
    return audit_blocks(blocks, _library())


def _kinds(result) -> list[str]:
    return [f.kind for f in result.findings]


# --- text helpers -------------------------------------------------------------


def test_references_are_read_from_brackets_in_order_without_duplicates():
    assert extract_refs("Refunds take 14 days [S1.1]. See also [S1.2, S2.1] and [S1.1].") == ["S1.1", "S1.2", "S2.1"]
    assert extract_refs("An array a[S1.x] or S3.4 outside brackets") == []


def test_markers_and_jargon_tags_are_removed_from_the_shown_sentence():
    assert strip_refs('The <jargon term="SLA">service level</jargon> holds [S1.2].') == "The service level holds ."


def test_sentences_split_on_full_stops_but_not_abbreviations_and_keep_their_markers():
    text = "Use a tool, e.g. a script, to do it [S1.1]. Dr. Smith agrees. It costs 4.5 dollars! Really?"
    assert split_sentences(text) == [
        "Use a tool, e.g. a script, to do it [S1.1].", "Dr. Smith agrees.", "It costs 4.5 dollars!", "Really?",
    ]


def test_a_marker_after_the_full_stop_stays_with_its_sentence():
    assert split_sentences("Refunds take fourteen days. [S1.1] Another sentence follows here.") == [
        "Refunds take fourteen days. [S1.1]", "Another sentence follows here.",
    ]


def test_number_words_and_digits_read_the_same():
    assert normalise_numbers("within fourteen days and Twenty items") == "within 14 days and 20 items"
    assert set(content_tokens("Refunds take 14 days")) >= set(content_tokens("refunds take fourteen days")) - {"14"}


@pytest.mark.parametrize("text,expected", [
    ("Refunds take 14 days.", True), ("Digital goods are never refundable.", True),
    ("Contact the Billing team for help.", True), ('The term "refund" is defined here.', True),
    ("This is a general overview of the topic here.", False),
])
def test_specific_claims_are_told_from_general_prose(text, expected):
    assert has_specifics(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("Is the product returned unused in time?", False), ("Run `pip install` now to continue here.", False),
    ("Too short.", False), ("Refunds are issued within fourteen days of a valid request.", True),
])
def test_only_factual_looking_sentences_are_checked(text, expected):
    assert is_candidate(text) is expected


# --- the deterministic audit --------------------------------------------------


def test_a_sentence_the_sources_state_is_supported_whether_or_not_it_cites():
    result = _audit("Refunds are issued within fourteen days of a valid request.",
                    "Refunds are issued within 14 days of a valid request [S1.1].")
    assert result.findings == [] and result.sentences_checked == 2
    assert result.supported_sentences == 2 and result.cited_sentences == 1


def test_a_citation_to_a_passage_that_does_not_exist_is_a_high_severity_finding():
    result = _audit("Refunds are issued within fourteen days of a valid request [S9.9].")
    finding = result.findings[0]
    assert finding.kind == "invalid_citation" and finding.severity == "high" and finding.refs == ["S9.9"]
    assert "S9.9" in finding.explanation and finding.evidence == []


def test_one_missing_reference_among_valid_ones_is_still_reported():
    result = _audit("Refunds are issued within fourteen days of a valid request [S1.1, S4.2].")
    assert _kinds(result) == ["invalid_citation"] and result.findings[0].evidence[0].ref == "S1.1"


def test_a_citation_that_does_not_support_the_sentence_is_weak():
    result = _audit("Premium subscribers receive complimentary quarterly consultations with specialists [S1.1].")
    finding = result.findings[0]
    assert finding.kind == "weak_citation" and finding.severity == "medium"
    assert finding.evidence[0].ref == "S1.1" and "Refunds are issued" in finding.evidence[0].excerpt


def test_an_uncited_specific_claim_the_sources_lack_is_unsupported():
    finding = _audit("Premium subscribers receive 12 complimentary consultations every year.").findings[0]
    assert finding.kind == "unsupported" and finding.severity == "medium"


def test_uncited_general_content_outside_the_sources_is_only_an_assumption():
    finding = _audit("Customer retention depends on consistent communication throughout the whole journey.").findings[0]
    assert finding.kind == "assumption" and finding.severity == "low"


def test_the_nearest_passage_is_shown_as_evidence_when_it_is_close_enough():
    finding = _audit("Refunds are issued within thirty days of a valid request after approval.").findings[0]
    assert finding.evidence and finding.evidence[0].ref == "S1.1"
    assert "fourteen days" in finding.evidence[0].excerpt


def test_findings_are_ordered_most_serious_first_and_carry_their_location():
    result = _audit(
        "Customer retention depends on consistent communication throughout the whole journey.",
        "Premium subscribers receive 12 complimentary consultations every year.",
        "Refunds are issued within fourteen days of a valid request [S8.1].",
    )
    assert [f.severity for f in result.findings] == ["high", "medium", "low"]
    assert result.findings[0].block_id == "block_2" and result.findings[0].page_number == 3
    assert result.findings[0].chapter == "Refunds"


def test_finding_ids_are_stable_across_runs_and_distinct_per_sentence():
    one = _audit("Premium subscribers receive 12 complimentary consultations every year.").findings[0]
    again = _audit("Premium subscribers receive 12 complimentary consultations every year.").findings[0]
    other = _audit("Premium subscribers receive 13 complimentary consultations every year.").findings[0]
    assert one.finding_id == again.finding_id != other.finding_id


def test_a_library_with_nothing_in_it_flags_specific_claims_without_crashing():
    result = audit_blocks([AuditBlock("b", 1, "C", "Premium subscribers receive 12 complimentary consultations every year.")], SourceLibrary({}, []))
    assert _kinds(result) == ["unsupported"]


def test_passage_search_finds_the_passage_sharing_the_rarest_words():
    index = PassageIndex(_library())
    tokens = set(content_tokens("The courier ships orders from the warehouse"))
    assert index.best(tokens)[0][0] == "S2.1"


def test_only_prose_blocks_are_audited_in_reading_order_with_chapter_titles():
    def block(kind, **content):
        return Block(type=kind, content=content)

    document = CourseDocument(
        document_id="d", course_id="c", course_title="T", template_id="technical_v1",
        pages=[
            Page(id="p0", page_number=1, kind="cover", blocks=[block(BlockType.PARAGRAPH, text="Cover text should be skipped.")]),
            Page(id="p1", page_number=2, kind="content", blocks=[
                block(BlockType.HEADING, text="First chapter", level=1),
                block(BlockType.PARAGRAPH, text="Prose in chapter one."),
                block(BlockType.CODE, code="print(1)"),
                block(BlockType.CALLOUT, text="A callout in chapter one."),
                block(BlockType.HEADING, text="Second chapter", level=1),
                block(BlockType.TIP, text="A tip in chapter two."),
                block(BlockType.QUIZ, questions=[]),
            ]),
        ],
    )
    assert [(b.chapter, b.text) for b in collect_audit_blocks(document)] == [
        ("First chapter", "Prose in chapter one."), ("First chapter", "A callout in chapter one."),
        ("Second chapter", "A tip in chapter two."),
    ]


# --- merging the model's verdicts -----------------------------------------------


def _findings():
    return _audit(
        "Premium subscribers receive 12 complimentary consultations every year.",
        "Customer retention depends on consistent communication throughout the whole journey.",
        "Refunds are issued within thirty days of a valid request after approval.",
        "Refunds are issued within fourteen days of a valid request [S8.1].",
    ).findings


def _by_kind(findings, kind):
    return next(f for f in findings if f.kind == kind)


def test_a_sentence_the_model_finds_supported_is_removed():
    findings = _findings()
    target = _by_kind(findings, "unsupported")
    merged = apply_verdicts(findings, {target.finding_id: ClaimVerdict(claim_id=target.finding_id, verdict="supported")})
    assert target.finding_id not in {f.finding_id for f in merged} and len(merged) == len(findings) - 1


def test_not_a_claim_is_removed_too():
    findings = _findings()
    target = _by_kind(findings, "assumption")
    merged = apply_verdicts(findings, {target.finding_id: ClaimVerdict(claim_id=target.finding_id, verdict="not_a_claim")})
    assert len(merged) == len(findings) - 1


def test_a_contradiction_is_escalated_to_high_with_the_models_reason():
    findings = _findings()
    target = _by_kind(findings, "unsupported")
    reason = "The passage says fourteen days, not thirty."
    merged = apply_verdicts(findings, {target.finding_id: ClaimVerdict(claim_id=target.finding_id, verdict="contradicted", reason=reason)})
    escalated = next(f for f in merged if f.finding_id == target.finding_id)
    assert (escalated.kind, escalated.severity, escalated.verified_by) == ("contradicted", "high", "ai")
    assert escalated.explanation == reason and merged[0].severity == "high"


def test_a_partial_verdict_keeps_the_finding_and_says_so():
    findings = _findings()
    target = _by_kind(findings, "unsupported")
    merged = apply_verdicts(findings, {target.finding_id: ClaimVerdict(claim_id=target.finding_id, verdict="partial", reason="The figure is missing.")})
    kept = next(f for f in merged if f.finding_id == target.finding_id)
    assert kept.kind == "unsupported" and kept.verified_by == "ai" and kept.explanation.startswith("Only partly supported")


def test_an_unsupported_verdict_confirms_the_finding_as_read_by_the_model():
    findings = _findings()
    target = _by_kind(findings, "assumption")
    merged = apply_verdicts(findings, {target.finding_id: ClaimVerdict(claim_id=target.finding_id, verdict="unsupported", reason="Not in the passages.")})
    kept = next(f for f in merged if f.finding_id == target.finding_id)
    assert kept.verified_by == "ai" and kept.explanation == "Not in the passages."


def test_a_verdict_for_an_id_that_was_never_shown_changes_nothing():
    findings = _findings()
    merged = apply_verdicts(findings, {"made-up-id": ClaimVerdict(claim_id="made-up-id", verdict="supported")})
    assert [f.finding_id for f in merged] == [f.finding_id for f in findings]


def test_invalid_citations_are_never_sent_to_a_model_and_the_most_serious_go_first():
    findings = _findings()
    candidates = verification_candidates(findings, 10)
    assert "invalid_citation" not in {f.kind for f in candidates}
    assert [f.severity for f in candidates] == sorted((f.severity for f in candidates), key={"high": 0, "medium": 1, "low": 2}.get)
    assert len(verification_candidates(findings, 1)) == 1


# --- the AI agent ----------------------------------------------------------------


class StubAI:
    def __init__(self, verdicts=None, fail=False):
        self.verdicts, self.fail, self.prompts = verdicts or [], fail, []

    async def structured(self, *, schema, system, user, **kwargs):
        self.prompts.append(user)
        if self.fail:
            raise RuntimeError("model down")
        return schema(verdicts=[{"claim_id": c, "verdict": v, "reason": r} for c, v, r in self.verdicts])


async def test_the_agent_shows_the_model_each_sentence_with_its_evidence_and_nothing_else():
    findings = _findings()
    ai = StubAI()
    await AuditorAgent(ai).verify("Refunds", [_by_kind(findings, "unsupported")])
    prompt = ai.prompts[0]
    assert "CHAPTER: Refunds" in prompt and "Premium subscribers receive 12" in prompt
    assert "Passage [S1.1]" not in prompt or "fourteen days" in prompt


async def test_the_agent_ignores_ids_it_was_not_shown_and_survives_a_failure():
    target = _by_kind(_findings(), "unsupported")
    ai = StubAI([(target.finding_id, "supported", ""), ("invented", "supported", "")])
    assert set(await AuditorAgent(ai).verify("C", [target])) == {target.finding_id}
    assert await AuditorAgent(StubAI(fail=True)).verify("C", [target]) == {}
    assert await AuditorAgent(StubAI()).verify("C", []) == {}


# --- the service ---------------------------------------------------------------


def _document(course_id: str, sentences: list[str]) -> CourseDocument:
    blocks = [Block(id=f"block_{i:012d}", type=BlockType.PARAGRAPH, content={"text": s}) for i, s in enumerate(sentences)]
    return CourseDocument(
        document_id=course_id.replace("crs", "doc"), course_id=course_id, course_title="T", template_id="technical_v1",
        pages=[Page(id="p1", page_number=1, kind="content", blocks=[Block(type=BlockType.HEADING, content={"text": "Refunds", "level": 1}), *blocks])],
    )


async def _source(name="policy.txt", data=POLICY_TEXT):
    sources = get_source_service()
    record = sources.upload(OWNER, name, data)
    await sources.wait(record.source_id)
    return record.source_id


CLAIMS = [
    "Refunds are issued within fourteen days of a valid request.",
    "Premium subscribers receive 12 complimentary consultations every year.",
    "Disputed refunds are escalated to the billing team [S1.9].",
]


async def test_running_the_audit_saves_a_report_with_counts_and_findings(storage):
    source = await _source()
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    report = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source])
    assert report.sentences_checked == 3 and report.supported_sentences == 1 and not report.ai_used
    assert {f.kind for f in report.findings} == {"unsupported", "invalid_citation"}
    assert service.load("crs_aaaaaaaaaaaa").model_dump() == report.model_dump()
    assert report.summary()["by_severity"]["high"] == 1


async def test_the_model_can_overturn_a_finding_and_the_report_says_ai_was_used(storage):
    source = await _source()
    base = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    first = await base.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source])
    unsupported = next(f for f in first.findings if f.kind == "unsupported")
    ai = StubAI([(unsupported.finding_id, "contradicted", "The policy gives no such benefit.")])
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(ai))
    report = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source])
    assert report.ai_used and "contradicted" in {f.kind for f in report.findings}
    assert next(f for f in report.findings if f.kind == "contradicted").verified_by == "ai"


async def test_when_the_model_is_down_the_rule_findings_stand_with_a_warning(storage):
    source = await _source()
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI(fail=True)))
    report = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source])
    assert not report.ai_used and report.findings and any("rules alone" in w for w in report.warnings)


async def test_ai_can_be_switched_off_for_a_run(storage):
    source = await _source()
    ai = StubAI()
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(ai))
    await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source], use_ai=False)
    assert ai.prompts == []


async def test_reviewer_decisions_survive_a_rerun_for_findings_still_raised(storage):
    source = await _source()
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    first = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source], use_ai=False)
    target = first.findings[0]
    service.set_status("crs_aaaaaaaaaaaa", target.finding_id, "accepted")
    again = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source], use_ai=False)
    assert next(f for f in again.findings if f.finding_id == target.finding_id).status == "accepted"
    assert [f.status for f in again.findings].count("open") == len(again.findings) - 1
    assert again.summary()["open"] == len(again.findings) - 1


async def test_a_finding_that_no_longer_applies_takes_its_decision_with_it(storage):
    source = await _source()
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    first = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source], use_ai=False)
    service.set_status("crs_aaaaaaaaaaaa", first.findings[0].finding_id, "dismissed")
    fixed = await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS[:1]), owner_key=OWNER, source_ids=[source], use_ai=False)
    assert fixed.findings == []


async def test_the_audit_refuses_a_course_with_no_usable_sources(storage):
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    with pytest.raises(ConflictError, match="no source documents"):
        await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[])
    with pytest.raises(ConflictError, match="available and ready"):
        await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=["src_aaaaaaaaaaaa"])


async def test_setting_the_status_of_an_unknown_finding_or_report_is_a_not_found(storage):
    service = AuditService(storage=storage, sources=get_source_service(), auditor=AuditorAgent(StubAI()))
    with pytest.raises(NotFoundError):
        service.set_status("crs_aaaaaaaaaaaa", "x", "accepted")
    source = await _source()
    await service.run(document=_document("crs_aaaaaaaaaaaa", CLAIMS), owner_key=OWNER, source_ids=[source], use_ai=False)
    with pytest.raises(NotFoundError):
        service.set_status("crs_aaaaaaaaaaaa", "nope", "accepted")


# --- the endpoints and automatic run -----------------------------------------------


def _wait_ready(client, source_id):
    for _ in range(200):
        body = client.get(f"/api/sources/{source_id}").json()
        if body["status"] in {"ready", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError("not ready")


def _course_with_source(client):
    uploaded = client.post("/api/sources", files={"file": ("policy.txt", POLICY_TEXT, "text/plain")}).json()
    _wait_ready(client, uploaded["source_id"])
    return client.post("/api/courses", json={
        "course_title": "Refund handling", "toc": [{"title": "Refund policy"}, {"title": "Escalation"}],
        "target_audience": "Support agents", "template": "non_technical", "run_planner": False,
        "source_ids": [uploaded["source_id"]],
    }).json()


def test_generating_a_course_with_sources_audits_it_automatically(client):
    course = _course_with_source(client)
    assert client.get(f"/api/courses/{course['course_id']}/audit").status_code == 404
    result = client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert result.status_code == 200, result.text
    report = client.get(f"/api/courses/{course['course_id']}/audit").json()
    assert report["source_count"] == 1 and report["sentences_checked"] > 0
    assert set(report["summary"]) >= {"findings", "open", "by_kind", "by_severity", "supported_percent"}


def test_a_course_without_sources_is_not_audited_and_the_endpoint_says_why(client):
    course = client.post("/api/courses", json={
        "course_title": "T", "toc": [{"title": "One"}], "target_audience": "A", "template": "technical", "run_planner": False,
    }).json()
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert client.get(f"/api/courses/{course['course_id']}/audit").status_code == 404
    rerun = client.post(f"/api/courses/{course['course_id']}/audit")
    assert rerun.status_code == 409 and "no source documents" in rerun.json()["error"]["message"]


def test_rerunning_updating_and_inspecting_findings_over_http(client):
    course = _course_with_source(client)
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    report = client.post(f"/api/courses/{course['course_id']}/audit?use_ai=false").json()
    assert report["findings"], "the offline writer's text is not in the policy, so something is flagged"
    finding_id = report["findings"][0]["finding_id"]

    updated = client.patch(f"/api/courses/{course['course_id']}/audit/findings/{finding_id}", json={"status": "dismissed"})
    assert updated.status_code == 200 and updated.json()["status"] == "dismissed"
    fresh = client.get(f"/api/courses/{course['course_id']}/audit").json()
    assert fresh["summary"]["open"] == len(fresh["findings"]) - 1

    assert client.patch(f"/api/courses/{course['course_id']}/audit/findings/{finding_id}", json={"status": "bogus"}).status_code == 422
    assert client.patch(f"/api/courses/{course['course_id']}/audit/findings/nope", json={"status": "open"}).status_code == 404


def test_a_failed_audit_never_fails_the_generation(client, monkeypatch):
    course = _course_with_source(client)

    async def boom(*args, **kwargs):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(get_audit_service(), "run", boom)
    result = client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert result.status_code == 200 and "The fact check could not be run." in result.json()["warnings"]


def test_the_audit_can_be_switched_off(client, monkeypatch):
    from app.core.config import reset_settings_cache
    from app.services import course_service

    monkeypatch.setenv("ENABLE_FACT_CHECK", "false")
    reset_settings_cache()
    course_service.reset_course_service()
    course = _course_with_source(client)
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    assert client.get(f"/api/courses/{course['course_id']}/audit").status_code == 404


# --- figures are checked on their own ------------------------------------------


def test_a_different_figure_is_caught_even_when_the_wording_matches_closely():
    finding = _audit("Refunds are issued within thirty days of a valid request after approval.").findings[0]
    assert finding.kind == "unsupported" and "30" in finding.explanation


def test_a_cited_passage_that_lacks_the_stated_figure_is_a_weak_citation():
    finding = _audit("Refunds are issued within thirty days of a valid request [S1.1].").findings[0]
    assert finding.kind == "weak_citation" and "30" in finding.explanation
    assert finding.evidence[0].ref == "S1.1"


def test_the_same_figure_written_as_a_word_or_digits_is_supported():
    assert _audit("Refunds are issued within 14 days of a valid request.").findings == []
    assert _audit("Disputed refunds reach the billing team within 2 business days [S1.2].").findings == []
