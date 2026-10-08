"""HTTP surface of the executor.

Three endpoints, none of which know anything about any particular language:

    GET  /health      200 only when network isolation is verified active
    GET  /languages   the runtimes actually installed, with metadata
    POST /execute     run code in a language, get a normalised result

The service fails closed: if the firewall rule that silences executed code
cannot be installed, `/execute` refuses to run anything.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import config, firewall
from .models import ExecutionRequest, ExecutionResult, LanguageInfo
from .runners import NetworkNotSupported, registry

log = logging.getLogger("executor")
logging.basicConfig(level=logging.INFO)


class _State:
    isolated: bool = False
    detail: str = "not initialised"
    slots: asyncio.Semaphore | None = None
    languages: list[LanguageInfo] = []


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.isolated, state.detail = firewall.install()
    if not state.isolated:
        log.error("NETWORK ISOLATION UNAVAILABLE - refusing to execute code: %s", state.detail)
    state.slots = asyncio.Semaphore(config.MAX_CONCURRENT)
    # Resolved once: version probes spawn processes, so they must not run per
    # request.
    state.languages = [runner.info() for runner in registry.available()]
    log.info("Languages available: %s", [language.id for language in state.languages])
    yield


app = FastAPI(title="Code Executor", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    body = {
        "status": "ok" if state.isolated else "unavailable",
        "network_isolated": state.isolated,
        "detail": state.detail,
        "languages": [language.id for language in state.languages],
    }
    return JSONResponse(body, status_code=200 if state.isolated else 503)


@app.get("/languages", response_model=list[LanguageInfo])
async def languages() -> list[LanguageInfo]:
    return state.languages


@app.post("/execute", response_model=ExecutionResult)
async def execute(request: ExecutionRequest) -> ExecutionResult:
    if not state.isolated:
        raise HTTPException(503, f"Sandbox network isolation is not active: {state.detail}")

    if request.network_enabled:
        # Network access is a deployment capability, never a request one.
        raise HTTPException(400, "Network access is not available in this deployment.")

    if len(request.code.encode("utf-8")) > config.MAX_CODE_BYTES:
        raise HTTPException(413, f"Code exceeds the {config.MAX_CODE_BYTES}-byte limit.")

    runner = registry.get(request.language)
    if runner is None:
        raise HTTPException(400, f"Unsupported language: {request.language!r}")

    timeout = min(request.timeout or config.DEFAULT_TIMEOUT_SECONDS, config.MAX_TIMEOUT_SECONDS)
    memory = min(request.memory_limit_mb or config.DEFAULT_MEMORY_MB, config.MAX_MEMORY_MB)

    assert state.slots is not None
    try:
        # Bounded wait: when every slot is busy, turn the caller away rather
        # than queueing requests (and their code) without limit.
        await asyncio.wait_for(state.slots.acquire(), timeout=config.QUEUE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(429, "The code runner is busy. Try again in a moment.") from None

    try:
        return await runner.execute(request.code, timeout=timeout, memory_limit=memory)
    except NetworkNotSupported as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        state.slots.release()
