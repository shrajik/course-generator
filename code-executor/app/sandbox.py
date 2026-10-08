"""Run one untrusted process with hard limits.

This is the only place that starts a child process on behalf of submitted
code, so every guarantee the service makes is enforced here:

* **Identity** - the child drops to an unprivileged uid/gid, clears its
  supplementary groups and sets no_new_privs, so it cannot regain anything.
* **Resources** - CPU time, address space (where the runtime tolerates it),
  open files, process count and file size are all rlimited.
* **Time** - a wall-clock timeout that kills the *whole process group*, so a
  forked child cannot outlive its parent.
* **Output** - captured with a hard cap; a program that prints forever is
  killed instead of being allowed to exhaust this process's memory.
* **Environment** - a minimal, explicit environment. Nothing is inherited,
  so no secret in this process's environment can leak into submitted code.
* **Workspace** - a fresh private directory per execution, always removed.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import os
import resource
import shutil
import signal
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from . import config

_PR_SET_NO_NEW_PRIVS = 38

# A stream producing this many times the cap is not "verbose", it is a loop.
_RUNAWAY_FACTOR = 64


@dataclass
class ProcessOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration: float
    signal_number: int | None = None


@contextmanager
def workspace() -> Iterator[str]:
    """A private scratch directory, owned by the runner uid, always removed."""
    os.makedirs(config.WORK_ROOT, exist_ok=True)
    path = tempfile.mkdtemp(prefix="run-", dir=config.WORK_ROOT)
    try:
        if os.geteuid() == 0:
            os.chown(path, config.RUNNER_UID, config.RUNNER_GID)
        os.chmod(path, 0o700)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def minimal_env(workdir: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if extra:
        env.update(extra)
    return env


def _limits(*, cpu_seconds: int, memory_bytes: int | None) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (config.MAX_OPEN_FILES, config.MAX_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_NPROC, (config.MAX_PROCESSES, config.MAX_PROCESSES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (config.MAX_FILE_BYTES, config.MAX_FILE_BYTES))
    if memory_bytes is not None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _drop_privileges() -> None:
    if os.geteuid() != 0:
        return  # already unprivileged (local development); nothing to drop
    os.setgroups([])
    os.setgid(config.RUNNER_GID)
    os.setuid(config.RUNNER_UID)
    # Even a setuid binary on the image cannot raise privileges after this.
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)


async def _drain(stream: asyncio.StreamReader, cap: int, kill) -> tuple[bytes, bool]:
    kept = bytearray()
    seen = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        seen += len(chunk)
        room = cap - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > room:
            truncated = True
        if seen > cap * _RUNAWAY_FACTOR:
            kill()
            truncated = True
            break
    return bytes(kept), truncated


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_process(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    memory_limit_mb: int | None,
    env: dict[str, str],
    limit_address_space: bool = True,
) -> ProcessOutcome:
    """Run `argv` inside the sandbox and return everything observed.

    `limit_address_space=False` is for runtimes (the JVM, V8) that reserve
    enormous virtual ranges up front and abort under RLIMIT_AS; they are held
    to `memory_limit_mb` by their own heap flag and the container cgroup.
    """
    cpu_seconds = max(1, math.ceil(timeout))
    memory_bytes = memory_limit_mb * 1024 * 1024 if (memory_limit_mb and limit_address_space) else None

    def preexec() -> None:
        _limits(cpu_seconds=cpu_seconds, memory_bytes=memory_bytes)
        _drop_privileges()

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so one kill reaps all
            preexec_fn=preexec,
        )
    except (OSError, ValueError) as exc:
        return ProcessOutcome(None, "", f"Could not start process: {exc}", False, False, 0.0)

    kill = lambda: _kill_group(process.pid)  # noqa: E731
    out_task = asyncio.create_task(_drain(process.stdout, config.MAX_OUTPUT_BYTES, kill))
    err_task = asyncio.create_task(_drain(process.stderr, config.MAX_OUTPUT_BYTES, kill))

    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        kill()
        await process.wait()
    finally:
        # Whether it finished or was killed, nothing it started may survive.
        kill()

    (stdout, out_trunc), (stderr, err_trunc) = await asyncio.gather(out_task, err_task)
    duration = time.monotonic() - started

    code = process.returncode
    return ProcessOutcome(
        returncode=code,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        timed_out=timed_out,
        truncated=out_trunc or err_trunc,
        duration=duration,
        signal_number=-code if code is not None and code < 0 else None,
    )
