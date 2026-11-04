"""Offering approved artifacts to a chapter being written - and the knowledge they were written from.

This is the one place the two memories are used together, and it keeps them apart:

* artifacts come from Memory 2 (`ArtifactMemory`), knowledge from Memory 1, each through its own
  retrieval and its own rules (only approved artifacts; only published knowledge);
* the connection is a *link*: an artifact records the knowledge citations it was written from. If such a
  passage is still live in Memory 1 it is offered alongside the artifact - through Memory 1's own channel,
  so it carries a `[G..]` citation the audit can check. If Memory 1 has since unpublished it, it is not
  offered: an artifact's age never revives withdrawn knowledge.

A course is never offered its own artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.artifacts.retrieval import ArtifactExcerpt, ArtifactMemory
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.source import SourceExcerpt

log = get_logger(__name__)

_KB_REF = re.compile(r"^G\d+\.\d+$")
MAX_LINKED_PASSAGES = 4


@dataclass
class Offer:
    artifacts: list[ArtifactExcerpt] = field(default_factory=list)
    # Memory 1 passages reached through the artifacts' links, not already offered by a search.
    linked_knowledge: list[SourceExcerpt] = field(default_factory=list)


class ArtifactReuse:
    def __init__(self, settings: Settings | None = None, memory: ArtifactMemory | None = None) -> None:
        self.settings = settings or get_settings()
        self.memory = memory or ArtifactMemory(self.settings)

    async def available(self) -> bool:
        """Whether there is anything to offer. Never raises: an unreachable database or a missing table
        means no reuse, never a failed chapter."""
        if not (self.settings.enable_artifact_reuse and self.settings.use_database):
            return False
        try:
            return await self.memory.has_artifacts()
        except Exception as exc:  # noqa: BLE001
            log.warning("Artifact reuse unavailable: %s", exc)
            return False

    async def offer(self, query: str, *, course_id: str, chapter_id: str, knowledge, already_offered: set[str]) -> Offer:
        """`knowledge` is the Memory 1 service; `already_offered` the `G` references its own search returned."""
        artifacts = await self.memory.search(query, exclude_course_id=course_id, trace=(course_id, chapter_id))
        if not artifacts:
            return Offer()
        wanted: list[str] = []
        for artifact in artifacts:
            for ref in artifact.source_refs:
                if _KB_REF.match(ref) and ref not in already_offered and ref not in wanted:
                    wanted.append(ref)
        linked: list[SourceExcerpt] = []
        if wanted:
            try:
                linked = (await knowledge.apassages_by_refs(wanted[:MAX_LINKED_PASSAGES], live_only=True))[:MAX_LINKED_PASSAGES]
            except Exception as exc:  # noqa: BLE001 - linked context is a bonus, never a reason to fail
                log.warning("Could not fetch the knowledge linked to artifacts: %s", exc)
        return Offer(artifacts=artifacts, linked_knowledge=linked)


_reuse: ArtifactReuse | None = None


def get_artifact_reuse() -> ArtifactReuse:
    global _reuse
    if _reuse is None:
        _reuse = ArtifactReuse()
    return _reuse


def reset_artifact_reuse() -> None:
    global _reuse
    _reuse = None
