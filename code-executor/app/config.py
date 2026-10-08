"""Limits and paths. Every value is overridable by environment so a
deployment can tighten them without a rebuild; the defaults are deliberately
conservative for a teaching tool running short, deterministic snippets."""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Unprivileged identity every executed program runs as. The firewall rule in
# firewall.py is keyed on this uid, so the two must agree.
RUNNER_UID = _int("EXECUTOR_RUNNER_UID", 10001)
RUNNER_GID = _int("EXECUTOR_RUNNER_GID", 10001)

# Scratch space. A tmpfs in compose, so nothing survives the container and
# a runaway write is bounded by the mount size rather than the host disk.
WORK_ROOT = os.environ.get("EXECUTOR_WORK_ROOT", "/work")

# Request ceilings. The *backend* chooses the limits for a run; these are the
# hard caps the executor enforces even if asked for more.
MAX_TIMEOUT_SECONDS = _int("EXECUTOR_MAX_TIMEOUT", 30)
MAX_MEMORY_MB = _int("EXECUTOR_MAX_MEMORY_MB", 512)
MAX_CODE_BYTES = _int("EXECUTOR_MAX_CODE_BYTES", 200_000)
DEFAULT_TIMEOUT_SECONDS = _int("EXECUTOR_DEFAULT_TIMEOUT", 10)
DEFAULT_MEMORY_MB = _int("EXECUTOR_DEFAULT_MEMORY_MB", 256)

# Compilers get their own, more generous budget: javac and g++ legitimately
# need far more memory and a few seconds that the user's program does not.
COMPILE_TIMEOUT_SECONDS = _int("EXECUTOR_COMPILE_TIMEOUT", 30)

# Per-stream capture cap. A program that prints in a loop must not be able to
# exhaust the executor's own memory.
MAX_OUTPUT_BYTES = _int("EXECUTOR_MAX_OUTPUT_BYTES", 64_000)

# Executions allowed to run at once, and how long a request waits for a slot
# before being turned away rather than queueing without bound.
MAX_CONCURRENT = _int("EXECUTOR_MAX_CONCURRENT", 4)
QUEUE_WAIT_SECONDS = _int("EXECUTOR_QUEUE_WAIT", 3)

# Per-process rlimits. NPROC is per *uid*, so it is shared by every concurrent
# run and sized for MAX_CONCURRENT runs of a multi-threaded runtime (the JVM).
MAX_PROCESSES = _int("EXECUTOR_MAX_PROCESSES", 256)
MAX_OPEN_FILES = _int("EXECUTOR_MAX_OPEN_FILES", 128)
MAX_FILE_BYTES = _int("EXECUTOR_MAX_FILE_BYTES", 16 * 1024 * 1024)
