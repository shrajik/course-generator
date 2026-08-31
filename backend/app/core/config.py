"""Application settings. Everything is environment driven - no secrets in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ResearchMode = Literal["fast", "deep"]
WritingMode = Literal["parallel", "sequential"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # --- OpenAI -------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str | None = None

    # Authoring quality matters most for the writer; planning, reviewing and
    # note-shaping are judgement/formatting tasks that a faster tier handles
    # well. If a configured model is unavailable the client falls back to
    # `fallback_model` once, so an aggressive default cannot break a deployment.
    writer_model: str = "gpt-5"
    editor_model: str = "gpt-5"
    planner_model: str = "gpt-5-mini"
    reviewer_model: str = "gpt-5-mini"
    research_model: str = "gpt-5-mini"
    deep_research_model: str = "o4-mini-deep-research"
    fallback_model: str = "gpt-5"
    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"

    # --- pipeline behaviour -------------------------------------------------
    research_mode: ResearchMode = "fast"
    #  parallel  - chapters are written concurrently against the blueprint's
    #              planned summaries (fast; the default)
    #  sequential- each chapter waits for the previous chapter's real summary
    #              (slow; kept for A/B comparison of continuity quality)
    writing_mode: WritingMode = "parallel"
    #  One extra cheap call after a parallel run that checks for repetition and
    #  broken transitions across chapters.
    continuity_pass: bool = True
    #  Ask the search-enabled call for structured output directly instead of
    #  shaping the notes in a second call.
    single_call_research: bool = True
    #  On review failure, rewrite only the offending blocks rather than the
    #  whole chapter.
    surgical_revision: bool = True
    max_review_revisions: int = Field(default=1, ge=0, le=3)
    #  Trim the context handed to the writer / reviewer: input size is latency.
    research_context_chars: int = Field(default=6000, ge=1000, le=40000)
    reviewer_context_chars: int = Field(default=20000, ge=2000, le=120000)

    mock_openai: bool = False
    #  Test-only: makes the offline mock client wait, so concurrency behaviour
    #  can be measured without an API key. Never set this in production.
    mock_latency_ms: int = Field(default=0, ge=0, le=10000)
    enable_image_generation: bool = True

    # --- concurrency --------------------------------------------------------
    #  Per-phase limits; 0 means "use max_concurrency". Each phase gets its own
    #  adaptive limiter that halves itself when the provider returns 429.
    max_concurrency: int = Field(default=6, ge=1, le=32)
    research_concurrency: int = Field(default=0, ge=0, le=32)
    writer_concurrency: int = Field(default=0, ge=0, le=32)
    image_concurrency: int = Field(default=0, ge=0, le=32)
    request_timeout: float = 900.0
    max_call_retries: int = Field(default=4, ge=1, le=8)

    # --- Storage / misc -----------------------------------------------------
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    app_name: str = "AI Course Creation Platform - Backend POC"
    app_version: str = "0.2.0"

    # --- derived ------------------------------------------------------------
    @property
    def use_mock_ai(self) -> bool:
        """Mock client is used explicitly, or implicitly when no key is configured."""
        return self.mock_openai or not self.openai_api_key.strip()

    @property
    def courses_dir(self) -> Path:
        return self.data_dir / "courses"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    def concurrency_for(self, phase: str) -> int:
        override = {
            "research": self.research_concurrency,
            "writer": self.writer_concurrency,
            "image": self.image_concurrency,
        }.get(phase, 0)
        return override or self.max_concurrency


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests after mutating the environment."""
    get_settings.cache_clear()
