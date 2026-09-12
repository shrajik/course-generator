"""OpenAI access layer.

Every AI call in the application goes through `AIClient`. There are two
implementations:

* `OpenAIClient` - the real thing (structured output, web/deep research, images).
* `MockAIClient` - deterministic offline responses (`app/services/mock_ai.py`).

The rest of the code never imports `openai` directly, which is what makes the
pipeline testable without an API key and swappable later.

Performance-relevant behaviour lives here: every call is timed into the run
metrics, retries honour `Retry-After`, a 429 shrinks the calling phase's
concurrency limiter, and an unavailable model falls back once to
`settings.fallback_model` so a fast default cannot break a deployment.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import random
import re
import time
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from app.core.concurrency import note_rate_limit
from app.core.config import Settings, get_settings
from app.core.errors import AIServiceError
from app.core.logging import get_logger
from app.core.metrics import CallRecord, current_metrics

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class StreamSink(Protocol):
    """Receives real model output as it streams in, for `structured(on_delta=...)`.

    `reset()` is called immediately before every attempt (including retries),
    so a failed attempt's partial text is discarded rather than bleeding into
    the next one. When not given, `structured()` behaves exactly as before -
    one blocking call, no streaming.
    """

    def reset(self) -> None: ...
    def append(self, delta: str) -> None: ...

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_MISSING_MODEL_MARKERS = ("model_not_found", "does not exist", "do not have access")


class ResearchResult(BaseModel):
    text: str = ""
    citations: list[dict[str, str]] = []
    model: str = ""
    mode: str = "fast"
    # Set when the search call was able to return the structured payload itself,
    # which saves the second shaping call.
    structured: dict[str, Any] | None = None


class AIClient(abc.ABC):
    """Interface used by every agent."""

    is_mock: bool = False

    @abc.abstractmethod
    async def structured(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        model: str | None = None,
        purpose: str = "",
        phase: str = "default",
        max_output_tokens: int | None = None,
        on_delta: StreamSink | None = None,
    ) -> T:
        """Return an instance of `schema` produced by the model.

        When `on_delta` is given, the *initial* attempt streams real model
        output to it as it arrives (a retry or the validation-repair pass
        still happen as plain blocking calls - see the real implementation).
        """

    @abc.abstractmethod
    async def research(
        self,
        *,
        prompt: str,
        deep: bool = False,
        system: str = "",
        schema: type[BaseModel] | None = None,
        phase: str = "research",
    ) -> ResearchResult:
        """Gather grounded notes, using web search where the model supports it.

        When `schema` is given the client tries to have the same call return the
        structured payload, saving a second round trip.
        """

    @abc.abstractmethod
    async def image(
        self, *, prompt: str, size: str | None = None, phase: str = "image"
    ) -> bytes:
        """Return PNG bytes."""

    @abc.abstractmethod
    async def embed(
        self, *, texts: list[str], model: str | None = None, phase: str = "embedding"
    ) -> list[list[float]]:
        """Return one embedding vector per input text, same order as `texts`.

        Callers (EmbeddingService) are responsible for batching and for
        treating any failure here as soft - this method itself may raise.
        """


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def extract_json_object(text: str) -> dict[str, Any]:
    """Tolerant JSON extraction - models occasionally wrap or prefix output."""
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise AIServiceError(
        "Model did not return valid JSON", details={"preview": cleaned[:400]}
    )


def schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False)


def _status_of(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for key in ("retry-after", "Retry-After"):
        value = headers.get(key) if hasattr(headers, "get") else None
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# real client
# ---------------------------------------------------------------------------


class OpenAIClient(AIClient):
    is_mock = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = None
        self._unavailable_models: set[str] = set()

    # --- plumbing ---------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover
                raise AIServiceError("The `openai` package is not installed") from exc
            if not self.settings.openai_api_key:
                raise AIServiceError("OPENAI_API_KEY is not configured")
            kwargs: dict[str, Any] = {
                "api_key": self.settings.openai_api_key,
                "timeout": self.settings.request_timeout,
                "max_retries": 0,  # we handle retries ourselves
            }
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def _resolve_model(self, model: str) -> str:
        """Swap a model we already know is unavailable for the fallback."""
        if model in self._unavailable_models and self.settings.fallback_model:
            return self.settings.fallback_model
        return model

    @staticmethod
    def _is_missing_model(exc: Exception) -> bool:
        if _status_of(exc) not in (400, 403, 404):
            return False
        message = str(exc).lower()
        return any(marker in message for marker in _MISSING_MODEL_MARKERS)

    async def _with_retries(self, label: str, phase: str, factory):
        """Returns (result, retries). Raises AIServiceError when out of attempts."""
        attempts = self.settings.max_call_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await factory(), attempt - 1
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                status = _status_of(exc)
                if status == 429:
                    await note_rate_limit(phase)
                if not self._is_retryable(exc) or attempt == attempts:
                    break
                delay = _retry_after(exc) or min(2**attempt + random.random(), 30)
                log.warning(
                    "%s failed (attempt %s/%s): %s - retrying in %.1fs",
                    label,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise AIServiceError(f"{label} failed: {last_error}") from last_error

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status = _status_of(exc)
        if status is not None and (status == 429 or status >= 500):
            return True
        name = type(exc).__name__
        return name in {
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "InternalServerError",
            "APIConnectionTimeoutError",
        }

    def _record(
        self,
        *,
        kind: str,
        purpose: str,
        model: str,
        started: float,
        retries: int,
        usage: Any = None,
        failed: bool = False,
    ) -> None:
        metrics = current_metrics()
        if metrics is None:
            return
        input_tokens = output_tokens = 0
        if usage is not None:
            input_tokens = (
                getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
            )
            output_tokens = (
                getattr(usage, "completion_tokens", None)
                or getattr(usage, "output_tokens", 0)
                or 0
            )
        metrics.record_call(
            CallRecord(
                kind=kind,
                purpose=purpose or kind,
                model=model,
                seconds=time.perf_counter() - started,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                retries=retries,
                failed=failed,
            )
        )

    # --- structured output -------------------------------------------------
    async def structured(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        model: str | None = None,
        purpose: str = "",
        phase: str = "default",
        max_output_tokens: int | None = None,
        on_delta: StreamSink | None = None,
    ) -> T:
        requested = model or self.settings.writer_model
        model_name = self._resolve_model(requested)
        label = f"structured[{purpose or schema.__name__}]"
        json_schema = schema.model_json_schema()
        started = time.perf_counter()
        metrics = current_metrics()
        if metrics:
            metrics.call_started()
        usage: Any = None
        retries = 0

        async def call(
            response_format: dict[str, Any],
            system_prompt: str,
            name: str,
            stream_sink: StreamSink | None = None,
        ) -> str:
            nonlocal usage
            kwargs: dict[str, Any] = {
                "model": name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user},
                ],
                "response_format": response_format,
            }
            if max_output_tokens:
                kwargs["max_completion_tokens"] = max_output_tokens

            if stream_sink is None:
                completion = await self.client.chat.completions.create(**kwargs)
                usage = getattr(completion, "usage", None)
                return completion.choices[0].message.content or ""

            # Real token streaming: reset right before this attempt starts so a
            # retry never mixes a failed attempt's text into the next one.
            stream_sink.reset()
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            parts: list[str] = []
            stream = await self.client.chat.completions.create(**kwargs)
            async for event in stream:
                if getattr(event, "usage", None) is not None:
                    usage = event.usage
                if not event.choices:
                    continue
                delta = event.choices[0].delta.content
                if delta:
                    parts.append(delta)
                    stream_sink.append(delta)
            return "".join(parts)

        schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__.lower(),
                "schema": json_schema,
                "strict": False,
            },
        }
        json_format = {"type": "json_object"}
        system_with_schema = (
            f"{system}\n\nReturn a single JSON object that conforms to this JSON Schema:\n"
            f"{json.dumps(json_schema, ensure_ascii=False)}"
        )

        failed = True
        try:
            try:
                raw, retries = await self._with_retries(
                    label, phase, lambda: call(schema_format, system, model_name, on_delta)
                )
            except AIServiceError as exc:
                if self._is_missing_model_error(exc) and model_name != self.settings.fallback_model:
                    log.warning(
                        "%s: model '%s' unavailable - falling back to '%s'",
                        label,
                        model_name,
                        self.settings.fallback_model,
                    )
                    self._unavailable_models.add(requested)
                    model_name = self.settings.fallback_model
                    raw, retries = await self._with_retries(
                        label, phase, lambda: call(schema_format, system, model_name, on_delta)
                    )
                else:
                    log.warning(
                        "%s: json_schema mode unavailable (%s) - falling back", label, exc
                    )
                    raw, retries = await self._with_retries(
                        label,
                        phase,
                        lambda: call(json_format, system_with_schema, model_name, on_delta),
                    )

            try:
                result = schema.model_validate(extract_json_object(raw))
            except (ValidationError, AIServiceError) as exc:
                log.warning("%s: invalid payload, one repair pass: %s", label, exc)
                repair_system = (
                    f"{system_with_schema}\n\nYour previous answer was rejected with these "
                    f"validation errors:\n{exc}\nReturn corrected JSON only."
                )
                repaired, extra = await self._with_retries(
                    label + ":repair", phase, lambda: call(json_format, repair_system, model_name)
                )
                retries += extra + 1
                try:
                    result = schema.model_validate(extract_json_object(repaired))
                except (ValidationError, AIServiceError) as final_exc:
                    raise AIServiceError(
                        f"{label}: model output failed validation twice: {final_exc}"
                    ) from final_exc
            failed = False
            return result
        finally:
            self._record(
                kind="structured",
                purpose=purpose or schema.__name__,
                model=model_name,
                started=started,
                retries=retries,
                usage=usage,
                failed=failed,
            )
            if metrics:
                metrics.call_finished()

    @staticmethod
    def _is_missing_model_error(exc: AIServiceError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _MISSING_MODEL_MARKERS)

    # --- research ----------------------------------------------------------
    async def research(
        self,
        *,
        prompt: str,
        deep: bool = False,
        system: str = "",
        schema: type[BaseModel] | None = None,
        phase: str = "research",
    ) -> ResearchResult:
        requested = self.settings.deep_research_model if deep else self.settings.research_model
        model_name = self._resolve_model(requested)
        tool = {"type": "web_search_preview"} if deep else {"type": "web_search"}
        instructions = system or "You are a meticulous research assistant."
        label = f"research[{'deep' if deep else 'fast'}]"
        started = time.perf_counter()
        metrics = current_metrics()
        if metrics:
            metrics.call_started()

        want_structured = schema is not None and self.settings.single_call_research

        async def call(tools: list[dict[str, Any]] | None, structured_output: bool):
            kwargs: dict[str, Any] = {
                "model": model_name,
                "input": prompt,
                "instructions": instructions,
            }
            if tools:
                kwargs["tools"] = tools
            if structured_output and schema is not None:
                kwargs["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": schema.__name__.lower(),
                        "schema": schema.model_json_schema(),
                        "strict": False,
                    }
                }
            return await self.client.responses.create(**kwargs)

        retries = 0
        response = None
        payload: dict[str, Any] | None = None

        try:
            if want_structured:
                # One call that searches AND returns the structured artifact.
                try:
                    response, retries = await self._with_retries(
                        label + ":structured", phase, lambda: call([tool], True)
                    )
                    payload = extract_json_object(self._response_text(response))
                except Exception as exc:  # noqa: BLE001 - any failure falls back
                    log.info(
                        "%s: combined search+structured unavailable (%s) - using two calls",
                        label,
                        exc,
                    )
                    response = None
                    payload = None

            if response is None:
                try:
                    response, retries = await self._with_retries(
                        label, phase, lambda: call([tool], False)
                    )
                except AIServiceError as exc:
                    if (
                        self._is_missing_model_error(exc)
                        and model_name != self.settings.fallback_model
                    ):
                        self._unavailable_models.add(requested)
                        model_name = self.settings.fallback_model
                        response, retries = await self._with_retries(
                            label, phase, lambda: call([tool], False)
                        )
                    else:
                        log.warning(
                            "%s: web search unavailable (%s) - retrying ungrounded", label, exc
                        )
                        response, retries = await self._with_retries(
                            label + ":no-tools", phase, lambda: call(None, False)
                        )

            return ResearchResult(
                text=self._response_text(response),
                citations=self._citations(response),
                model=model_name,
                mode="deep" if deep else "fast",
                structured=payload,
            )
        finally:
            self._record(
                kind="research",
                purpose=label,
                model=model_name,
                started=started,
                retries=retries,
                usage=getattr(response, "usage", None),
            )
            if metrics:
                metrics.call_finished()

    @staticmethod
    def _response_text(response: Any) -> str:
        text = getattr(response, "output_text", None)
        if text:
            return text
        chunks: list[str] = []
        for item in getattr(response, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                value = getattr(part, "text", None)
                if isinstance(value, str):
                    chunks.append(value)
        return "\n".join(chunks)

    @staticmethod
    def _citations(response: Any) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in getattr(response, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                for annotation in getattr(part, "annotations", []) or []:
                    url = getattr(annotation, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        found.append(
                            {"title": getattr(annotation, "title", "") or url, "url": url}
                        )
        return found

    # --- images ------------------------------------------------------------
    async def image(
        self, *, prompt: str, size: str | None = None, phase: str = "image"
    ) -> bytes:
        started = time.perf_counter()
        metrics = current_metrics()
        if metrics:
            metrics.call_started()

        async def call():
            return await self.client.images.generate(
                model=self.settings.image_model,
                prompt=prompt,
                size=size or self.settings.image_size,
                n=1,
            )

        retries = 0
        try:
            response, retries = await self._with_retries("image", phase, call)
            item = response.data[0]
            b64 = getattr(item, "b64_json", None)
            if b64:
                return base64.b64decode(b64)
            url = getattr(item, "url", None)
            if url:
                import httpx

                async with httpx.AsyncClient(timeout=120) as http:
                    fetched = await http.get(url)
                    fetched.raise_for_status()
                    return fetched.content
            raise AIServiceError("Image response contained neither b64_json nor url")
        finally:
            self._record(
                kind="image",
                purpose="image",
                model=self.settings.image_model,
                started=started,
                retries=retries,
            )
            if metrics:
                metrics.call_finished()

    # --- embeddings ----------------------------------------------------------
    async def embed(
        self, *, texts: list[str], model: str | None = None, phase: str = "embedding"
    ) -> list[list[float]]:
        if not texts:
            return []
        model_name = model or self.settings.embedding_model
        started = time.perf_counter()
        metrics = current_metrics()
        if metrics:
            metrics.call_started()

        usage: Any = None

        async def call():
            nonlocal usage
            response = await self.client.embeddings.create(model=model_name, input=texts)
            usage = getattr(response, "usage", None)
            return response

        retries = 0
        failed = True
        try:
            response, retries = await self._with_retries("embed", phase, call)
            # The API guarantees `data` is returned in the same order as `input`.
            vectors = [item.embedding for item in response.data]
            failed = False
            return vectors
        finally:
            self._record(
                kind="embedding",
                purpose="embedding",
                model=model_name,
                started=started,
                retries=retries,
                usage=usage,
                failed=failed,
            )
            if metrics:
                metrics.call_finished()


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

_client: AIClient | None = None


def get_ai_client() -> AIClient:
    global _client
    if _client is None:
        settings = get_settings()
        if settings.use_mock_ai:
            from app.services.mock_ai import MockAIClient

            reason = "MOCK_OPENAI=true" if settings.mock_openai else "no OPENAI_API_KEY set"
            log.warning("Using the offline mock AI client (%s).", reason)
            _client = MockAIClient(settings)
        else:
            _client = OpenAIClient(settings)
    return _client


def set_ai_client(client: AIClient | None) -> None:
    """Dependency injection hook for tests."""
    global _client
    _client = client
