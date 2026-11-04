"""The contract every knowledge-library backend satisfies.

Callers outside the library (course generation, the fact-check audit, the
passage-lookup endpoint) use only these async methods, so the storage behind
them - the filesystem today, PostgreSQL next - can change without touching them.

`trace` and `extra_refs` are accepted by every backend and may be ignored by
one that cannot use them:

* `trace` is `(course_id, chapter_id)`: who is searching. A backend that keeps a
  retrieval log records it, so a citation can later be tied to the moment a
  course was generated.
* `live_only` asks `apassages_by_refs` for passages a course may use *now*: a backend that keeps
  superseded and unpublished passages so old citations still resolve returns only the live ones.
* `extra_refs` are references (`G2.5`) a caller already holds citations to. A
  backend that retains superseded passages includes the ones cited, so an old
  citation can be checked against the text it originally pointed at.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.source import SourceExcerpt
from app.services.source_service import SourceLibrary


@runtime_checkable
class KnowledgeBackend(Protocol):
    async def asearch(
        self,
        query: str,
        *,
        limit: int | None = None,
        char_budget: int | None = None,
        trace: tuple[str, str] | None = None,
    ) -> list[SourceExcerpt]: ...

    async def ahas_documents(self) -> bool: ...

    async def alibrary(self, extra_refs: list[str] | None = None) -> SourceLibrary: ...

    async def apassages_by_refs(self, refs: list[str], *, live_only: bool = False) -> list[SourceExcerpt]: ...
