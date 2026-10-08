"""Language -> runner registry.

Adding a language:

1. write a `SandboxRunner` subclass describing its source file and steps,
2. add one line to `_ALL` below,
3. install its toolchain in the Dockerfile,
4. add its tests.

The editor, the API and the PDF renderer need no change: they only ever see a
language id and a normalised result.

A language is *offered* only when its toolchain is actually installed, so the
UI can never list a language that would then fail to run.
"""

from __future__ import annotations

from .base import CodeRunner, NetworkNotSupported
from .cpp import CppRunner
from .java import JavaRunner
from .javascript import JavaScriptRunner
from .python import PythonRunner

_ALL: tuple[CodeRunner, ...] = (
    PythonRunner(),
    JavaScriptRunner(),
    CppRunner(),
    JavaRunner(),
)


class RuntimeRegistry:
    def __init__(self, runners: tuple[CodeRunner, ...] = _ALL) -> None:
        self._runners = {runner.spec.id: runner for runner in runners}
        self._aliases: dict[str, str] = {}
        for runner in runners:
            self._aliases[runner.spec.id] = runner.spec.id
            for alias in runner.spec.aliases:
                self._aliases[_norm(alias)] = runner.spec.id

    def resolve(self, language: str) -> str | None:
        """Canonical id for `language` (accepting aliases), else None."""
        return self._aliases.get(_norm(language))

    def get(self, language: str) -> CodeRunner | None:
        """The runner for `language`, only if it is installed and usable."""
        language_id = self.resolve(language)
        runner = self._runners.get(language_id) if language_id else None
        return runner if runner is not None and runner.is_available() else None

    def available(self) -> list[CodeRunner]:
        return [runner for runner in self._runners.values() if runner.is_available()]


def _norm(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


registry = RuntimeRegistry()

__all__ = ["CodeRunner", "NetworkNotSupported", "RuntimeRegistry", "registry"]
