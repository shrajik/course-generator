"""Deterministic, local OCR-based text-accuracy check for generated
`textbook`-style images (Best Candidate Image Tracking / OCR phase).

This module is a companion to, never a replacement for, the existing
vision-model checks in `image_service.py` (`_verify_generated_text`'s
declared-label transcription, and `ImageRelevanceCheck.text_accurately_
spelled`'s holistic judgment). Those checks ask an LLM "does this look
right"; this module asks a different, narrower, fully deterministic
question: "does a specific OCR-extracted word closely but imperfectly match
a specific piece of KNOWN-CORRECT ground truth (a declared expected label,
or a word actually present in the course's own source text)". It never
judges scientific correctness, diagram structure, arrows, relationships or
relevance - that remains entirely Vision QA's job, untouched.

Modeled on `numeric_consistency_qa.py`'s own shape and philosophy: a small,
narrow, explicitly-named issue type; conservative by construction (silent
whenever ground truth doesn't clearly exist); never a scientific-truth
claim. An OCR-confirmed mismatch here is a consistency claim ("this word
doesn't match the known-correct spelling"), not an independent judgment
that the underlying image is otherwise good or bad.

Hard limitation, by design, never worked around: Tesseract (via
`pytesseract`) is a general-purpose printed-Latin-script OCR engine, not a
math/chemistry recognizer. It is NOT authoritative for Greek letters,
subscripts/superscripts, chemical formulas, or dense mathematical notation -
`_is_formula_like` exists specifically to keep this module silent on that
content rather than risk a false "mismatch" on a legitimate symbol it
cannot read reliably. `formula_qa.py` remains the sole, separate authority
on formula correctness and is never touched or duplicated here.

Never raises. OCR initialization, every Tesseract call, and every
preprocessing step are wrapped in broad exception handling - a missing
`tesseract` binary, a corrupt image, a timeout, or any other failure
degrades to `OcrOutcome.UNCERTAIN` (via `run_ocr` returning `None`) so the
rest of the pipeline (Vision QA, Strategy A/D, Best Candidate Image
Tracking) continues exactly as it already does today, completely
unaffected - an OCR failure must never masquerade as any kind of quality
signal, positive or negative.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised via the real container rebuild, not unit tests
    import pytesseract
    from PIL import Image

    _PYTESSERACT_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - genuinely any import-time failure must degrade, not crash
    pytesseract = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    _PYTESSERACT_IMPORT_ERROR = exc


# Below this, a word's OCR confidence is treated as too unreliable to ever
# back a CONFIRMED verdict - stays UNCERTAIN instead, never FAIL. Tesseract
# reports 0-100 per word; this threshold is deliberately conservative (a
# real correct word rendered in an unusual font can score lower than this
# without being wrong) - the cost of a missed detection is far lower than
# the cost of a false "confirmed" claim on a genuinely well-spelled label.
_MIN_CONFIDENT_CONFIDENCE = 40.0

# A near-miss must be close enough to be plausibly the SAME intended word
# gone wrong (a garbled rendering), not merely similar-looking vocabulary -
# below this ratio, two words are just different, not "the same word
# misspelled", and are never compared at all. Applies to `expected_label`-
# sourced ground truth - a block's own declared labels are deliberately
# curated (a human/writer chose exactly these words for this image), so a
# 0.55 near-miss there is trustworthy evidence of real garbling, not
# coincidence (confirmed real case: "Oucupy" vs "Occupy", ratio 0.83).
_NEAR_MISS_MIN_RATIO = 0.55
# `source_text`-sourced ground truth is NOT curated the same way - it is
# every sufficiently-long word anywhere in the image's own chapter/section
# prose (see `chapter_section_context`'s own docstring on why that's the
# right scope for the SEPARATE source-grounding/contradiction check this
# module's ground truth is borrowed from), which can and does contain
# several genuinely different sentences about different sub-points. A real,
# confirmed false positive (live production course, "Motional Emf
# Derivation"): the real label "present" (from "No battery present...", a
# correct, legitimate expected_labels-shaped string already confirmed
# correct elsewhere on the image) was compared against "release" - a real
# word, but from a completely unrelated sentence two paragraphs earlier
# ("if you release a charge..."). Both pairs happen to share
# `_NEAR_MISS_MIN_RATIO`'s floor exactly (0.571), the SAME ratio as the
# earlier-fixed "rings"/"terminals" coincidence - but this one isn't a
# coverage problem (OCR genuinely read "present" just fine), so the
# coverage fix above cannot catch it. A stricter floor for this specific,
# less-trustworthy ground-truth source closes it without affecting the
# curated `expected_labels` pool at all - every existing confirmed real
# near-miss sourced from source_text ("oucupy" vs "occupy", ratio 0.83)
# clears this comfortably; "present" vs "release" (0.571) does not.
_NEAR_MISS_MIN_RATIO_SOURCE_TEXT = 0.72
# At or above this ratio the words are considered the same (OCR noise only,
# e.g. a stray comma Tesseract attached) - never flagged, since there is no
# real discrepancy to report.
_EXACT_MATCH_MIN_RATIO = 0.97

# Trivial suffix variants (plain singular/plural - "force"/"forces",
# "voltage"/"voltages") are NOT a text defect; they are two equally-correct
# words, and treating one as "wrong" because the OTHER happens to appear
# verbatim in the source prose is exactly as confirmed, real, and wrong as
# the cases above (same live course: "force" flagged against source text's
# "forces", ratio 0.91; "voltage" against "voltages", ratio 0.93 - both
# comfortably clear every similarity floor above, so only an explicit
# suffix-aware equality check - not a stricter ratio - can catch this
# class). Deliberately just a trailing "s"/"es" strip, not real stemming or
# a new NLP dependency - this module's whole philosophy (see module
# docstring) is "small, deterministic, dependency-free, conservative";
# matching the exact two confirmed real cases is enough, not an attempt at
# a general English pluralization engine.
def _singular_plural_equivalent(a: str, b: str) -> bool:
    if a == b or len(a) < 3 or len(b) < 3:
        return False
    return a + "s" == b or b + "s" == a or a + "es" == b or b + "es" == a

# Real, confirmed false-positive case (live verification run, a dense
# 3-panel AC-generator illustration): Tesseract detected only a small
# fraction of the actual visible text - several real, correctly-spelled
# labels ("Terminals", "Rotation") were never read at all, not misread.
# Because those labels were simply absent from OCR's own output, the near-
# miss search compared them against unrelated OCR words that happened to
# clear `_NEAR_MISS_MIN_RATIO` by a small margin ("rings" vs "terminals",
# 0.571) - a stray, unrelated word standing in as "evidence" that a
# perfectly good label was wrong. The fix: before trusting ANY near-miss
# verdict, confirm OCR's overall coverage of this image was substantial
# enough that "if a label's text were really there, OCR would likely have
# found something near it" is a reasonable inference. Below this ratio -
# `len(ocr_result.words)` (ALL raw detections, unfiltered - a genuine
# measure of how much OCR activity happened on this image, independent of
# whether any of it was useful for matching) against the total word-count
# across every ground-truth phrase being checked - near-miss matching is
# skipped entirely for this call; every missing label stays UNCERTAIN
# (no finding), never guessed at from an incomplete word pool. Deliberately
# NOT based on how many labels were successfully matched (that would be
# circular - a single genuinely-misspelled label with nothing else to check
# against always looks like "0% confirmed" even when OCR read it perfectly
# fine, just wrong - confirmed via `test_misspelled_declared_label_is_
# confirmed`'s own real numbers, which must keep working unchanged).
_MIN_COVERAGE_RATIO = 0.5

# Small, deliberately conservative allowlist of legitimate scientific
# terminology/units/symbol-names that must never be treated as a mismatch
# just because they are not the specific word being checked against -
# covers the real false-positive risk this module's own docstring and the
# user's spec both call out explicitly (chemical symbols, SI units, Greek
# letter names, common physics/chemistry vocabulary). Deliberately small -
# this is a safety net against mislabelling a KNOWN-good word as wrong, not
# an attempt at an exhaustive scientific dictionary (a generic dictionary
# is explicitly disallowed by the spec).
SCIENTIFIC_TERM_ALLOWLIST: frozenset[str] = frozenset({
    "electron", "proton", "neutron", "nucleus", "nuclei", "atom", "atomic", "ion", "isotope",
    "orbital", "valence", "molecule", "molecular", "covalent", "ionic", "catalyst", "reagent",
    "emf", "ohm", "ohms", "volt", "volts", "ampere", "amperes", "watt", "watts", "joule", "joules",
    "newton", "newtons", "pascal", "pascals", "hertz", "coulomb", "coulombs", "farad", "henry",
    "tesla", "kelvin", "mole", "moles", "candela", "radian", "steradian",
    "alpha", "beta", "gamma", "delta", "epsilon", "theta", "lambda", "mu", "nu", "pi", "rho",
    "sigma", "tau", "phi", "chi", "psi", "omega", "zeta", "eta", "kappa", "iota", "upsilon", "xi",
    "flux", "torque", "velocity", "acceleration", "momentum", "frequency", "wavelength", "amplitude",
    "resistor", "capacitor", "inductor", "diode", "transistor", "voltmeter", "ammeter", "oscilloscope",
    "ph", "naoh", "nacl", "hcl", "h2o", "co2", "o2", "n2", "h2", "ch4", "nh3",
})


def _is_formula_like(text: str) -> bool:
    """True when `text` looks like dense math/chemistry/symbol notation
    rather than a plain English label - Tesseract is not authoritative for
    this content (see module docstring), so matching logic must stay
    completely silent on it rather than risk a false mismatch. Deliberately
    broad/conservative: any hint of formula-shaped content opts out."""
    if not text:
        return False
    # Greek letters, subscript/superscript digits, common math operators.
    if re.search(r"[Α-Ωα-ω₀-₉²³¹±√∫∑∏≤≥≈∞∂∇×÷]", text):
        return True
    # A short token with digits mixed into letters (e.g. "H2O", "CO2",
    # "Fe3+", "x^2") reads as a formula/chemical-notation fragment, not a
    # misspelled word - true whether or not an operator is also present.
    if len(text) <= 12 and re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", text):
        return True
    # A short equation-shaped string with no genuine natural-language word
    # in it (e.g. "F = ma", "PV = nRT") - an "=" alongside only short
    # variable-name-like tokens reads as a formula, not prose that merely
    # contains a punctuation mark.
    if "=" in text and len(text) <= 24 and not re.search(r"\b[A-Za-z]{5,}\b", text):
        return True
    return False


def _normalize(text: str) -> str:
    """Case/punctuation/whitespace-insensitive comparison key - mirrors
    `ImageService._verify_generated_text`'s own `normalize` lambda, applied
    here to OCR output instead of a vision transcription."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float  # 0-100, Tesseract's own scale; -1.0 means "not reported"
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OcrResult:
    words: list[OcrWord] = field(default_factory=list)
    average_confidence: float = 0.0

    @property
    def joined_text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class OcrMismatch:
    """One confirmed, ground-truth-backed mismatch - the `(wrong, correct)`
    pair plus enough context for a human-readable finding detail. `source`
    records which ground truth confirmed it (`"expected_label"` or
    `"source_text"`) purely for diagnostics; it never changes severity."""

    ocr_text: str
    expected_text: str
    confidence: float
    source: str

    @property
    def detail(self) -> str:
        return f"OCR detected '{self.ocr_text}', expected '{self.expected_text}'"


# Process-lifetime, bounded cache keyed by the image's own content hash -
# not a new persistent cache layer (nothing is written to disk or survives
# a restart), just avoiding redundant Tesseract calls when the SAME bytes
# are checked more than once within one generation attempt (the early-exit
# check inside the relevance-retry loop, the final candidate-ranking pass,
# and the post-selection finding-construction step can all reference the
# same candidate's bytes - see `ImageService._generate_illustration`).
# `maxsize` is deliberately small: at most a handful of candidates exist
# per block (1 original + `_MAX_RELEVANCE_RETRIES` retries), so this never
# needs to hold more than a few entries at once.
@lru_cache(maxsize=32)
def _run_ocr_cached(image_bytes: bytes) -> OcrResult | None:
    return _run_ocr_uncached(image_bytes)


def run_ocr(image_bytes: bytes) -> OcrResult | None:
    """Extract recognized words, their bounding boxes and Tesseract's own
    per-word confidence scores from `image_bytes`. Returns `None` - never
    raises, never returns an empty-but-successful result to mean "OCR
    failed" - whenever OCR genuinely could not run at all (no `tesseract`
    binary, corrupt/unreadable image bytes, a pytesseract/PIL exception of
    any kind). `None` must be treated as UNCERTAIN by every caller, never
    as "confirmed no text" or "confirmed clean"."""
    if pytesseract is None:
        return None
    try:
        return _run_ocr_cached(image_bytes)
    except Exception as exc:  # noqa: BLE001 - the cache wrapper itself must never raise either
        log.warning("OCR cache lookup failed, treating as unavailable: %s", exc)
        return None


def _run_ocr_uncached(image_bytes: bytes) -> OcrResult | None:
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Flat 2D vector illustrations (this pipeline's whole textbook-
            # style contract) are already high-contrast, clean line art -
            # no preprocessing (deskew/threshold/denoise) has been found
            # necessary for this image style; converting to RGB avoids a
            # rare Tesseract failure on palette/CMYK images, which is the
            # one genuinely necessary step.
            rgb = img.convert("RGB")
            data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)
    except Exception as exc:  # noqa: BLE001 - any OCR/image failure degrades to "unavailable"
        log.warning("OCR extraction failed, treating as unavailable: %s", exc)
        return None

    words: list[OcrWord] = []
    confidences: list[float] = []
    for i, text in enumerate(data.get("text", [])):
        text = text.strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][i])
        except (ValueError, KeyError, IndexError):
            confidence = -1.0
        if confidence >= 0:
            confidences.append(confidence)
        words.append(OcrWord(
            text=text,
            confidence=confidence,
            left=int(data.get("left", [0])[i] if i < len(data.get("left", [])) else 0),
            top=int(data.get("top", [0])[i] if i < len(data.get("top", [])) else 0),
            width=int(data.get("width", [0])[i] if i < len(data.get("width", [])) else 0),
            height=int(data.get("height", [0])[i] if i < len(data.get("height", [])) else 0),
        ))
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return OcrResult(words=words, average_confidence=average_confidence)


def find_ground_truth_mismatches(
    ocr_result: OcrResult, *, expected_labels: list[str] | None = None, source_text: str = "",
) -> list[OcrMismatch]:
    """The core deterministic matching step. For each piece of real
    ground truth available (a declared `expected_labels` entry, or a
    significant word actually present in `source_text`), checks whether
    OCR's own output contains it (exact/near-exact, case/punctuation-
    insensitive) or only a confident-but-close NEAR MISS of it - a near
    miss is reported as a confirmed mismatch; an exact match is silently
    fine; anything too dissimilar to plausibly be the same word is never
    compared at all (conservative by construction - this is "did a known-
    correct word come out garbled", never "does every OCR word make
    sense").

    Hard rules enforced here, matching the spec exactly:
    - No ground truth at all (`expected_labels` empty AND `source_text`
      blank) -> returns `[]` unconditionally. This app explicitly supports
      "no source documents" courses; free text with nothing to check
      against must stay unverified, never guessed at.
    - A formula-like OCR token or ground-truth string (`_is_formula_like`)
      is never compared - Tesseract is not authoritative for that content.
    - A token matching `SCIENTIFIC_TERM_ALLOWLIST` is never treated as a
      mismatch, even as a "near miss" of some other word.
    - An OCR word with confidence below `_MIN_CONFIDENT_CONFIDENCE` is
      never used to confirm a mismatch (stays silent, not flagged).
    - Sparse overall OCR coverage for this image (see `_MIN_COVERAGE_
      RATIO`) suppresses near-miss matching entirely - a label OCR never
      detected at all must never be "confirmed wrong" via a coincidentally
      similar, unrelated word from elsewhere on the image.
    - A plain singular/plural variant of the exact ground-truth word is
      never a mismatch (see `_singular_plural_equivalent`).
    - `source_text`-sourced ground truth (incidentally extracted from the
      image's chapter/section prose, not curated per-block the way
      `expected_labels` is) requires a stricter near-miss floor than
      `expected_labels` (see `_NEAR_MISS_MIN_RATIO_SOURCE_TEXT`) - a
      coincidental lexical similarity to an unrelated word elsewhere in
      that prose is not evidence of garbling."""
    if not expected_labels and not source_text.strip():
        return []

    ground_truth: list[tuple[str, str]] = []  # (normalized, source_label)
    for label in expected_labels or []:
        normalized = _normalize(label)
        if normalized and not _is_formula_like(label):
            ground_truth.append((normalized, "expected_label"))
    if source_text.strip():
        for raw_word in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", source_text):
            normalized = _normalize(raw_word)
            if normalized and len(normalized) >= 4 and not _is_formula_like(raw_word):
                ground_truth.append((normalized, "source_text"))
    if not ground_truth:
        return []

    # OCR coverage check (false-positive fix - see `_MIN_COVERAGE_RATIO`'s
    # own docstring for the real case this closes): if OCR's total detected-
    # word count is small relative to how much text this image is expected
    # to carry, a "missing" label is far more likely "OCR never looked here
    # closely enough" than "confirmed garbled" - stay silent on every
    # near-miss for this call rather than let a coincidentally-similar,
    # unrelated leftover word stand in as evidence.
    expected_word_count = sum(len(normalized.split()) for normalized, _ in ground_truth)
    actual_word_count = len(ocr_result.words)
    if expected_word_count and (actual_word_count / expected_word_count) < _MIN_COVERAGE_RATIO:
        return []

    ocr_words = [
        w for w in ocr_result.words
        if w.text.strip() and not _is_formula_like(w.text) and _normalize(w.text) not in SCIENTIFIC_TERM_ALLOWLIST
    ]
    ocr_normalized_set = {_normalize(w.text) for w in ocr_words}
    ocr_full_text = _normalize(ocr_result.joined_text)

    mismatches: list[OcrMismatch] = []
    seen_ground_truth: set[str] = set()
    for normalized_truth, source in ground_truth:
        if normalized_truth in seen_ground_truth:
            continue
        # Exact (or substring, for a multi-word label) match anywhere in
        # OCR's output - confirmed fine, nothing to report.
        if normalized_truth in ocr_normalized_set or normalized_truth in ocr_full_text:
            seen_ground_truth.add(normalized_truth)
            continue
        # A plain singular/plural variant (or vice versa) of this exact
        # ground-truth word anywhere in OCR's output is NOT a mismatch - two
        # equally-correct words, never flagged (see `_singular_plural_
        # equivalent`'s own docstring for the confirmed real case this
        # closes). Checked before the fuzzy search below so it short-
        # circuits regardless of which ratio floor would otherwise apply.
        if any(_singular_plural_equivalent(normalized_truth, _normalize(w.text)) for w in ocr_words):
            seen_ground_truth.add(normalized_truth)
            continue
        best_word: OcrWord | None = None
        best_ratio = 0.0
        for ocr_word in ocr_words:
            ratio = difflib.SequenceMatcher(None, normalized_truth, _normalize(ocr_word.text)).ratio()
            if ratio > best_ratio:
                best_ratio, best_word = ratio, ocr_word
        # `source_text`-derived ground truth is incidentally extracted, not
        # curated like `expected_labels` - see `_NEAR_MISS_MIN_RATIO_SOURCE_
        # TEXT`'s own docstring for the confirmed real false positive
        # ("present" vs "release") a shared, lower floor let through.
        near_miss_floor = _NEAR_MISS_MIN_RATIO_SOURCE_TEXT if source == "source_text" else _NEAR_MISS_MIN_RATIO
        if best_word is None or best_ratio < near_miss_floor or best_ratio >= _EXACT_MATCH_MIN_RATIO:
            # Either nothing plausibly close exists (OCR may simply have
            # missed this label entirely - inconclusive, not a mismatch),
            # or it's close enough to be noise, not a real discrepancy.
            continue
        if best_word.confidence >= 0 and best_word.confidence < _MIN_CONFIDENT_CONFIDENCE:
            # A low-confidence near-miss is exactly the "low OCR confidence"
            # case the spec requires to stay UNCERTAIN, never FAIL.
            continue
        seen_ground_truth.add(normalized_truth)
        mismatches.append(OcrMismatch(
            ocr_text=best_word.text, expected_text=normalized_truth, confidence=best_word.confidence, source=source,
        ))
    return mismatches


def ocr_cache_info() -> str:
    """Diagnostics only (reported in verification, not used by any
    decision logic) - the cache's own hit/miss counters."""
    return str(_run_ocr_cached.cache_info())


def image_content_hash(image_bytes: bytes) -> str:
    """Stable identity for logging/diagnostics - not used as the cache key
    itself (`lru_cache` already hashes the bytes directly), just a short,
    loggable fingerprint."""
    return hashlib.sha256(image_bytes).hexdigest()[:12]
