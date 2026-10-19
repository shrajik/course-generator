"""Gate 2: the reviewer's screen - brief, source access, approval gate, notifications."""

from __future__ import annotations

import json
import time

import pytest

from app.core.config import get_settings, reset_settings_cache
from app.core.errors import ConflictError
from app.course.compliance import build_report
from app.course.fact_check import AuditBlock, audit_blocks
from app.course.review_brief import approval_blockers, build_review_brief
from app.schemas.audit import AuditFinding, AuditReport
from app.schemas.blocks import BlockType
from app.schemas.course import CourseInput
from app.schemas.document import Block, CourseDocument, Page
from app.services import course_service
from app.services.audit_service import get_audit_service
from app.services.notification_service import (
    MAX_PER_USER,
    Mailer,
    NotificationService,
    Recipient,
    get_notification_service,
)
from tests.sample_files import POLICY_TEXT
from tests.test_fact_check import _library


# --- notifications -------------------------------------------------------------


@pytest.fixture
def inbox() -> NotificationService:
    return get_notification_service()


def test_an_inbox_starts_empty_and_newest_comes_first(inbox):
    assert inbox.list_for("u1") == ([], 0)
    inbox.add("u1", "generation_done", "First")
    inbox.add("u1", "review_requested", "Second", "Please look", course_id="crs_a", link="/review/doc_a")
    items, unread = inbox.list_for("u1")
    assert [i.title for i in items] == ["Second", "First"] and unread == 2
    assert items[0].link == "/review/doc_a" and items[0].course_id == "crs_a" and not items[0].read


def test_inboxes_are_separate_per_user(inbox):
    inbox.add("u1", "generation_done", "Mine")
    assert inbox.list_for("u2") == ([], 0)


def test_reading_one_or_all_updates_the_unread_count(inbox):
    first = inbox.add("u1", "generation_done", "A")
    inbox.add("u1", "generation_done", "B")
    assert inbox.mark_read("u1", first.notification_id) is True
    assert inbox.mark_read("u1", first.notification_id) is False  # already read
    assert inbox.list_for("u1")[1] == 1
    assert inbox.mark_all_read("u1") == 1 and inbox.list_for("u1")[1] == 0
    assert inbox.mark_read("u1", "ntf_nonexistent") is False


def test_an_inbox_keeps_only_the_most_recent_notifications(inbox):
    for index in range(MAX_PER_USER + 5):
        inbox.add("u1", "generation_done", f"n{index}")
    items, _ = inbox.list_for("u1", limit=500)
    assert len(items) == MAX_PER_USER and items[0].title == f"n{MAX_PER_USER + 4}"


def test_a_malformed_user_id_cannot_write_outside_the_inbox_folder(inbox):
    assert inbox.add("../../etc/passwd", "generation_done", "x") is not None  # accepted but goes nowhere
    assert not (inbox.root.parent / "etc").exists()
    assert inbox.list_for("../../etc/passwd") == ([], 0)


def test_a_corrupt_inbox_file_reads_as_empty_instead_of_crashing(inbox):
    inbox.root.mkdir(parents=True, exist_ok=True)
    (inbox.root / "u1.json").write_text("{not json", encoding="utf-8")
    assert inbox.list_for("u1") == ([], 0)
    assert inbox.add("u1", "generation_done", "recovers") is not None


class FakeMailer(Mailer):
    def __init__(self, settings, fail=False):
        super().__init__(settings)
        self.sent, self.fail = [], fail

    @property
    def enabled(self):
        return True

    def _send(self, to, subject, body):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, subject, body))


async def test_notify_writes_the_inbox_and_emails_with_a_link(monkeypatch):
    monkeypatch.setenv("PUBLIC_APP_URL", "https://app.example.com/")
    reset_settings_cache()
    settings = get_settings()
    mailer = FakeMailer(settings)
    service = NotificationService(settings, mailer)
    delivered = await service.notify(
        [Recipient("u1", "a@example.com"), Recipient("u2", None)],
        "review_requested", "Review requested", "A course needs you.", course_id="crs_a", link="/review/doc_a",
    )
    assert delivered == 2 and service.list_for("u1")[1] == 1 and service.list_for("u2")[1] == 1
    assert mailer.sent == [("a@example.com", "Review requested", "A course needs you.\n\nOpen: https://app.example.com/review/doc_a")]


async def test_a_failing_mail_server_never_stops_the_inbox_notification():
    settings = get_settings()
    service = NotificationService(settings, FakeMailer(settings, fail=True))
    assert await service.notify([Recipient("u1", "a@example.com")], "generation_done", "Done") == 1
    assert service.list_for("u1")[1] == 1


async def test_email_is_skipped_when_no_host_is_configured():
    settings = get_settings()
    assert Mailer(settings).enabled is False
    assert await Mailer(settings).send("a@example.com", "s", "b") is False


async def test_notifications_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    reset_settings_cache()
    service = NotificationService(get_settings())
    assert await service.notify([Recipient("u1")], "generation_done", "x") == 0
    assert service.list_for("u1") == ([], 0)


def test_the_inbox_endpoints(client):
    inbox = get_notification_service()
    first = inbox.add("local", "generation_done", "Ready", "Course finished", link="/editor/doc_1")
    inbox.add("local", "course_approved", "Approved")
    body = client.get("/api/notifications").json()
    assert body["unread"] == 2 and body["notifications"][0]["title"] == "Approved"
    assert client.post(f"/api/notifications/{first.notification_id}/read").status_code == 204
    assert client.get("/api/notifications").json()["unread"] == 1
    assert client.post("/api/notifications/ntf_nope/read").status_code == 404
    assert client.post("/api/notifications/read-all").json() == {"marked": 1}
    assert client.get("/api/notifications").json()["unread"] == 0


# --- the audit report keeps where each sentence sits ---------------------------


def test_findings_say_where_in_the_block_their_sentence_is():
    text = "Intro words that are not checked. Premium subscribers receive 12 complimentary consultations every year. Closing."
    finding = audit_blocks([AuditBlock("b1", 1, "C", text)], _library()).findings[0]
    assert text[finding.start : finding.end] == "Premium subscribers receive 12 complimentary consultations every year."


def test_offsets_survive_a_citation_marker_after_the_full_stop():
    text = "Refunds are issued within fourteen days of a valid request. [S9.9] Another sentence follows here for padding."
    finding = audit_blocks([AuditBlock("b1", 1, "C", text)], _library()).findings[0]
    assert finding.kind == "invalid_citation" and finding.start == 0
    assert "[S9.9]" in text[finding.start : finding.end]


# --- the approval rule ---------------------------------------------------------


def _finding(severity="high", status="open", chapter="Refunds", kind="invalid_citation") -> AuditFinding:
    return AuditFinding(
        finding_id=f"f-{severity}-{status}", block_id="b", chapter=chapter, sentence="S",
        kind=kind, severity=severity, status=status,
    )


def _report(*findings: AuditFinding) -> AuditReport:
    return AuditReport(course_id="crs_a", document_id="doc_a", generated_at="t", source_count=1, findings=list(findings), sentences_checked=10, supported_sentences=4, cited_sentences=3)


def test_a_course_without_sources_is_never_blocked():
    assert approval_blockers(has_sources=False, audit=None) == []


def test_a_course_with_sources_needs_a_fact_check_first():
    assert "has not been run" in approval_blockers(has_sources=True, audit=None)[0]


def test_an_open_high_severity_finding_blocks_approval_but_decided_or_lower_ones_do_not():
    assert "1 high-severity fact-check finding must" in approval_blockers(has_sources=True, audit=_report(_finding()))[0]
    assert "2 high-severity fact-check findings must" in approval_blockers(
        has_sources=True, audit=_report(_finding(), _finding().model_copy(update={"finding_id": "x2"})))[0]
    assert approval_blockers(has_sources=True, audit=_report(_finding(status="accepted"))) == []
    assert approval_blockers(has_sources=True, audit=_report(_finding(status="dismissed"))) == []
    assert approval_blockers(has_sources=True, audit=_report(_finding("medium"), _finding("low"))) == []


# --- the Review Assistant --------------------------------------------------------


def _document(blocks: list[Block]) -> CourseDocument:
    return CourseDocument(
        document_id="doc_a", course_id="crs_a", course_title="T", template_id="technical_v1",
        pages=[Page(id="p1", page_number=1, kind="content", blocks=[
            Block(type=BlockType.HEADING, content={"text": "Refunds", "level": 1}), *blocks,
            Block(type=BlockType.HEADING, content={"text": "Shipping", "level": 1}),
        ])],
    )


def _brief(audit, *, blocks=None, has_sources=True, status="in_review"):
    document = _document(blocks or [])
    return build_review_brief(document=document, audit=audit, compliance=build_report(document), has_sources=has_sources, review_status=status)


def test_the_brief_leads_with_the_blocker_and_says_the_course_is_not_ready():
    brief = _brief(_report(_finding()))
    assert brief["ready_to_approve"] is False and brief["headline"] == brief["blockers"][0]


def test_the_brief_counts_open_findings_per_chapter_and_ignores_decided_ones():
    brief = _brief(_report(_finding("medium", chapter="Refunds"), _finding("low", chapter="Refunds"), _finding("high", status="accepted", chapter="Shipping")))
    assert brief["chapters"] == [
        {"chapter": "Refunds", "high": 0, "medium": 1, "low": 1}, {"chapter": "Shipping", "high": 0, "medium": 0, "low": 0},
    ]
    assert brief["ready_to_approve"] is True and brief["stats"]["open_findings"] == 2


def test_the_brief_lists_advisories_a_person_should_weigh():
    image = Block(type=BlockType.IMAGE, content={"generated": True, "kind": "illustration", "alt": "x"})
    brief = _brief(_report(_finding("medium"), _finding("low")), blocks=[image])
    text = " ".join(brief["advisories"])
    assert "1 medium-severity finding" in text and "1 assumption" in text
    assert "1 AI-generated image" in text and "Quality checks failing" in text and "must confirm" in text


def test_the_brief_says_so_when_there_is_nothing_to_check_against():
    brief = _brief(None, has_sources=False)
    assert brief["ready_to_approve"] is True and brief["stats"]["fact_checked"] is False
    assert any("not written from source documents" in a for a in brief["advisories"])


def test_the_headline_reflects_an_approved_course_and_a_clean_one():
    assert "has been approved" in _brief(_report(), status="approved")["headline"]
    clean = _brief(_report(), blocks=[Block(type=BlockType.QUIZ, content={"questions": []})])
    assert clean["blockers"] == []


# --- approval through the course service ---------------------------------------


def _upload(client, data=POLICY_TEXT):
    uploaded = client.post("/api/sources", files={"file": ("policy.txt", data, "text/plain")}).json()
    for _ in range(200):
        if client.get(f"/api/sources/{uploaded['source_id']}").json()["status"] in {"ready", "failed"}:
            break
        time.sleep(0.05)
    return uploaded["source_id"]


def _generated_course(client, source_ids):
    course = client.post("/api/courses", json={
        "course_title": "Refund handling", "toc": [{"title": "Refund policy"}], "target_audience": "Agents",
        "template": "non_technical", "run_planner": False, "source_ids": source_ids,
    }).json()
    client.post(f"/api/courses/{course['course_id']}/generate", json={"mode": "sync"})
    return course


async def _submit(course_id):
    return await course_service.get_course_service().submit_for_review(course_id, "author-1")


async def test_final_approval_is_blocked_until_high_findings_are_decided(client):
    course = _generated_course(client, [_upload(client)])
    service = course_service.get_course_service()
    audit = get_audit_service()
    report = audit.load(course["course_id"])
    assert report is not None
    report.findings.append(_finding())
    audit._save(report)

    await _submit(course["course_id"])
    with pytest.raises(ConflictError, match="Final approval is blocked.*high-severity"):
        await service.approve_course(course["course_id"], "reviewer-1", "ok")

    audit.set_status(course["course_id"], "f-high-open", "dismissed")
    approved = await service.approve_course(course["course_id"], "reviewer-1", "Checked against the manual.")
    assert approved.review_status == "approved"


async def test_final_approval_is_blocked_while_no_fact_check_exists(client):
    course = _generated_course(client, [_upload(client)])
    audit = get_audit_service()
    audit._path(course["course_id"]).unlink()
    await _submit(course["course_id"])
    with pytest.raises(ConflictError, match="fact check has not been run"):
        await course_service.get_course_service().approve_course(course["course_id"], "reviewer-1")


async def test_a_course_without_sources_is_approved_as_before(client):
    course = _generated_course(client, [])
    await _submit(course["course_id"])
    approved = await course_service.get_course_service().approve_course(course["course_id"], "reviewer-1")
    assert approved.review_status == "approved"


async def test_the_rule_can_be_switched_off(client, monkeypatch):
    course = _generated_course(client, [_upload(client)])
    report = get_audit_service().load(course["course_id"])
    report.findings.append(_finding())
    get_audit_service()._save(report)
    monkeypatch.setenv("REQUIRE_FINDINGS_RESOLVED", "false")
    reset_settings_cache()
    course_service.reset_course_service()
    service = course_service.get_course_service()
    await service.submit_for_review(course["course_id"], "author-1")
    assert (await service.approve_course(course["course_id"], "reviewer-1")).review_status == "approved"


async def test_the_author_is_told_when_a_course_is_approved_or_sent_back(client):
    service = course_service.get_course_service()
    record = await service.create_course(
        CourseInput(course_title="Owned", toc=[{"title": "One"}], target_audience="A", template="technical"),
        run_planner=False, owner_id="author-1",
    )
    await service.submit_for_review(record.course_id, "author-1")
    await service.request_changes(record.course_id, "reviewer-1", "Please cite the manual.")
    await service.submit_for_review(record.course_id, "author-1")
    await service.approve_course(record.course_id, "reviewer-1", "Looks right.")
    items, unread = get_notification_service().list_for("author-1")
    assert [i.kind for i in items] == ["course_approved", "changes_requested"] and unread == 2
    assert items[1].message == "Please cite the manual." and items[0].link.startswith("/editor/doc_")


async def test_the_author_is_told_when_generation_finishes(client):
    service = course_service.get_course_service()
    record = await service.create_course(
        CourseInput(course_title="Owned", toc=[{"title": "One"}], target_audience="A", template="technical"),
        run_planner=False, owner_id="author-2",
    )
    await service.generate(record.course_id, __import__("app.schemas.course", fromlist=["GenerateRequest"]).GenerateRequest(mode="sync"))
    items, _ = get_notification_service().list_for("author-2")
    assert items and items[0].kind == "generation_done" and "is ready" in items[0].title


async def test_a_failing_notifier_never_blocks_a_review_action(client, monkeypatch):
    service = course_service.get_course_service()
    record = await service.create_course(
        CourseInput(course_title="Owned", toc=[{"title": "One"}], target_audience="A", template="technical"),
        run_planner=False, owner_id="author-3",
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("inbox exploded")

    monkeypatch.setattr(get_notification_service(), "notify", boom)
    await service.submit_for_review(record.course_id, "author-3")
    assert (await service.request_changes(record.course_id, "reviewer-1", "Fix it.")).review_status == "changes_requested"


# --- the reviewer reads the sources through the course --------------------------


def test_a_reviewer_can_read_the_courses_sources_and_cited_passages(client):
    course = _generated_course(client, [_upload(client)])
    sources = client.get(f"/api/courses/{course['course_id']}/sources").json()
    assert [(s["prefix"], s["filename"], s["status"]) for s in sources] == [("S1", "policy.txt", "ready")]

    passages = client.get(f"/api/courses/{course['course_id']}/sources/1/passages?limit=2").json()
    assert [p["ref"] for p in passages] == ["S1.1", "S1.2"] and "fourteen days" in passages[0]["text"]
    assert client.get(f"/api/courses/{course['course_id']}/sources/1/passages?offset=99").json() == []
    assert client.get(f"/api/courses/{course['course_id']}/sources/7/passages").json() == []

    cited = client.get(f"/api/courses/{course['course_id']}/passages?refs=S1.3,S1.3,S9.9").json()
    assert [p["ref"] for p in cited] == ["S1.3"] and "billing team" in cited[0]["text"]


def test_the_review_brief_endpoint_reports_blockers_for_a_course_with_sources(client):
    course = _generated_course(client, [_upload(client)])
    brief = client.get(f"/api/courses/{course['course_id']}/review-brief").json()
    assert set(brief) >= {"headline", "ready_to_approve", "blockers", "advisories", "chapters", "stats"}
    assert brief["stats"]["fact_checked"] is True and brief["stats"]["sentences_checked"] > 0

    report = get_audit_service().load(course["course_id"])
    report.findings.append(_finding())
    get_audit_service()._save(report)
    blocked = client.get(f"/api/courses/{course['course_id']}/review-brief").json()
    assert blocked["ready_to_approve"] is False and "high-severity" in blocked["headline"]


def test_the_review_brief_for_a_course_without_sources_is_ready(client):
    course = _generated_course(client, [])
    brief = client.get(f"/api/courses/{course['course_id']}/review-brief").json()
    assert brief["ready_to_approve"] is True and brief["stats"]["fact_checked"] is False


def test_an_unknown_course_is_a_404_on_every_reviewer_endpoint(client):
    for path in ("sources", "sources/1/passages", "passages?refs=S1.1", "review-brief"):
        assert client.get(f"/api/courses/crs_aaaaaaaaaaaa/{path}").status_code == 404
