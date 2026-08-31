"""Run metrics: where did the wall clock actually go.

A `RunMetrics` object is put on a context variable for the duration of a
generation run. The AI client records every call into it without any of the
agents having to thread an extra argument through their signatures.

This exists so performance work is driven by measurement rather than guesswork.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CallRecord:
    kind: str  # structured | research | image
    purpose: str
    model: str
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    failed: bool = False


@dataclass
class PhaseRecord:
    name: str
    seconds: float = 0.0
    count: int = 0


@dataclass
class RunMetrics:
    started_at: float = field(default_factory=time.perf_counter)
    calls: list[CallRecord] = field(default_factory=list)
    phases: dict[str, PhaseRecord] = field(default_factory=dict)
    peak_concurrency: int = 0
    _in_flight: int = 0

    # --- recording --------------------------------------------------------
    def record_call(self, record: CallRecord) -> None:
        self.calls.append(record)

    def call_started(self) -> None:
        self._in_flight += 1
        self.peak_concurrency = max(self.peak_concurrency, self._in_flight)

    def call_finished(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    def add_phase(self, name: str, seconds: float) -> None:
        phase = self.phases.setdefault(name, PhaseRecord(name=name))
        phase.seconds += seconds
        phase.count += 1

    # --- reporting --------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def call_seconds(self) -> float:
        """Sum of call durations - exceeds wall clock when calls run in parallel."""
        return sum(call.seconds for call in self.calls)

    def tokens(self) -> tuple[int, int]:
        return (
            sum(call.input_tokens for call in self.calls),
            sum(call.output_tokens for call in self.calls),
        )

    def summary(self) -> dict[str, object]:
        input_tokens, output_tokens = self.tokens()
        wall = round(self.elapsed, 2)
        serial = round(self.call_seconds(), 2)
        return {
            "wall_seconds": wall,
            "model_seconds": serial,
            # >1 means calls genuinely overlapped; 1.0 means fully serial.
            "parallel_speedup": round(serial / wall, 2) if wall > 0 else 0.0,
            "calls": len(self.calls),
            "failed_calls": sum(1 for call in self.calls if call.failed),
            "retries": sum(call.retries for call in self.calls),
            "peak_concurrent_calls": self.peak_concurrency,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "phases": {
                name: {"seconds": round(phase.seconds, 2), "count": phase.count}
                for name, phase in sorted(
                    self.phases.items(), key=lambda item: -item[1].seconds
                )
            },
            "slowest_calls": [
                {
                    "purpose": call.purpose,
                    "seconds": round(call.seconds, 2),
                    "model": call.model,
                }
                for call in sorted(self.calls, key=lambda c: -c.seconds)[:5]
            ],
        }

    def log_waterfall(self, label: str = "generation") -> None:
        summary = self.summary()
        log.info(
            "%s finished in %ss (%s model-seconds across %s calls, speedup %sx, peak %s concurrent)",
            label,
            summary["wall_seconds"],
            summary["model_seconds"],
            summary["calls"],
            summary["parallel_speedup"],
            summary["peak_concurrent_calls"],
        )
        for name, phase in (summary["phases"] or {}).items():  # type: ignore[union-attr]
            log.info("  %-22s %8.2fs  x%s", name, phase["seconds"], phase["count"])


_current: ContextVar[RunMetrics | None] = ContextVar("run_metrics", default=None)


def current_metrics() -> RunMetrics | None:
    return _current.get()


@contextmanager
def run_metrics() -> Iterator[RunMetrics]:
    """Collect metrics for everything that happens inside this block."""
    metrics = RunMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        _current.reset(token)


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Time a named phase into the current run's metrics (no-op if unset)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics = _current.get()
        if metrics is not None:
            metrics.add_phase(name, time.perf_counter() - started)
