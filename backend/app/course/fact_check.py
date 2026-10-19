"""Compare a course with its source passages, sentence by sentence.

This is the deterministic half of the auditor: cheap, repeatable, and the same
on every run. For each factual-looking sentence of the course it asks three
things of the creator's own sources:

* does every `[S1.3]` reference the sentence carries really exist?
* if it cites a passage, does that passage actually contain what the sentence
  says (lexical containment, with spelled-out numbers read as digits)?
* if it cites nothing, is it nevertheless covered by some passage, and if not,
  does it make a specific claim (a figure, a name, an absolute) that someone
  must confirm?

What it cannot do is understand meaning - "refunds take 14 days" against a
passage saying "refunds take 30 days" shares most of its words. The AI half
(app/agents/auditor.py) reads the doubtful sentences against their evidence and
can overturn or escalate a finding; without it the rules still stand and every
finding says it was decided by rules alone.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.ingestion.retrieval import tokenize
from app.schemas.audit import KIND_SEVERITY, SEVERITY_ORDER, AuditFinding, ClaimVerdict, Evidence
from app.schemas.blocks import TEXTUAL_BLOCK_TYPES, BlockType
from app.schemas.document import CourseDocument
from app.services.source_service import SourceLibrary

# Sentences with fewer distinctive words than this are labels or fragments.
MIN_CONTENT_TOKENS = 5
# Share of a sentence's words found in one passage.
SUPPORTED_AT = 0.6
CITED_SUPPORTED_AT = 0.35
EXCERPT_CHARS = 400
PASSAGES_PER_SENTENCE = 3

_AUDITED_TYPES = TEXTUAL_BLOCK_TYPES | {BlockType.CALLOUT}
_REF_TOKEN_RE = re.compile(r"[SG]\d+\.\d+")
_BRACKET_RE = re.compile(r"\[([^\]]*?[SG]\d+\.\d+[^\]]*?)\]")
_TAG_RE = re.compile(r"</?jargon[^>]*>", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[\x00])")
_LEADING_MARKER_RE = re.compile(r"^(\[[^\]]*[SG]\d+\.\d+[^\]]*\])\s*(.*)$", re.DOTALL)
_PROTECTED = ("e.g.", "i.e.", "etc.", "vs.", "Dr.", "Mr.", "Ms.", "Fig.", "No.", "approx.")
_ABSOLUTE_RE = re.compile(
    r"\b(always|never|every|all|only|none|must|guarantee[sd]?|exactly|cannot|impossible|required|mandatory)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    **{w: str(n) for n, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split()
    )},
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100",
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_CODE_LIKE_RE = re.compile(r"[`{}=<>]|\(\)|->|::")


# --- reading the course --------------------------------------------------------


@dataclass
class AuditBlock:
    block_id: str
    page_number: int | None
    chapter: str
    text: str


def collect_audit_blocks(document: CourseDocument) -> list[AuditBlock]:
    """The prose of the course, in reading order, with its chapter and page."""
    chapter = "Introduction"
    blocks: list[AuditBlock] = []
    for page in document.pages:
        if page.kind != "content":
            continue
        for block in page.blocks:
            if block.type is BlockType.HEADING:
                if int(block.content.get("level", 2) or 2) <= 1:
                    chapter = str(block.content.get("text", "")).strip() or chapter
                continue
            if block.type not in _AUDITED_TYPES:
                continue
            text = str(block.content.get("text") or "").strip()
            if text:
                blocks.append(AuditBlock(block.id, page.page_number, chapter, text))
    return blocks


# --- text helpers --------------------------------------------------------------


def normalise_numbers(text: str) -> str:
    """"fourteen days" and "14 days" must read the same on both sides."""
    return _NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)


def extract_refs(sentence: str) -> list[str]:
    """Every `S1.3` inside square brackets, in order, without duplicates."""
    found: list[str] = []
    for bracket in _BRACKET_RE.findall(sentence):
        found.extend(_REF_TOKEN_RE.findall(bracket))
    return list(dict.fromkeys(found))


def strip_refs(sentence: str) -> str:
    return re.sub(r"\s+", " ", _BRACKET_RE.sub("", _TAG_RE.sub("", sentence))).strip()


def split_sentences(text: str) -> list[str]:
    """Sentences of a paragraph, keeping each one's own `[S1.3]` markers."""
    protected = text
    for index, abbreviation in enumerate(_PROTECTED):
        protected = protected.replace(abbreviation, f"\x00{index}\x00")
    pieces: list[str] = []
    for piece in _SENTENCE_RE.split(protected.strip()):
        for index, abbreviation in enumerate(_PROTECTED):
            piece = piece.replace(f"\x00{index}\x00", abbreviation)
        piece = piece.strip()
        if not piece:
            continue
        # A marker that landed after the full stop belongs to the sentence before it.
        lead = _LEADING_MARKER_RE.match(piece)
        if pieces and lead:
            pieces[-1] = f"{pieces[-1]} {lead.group(1)}"
            piece = lead.group(2).strip()
            if not piece:
                continue
        pieces.append(piece)
    return pieces


def content_tokens(sentence: str) -> list[str]:
    return tokenize(normalise_numbers(strip_refs(sentence)))


def has_specifics(sentence: str) -> bool:
    """A figure, a quoted term, a name, or an absolute: the kinds of statement
    that are wrong in ways that matter."""
    plain = strip_refs(sentence)
    if re.search(r"\d", normalise_numbers(plain)) or '"' in plain or "“" in plain:
        return True
    if _ABSOLUTE_RE.search(plain):
        return True
    words = plain.split()
    return any(w[:1].isupper() and w.strip(".,;:()") not in {"I", "A"} and len(w) > 2 for w in words[1:])


def is_candidate(sentence: str) -> bool:
    plain = strip_refs(sentence)
    if plain.endswith("?") or _CODE_LIKE_RE.search(plain):
        return False
    return len(set(content_tokens(sentence))) >= MIN_CONTENT_TOKENS


# --- searching the passages ------------------------------------------------------


class PassageIndex:
    """An inverted index over the course's passages, built once per audit."""

    def __init__(self, library: SourceLibrary) -> None:
        self.library = library
        self.tokens: dict[str, set[str]] = {}
        self.postings: dict[str, set[str]] = defaultdict(set)
        for ref, (_record, chunk) in library.passages.items():
            tokens = set(tokenize(normalise_numbers(chunk.embed_text())))
            self.tokens[ref] = tokens
            for token in tokens:
                self.postings[token].add(ref)
        self._total = max(len(self.tokens), 1)

    def containment(self, sentence_tokens: set[str], ref: str) -> float:
        if not sentence_tokens or ref not in self.tokens:
            return 0.0
        return len(sentence_tokens & self.tokens[ref]) / len(sentence_tokens)

    def missing_numbers(self, sentence_tokens: set[str], ref: str) -> list[str]:
        """Figures the sentence states that the passage does not. Wording can
        match closely while the number differs ("14 days" against "30 days"),
        so a figure is checked on its own."""
        passage = self.tokens.get(ref, set())
        return sorted((t for t in sentence_tokens if t.isdigit() and t not in passage), key=int)

    def best(self, sentence_tokens: set[str], limit: int = PASSAGES_PER_SENTENCE) -> list[tuple[str, float]]:
        """The passages sharing the sentence's rarest words, with how much of
        the sentence each one contains."""
        weight: dict[str, float] = defaultdict(float)
        for token in sentence_tokens:
            refs = self.postings.get(token)
            if not refs:
                continue
            idf = math.log(1 + self._total / len(refs))
            for ref in refs:
                weight[ref] += idf
        top = sorted(weight, key=lambda ref: weight[ref], reverse=True)[:limit]
        scored = [(ref, self.containment(sentence_tokens, ref)) for ref in top]
        return sorted(scored, key=lambda item: item[1], reverse=True)


def best_excerpt(passage_text: str, sentence_tokens: set[str]) -> str:
    """The part of a passage that answers the sentence, not just its opening."""
    options = split_sentences(passage_text) or [passage_text]
    best = max(
        options,
        key=lambda option: len(sentence_tokens & set(tokenize(normalise_numbers(option)))),
    )
    return best if len(best) <= EXCERPT_CHARS else best[: EXCERPT_CHARS - 1].rstrip() + "…"


def _evidence(index: PassageIndex, refs: list[str], sentence_tokens: set[str]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for ref in refs:
        excerpt = index.library.excerpt(ref)
        if excerpt is None:
            continue
        evidence.append(
            Evidence(
                ref=ref,
                filename=excerpt.filename,
                page=excerpt.page,
                heading=excerpt.heading,
                excerpt=best_excerpt(excerpt.text, sentence_tokens),
            )
        )
    return evidence


def finding_id(block_id: str, sentence: str, kind: str) -> str:
    """Stable across runs, so a reviewer's decision survives a re-audit."""
    digest = hashlib.sha1(f"{block_id}|{strip_refs(sentence)}|{kind}".encode("utf-8")).hexdigest()
    return digest[:12]


# --- the audit -------------------------------------------------------------------


@dataclass
class AuditResult:
    findings: list[AuditFinding] = field(default_factory=list)
    sentences_checked: int = 0
    cited_sentences: int = 0
    supported_sentences: int = 0


def audit_blocks(blocks: list[AuditBlock], library: SourceLibrary) -> AuditResult:
    """Check every factual-looking sentence against the passages."""
    index = PassageIndex(library)
    result = AuditResult()

    for block in blocks:
        cursor = 0
        for sentence in split_sentences(block.text):
            found = block.text.find(sentence, cursor)
            span = (found, found + len(sentence)) if found >= 0 else (None, None)
            if found >= 0:
                cursor = found + len(sentence)
            if not is_candidate(sentence):
                continue
            result.sentences_checked += 1
            refs = extract_refs(sentence)
            tokens = set(content_tokens(sentence))
            plain = strip_refs(sentence)

            def add(kind: str, shown_refs: list[str], explanation: str) -> None:
                result.findings.append(
                    AuditFinding(
                        finding_id=finding_id(block.block_id, sentence, kind),
                        block_id=block.block_id,
                        page_number=block.page_number,
                        chapter=block.chapter,
                        sentence=plain,
                        start=span[0],
                        end=span[1],
                        kind=kind,  # type: ignore[arg-type]
                        severity=KIND_SEVERITY[kind],  # type: ignore[arg-type]
                        refs=refs,
                        evidence=_evidence(index, shown_refs, tokens),
                        explanation=explanation,
                    )
                )

            if refs:
                result.cited_sentences += 1
                missing = [ref for ref in refs if ref not in library.passages]
                valid = [ref for ref in refs if ref in library.passages]
                if missing:
                    add(
                        "invalid_citation",
                        valid,
                        "Cites " + ", ".join(missing) + ", which "
                        + ("is not a passage of the course's sources." if len(missing) == 1 else "are not passages of the course's sources."),
                    )
                    continue
                best_ref, best = max(
                    ((ref, index.containment(tokens, ref)) for ref in valid), key=lambda item: item[1]
                )
                wrong = index.missing_numbers(tokens, best_ref)
                if best >= CITED_SUPPORTED_AT and wrong:
                    add(
                        "weak_citation",
                        [best_ref],
                        "The cited passage does not contain the figure " + ", ".join(wrong) + " stated here.",
                    )
                elif best < CITED_SUPPORTED_AT:
                    # Show what the writer cited, and what the sources actually say about it.
                    nearest = [ref for ref, _ in index.best(tokens, 1) if ref not in valid]
                    add(
                        "weak_citation",
                        valid[:2] + nearest[:1],
                        f"The cited passage shares only {round(best * 100)}% of this sentence's wording, "
                        "so it may not support it.",
                    )
                else:
                    result.supported_sentences += 1
                continue

            ranked = index.best(tokens)
            top_ref, top = ranked[0] if ranked else ("", 0.0)
            wrong = index.missing_numbers(tokens, top_ref) if top_ref else []
            if top >= SUPPORTED_AT and not wrong:
                result.supported_sentences += 1
                continue
            shown = [top_ref] if top_ref and top >= 0.2 else []
            if wrong and shown:
                add(
                    "unsupported",
                    shown,
                    "Close to a passage, but the figure " + ", ".join(wrong) + " is not in it.",
                )
            elif has_specifics(sentence):
                add(
                    "unsupported",
                    shown,
                    "A specific claim (a figure, name or absolute) that no passage of the sources "
                    "clearly states.",
                )
            else:
                add(
                    "assumption",
                    shown,
                    "General content the sources do not cover. Confirm it is accurate.",
                )

    result.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.chapter, f.page_number or 0))
    return result


# --- merging what the model decided ---------------------------------------------

_VERIFIABLE_KINDS = {"weak_citation", "unsupported", "assumption"}


def verification_candidates(findings: list[AuditFinding], limit: int) -> list[AuditFinding]:
    """The doubtful findings a model should read, most serious first. An
    invalid citation is a plain fact (the reference does not exist), so it is
    never put to a model."""
    doubtful = [f for f in findings if f.kind in _VERIFIABLE_KINDS and f.status == "open"]
    return sorted(doubtful, key=lambda f: (SEVERITY_ORDER[f.severity], -len(f.evidence)))[:limit]


def apply_verdicts(findings: list[AuditFinding], verdicts: dict[str, ClaimVerdict]) -> list[AuditFinding]:
    """Overturn, confirm or escalate findings by what the model concluded.

    A model may only act on a finding it was actually shown: an id it invents
    changes nothing.
    """
    merged: list[AuditFinding] = []
    for finding in findings:
        verdict = verdicts.get(finding.finding_id)
        if verdict is None:
            merged.append(finding)
            continue
        reason = verdict.reason.strip()
        if verdict.verdict in {"supported", "not_a_claim"}:
            continue  # the model read it against the evidence and it holds
        updated = finding.model_copy(update={"verified_by": "ai"})
        if verdict.verdict == "contradicted":
            updated.kind, updated.severity = "contradicted", "high"
            updated.explanation = reason or "The sources say something different."
        elif verdict.verdict == "unsupported":
            if finding.kind == "assumption" and not has_specifics(finding.sentence):
                updated.kind, updated.severity = "assumption", "low"
            else:
                updated.kind, updated.severity = "unsupported", "medium"
            updated.explanation = reason or finding.explanation
        else:  # partial
            updated.explanation = ("Only partly supported. " + reason).strip()
        updated.finding_id = finding.finding_id  # an escalated kind keeps its identity
        merged.append(updated)
    merged.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.chapter, f.page_number or 0))
    return merged
