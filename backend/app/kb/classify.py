"""Working out what a document is about, so an admin does not have to say.

An upload needs only the file. These rules propose a title, description, tags and a
category (and topic labels for its sections); the admin reviews them, because nothing
is published until they do.

Where it can, it uses what is already computed and costs nothing:

* **Category**: the nearest category centroid (the mean vector of that category's
  items) to the document's own centroid. No model call.
* **Section topics**: each passage's nearest category centroid. No model call.
* **Tags**: the document's own frequent terms and phrases. No model call.
* **Title / description / better tags**: one small-model call per document, from its
  outline and about 1,500 words - and only if enabled. Without it the rules above
  still produce a usable draft.

The thresholds are starting points, not calibrated values: they must be tuned on a
real corpus before anyone relies on "assigned" meaning "right".

Pure functions apart from the prompt/response types: no database, no model calls.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.ingestion.retrieval import _STOPWORDS, _TOKEN_RE, _stem  # noqa: PLC2701 - same notion of a word as search

# Cosine similarity at or above which a category is proposed outright, and below which
# nothing is proposed. Between them the admin is shown the candidates.
ASSIGN_SIMILARITY = 0.72
CANDIDATE_SIMILARITY = 0.45
# The best category must beat the runner-up by this much to be "assigned".
ASSIGN_MARGIN = 0.04
SECTION_TOPIC_SIMILARITY = 0.5
PROMPT_SAMPLE_WORDS = 1500


# --- vectors --------------------------------------------------------------------------


def _unit(vector: Sequence[float]) -> np.ndarray | None:
    row = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(row))
    return row / norm if norm > 0 else None


def mean_vector(vectors: Sequence[Sequence[float] | None]) -> list[float] | None:
    """The mean of the unit vectors; None when there are none to average."""
    rows = [u for v in vectors if v is not None and (u := _unit(v)) is not None]
    if not rows:
        return None
    return [float(x) for x in np.mean(rows, axis=0)]


def update_centroid(centroid: Sequence[float] | None, count: int, vector: Sequence[float], *, remove: bool = False) -> tuple[list[float] | None, int]:
    """Fold one item's vector into (or out of) a category's running mean."""
    unit = _unit(vector)
    if unit is None:
        return (list(centroid) if centroid is not None else None), count
    if remove:
        if centroid is None or count <= 1:
            return None, 0
        total = np.asarray(centroid, dtype=np.float32) * count - unit
        return [float(x) for x in total / (count - 1)], count - 1
    if centroid is None or count <= 0:
        return [float(x) for x in unit], 1
    total = np.asarray(centroid, dtype=np.float32) * count + unit
    return [float(x) for x in total / (count + 1)], count + 1


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    ua, ub = _unit(a), _unit(b)
    return float(ua @ ub) if ua is not None and ub is not None else 0.0


# --- category -------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryCentroid:
    id: str
    name: str
    vector: Sequence[float]
    count: int = 1


@dataclass
class CategoryMatch:
    # assigned: confident; needs_review: plausible candidates, an admin should choose;
    # none: nothing close (or no categories yet) - propose a new category instead.
    status: Literal["assigned", "needs_review", "none"]
    category_id: str | None = None
    name: str | None = None
    similarity: float = 0.0
    confidence: float = 0.0
    alternatives: list[dict] = field(default_factory=list)


def match_category(
    centroid: Sequence[float] | None,
    categories: Sequence[CategoryCentroid],
    *,
    assign_similarity: float = ASSIGN_SIMILARITY,
    candidate_similarity: float = CANDIDATE_SIMILARITY,
    margin: float = ASSIGN_MARGIN,
) -> CategoryMatch:
    if centroid is None:
        return CategoryMatch("none")
    scored = sorted(
        ((cosine(centroid, c.vector), c) for c in categories if c.count > 0 and _unit(c.vector) is not None),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] < candidate_similarity:
        return CategoryMatch("none")
    best_similarity, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else -1.0
    span = max(assign_similarity - candidate_similarity, 1e-6)
    result = CategoryMatch(
        status="assigned" if best_similarity >= assign_similarity and best_similarity - runner_up >= margin else "needs_review",
        category_id=best.id,
        name=best.name,
        similarity=round(best_similarity, 4),
        confidence=round(min(1.0, max(0.0, (best_similarity - candidate_similarity) / span)), 3),
        alternatives=[
            {"id": c.id, "name": c.name, "similarity": round(s, 4)}
            for s, c in scored[1:4]
            if s >= candidate_similarity
        ],
    )
    return result


def label_sections(
    vectors: Sequence[Sequence[float] | None],
    categories: Sequence[CategoryCentroid],
    *,
    threshold: float = SECTION_TOPIC_SIMILARITY,
    max_labels: int = 2,
) -> list[list[str]]:
    """For each passage, the names of the categories it is close to (best first, at most
    `max_labels`): lets retrieval filter inside a document that covers several subjects."""
    usable = [(c.name, _unit(c.vector)) for c in categories if c.count > 0]
    usable = [(name, unit) for name, unit in usable if unit is not None]
    labels: list[list[str]] = []
    for vector in vectors:
        unit = _unit(vector) if vector is not None else None
        if unit is None or not usable:
            labels.append([])
            continue
        scored = sorted(((float(unit @ u), name) for name, u in usable), reverse=True)
        labels.append([name for s, name in scored[:max_labels] if s >= threshold])
    return labels


# --- title and tags (no model) ---------------------------------------------------------


def _clean(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit].strip()


def suggest_title(filename: str, outline: Sequence[str] = (), first_text: str = "") -> str:
    """The document's own top-level heading when it has a sensible one, else its filename."""
    for line in outline:
        if line == line.lstrip() and 4 <= len(line.strip()) <= 120:  # an unindented (top-level) heading
            return _clean(line, 120)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    named = _clean(re.sub(r"[_\-]+", " ", stem), 120)
    return named or _clean(first_text, 120) or "Untitled"


def key_terms(texts: Sequence[str], *, top: int = 8) -> list[tuple[str, float]]:
    """The document's most characteristic words and two-word phrases with a 0-1 weight.

    Repeated terms win; a phrase that repeats outranks the single words inside it."""
    stems: Counter[str] = Counter()
    surface: dict[str, Counter[str]] = {}
    pairs: Counter[tuple[str, str]] = Counter()
    for text in texts:
        previous: tuple[str, str] | None = None
        for word in _TOKEN_RE.findall(text.lower()):
            if word in _STOPWORDS or word.isdigit() or len(word) < 3:
                previous = None
                continue
            stem = _stem(word)
            stems[stem] += 1
            surface.setdefault(stem, Counter())[word] += 1
            if previous is not None and previous[0] != stem:
                pairs[(previous[0], stem)] += 1
            previous = (stem, word)

    def show(stem: str) -> str:
        return surface[stem].most_common(1)[0][0]

    scored: list[tuple[float, str, tuple[str, ...]]] = []
    for pair, count in pairs.items():
        if count >= 2:
            scored.append((count * 2.0, f"{show(pair[0])} {show(pair[1])}", pair))
    for stem, count in stems.items():
        if count >= 2 or len(stems) <= 5:
            scored.append((float(count) * (1 + math.log(len(show(stem)))) / 2, show(stem), (stem,)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[float, str]] = []
    covered: set[str] = set()
    for score, name, parts in scored:
        if len(parts) == 1 and parts[0] in covered:
            continue
        chosen.append((score, name))
        covered.update(parts)
        if len(chosen) == top:
            break
    best = chosen[0][0] if chosen else 1.0
    return [(name, round(score / best, 3)) for score, name in chosen]


# --- the model call (optional) -----------------------------------------------------------


class MetadataSuggestion(BaseModel):
    """What the small model is asked to return."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", description="A clear title for this document, at most 12 words.")
    description: str = Field(default="", description="One or two sentences saying what the document covers and who it is for.")
    tags: list[str] = Field(default_factory=list, description="Up to 8 short topic tags, 1-3 words each, lower case.")


METADATA_SYSTEM = (
    "You label documents for an organisation's knowledge base. Reply with a title, a one or "
    "two sentence description and up to 8 short topic tags. Describe only what the text says; "
    "do not invent facts. Prefer tags from the existing list when one fits, so the same topic "
    "is not tagged two different ways."
)


def sample_text(chunks: Sequence[str], *, words: int = PROMPT_SAMPLE_WORDS) -> str:
    """About `words` words spread across the document (start, middle, end), not just its opening."""
    if not chunks:
        return ""
    total = sum(len(c.split()) for c in chunks)
    if total <= words:
        return "\n\n".join(chunks)
    picked: list[str] = []
    budget = words
    step = max(1, len(chunks) // max(1, min(len(chunks), 8)))
    for chunk in chunks[::step]:
        take = chunk.split()[: max(40, words // 8)]
        picked.append(" ".join(take))
        budget -= len(take)
        if budget <= 0:
            break
    return "\n\n".join(picked)


def build_metadata_prompt(filename: str, outline: Sequence[str], chunks: Sequence[str], existing_tags: Sequence[str] = ()) -> tuple[str, str]:
    parts = [f"Filename: {filename}"]
    if outline:
        parts.append("Headings:\n" + "\n".join(outline[:40]))
    if existing_tags:
        parts.append("Existing tags: " + ", ".join(list(existing_tags)[:50]))
    parts.append("Text (excerpts):\n" + sample_text(chunks))
    return METADATA_SYSTEM, "\n\n".join(parts)


_TAG_RE = re.compile(r"[^a-z0-9+#. -]")


def clean_metadata(raw: MetadataSuggestion) -> MetadataSuggestion:
    """Whatever the model returned, bounded and tidy."""
    tags: list[str] = []
    for tag in raw.tags:
        name = " ".join(_TAG_RE.sub("", tag.lower()).split())
        if 2 <= len(name) <= 40 and name not in tags:
            tags.append(name)
    return MetadataSuggestion(title=_clean(raw.title, 200), description=_clean(raw.description, 500), tags=tags[:8])


# --- the result stored on the item ---------------------------------------------------------


def assemble_suggestions(
    *,
    filename: str,
    outline: Sequence[str],
    chunks: Sequence[str],
    category: CategoryMatch,
    model: MetadataSuggestion | None = None,
) -> tuple[dict, bool]:
    """(suggestions for `kb_items.suggestions`, needs_review).

    The model's title, description and tags are used when it was called and returned
    something; otherwise the rules' title and the document's own terms. `needs_review`
    marks what the system is not sure of: no confident category, or no description."""
    tidy = clean_metadata(model) if model is not None else None
    title = (tidy.title if tidy and tidy.title else "") or suggest_title(filename, outline, chunks[0] if chunks else "")
    description = tidy.description if tidy else ""
    if tidy and tidy.tags:
        tags = [{"name": name, "confidence": 0.8, "source": "ai"} for name in tidy.tags]
    else:
        tags = [{"name": name, "confidence": confidence, "source": "rules"} for name, confidence in key_terms(chunks)]
    suggestions = {
        "generated_by": "model" if tidy and (tidy.title or tidy.description or tidy.tags) else "rules",
        "title": title,
        "description": description,
        "tags": tags,
        "category": None
        if category.status == "none"
        else {
            "id": category.category_id,
            "name": category.name,
            "status": category.status,
            "similarity": category.similarity,
            "confidence": category.confidence,
            "alternatives": category.alternatives,
        },
    }
    return suggestions, category.status != "assigned" or not description
