"""Client for the sandboxed code executor.

The backend never runs submitted code itself, and never imports a language
runtime: it validates a request, picks the limits, and forwards it to the
executor service, which owns every language and every sandbox. That keeps this
process - which holds the database credentials and the model API key - at
arm's length from untrusted code.
"""

from __future__ import annotations

import time

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import CodeExecutionUnavailableError, ValidationFailedError
from app.core.logging import get_logger
from app.schemas.code import CodeLanguage, ExecuteResponse

log = get_logger(__name__)

# The language list changes only when an image is rebuilt, so a short cache
# keeps the editor from hitting the executor on every cell it renders.
_LANGUAGES_TTL_SECONDS = 30.0


class CodeExecutionService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # `transport` exists so tests can stand in for the executor without a
        # network or a container.
        self._transport = transport
        self._languages: list[CodeLanguage] = []
        self._languages_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.code_executor_url,
            timeout=self.settings.code_executor_http_timeout_seconds,
            transport=self._transport,
        )

    # --- languages ---------------------------------------------------------

    async def languages(self, *, refresh: bool = False) -> tuple[list[CodeLanguage], bool]:
        """(languages, executor_reachable).

        Never raises: an unreachable executor is a state the editor shows, not
        an error that should break the page the cell lives on.
        """
        fresh = (time.monotonic() - self._languages_at) < _LANGUAGES_TTL_SECONDS
        if self._languages and fresh and not refresh:
            return self._languages, True

        try:
            async with self._client() as client:
                response = await client.get("/languages")
                response.raise_for_status()
            self._languages = [CodeLanguage.model_validate(item) for item in response.json()]
            self._languages_at = time.monotonic()
            return self._languages, True
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not list executor languages: %s", exc)
            self._languages = []
            return [], False

    async def resolve_language(
        self, language: str, *, refresh: bool = False
    ) -> CodeLanguage | None:
        """The executor's canonical language for `language`, aliases included.

        Raises CodeExecutionUnavailableError when the executor cannot be
        reached: "I could not ask" must never be reported as "that language is
        not supported", which would send a user off to debug the wrong thing.
        """
        wanted = " ".join((language or "").strip().lower().split())
        languages, reachable = await self.languages(refresh=refresh)
        if not reachable:
            raise CodeExecutionUnavailableError("The code runner is not available right now.")
        for item in languages:
            names = {item.id, item.label.lower(), *(a.lower() for a in item.aliases)}
            if wanted in names:
                return item
        return None

    # --- execution ----------------------------------------------------------

    async def execute(self, language: str, code: str) -> ExecuteResponse:
        if len(code.encode("utf-8")) > self.settings.code_execution_max_bytes:
            raise ValidationFailedError(
                f"Code is too large to run (limit {self.settings.code_execution_max_bytes} bytes)."
            )
        if not code.strip():
            raise ValidationFailedError("There is no code to run.")

        resolved = await self.resolve_language(language)
        if resolved is None:
            # Re-check once against a fresh list in case the executor was
            # restarted with a new toolchain since the cache was filled.
            resolved = await self.resolve_language(language, refresh=True)
        if resolved is None:
            raise ValidationFailedError(f"Running {language!r} code is not supported.")

        payload = {
            "language": resolved.id,
            "code": code,
            "timeout": self.settings.code_execution_timeout_seconds,
            "memory_limit_mb": self.settings.code_execution_memory_mb,
            # Always off here, and not a parameter: network access is not
            # something a caller of this method can ask for.
            "network_enabled": False,
        }
        try:
            async with self._client() as client:
                response = await client.post("/execute", json=payload)
        except httpx.HTTPError as exc:
            log.error("Code executor unreachable: %s", exc)
            raise CodeExecutionUnavailableError(
                "The code runner is not available right now."
            ) from exc

        if response.status_code == 429:
            raise CodeExecutionUnavailableError("The code runner is busy. Try again in a moment.")
        if response.status_code >= 500:
            raise CodeExecutionUnavailableError("The code runner is not available right now.")
        if response.status_code >= 400:
            raise ValidationFailedError(_detail(response))

        return ExecuteResponse.model_validate(response.json())


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail") or "The code could not be run.")
    except ValueError:
        return "The code could not be run."


_service: CodeExecutionService | None = None


def get_code_execution_service() -> CodeExecutionService:
    global _service
    if _service is None:
        _service = CodeExecutionService()
    return _service
