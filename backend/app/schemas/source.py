"""Uploaded source material: the client's own documents as grounded context.

Client requirement (Blueprint panel D, HLD "Uploaded Document RAG"): creators
upload SOPs, PDF manuals, transcripts and notes, which then serve as the ground
truth for generation. A source goes through uploaded -> parsing -> chunking ->
embedding -> ready, and the creator sees that status.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal["pdf", "docx", "txt", "md"]
SourceStatus = Literal["uploaded", "parsing", "chunking", "embedding", "ready", "failed"]

# States a source passes through while it is still being processed.
IN_PROGRESS: tuple[str, ...] = ("uploaded", "parsing", "chunking", "embedding")


class SourceRecord(BaseModel):
    """What is stored and shown about one uploaded source."""

    model_config = ConfigDict(extra="allow")

    source_id: str
    # Who uploaded it: the user id, or "local" when there is no user system.
    owner_key: str
    filename: str
    kind: SourceKind
    size_bytes: int
    sha256: str
    status: SourceStatus = "uploaded"
    # A short human line for the status screen ("Reading page 12 of 40").
    message: str = ""
    error: str | None = None
    page_count: int | None = None
    word_count: int = 0
    chunk_count: int = 0
    # How many chunks have a vector. Fewer than chunk_count means retrieval for
    # this source falls back to keyword search for the rest.
    embedded_chunks: int = 0
    # The headings found, so the creator can see what was understood.
    outline: list[str] = Field(default_factory=list)
    # --- the global knowledge library only (app/services/knowledge_service.py) ----
    # A number that stays with the document for good, so a citation [G2.5]
    # means the same passage in every course, forever.
    global_number: int | None = None
    # A library document an admin switched off is kept but never searched.
    enabled: bool = True
    description: str = ""
    created_at: str
    updated_at: str


class SourceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    heading_path: list[str] = Field(default_factory=list)
    page: int | None = None
    text: str
    words: int
    embedded: bool = False

    @property
    def heading(self) -> str:
        return " > ".join(self.heading_path)

    def embed_text(self) -> str:
        """What is embedded: the heading context plus the text."""
        return f"{self.heading}\n{self.text}" if self.heading_path else self.text


class SourceExcerpt(BaseModel):
    """One retrieved passage, with the reference the writer cites it by."""

    model_config = ConfigDict(extra="forbid")

    ref: str  # "S1.3" = source 1 of the course, passage 3
    source_id: str
    filename: str
    heading: str = ""
    page: int | None = None
    text: str
    score: float = 0.0
