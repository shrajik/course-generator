"""The writing style guide a course follows.

Client requirement (Methodologies section 3.5): a base guide - MSTP (Microsoft
Writing Style Guide, the default for instructional content) or CMOS (Chicago
Manual of Style, for long-form, cited, academic deliverables) - plus client
overrides: brand terms, banned words and preferred product names. Overrides win
over the base guide.

Optional on `CourseInput`: a course without it keeps today's behaviour exactly
(MSTP house rules when a learner profile is set, no rules otherwise).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BaseGuide = Literal["mstp", "cmos"]
CitationSystem = Literal["author_date", "notes_bibliography"]

STYLE_GUIDE_LABELS: dict[str, str] = {
    "mstp": "Microsoft Writing Style Guide (MSTP)",
    "cmos": "Chicago Manual of Style (CMOS)",
}

MAX_TERMS = 50
MAX_TERM_LENGTH = 80


def _clean(values: list[str]) -> list[str]:
    """Trim, drop blanks, drop case-insensitive duplicates, keep order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            cleaned.append(text)
    return cleaned


class StyleGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: BaseGuide = "mstp"
    # CMOS wants exactly one citation system per deliverable; the client's
    # default for business/research content is author-date.
    citation_system: CitationSystem = "author_date"
    # Written exactly as given (product names, brand capitalisation).
    brand_terms: list[str] = Field(default_factory=list, max_length=MAX_TERMS)
    # Words or phrases that must never appear.
    banned_words: list[str] = Field(default_factory=list, max_length=MAX_TERMS)

    @field_validator("brand_terms", "banned_words")
    @classmethod
    def _normalise_terms(cls, value: list[str]) -> list[str]:
        cleaned = _clean(value)
        for term in cleaned:
            if len(term) > MAX_TERM_LENGTH:
                raise ValueError(f"A term is longer than {MAX_TERM_LENGTH} characters: {term[:30]}...")
        return cleaned

    @property
    def has_overrides(self) -> bool:
        return bool(self.brand_terms or self.banned_words)
