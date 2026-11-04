"""Turning a finished course document into artifacts.

A course is one document of blocks. Reuse needs smaller, independently identifiable units, so
each chapter becomes an artifact, and so does each visual, question set, activity and case study
inside it. A chapter's artifact still contains those blocks (a chapter is reusable whole); the
child artifacts exist so a diagram or a quiz can be found and reused on its own.

Identity is stable: an artifact's `key` is derived from the chapter or block id, so submitting the
same course again finds the same artifacts instead of creating copies. `content_hash` says whether
the content actually changed since the last time.

Pure: no database, no model calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.course.fact_check import extract_refs
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument

# Keyword search and excerpts see the whole artifact (a long chapter included); the embedding sees a
# representative sample of it (see `embedding_text`), because a vector can only describe so much text.
MAX_SEARCH_CHARS = 60000
EMBED_CHARS = 4000
# Keys whose strings say nothing about what the content means.
_NOISE_KEYS = frozenset({"id", "path", "asset_id", "error", "prompt", "style"})
_ASSESSMENT_RE = re.compile(r"\b(assessment|exam|final test|end[- ]of[- ]course)\b", re.IGNORECASE)

# Block type -> artifact type, for blocks that are reusable on their own.
_STANDALONE = {
    BlockType.QUIZ: "questions",
    BlockType.EXERCISE: "activity",
    BlockType.CHALLENGE: "activity",
    BlockType.CASE_STUDY: "case_study",
}


@dataclass(frozen=True)
class ArtifactDraft:
    key: str
    artifact_type: str
    title: str
    chapter_id: str | None
    content: dict[str, Any]
    content_hash: str
    search_text: str
    description: str = ""
    asset: dict[str, Any] | None = None
    source_refs: tuple[str, ...] = ()
    # Keys of the child artifacts a chapter contains (visuals, questions, ...), for `contains_*` links.
    children: tuple[str, ...] = field(default=())


def embedding_text(search_text: str, *, limit: int = EMBED_CHARS) -> str:
    """What to embed for an artifact: all of it if it is short, else its opening (title and first
    section) plus evenly spaced windows from the rest, so a long chapter's vector reflects the whole
    chapter and not just its first few paragraphs."""
    if len(search_text) <= limit:
        return search_text
    head = limit // 3
    windows = 6
    window = (limit - head) // windows
    stride = (len(search_text) - head) // windows
    parts = [search_text[:head]]
    for i in range(windows):
        start = head + i * stride
        parts.append(search_text[start : start + window])
    return " ... ".join(parts)[:limit]


def _without_ids(node: Any) -> Any:
    """`node` with every `id` key removed: block ids are minted per document and say nothing about the
    content, so two documents with the same words must not look different (nor the same document re-saved)."""
    if isinstance(node, Mapping):
        return {key: _without_ids(child) for key, child in node.items() if key != "id"}
    if isinstance(node, list):
        return [_without_ids(child) for child in node]
    return node


def canonical_hash(content: Mapping[str, Any]) -> str:
    """The same content always hashes the same, whatever the key order and whatever its ids."""
    return hashlib.sha256(json.dumps(_without_ids(content), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def text_of(value: Any, *, limit: int = MAX_SEARCH_CHARS) -> str:
    """Every human-readable string in a content structure, in order, for search and embedding."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.strip():
                parts.append(node.strip())
        elif isinstance(node, Mapping):
            for key, child in node.items():
                if key not in _NOISE_KEYS:
                    walk(child)
        elif isinstance(node, Sequence):
            for child in node:
                walk(child)

    walk(value)
    return " ".join(parts)[:limit]


def _block_json(block: Block) -> dict[str, Any]:
    return {"id": block.id, "type": block.type.value, "content": block.content, "section_key": block.meta.section_key}


def _draft(key: str, artifact_type: str, title: str, chapter_id: str | None, content: dict[str, Any], *, asset=None, extra_refs: Sequence[str] = (), children=()) -> ArtifactDraft:
    search = text_of(content)
    refs = list(dict.fromkeys([*extract_refs(search), *extra_refs]))
    return ArtifactDraft(
        key=key, artifact_type=artifact_type, title=" ".join(title.split())[:300] or "Untitled", chapter_id=chapter_id,
        content=content, content_hash=canonical_hash(content), search_text=search, asset=asset,
        source_refs=tuple(refs), children=tuple(children),
    )


def _visual(block: Block, chapter_id: str) -> ArtifactDraft | None:
    c = block.content
    if c.get("decorative") or not (c.get("generated") and (c.get("path") or c.get("asset_id"))):
        return None  # a picture that was never produced, or carries no information, is not worth reusing
    asset = {k: c.get(k) for k in ("path", "asset_id", "alt", "caption", "width", "height") if c.get(k) is not None}
    # What the picture shows is its caption and alt text; the generation prompt is not part of the artifact.
    return _draft(f"block:{block.id}", "visual", c.get("caption") or c.get("alt") or "Figure", chapter_id, {"kind": "image", "block": _block_json(block)}, asset=asset)


def extract_artifacts(document: CourseDocument, *, offered: Mapping[str, Sequence[str]] | None = None) -> list[ArtifactDraft]:
    """The artifacts of `document`: one per chapter, plus its visuals, question sets, activities and
    case studies. `offered` maps a chapter id to the citations its writer was given (what it drew on)."""
    chapters: dict[str, list[Block]] = {}
    for _, block in document.iter_blocks():
        if block.meta.chapter_id:
            chapters.setdefault(block.meta.chapter_id, []).append(block)

    drafts: list[ArtifactDraft] = []
    for chapter_id, blocks in chapters.items():
        children: list[ArtifactDraft] = []
        for block in blocks:
            if block.type == BlockType.IMAGE:
                child = _visual(block, chapter_id)
            elif block.type in _STANDALONE:
                kind = _STANDALONE[block.type]
                title = str(block.content.get("title") or kind.replace("_", " ").title())
                if block.type == BlockType.QUIZ:
                    if not block.content.get("questions"):
                        continue
                    kind = "assessment" if _ASSESSMENT_RE.search(title) else "questions"
                child = _draft(f"block:{block.id}", kind, title, chapter_id, {"kind": block.type.value, "block": _block_json(block)})
            else:
                continue
            if child is not None:
                children.append(child)
        heading = next((b.content.get("text", "") for b in blocks if b.type == BlockType.HEADING), "")
        number = blocks[0].meta.chapter_number
        title = heading or (f"Chapter {number}" if number else chapter_id)
        drafts.append(
            _draft(
                f"chapter:{chapter_id}", "chapter", title, chapter_id,
                {"kind": "chapter", "blocks": [_block_json(b) for b in blocks]},
                extra_refs=list((offered or {}).get(chapter_id, ())), children=[c.key for c in children],
            )
        )
        drafts.extend(children)
    return drafts
