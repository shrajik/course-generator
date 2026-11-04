"""What to do when an ingestion job fails: retry, give up, or settle for less.

* A **permanent** error (a scanned PDF, an empty file, an unsupported type) fails at
  once with the message an admin can act on; trying again cannot help.
* A **transient** error (provider outage, timeout, dropped connection) is retried
  with exponential backoff and jitter, up to `max_attempts`.
* When retries run out: a stage that can **degrade** (embedding: the item is still
  searchable by keyword) finishes `ready` with a warning, so an outage never blocks
  knowledge; any other stage goes to `dead`, where an admin can retry it by hand.
* A job whose worker stopped heartbeating (a crash or restart) is **reclaimable**
  once its lease has expired; this replaces the old "stuck for ten minutes means
  failed" guess.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from app.ingestion.parsers import IngestionError

ErrorKind = Literal["permanent", "transient"]
Action = Literal["retry", "fail", "dead", "degrade"]

PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    IngestionError,
    ValidationFailedError,
    ConflictError,
    NotFoundError,
    ForbiddenError,
    UnauthorizedError,
)

BASE_DELAY_SECONDS = 30.0
MAX_DELAY_SECONDS = 1800.0
LEASE_SECONDS = 120.0


class EmbeddingIncomplete(Exception):
    """Some passages still have no vector after a pass. Transient by nature."""


def classify_error(error: BaseException) -> ErrorKind:
    return "permanent" if isinstance(error, PERMANENT_ERRORS) else "transient"


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Delay before the next attempt after `attempt` have failed: 30 s, 60 s, 120 s ...
    capped at 30 minutes, plus up to 20% jitter so a batch of failed jobs does not
    retry in lockstep."""
    base = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** max(attempt - 1, 0)))
    jitter = (rng or random).uniform(0.0, 0.2)
    return round(base * (1 + jitter), 3)


@dataclass(frozen=True)
class Decision:
    action: Action
    delay_seconds: float = 0.0
    reason: str = ""


def decide(
    error: BaseException,
    *,
    attempts: int,
    max_attempts: int,
    degradable: bool = False,
    rng: random.Random | None = None,
) -> Decision:
    """`attempts` counts the attempt that just failed."""
    if classify_error(error) == "permanent":
        return Decision("fail", reason="permanent error: not retried")
    if attempts < max_attempts:
        return Decision("retry", backoff_seconds(attempts, rng=rng), reason=f"attempt {attempts} of {max_attempts} failed")
    if degradable:
        return Decision("degrade", reason=f"gave up after {attempts} attempts; continuing without it")
    return Decision("dead", reason=f"gave up after {attempts} attempts")


def lease_expired(heartbeat_at: datetime | None, now: datetime, *, lease_seconds: float = LEASE_SECONDS) -> bool:
    """A running job nobody has touched for `lease_seconds` belongs to a dead worker."""
    return heartbeat_at is None or now - heartbeat_at > timedelta(seconds=lease_seconds)
