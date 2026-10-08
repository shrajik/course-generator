"""Code cells: schema, staleness, the execution API, layout and the PDF.

The executor is a separate sandboxed service, so these tests substitute it
with an in-process fake (an httpx transport) and assert on the contract the
backend keeps with it. The executor's own isolation is tested where it runs,
in code-executor/tests.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import CodeExecutionUnavailableError, ValidationFailedError
from app.course.document.code_cell import MAX_OUTPUT_LINES, language_label, visible_output
from app.course.document.layout import estimate_height
from app.course.templates.registry import load_template
from app.render.html_renderer import render_document_html
from app.schemas.blocks import BlockType, execution_is_current, validate_content
from app.schemas.document import Block, BlockMeta, CourseDocument, DocumentMeta, Page, PageSize
from app.services.code_execution import CodeExecutionService

LANGUAGES = [
    {"id": "python", "label": "Python", "highlight": "python", "file_extension": "py",
     "version": "Python 3.12", "aliases": ["py", "python3"]},
    {"id": "cpp", "label": "C++", "highlight": "cpp", "file_extension": "cpp",
     "version": "g++ 12", "aliases": ["c++", "cxx"]},
    {"id": "javascript", "label": "JavaScript", "highlight": "javascript",
     "file_extension": "js", "version": "v22", "aliases": ["js", "node"]},
]


class FakeExecutor:
    """Stands in for the executor service and records what it was sent."""

    def __init__(self, *, result: dict | None = None, status: int = 200, fail: bool = False):
        self.requests: list[dict] = []
        self.result = result or {
            "status": "success", "language": "python", "stdout": "30\n", "stderr": "",
            "execution_time": 0.31, "phase": "run", "exit_code": 0, "truncated": False,
        }
        self.status = status
        self.fail = fail

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.fail:
                raise httpx.ConnectError("executor is down")
            if request.url.path == "/languages":
                return httpx.Response(200, json=LANGUAGES)
            body = json.loads(request.content)
            self.requests.append(body)
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": "nope"})
            return httpx.Response(200, json={**self.result, "language": body["language"]})

        return httpx.MockTransport(handle)


def service(fake: FakeExecutor, **overrides) -> CodeExecutionService:
    settings = Settings(code_executor_url="http://executor", **overrides)
    return CodeExecutionService(settings, transport=fake.transport())


# --- schema --------------------------------------------------------------------


def test_code_cell_is_a_distinct_block_type_from_static_code() -> None:
    assert BlockType.CODE_CELL.value == "code_cell"
    assert BlockType.CODE.value == "code"
    assert BlockType.CODE_CELL != BlockType.CODE


def test_language_is_generic_not_python() -> None:
    for language in ("python", "javascript", "cpp", "java", "rust"):
        content = validate_content(BlockType.CODE_CELL, {"language": language, "code": "x"})
        assert content["language"] == language


def test_a_new_cell_has_no_execution() -> None:
    assert validate_content(BlockType.CODE_CELL, {"code": "x"})["execution"] is None


def test_execution_round_trips_through_validation() -> None:
    content = validate_content(
        BlockType.CODE_CELL,
        {
            "language": "python",
            "code": "print(10 + 20)",
            "execution": {"status": "success", "stdout": "30\n", "stderr": "",
                          "execution_time": 0.3, "language": "python", "source": "print(10 + 20)"},
        },
    )
    assert content["execution"]["stdout"] == "30\n"
    assert content["execution"]["status"] == "success"


# --- stale output (acceptance test 8) ------------------------------------------


def cell(code: str = "print(1)", language: str = "python", **execution) -> dict:
    content = {"language": language, "code": code, "caption": ""}
    if execution:
        content["execution"] = {
            "status": "success", "stdout": "1\n", "stderr": "", "language": "python",
            "source": "print(1)", **execution,
        }
    return content


def test_output_is_current_while_the_code_is_unchanged() -> None:
    assert execution_is_current(cell()) is False  # never run
    assert execution_is_current(cell(status="success")) is True


def test_editing_the_code_makes_the_output_stale() -> None:
    assert execution_is_current(cell(code="print(2)", status="success")) is False


def test_changing_the_language_makes_the_output_stale() -> None:
    assert execution_is_current(cell(language="javascript", status="success")) is False


def test_whitespace_changes_count_as_a_change() -> None:
    assert execution_is_current(cell(code="print(1) ", status="success")) is False


def test_stale_output_is_never_visible() -> None:
    assert visible_output(cell(code="print(2)", status="success")) is None
    assert visible_output(cell()) is None
    assert visible_output(cell(status="success")) is not None


# --- the execution API ---------------------------------------------------------


async def test_execute_returns_the_normalised_result() -> None:
    fake = FakeExecutor()
    result = await service(fake).execute("python", "print(10 + 20)")
    assert (result.status, result.stdout, result.stderr) == ("success", "30\n", "")
    assert result.language == "python"
    assert result.execution_time == pytest.approx(0.31)


async def test_the_backend_chooses_the_limits_not_the_caller() -> None:
    fake = FakeExecutor()
    await service(fake, code_execution_timeout_seconds=7, code_execution_memory_mb=128).execute(
        "python", "print(1)"
    )
    sent = fake.requests[0]
    assert sent["timeout"] == 7
    assert sent["memory_limit_mb"] == 128


async def test_network_access_is_never_requested() -> None:
    fake = FakeExecutor()
    await service(fake).execute("python", "print(1)")
    assert fake.requests[0]["network_enabled"] is False


@pytest.mark.parametrize("alias", ["C++", "c++", "cxx", "cpp"])
async def test_aliases_resolve_before_reaching_the_executor(alias: str) -> None:
    fake = FakeExecutor()
    await service(fake).execute(alias, "int main(){}")
    assert fake.requests[0]["language"] == "cpp"


async def test_unsupported_language_is_rejected_without_calling_the_executor() -> None:
    fake = FakeExecutor()
    with pytest.raises(ValidationFailedError, match="not supported"):
        await service(fake).execute("cobol", "DISPLAY 'hi'.")
    assert fake.requests == []


async def test_empty_code_is_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        await service(FakeExecutor()).execute("python", "   \n")


async def test_oversized_code_is_rejected() -> None:
    fake = FakeExecutor()
    with pytest.raises(ValidationFailedError, match="too large"):
        await service(fake, code_execution_max_bytes=1000).execute("python", "#" * 1001)
    assert fake.requests == []


async def test_an_unreachable_executor_is_a_clear_unavailable_error() -> None:
    with pytest.raises(CodeExecutionUnavailableError):
        await service(FakeExecutor(fail=True)).execute("python", "print(1)")


async def test_a_busy_executor_asks_the_user_to_retry() -> None:
    with pytest.raises(CodeExecutionUnavailableError, match="busy"):
        await service(FakeExecutor(status=429)).execute("python", "print(1)")


async def test_language_list_survives_an_unreachable_executor() -> None:
    languages, reachable = await service(FakeExecutor(fail=True)).languages()
    assert languages == []
    assert reachable is False


async def test_only_executor_languages_are_listed() -> None:
    languages, reachable = await service(FakeExecutor()).languages()
    assert reachable is True
    assert {item.id for item in languages} == {"python", "cpp", "javascript"}


def test_http_endpoints(client, monkeypatch) -> None:
    from app.services import code_execution

    fake = FakeExecutor()
    monkeypatch.setattr(code_execution, "_service", service(fake))

    languages = client.get("/api/code/languages").json()
    assert languages["available"] is True
    assert [item["id"] for item in languages["languages"]] == ["python", "cpp", "javascript"]

    response = client.post("/api/code/execute", json={"language": "python", "code": "print(10 + 20)"})
    assert response.status_code == 200, response.text
    assert response.json()["stdout"] == "30\n"


def test_http_rejects_client_supplied_limits(client, monkeypatch) -> None:
    from app.services import code_execution

    monkeypatch.setattr(code_execution, "_service", service(FakeExecutor()))
    response = client.post(
        "/api/code/execute",
        json={"language": "python", "code": "print(1)", "timeout": 999, "network_enabled": True},
    )
    assert response.status_code == 422


def test_http_reports_an_unavailable_executor_as_503(client, monkeypatch) -> None:
    from app.services import code_execution

    monkeypatch.setattr(code_execution, "_service", service(FakeExecutor(fail=True)))
    response = client.post("/api/code/execute", json={"language": "python", "code": "print(1)"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "code_execution_unavailable"


# --- layout --------------------------------------------------------------------


def make_block(content: dict, block_id: str = "block_cell0001") -> Block:
    return Block(id=block_id, type=BlockType.CODE_CELL, content=content, meta=BlockMeta())


def test_a_run_cell_reserves_room_for_its_output() -> None:
    plain = estimate_height(make_block(cell()))
    ran = estimate_height(make_block(cell(status="success")))
    assert ran > plain


def test_a_stale_cell_reserves_no_room_for_output() -> None:
    plain = estimate_height(make_block(cell(code="print(2)")))
    stale = estimate_height(make_block(cell(code="print(2)", status="success")))
    assert stale == plain


def test_longer_output_reserves_more_room() -> None:
    short = estimate_height(make_block(cell(status="success", stdout="1\n")))
    long = estimate_height(make_block(cell(status="success", stdout="line\n" * 20)))
    assert long > short


def test_output_height_is_capped_for_print() -> None:
    capped = estimate_height(make_block(cell(status="success", stdout="x\n" * MAX_OUTPUT_LINES)))
    huge = estimate_height(make_block(cell(status="success", stdout="x\n" * 5000)))
    assert huge < capped * 1.2


# --- PDF (acceptance test 10) ----------------------------------------------------


def render(blocks: list[Block]) -> str:
    document = CourseDocument(
        document_id="doc_cells", course_id="crs_cells", course_title="Cells",
        template_id="technical_v1", meta=DocumentMeta(),
        pages=[Page(id="page_1", page_number=1, kind="content", size=PageSize(), blocks=blocks)],
    )
    return render_document_html(document, load_template("technical_v1"))


@pytest.mark.parametrize(
    ("language", "label"),
    [("python", "Python"), ("javascript", "JavaScript"), ("cpp", "C++"), ("java", "Java")],
)
def test_pdf_labels_each_language_without_assuming_python(language: str, label: str) -> None:
    html = render([make_block(cell(language=language, source="print(1)", status="success"))])
    assert f"{label} Code" in html
    assert "Python Code" not in html or language == "python"


def test_pdf_shows_code_then_the_recorded_output() -> None:
    html = render([make_block(cell(code="print(10 + 20)", status="success", stdout="30\n",
                                   source="print(10 + 20)"))])
    # Anchored on the markup: the bare words "Output" and "30" also occur in
    # the stylesheet, which would make an index comparison meaningless.
    body = html[html.index("<body"):]
    assert body.index("print(10 + 20)") < body.index("cell-out-head") < body.index(
        '<pre class="cell-stdout">30'
    )


def test_pdf_says_so_for_a_cell_that_was_never_run() -> None:
    """Never fabricate output - and never leave the reader guessing either."""
    html = render([make_block(cell())])
    assert "Python Code" in html
    assert "OUTPUT · NOT EXECUTED" in html
    assert 'class="cell-stdout"' not in html


def test_pdf_never_prints_stale_output() -> None:
    stale = cell(code="print(2)", status="success", stdout="OLD-RESULT\n")
    html = render([make_block(stale)])
    assert "OLD-RESULT" not in html
    # Withheld, and said to be: not silently blank.
    assert "OUTPUT · STALE" in html
    assert 'class="cell-stdout"' not in html


def test_pdf_shows_errors_with_their_status() -> None:
    html = render([make_block(cell(status="error", stdout="", stderr="Traceback: boom"))])
    assert "Traceback: boom" in html
    assert "cell-out-error" in html


def test_pdf_never_executes_anything() -> None:
    """Rendering reads the recorded result; the executor is never involved."""
    fake = FakeExecutor()
    render([make_block(cell(status="success"))])
    assert fake.requests == []


def test_multiple_cells_keep_independent_results() -> None:
    first = cell(code="print(1)", status="success", stdout="ONE\n", source="print(1)")
    second = cell(code="print(2)", status="success", stdout="TWO\n", source="print(2)")
    html = render([make_block(first, "block_cell0001"), make_block(second, "block_cell0002")])
    assert "ONE" in html and "TWO" in html


def test_language_labels_have_a_safe_fallback() -> None:
    assert language_label("cpp") == "C++"
    assert language_label("zig") == "zig"
    assert language_label("") == "Code"


# --- generation ----------------------------------------------------------------


def test_the_technical_template_allows_code_cells_and_the_other_does_not() -> None:
    assert BlockType.CODE_CELL in load_template("technical_v1").allowed_block_types
    assert BlockType.CODE_CELL not in load_template("non_technical_v1").allowed_block_types


def test_static_code_remains_allowed() -> None:
    assert BlockType.CODE in load_template("technical_v1").allowed_block_types


def test_a_drafted_cell_never_carries_an_execution() -> None:
    from app.course.blocks.normalizer import normalize_draft_blocks
    from app.schemas.draft import DraftBlock

    draft = DraftBlock(type=BlockType.CODE_CELL, language="cpp", code="int main(){}", caption="A cell")
    (block,) = normalize_draft_blocks(
        [draft], template=load_template("technical_v1"), chapter_id="chapter_1", chapter_number=1
    )
    assert block.type is BlockType.CODE_CELL
    assert block.content["language"] == "cpp"
    assert block.content["code"] == "int main(){}"
    assert block.content["execution"] is None


def test_non_technical_courses_downgrade_a_cell_instead_of_failing() -> None:
    from app.course.blocks.normalizer import normalize_draft_blocks
    from app.schemas.draft import DraftBlock

    draft = DraftBlock(type=BlockType.CODE_CELL, language="python", code="print(1)", text="print(1)")
    (block,) = normalize_draft_blocks(
        [draft], template=load_template("non_technical_v1"), chapter_id="chapter_1", chapter_number=1
    )
    assert block.type is BlockType.PARAGRAPH
