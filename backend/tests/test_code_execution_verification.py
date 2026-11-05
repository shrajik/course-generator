"""Code-execution verification follow-up (quality governance): sandboxed
execution of `code_cell` blocks during chapter review.

The executor is a separate service, so these tests substitute it with an
in-process fake (an httpx transport), exactly the pattern already used by
test_code_cells.py - no live container, no network, no real model calls.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agents.reviewer import (
    _CODE_CELL_REVISION_TRIGGERING_CHECKS,
    ReviewerAgent,
    _verify_code_cells,
)
from app.core import concurrency
from app.core.config import Settings
from app.schemas.blocks import BlockType
from app.schemas.document import Block
from app.schemas.draft import GeneratedChapter
from app.schemas.review import ChapterReview
from app.services.code_execution import CodeExecutionService
from app.services.quality_framework import build_course_quality_report

LANGUAGES = [
    {"id": "python", "label": "Python", "highlight": "python", "file_extension": "py",
     "version": "Python 3.12", "aliases": ["py", "python3"]},
    {"id": "javascript", "label": "JavaScript", "highlight": "javascript",
     "file_extension": "js", "version": "v22", "aliases": ["js", "node"]},
    {"id": "cpp", "label": "C++", "highlight": "cpp", "file_extension": "cpp",
     "version": "g++ 12", "aliases": ["c++", "cxx"]},
    {"id": "java", "label": "Java", "highlight": "java", "file_extension": "java",
     "version": "21", "aliases": []},
]

_DEFAULT_RESULT = {
    "status": "success", "stdout": "30\n", "stderr": "", "execution_time": 0.2,
    "phase": "run", "exit_code": 0, "truncated": False,
}


class FakeExecutor:
    """Stands in for the executor service. `result_for(body)` decides the
    response per call, so tests can make the SAME code produce different
    outcomes on different calls (to prove no caching) or route on the
    request body's own `code` field (to prove the CURRENT code is what
    actually gets sent, never a stale version)."""

    def __init__(
        self,
        *,
        languages: list[dict] | None = None,
        fail_languages: bool = False,
        fail_execute: bool = False,
        status: int = 200,
        result_for=None,
        on_execute=None,
    ):
        self.requests: list[dict] = []
        self.languages = LANGUAGES if languages is None else languages
        self.fail_languages = fail_languages
        self.fail_execute = fail_execute
        self.status = status
        self.result_for = result_for or (lambda body: dict(_DEFAULT_RESULT))
        self.on_execute = on_execute  # optional async hook for concurrency tests

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/languages":
                if self.fail_languages:
                    raise httpx.ConnectError("executor is down")
                return httpx.Response(200, json=self.languages)
            if self.fail_execute:
                raise httpx.ConnectError("executor is down")
            body = json.loads(request.content)
            self.requests.append(body)
            if self.on_execute is not None:
                await self.on_execute()
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": "nope"})
            result = self.result_for(body)
            return httpx.Response(200, json={**result, "language": body["language"]})

        return httpx.MockTransport(handle)


def _service(fake: FakeExecutor, **overrides) -> CodeExecutionService:
    settings = Settings(code_executor_url="http://executor", **overrides)
    return CodeExecutionService(settings, transport=fake.transport())


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _cell(language: str = "python", code: str = "print(30)", **extra) -> Block:
    return Block(type=BlockType.CODE_CELL, content={"language": language, "code": code, **extra})


def _static_code(language: str = "python", code: str = "def f(): ...") -> Block:
    return Block(type=BlockType.CODE, content={"language": language, "code": code})


@pytest.fixture(autouse=True)
def _reset_limiters():
    concurrency.reset_limiters()
    yield
    concurrency.reset_limiters()


def _final_blocking_findings(structural_findings):
    """The FINAL, classified `QualityFinding`s a chapter's structural
    findings become once `_chapter_findings` applies its fallback defaults
    and `QualityReport` computes `approval_blocking_findings` - round-tripped
    through persistence exactly as storage does, not the raw
    `StructuralFinding` objects `_verify_code_cells` itself returns. This is
    the only way to assert what the approval gate actually sees, since
    `_chapter_findings` is where `severity`/`verification_status` get their
    real values resolved (each `StructuralFinding` override, or the
    pre-existing hardcoded fallback when unset)."""
    chapter = GeneratedChapter(
        chapter_id="chapter_1", chapter_number=1, title="T", structural_findings=structural_findings
    )
    reloaded = GeneratedChapter.model_validate(chapter.model_dump(mode="json"))
    report = build_course_quality_report(course_id="c1", chapters=[reloaded], document=None)
    return report.approval_blocking_findings


# --- successful execution ---------------------------------------------------


async def test_a_successful_code_cell_produces_no_finding():
    fake = FakeExecutor()
    findings = await _verify_code_cells(
        [_cell()], code_execution=_service(fake), settings=_settings()
    )
    assert findings == []
    assert fake.requests  # it really was sent to the executor


# --- compile/runtime errors, one per supported language --------------------


@pytest.mark.parametrize("language", ["python", "javascript", "cpp", "java"])
async def test_a_runtime_or_compile_error_is_an_independently_verified_blocker(language):
    def erroring(body):
        return {
            "status": "error", "stdout": "", "stderr": "NameError: x is not defined",
            "execution_time": 0.1, "phase": "run", "exit_code": 1, "truncated": False,
        }

    fake = FakeExecutor(result_for=erroring)
    findings = await _verify_code_cells(
        [_cell(language=language, code="bad code")],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.check == "code_cell_execution_failed"
    assert "NameError" in finding.description
    assert finding.check in _CODE_CELL_REVISION_TRIGGERING_CHECKS

    # The FINAL classification the approval gate actually sees, after
    # _chapter_findings resolves the (unset) override to its fallback -
    # a confirmed, independently-verified blocker, not merely unproven.
    blocking = _final_blocking_findings(findings)
    assert len(blocking) == 1
    assert blocking[0].verification_status == "independently_verified"
    assert blocking[0].severity == "blocker"


async def test_a_compile_error_is_worded_distinctly_from_a_runtime_error():
    def compile_error(body):
        return {
            "status": "error", "stdout": "", "stderr": "error: expected ';'",
            "execution_time": 0.1, "phase": "compile", "exit_code": 1, "truncated": False,
        }

    fake = FakeExecutor(result_for=compile_error)
    findings = await _verify_code_cells(
        [_cell(language="cpp", code="int main() {")],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert "compile" in findings[0].description


# --- timeout -----------------------------------------------------------------


async def test_a_timeout_never_claims_the_program_is_conceptually_wrong():
    def timed_out(body):
        return {
            "status": "timeout", "stdout": "", "stderr": "", "execution_time": 10.0,
            "phase": "run", "exit_code": None, "truncated": False,
        }

    fake = FakeExecutor(result_for=timed_out)
    findings = await _verify_code_cells(
        [_cell(code="while True: pass")], code_execution=_service(fake), settings=_settings()
    )
    assert len(findings) == 1
    assert findings[0].check == "code_cell_execution_timeout"
    assert "did not finish within the time budget" in findings[0].description
    # The disclaimer must actively deny conceptual-correctness proof, not
    # merely omit the phrase - "does not prove ... conceptually wrong" is
    # the required wording, not an assertion that it IS wrong.
    assert "does not prove the program is conceptually wrong" in findings[0].description


# --- executor unavailable: 429, 5xx, unreachable ----------------------------


async def test_executor_busy_429_is_verification_failed_not_a_content_defect():
    fake = FakeExecutor(status=429)
    findings = await _verify_code_cells(
        [_cell()], code_execution=_service(fake), settings=_settings()
    )
    assert len(findings) == 1
    assert findings[0].verification_status == "verification_failed"
    assert findings[0].verifier == "code_execution"
    assert findings[0].check not in _CODE_CELL_REVISION_TRIGGERING_CHECKS  # must never trigger a rewrite


async def test_executor_5xx_is_verification_failed():
    fake = FakeExecutor(status=500)
    findings = await _verify_code_cells(
        [_cell()], code_execution=_service(fake), settings=_settings()
    )
    assert findings[0].verification_status == "verification_failed"


async def test_an_unreachable_executor_is_verification_failed_and_short_circuits_remaining_cells():
    fake = FakeExecutor(fail_execute=True)
    findings = await _verify_code_cells(
        [_cell(code="a"), _cell(code="b"), _cell(code="c")],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert len(findings) == 3
    assert all(f.verification_status == "verification_failed" for f in findings)
    # Only the FIRST cell actually attempted resolve_language against the
    # dead executor; the rest were short-circuited without redundant calls.
    assert len(fake.requests) == 0


# --- unsupported language ----------------------------------------------------


async def test_an_unsupported_language_is_a_policy_violation_not_a_proven_defect():
    fake = FakeExecutor()  # LANGUAGES has no "ruby"
    findings = await _verify_code_cells(
        [_cell(language="ruby", code="puts 30")],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert len(findings) == 1
    assert findings[0].check == "code_cell_unsupported_language"
    assert not fake.requests  # never reached /execute at all
    assert findings[0].check in _CODE_CELL_REVISION_TRIGGERING_CHECKS

    # The FINAL classification the approval gate actually sees: a confirmed
    # policy violation (independently_verified), not left as an unresolved
    # claim about whether the code itself is correct.
    blocking = _final_blocking_findings(findings)
    assert len(blocking) == 1
    assert blocking[0].verification_status == "independently_verified"
    assert blocking[0].severity == "blocker"


# --- static CODE is never executed -------------------------------------------


async def test_static_code_blocks_are_never_sent_to_the_executor():
    fake = FakeExecutor()
    findings = await _verify_code_cells(
        [_static_code(), _static_code(language="python", code="incomplete fragment")],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert findings == []
    assert fake.requests == []


# --- zero code cells: existing behaviour preserved ---------------------------


async def test_a_chapter_with_no_code_cells_makes_no_executor_calls():
    fake = FakeExecutor()
    findings = await _verify_code_cells(
        [Block(type=BlockType.PARAGRAPH, content={"text": "just prose"})],
        code_execution=_service(fake),
        settings=_settings(),
    )
    assert findings == []
    assert fake.requests == []


async def test_the_feature_can_be_switched_off_entirely():
    fake = FakeExecutor()
    findings = await _verify_code_cells(
        [_cell()],
        code_execution=_service(fake),
        settings=_settings(enable_code_cell_execution_verification=False),
    )
    assert findings == []
    assert fake.requests == []


# --- empty code cell ----------------------------------------------------------


async def test_an_empty_code_cell_is_flagged_without_calling_the_executor():
    fake = FakeExecutor()
    findings = await _verify_code_cells(
        [_cell(code="   ")], code_execution=_service(fake), settings=_settings()
    )
    assert len(findings) == 1
    assert findings[0].check == "code_cell_no_code"
    assert not fake.requests


# --- oversized code -----------------------------------------------------------


async def test_oversized_code_raises_validation_failed_error_and_is_handled_as_a_content_defect():
    """`CodeExecutionService.execute` itself rejects code over its own byte
    ceiling with a `ValidationFailedError` (see test_code_cells.py's
    `test_oversized_code_is_rejected`, which proves that client-level
    behaviour) - this proves `_verify_code_cells` actually catches that
    specific exception and classifies it, rather than letting it propagate
    and discard the whole chapter review (the ONLY `ValidationFailedError`
    `execute()` can still raise once empty-code and unsupported-language
    have already been handled before it is ever called)."""
    fake = FakeExecutor()
    service = _service(fake, code_execution_max_bytes=1000)
    findings = await _verify_code_cells(
        [_cell(code="#" * 1001)], code_execution=service, settings=_settings()
    )

    assert len(findings) == 1
    finding = findings[0]
    # Inspected, not assumed: this is the same bucket a compile/runtime
    # failure lands in - a real content defect (the cell violates the
    # sandbox's own size ceiling), not an infrastructure/verification
    # failure, so it is independently_verified and revision-worthy.
    assert finding.check == "code_cell_execution_failed"
    assert "too large" in finding.description
    assert finding.check in _CODE_CELL_REVISION_TRIGGERING_CHECKS
    assert not fake.requests  # execute() rejected it before any HTTP call completed

    blocking = _final_blocking_findings(findings)
    assert len(blocking) == 1
    assert blocking[0].verification_status == "independently_verified"
    assert blocking[0].severity == "blocker"


# --- per-chapter budget / cap -------------------------------------------------


async def test_cells_beyond_the_budget_are_skipped_and_marked_unverified_not_verified():
    fake = FakeExecutor()
    cells = [_cell(code=f"print({i})") for i in range(5)]
    findings = await _verify_code_cells(
        cells,
        code_execution=_service(fake),
        settings=_settings(code_execution_max_cells_per_chapter=2),
    )
    skipped = [f for f in findings if f.check == "code_cell_execution_skipped"]
    assert len(skipped) == 1
    assert skipped[0].verification_status == "unverified"  # never silently "verified"
    assert skipped[0].severity == "major"  # visible, but does not block approval on its own
    assert skipped[0].check not in _CODE_CELL_REVISION_TRIGGERING_CHECKS
    assert len(fake.requests) == 2  # only the first 2 (the budget) were actually executed


# --- CodeCellContent.execution is never written ------------------------------


async def test_execution_verification_never_writes_block_content_execution():
    fake = FakeExecutor()
    cell = _cell()
    assert cell.content.get("execution") is None
    await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())
    assert cell.content.get("execution") is None  # still untouched - not the Run action


# --- revision loop: rewritten code must be re-executed, never cached --------


async def test_rewritten_code_is_re_executed_not_a_cached_prior_verdict():
    def route_on_code(body):
        if "bug" in body["code"]:
            return {
                "status": "error", "stdout": "", "stderr": "NameError", "execution_time": 0.1,
                "phase": "run", "exit_code": 1, "truncated": False,
            }
        return dict(_DEFAULT_RESULT)

    fake = FakeExecutor(result_for=route_on_code)
    service = _service(fake)

    first_pass = await _verify_code_cells(
        [_cell(code="bug_here()")], code_execution=service, settings=_settings()
    )
    assert len(first_pass) == 1
    assert first_pass[0].check == "code_cell_execution_failed"

    # The writer "fixed" the cell - a fresh review pass must re-execute the
    # NEW code, not reuse the earlier failing verdict.
    second_pass = await _verify_code_cells(
        [_cell(code="print(30)")], code_execution=service, settings=_settings()
    )
    assert second_pass == []
    assert len(fake.requests) == 2


# --- backend concurrency limiter ---------------------------------------------


async def test_code_execution_has_its_own_concurrency_phase():
    settings = _settings(code_execution_concurrency=3)
    assert settings.concurrency_for("code_execution") == 3


async def test_the_concurrency_limiter_actually_bounds_concurrent_executor_calls():
    in_flight = {"current": 0, "peak": 0}

    async def hook():
        in_flight["current"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["current"])
        await asyncio.sleep(0.05)
        in_flight["current"] -= 1

    fake = FakeExecutor(on_execute=hook)
    settings = _settings(code_execution_concurrency=1)
    service_a = _service(fake, code_execution_concurrency=1)
    service_b = _service(fake, code_execution_concurrency=1)

    await asyncio.gather(
        _verify_code_cells([_cell(code="a")], code_execution=service_a, settings=settings),
        _verify_code_cells([_cell(code="b")], code_execution=service_b, settings=settings),
    )
    assert in_flight["peak"] == 1, "concurrency=1 must never let both calls run at once"


# --- integration: findings survive persistence, reporting, approval gating --


async def test_findings_survive_chapter_round_trip_and_feed_the_approval_gate():
    def erroring(body):
        return {
            "status": "error", "stdout": "", "stderr": "ZeroDivisionError", "execution_time": 0.1,
            "phase": "run", "exit_code": 1, "truncated": False,
        }

    fake = FakeExecutor(result_for=erroring)
    structural_findings = await _verify_code_cells(
        [_cell(code="1/0")], code_execution=_service(fake), settings=_settings()
    )
    assert structural_findings

    chapter = GeneratedChapter(
        chapter_id="chapter_1",
        chapter_number=1,
        title="T",
        review={"approved": False},
        structural_findings=structural_findings,
    )
    # Round-trip through JSON exactly as storage does (model_dump -> model_validate).
    reloaded = GeneratedChapter.model_validate(chapter.model_dump(mode="json"))
    assert reloaded.structural_findings  # survived persistence/reload

    report = build_course_quality_report(course_id="c1", chapters=[reloaded], document=None)
    assert report.approval_blocking_findings  # a real, independently-verified blocker


async def test_executor_unavailable_findings_also_survive_round_trip_as_verification_failed():
    fake = FakeExecutor(status=503)
    structural_findings = await _verify_code_cells(
        [_cell()], code_execution=_service(fake), settings=_settings()
    )
    chapter = GeneratedChapter(
        chapter_id="chapter_1", chapter_number=1, title="T", structural_findings=structural_findings
    )
    reloaded = GeneratedChapter.model_validate(chapter.model_dump(mode="json"))
    report = build_course_quality_report(course_id="c1", chapters=[reloaded], document=None)
    blocking = report.approval_blocking_findings
    assert len(blocking) == 1
    assert blocking[0].verification_status == "verification_failed"
    assert blocking[0].reason != "incomplete_chapter_inventory"  # distinct failure category


# --- wiring into ReviewerAgent.review_chapter (missing_required_blocks) -----


def _review_chapter_agent(fake: FakeExecutor) -> ReviewerAgent:
    class _StructuredStub:
        is_mock = True

        async def structured(self, **kwargs):
            return ChapterReview(approved=True)

    agent = ReviewerAgent(
        ai=_StructuredStub(),
        settings=_settings(),
        code_execution=_service(fake),
    )
    return agent


async def test_a_compile_failure_feeds_missing_required_blocks_and_flips_approved():
    def erroring(body):
        return {
            "status": "error", "stdout": "", "stderr": "SyntaxError", "execution_time": 0.1,
            "phase": "run", "exit_code": 1, "truncated": False,
        }

    from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
    from app.schemas.course import CourseInput
    from app.agents.prompts import ContinuityContext
    from app.course.templates.registry import load_template

    fake = FakeExecutor(result_for=erroring)
    agent = _review_chapter_agent(fake)
    template = load_template("technical_v1")
    blueprint = CourseBlueprint(course_title="T", audience="eng", template_id="technical_v1")
    chapter = BlueprintChapter(id="chapter_1", title="T", order=1)
    course_input = CourseInput(
        course_title="T", toc=[{"title": "T"}], target_audience="eng", template="technical"
    )
    blocks = [_cell(code="broken(")]

    review, structural_findings = await agent.review_chapter(
        blueprint=blueprint,
        chapter=chapter,
        template=template,
        course_input=course_input,
        blocks=blocks,
        continuity=ContinuityContext(),
    )

    assert any(f.check == "code_cell_execution_failed" for f in structural_findings)
    assert any("SyntaxError" in m for m in review.missing_required_blocks)
    assert review.approved is False
    assert review.needs_revision() is True


# --- declared-expected-output correctness check (quality-governance -------
# follow-up, Phase 3.8B). `expected_stdout` is HUMAN-declared only - these
# tests set it directly on the fixture block, exactly as a manual document
# save (PUT /api/documents/{document_id}) would persist it; none of this
# goes through the writer prompt.


def _result(stdout: str, *, truncated: bool = False) -> dict:
    return {
        "status": "success", "stdout": stdout, "stderr": "", "execution_time": 0.1,
        "phase": "run", "exit_code": 0, "truncated": truncated,
    }


async def test_a_declared_expected_output_mismatch_produces_a_finding():
    """The exact scenario named in the brief: expected 4, program prints 5,
    execution succeeds - execution success alone must not count as
    correctness."""
    fake = FakeExecutor(result_for=lambda body: _result("5\n"))
    cell = _cell(expected_stdout="4")

    findings = await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.check == "code_cell_output_mismatch"
    assert finding.dimension == "accuracy"
    assert "'4'" in finding.description and "'5'" in finding.description

    blocking = _final_blocking_findings(findings)
    assert len(blocking) == 1
    assert blocking[0].verification_status == "independently_verified"
    assert blocking[0].severity == "blocker"


async def test_a_declared_expected_output_match_produces_no_finding():
    fake = FakeExecutor(result_for=lambda body: _result("4\n"))
    cell = _cell(expected_stdout="4")

    findings = await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())

    assert findings == []


async def test_missing_expected_output_is_unchanged_behaviour():
    """No `expected_stdout` declared (the default, and every pre-existing
    cell) - a successful run must still produce no finding, exactly as
    before this field existed."""
    fake = FakeExecutor(result_for=lambda body: _result("anything at all\n"))
    cell = _cell()  # no expected_stdout

    findings = await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())

    assert findings == []


async def test_truncated_output_never_claims_a_confident_match_or_mismatch():
    """A declared expectation that would otherwise mismatch must still
    produce NOTHING when the captured output was truncated - a partial
    capture is not a trustworthy basis for either verdict."""
    fake = FakeExecutor(result_for=lambda body: _result("5\n", truncated=True))
    cell = _cell(expected_stdout="4")

    findings = await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())

    assert findings == []


async def test_old_format_content_without_the_field_remains_compatible():
    """A genuinely pre-existing, already-persisted code_cell's raw content
    dict has no `expected_stdout` key at all (not even `None`) - unlike a
    freshly-constructed Block, whose own content normalization already
    fills in every CodeCellContent field's default (confirmed separately:
    `Block(...).content` always contains the key once this field exists).
    Mutating it out after construction is the only way to reproduce what a
    document.json written before this field existed actually looks like."""
    old_format_block = Block(
        type=BlockType.CODE_CELL, content={"language": "python", "code": "print(30)"}
    )
    del old_format_block.content["expected_stdout"]
    assert "expected_stdout" not in old_format_block.content

    fake = FakeExecutor(result_for=lambda body: _result("30\n"))
    findings = await _verify_code_cells(
        [old_format_block], code_execution=_service(fake), settings=_settings()
    )
    assert findings == []


async def test_a_mismatch_finding_still_never_writes_block_content_execution():
    """The learner-facing `execution` field (set only by the editor's Run
    action) must stay untouched even when the new correctness check fires -
    mirrors test_execution_verification_never_writes_block_content_execution
    above for this new code path specifically."""
    fake = FakeExecutor(result_for=lambda body: _result("5\n"))
    cell = _cell(expected_stdout="4")
    assert cell.content.get("execution") is None

    findings = await _verify_code_cells([cell], code_execution=_service(fake), settings=_settings())

    assert any(f.check == "code_cell_output_mismatch" for f in findings)
    assert cell.content.get("execution") is None  # still untouched
