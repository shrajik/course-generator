"""Wire models. `ExecutionResult` is the single normalised shape every
language returns, so no client ever needs language-specific handling."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExecutionStatus = Literal["success", "error", "timeout"]


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=1, max_length=40)
    code: str
    # Caller-requested limits, clamped to the executor's own ceilings.
    timeout: int | None = Field(default=None, ge=1)
    memory_limit_mb: int | None = Field(default=None, ge=16)
    # Present so the interface can grow a network capability later, but it is
    # a deployment decision: code can never grant itself network access, and
    # this build refuses True outright (see main.py).
    network_enabled: bool = False


class ExecutionResult(BaseModel):
    status: ExecutionStatus
    language: str
    stdout: str = ""
    stderr: str = ""
    execution_time: float = 0.0
    # Which stage ended the run, so a client can tell "your code did not
    # compile" from "your code crashed" without parsing stderr.
    phase: Literal["compile", "run"] | None = None
    exit_code: int | None = None
    truncated: bool = False


class LanguageInfo(BaseModel):
    id: str
    label: str
    # Name a syntax highlighter would know the language by.
    highlight: str
    file_extension: str
    version: str = ""
    # Other names the same language goes by (c++, js, node ...), so a caller
    # that received "C++" from a model can still resolve it.
    aliases: list[str] = Field(default_factory=list)
