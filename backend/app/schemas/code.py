"""Wire shapes for the code execution API.

The result is the executor's normalised shape, passed through unchanged: the
editor never handles a language-specific result or error format.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CodeLanguage(BaseModel):
    """A runtime the executor actually has installed."""

    id: str
    label: str
    highlight: str = ""
    file_extension: str = ""
    version: str = ""
    aliases: list[str] = Field(default_factory=list)


class CodeLanguagesResponse(BaseModel):
    languages: list[CodeLanguage]
    # False when the executor could not be reached. The editor uses this to
    # say "code execution is unavailable" rather than "no languages exist".
    available: bool = True


class ExecuteRequest(BaseModel):
    """What a client may ask for: a language and some code. Nothing else.

    Limits and network access are deliberately absent. They are properties of
    the deployment, so a request - or the code inside it - has no field to
    raise them with.
    """

    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=1, max_length=40)
    code: str


class ExecuteResponse(BaseModel):
    status: Literal["success", "error", "timeout"]
    language: str
    stdout: str = ""
    stderr: str = ""
    execution_time: float = 0.0
    phase: Literal["compile", "run"] | None = None
    exit_code: int | None = None
    truncated: bool = False
