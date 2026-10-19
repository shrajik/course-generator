"""Split a parsed document into retrieval-sized, heading-aware chunks.

Chunking directly decides retrieval quality, so the rules are explicit:

* a chunk never crosses a heading - each section is chunked on its own, and
  every chunk carries its heading path (Chapter > Section) as context;
* paragraphs are packed up to a target size; a paragraph longer than the
  maximum is split on sentence boundaries;
* a table is kept whole where it fits, so its rows stay together;
* inside one section consecutive chunks overlap by a few sentences, so a fact
  stated at a boundary is not cut off from its context.
"""

from __future__ import annotations

import re

from app.ingestion.parsers import ParsedDocument
from app.schemas.source import SourceChunk

TARGET_WORDS = 300
MAX_WORDS = 450
OVERLAP_WORDS = 40
MAX_CHUNKS = 2500

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _words(text: str) -> int:
    return len(text.split())


def _split_long(text: str) -> list[str]:
    """A paragraph over MAX_WORDS, cut at sentence boundaries (or by words)."""
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in _SENTENCE_RE.split(text):
        size = _words(sentence)
        if size > MAX_WORDS:  # one enormous sentence: cut by words
            words = sentence.split()
            for start in range(0, len(words), TARGET_WORDS):
                pieces.append(" ".join(words[start : start + TARGET_WORDS]))
            continue
        if current and count + size > TARGET_WORDS:
            pieces.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += size
    if current:
        pieces.append(" ".join(current))
    return pieces


def _tail(text: str) -> str:
    """The last ~OVERLAP_WORDS of a chunk, starting at a sentence boundary."""
    words = text.split()
    if len(words) <= OVERLAP_WORDS:
        return ""
    tail = " ".join(words[-OVERLAP_WORDS:])
    match = _SENTENCE_RE.search(tail)
    return tail[match.end() :] if match else tail


def chunk_document(document: ParsedDocument) -> list[SourceChunk]:
    chunks: list[SourceChunk] = []
    path: list[tuple[int, str]] = []  # (level, text) heading stack
    buffer: list[str] = []
    buffer_page: int | None = None
    carried = ""  # overlap carried from the previous chunk of this section

    def heading_path() -> list[str]:
        return [text for _, text in path]

    def flush() -> None:
        nonlocal buffer, buffer_page, carried
        body = "\n\n".join(buffer).strip()
        buffer = []
        if not body:
            return
        text = f"{carried}\n\n{body}".strip() if carried else body
        chunks.append(
            SourceChunk(
                index=len(chunks),
                heading_path=heading_path(),
                page=buffer_page,
                text=text,
                words=_words(text),
            )
        )
        carried = _tail(body)
        buffer_page = None

    for block in document.blocks:
        if block.kind == "heading":
            flush()
            carried = ""  # overlap never crosses a heading
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.text))
            continue

        pieces = [block.text] if _words(block.text) <= MAX_WORDS else _split_long(block.text)
        for piece in pieces:
            size = _words(piece)
            current = sum(_words(part) for part in buffer)
            if buffer and current + size > TARGET_WORDS:
                flush()
            if buffer_page is None:
                buffer_page = block.page
            buffer.append(piece)
            if block.kind == "table":
                flush()  # a table stands on its own
    flush()
    return chunks[:MAX_CHUNKS]
