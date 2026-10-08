"""Code execution API.

    GET  /api/code/languages   runtimes the executor really has
    POST /api/code/execute     run `code` in `language`

Deliberately language-agnostic: there is no /python/execute. The editor sends a
language id and code; which runtime handles it, and how it is isolated, is the
executor's concern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user_if_db_enabled
from app.core.logging import get_logger
from app.db.models import User
from app.schemas.code import CodeLanguagesResponse, ExecuteRequest, ExecuteResponse
from app.services.code_execution import CodeExecutionService, get_code_execution_service

router = APIRouter(prefix="/api/code", tags=["code"])
log = get_logger(__name__)


def _service() -> CodeExecutionService:
    return get_code_execution_service()


@router.get("/languages", response_model=CodeLanguagesResponse)
async def list_languages(
    service: CodeExecutionService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> CodeLanguagesResponse:
    """Only languages that are installed and runnable are returned, so the
    editor can offer exactly these and never a language that would fail."""
    languages, available = await service.languages()
    return CodeLanguagesResponse(languages=languages, available=available)


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code(
    request: ExecuteRequest,
    service: CodeExecutionService = Depends(_service),
    current_user: User | None = Depends(get_current_user_if_db_enabled),
) -> ExecuteResponse:
    """Run the code once. Always authenticated when accounts exist: this is the
    one endpoint that spends compute on caller-supplied code."""
    log.info(
        "CODE_EXECUTE language=%s bytes=%s user=%s",
        request.language,
        len(request.code),
        getattr(current_user, "id", None),
    )
    return await service.execute(request.language, request.code)
