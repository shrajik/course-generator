"""The runner abstraction.

`CodeRunner` is the one interface the rest of the service knows. A language is
described by a `LanguageSpec` and by the ordered `Step`s needed to run it - one
step for an interpreted language, a compile step then a run step for a
compiled one. `SandboxRunner` turns those steps into a normalised
`ExecutionResult`, so adding a language means describing its steps and
registering it; nothing else changes.

Runners do not decide *how* isolation works. Every step goes through
`sandbox.run_process`, which is the only place a child process is started.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from .. import config
from ..models import ExecutionResult, LanguageInfo
from ..sandbox import ProcessOutcome, minimal_env, run_process, workspace

Phase = Literal["compile", "run"]


class NetworkNotSupported(Exception):
    """Raised when a caller asks for network access this build cannot grant."""


@dataclass(frozen=True)
class LanguageSpec:
    id: str
    label: str
    highlight: str
    file_extension: str
    # Binaries that must exist for the language to count as configured. A
    # language whose toolchain is missing is simply not offered.
    required_binaries: tuple[str, ...]
    version_argv: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass
class Step:
    argv: list[str]
    phase: Phase
    # Runtimes that reserve huge virtual ranges (JVM, V8) abort under
    # RLIMIT_AS; they are bounded by a heap flag and the container cgroup.
    limit_address_space: bool = True
    env_extra: dict[str, str] = field(default_factory=dict)


class CodeRunner(ABC):
    """Run source code in one language and return a normalised result."""

    spec: LanguageSpec

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def info(self) -> LanguageInfo: ...

    @abstractmethod
    async def execute(
        self,
        code: str,
        *,
        timeout: int,
        memory_limit: int,
        network_enabled: bool = False,
    ) -> ExecutionResult: ...


class SandboxRunner(CodeRunner):
    """Shared implementation: write the source, run each step, classify."""

    def is_available(self) -> bool:
        return all(shutil.which(name) for name in self.spec.required_binaries)

    def info(self) -> LanguageInfo:
        return LanguageInfo(
            id=self.spec.id,
            label=self.spec.label,
            highlight=self.spec.highlight,
            file_extension=self.spec.file_extension,
            version=self._version(),
            aliases=list(self.spec.aliases),
        )

    def _version(self) -> str:
        if not self.spec.version_argv:
            return ""
        try:
            done = subprocess.run(
                list(self.spec.version_argv),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        # Some tools (java, javac) report their version on stderr.
        text = (done.stdout or done.stderr).strip()
        return text.splitlines()[0] if text else ""

    # --- what a language must describe --------------------------------

    @abstractmethod
    def prepare(self, workdir: str, code: str) -> dict[str, str]:
        """Write the source into `workdir`; return names later steps need."""

    @abstractmethod
    def steps(self, workdir: str, context: dict[str, str], memory_limit: int) -> list[Step]:
        """The ordered commands that compile and/or run the program."""

    # --- the shared pipeline ------------------------------------------

    async def execute(
        self,
        code: str,
        *,
        timeout: int,
        memory_limit: int,
        network_enabled: bool = False,
    ) -> ExecutionResult:
        if network_enabled:
            raise NetworkNotSupported("Network access is not available in this deployment.")

        with workspace() as workdir:
            context = self.prepare(workdir, code)
            steps = self.steps(workdir, context, memory_limit)
            elapsed = 0.0

            for index, step in enumerate(steps):
                is_last = index == len(steps) - 1
                budget = config.COMPILE_TIMEOUT_SECONDS if step.phase == "compile" else timeout
                outcome = await run_process(
                    step.argv,
                    cwd=workdir,
                    timeout=budget,
                    memory_limit_mb=memory_limit,
                    env=minimal_env(workdir, step.env_extra),
                    limit_address_space=step.limit_address_space,
                )
                elapsed += outcome.duration
                failure = self._failure(step, outcome, budget, elapsed)
                if failure is not None:
                    return failure
                if is_last:
                    return ExecutionResult(
                        status="success",
                        language=self.spec.id,
                        stdout=outcome.stdout,
                        stderr=outcome.stderr,
                        execution_time=round(elapsed, 3),
                        phase="run",
                        exit_code=0,
                        truncated=outcome.truncated,
                    )

        # A runner that yields no steps has nothing to run.
        return ExecutionResult(
            status="error", language=self.spec.id, stderr="Nothing to execute.", phase="run"
        )

    def _failure(
        self, step: Step, outcome: ProcessOutcome, budget: float, elapsed: float
    ) -> ExecutionResult | None:
        """A normalised failure result, or None when the step succeeded."""
        stage = "Compilation" if step.phase == "compile" else "Execution"

        if outcome.timed_out:
            note = f"{stage} timed out after {budget:g}s"
            return ExecutionResult(
                status="timeout",
                language=self.spec.id,
                stdout=outcome.stdout,
                stderr=_join(outcome.stderr, note),
                execution_time=round(elapsed, 3),
                phase=step.phase,
                exit_code=outcome.returncode,
                truncated=outcome.truncated,
            )

        if outcome.returncode == 0:
            return None

        stderr = outcome.stderr
        if outcome.signal_number is not None:
            stderr = _join(stderr, _describe_signal(outcome.signal_number))
        elif step.phase == "compile" and not stderr.strip():
            stderr = "Compilation failed."
        return ExecutionResult(
            status="error",
            language=self.spec.id,
            stdout=outcome.stdout,
            stderr=stderr,
            execution_time=round(elapsed, 3),
            phase=step.phase,
            exit_code=outcome.returncode,
            truncated=outcome.truncated,
        )


def _join(text: str, note: str) -> str:
    text = text.rstrip("\n")
    return f"{text}\n{note}" if text else note


def _describe_signal(number: int) -> str:
    import signal as _signal

    try:
        name = _signal.Signals(number).name
    except ValueError:
        name = f"signal {number}"
    hint = {
        "SIGKILL": " - usually the memory or CPU limit",
        "SIGSEGV": " - invalid memory access or the memory limit",
        "SIGXCPU": " - CPU time limit exceeded",
        "SIGXFSZ": " - file size limit exceeded",
    }.get(name, "")
    return f"Process terminated by {name}{hint}"


def write_source(workdir: str, name: str, code: str) -> str:
    """Write a source file owned by the runner uid; return its path."""
    path = os.path.join(workdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(code)
    if os.geteuid() == 0:
        os.chown(path, config.RUNNER_UID, config.RUNNER_GID)
    os.chmod(path, 0o600)
    return path
