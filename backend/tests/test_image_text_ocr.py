"""Unit tests for the OCR phase (`app.services.image_text_ocr`) - a
deterministic, local companion to the existing vision-based text checks in
`image_service.py`. All tests here use synthetic, directly-constructed
`OcrResult`/`OcrWord` fixtures or mocked `pytesseract`/`PIL` calls - never a
live Tesseract invocation or a real Azure call, per the spec's explicit
instruction."""

import io

import pytest

from app.services import image_text_ocr as ocr


def _result(*words: tuple[str, float]) -> ocr.OcrResult:
    ocr_words = [ocr.OcrWord(text=t, confidence=c, left=0, top=0, width=10, height=10) for t, c in words]
    confidences = [c for _, c in words if c >= 0]
    avg = sum(confidences) / len(confidences) if confidences else 0.0
    return ocr.OcrResult(words=ocr_words, average_confidence=avg)


# --- Case 1: correct declared labels --------------------------------------


def test_correct_declared_labels_produce_no_mismatches():
    result = _result(("Occupy", 95.0), ("Electron", 92.0), ("Nucleus", 90.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["Occupy", "Electron", "Nucleus"])
    assert mismatches == []


# --- Case 2: misspelled declared labels -----------------------------------


def test_misspelled_declared_label_is_confirmed():
    result = _result(("Oucupy", 88.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["Occupy"])
    assert len(mismatches) == 1
    assert mismatches[0].ocr_text == "Oucupy"
    assert mismatches[0].expected_text == "occupy"
    assert mismatches[0].source == "expected_label"
    assert "Oucupy" in mismatches[0].detail and "occupy" in mismatches[0].detail


# --- Case 3: multiple spelling mistakes in one image ----------------------


def test_multiple_independent_mismatches_are_all_reported():
    result = _result(("Oucupy", 88.0), ("Elecron", 85.0), ("Nucleus", 95.0))
    mismatches = ocr.find_ground_truth_mismatches(
        result, expected_labels=["Occupy", "Electron", "Nucleus"],
    )
    wrong_texts = {m.ocr_text for m in mismatches}
    assert wrong_texts == {"Oucupy", "Elecron"}  # Nucleus matched exactly, never flagged
    assert len(mismatches) == 2


# --- Case 4: free-text caption with no ground truth -> stays UNVERIFIED ---


def test_no_ground_truth_at_all_never_flags_anything():
    """This app explicitly supports "no source documents" courses - free
    text with no declared labels and no source excerpt must never be
    guessed at; it stays unverified, never auto-FAIL."""
    result = _result(("Sdome", 40.0), ("random", 90.0), ("wrods", 50.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=[], source_text="")
    assert mismatches == []


# --- Case 5: free-text caption WITH matching source ground truth ---------


def test_free_text_mismatch_confirmed_against_source_text_when_available():
    result = _result(("Electrons", 90.0), ("oucupy", 88.0), ("fixed", 90.0), ("levels", 90.0))
    source_text = "Electrons occupy fixed energy levels around the nucleus."
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=[], source_text=source_text)
    assert any(m.ocr_text == "oucupy" and m.source == "source_text" for m in mismatches)


def test_free_text_with_no_source_match_at_all_is_not_flagged():
    """An OCR word that simply isn't close to anything in the source text
    is NOT evidence of a misspelling - it might be unrelated, legitimate
    content this module has no ground truth for. Never guessed at."""
    result = _result(("spaceship", 90.0))
    source_text = "Electrons occupy fixed energy levels around the nucleus."
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=[], source_text=source_text)
    assert mismatches == []


# --- Case 6: valid scientific terminology is never flagged ----------------


@pytest.mark.parametrize("term", ["NaCl", "F = ma", "electron", "ohm", "alpha"])
def test_legitimate_scientific_terms_are_never_flagged(term):
    # A label that LOOKS like it could be a near-miss of something else,
    # but is itself legitimate scientific vocabulary.
    result = _result((term, 90.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["Something Unrelated"])
    assert mismatches == []  # too dissimilar to "Something Unrelated" to ever compare


def test_allowlisted_term_never_flagged_even_as_a_near_miss_of_a_similar_label():
    """A real risk case: OCR reads a legitimate term that happens to be
    textually close to a DIFFERENT expected label - the allowlist must
    still prevent a false mismatch."""
    result = _result(("ohm", 90.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["ohms"])
    # "ohm" is allowlisted, so it is excluded from comparison entirely -
    # the label "ohms" simply goes unconfirmed (inconclusive), not flagged.
    assert mismatches == []


# --- Case 7: low-confidence OCR -> UNCERTAIN, never FAIL ------------------


def test_low_confidence_near_miss_is_never_confirmed():
    result = _result(("Oucupy", 10.0))  # textually a near-miss, but confidence is too low to trust
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["Occupy"])
    assert mismatches == []


# --- Case 8 & 9: OCR init/runtime failures never crash, never flag -------


def test_ocr_init_failure_returns_none_not_a_crash(monkeypatch):
    monkeypatch.setattr(ocr, "pytesseract", None)
    assert ocr.run_ocr(b"irrelevant bytes") is None


def test_ocr_runtime_exception_returns_none_not_a_crash(monkeypatch):
    class _BoomImage:
        @staticmethod
        def open(*_a, **_kw):
            raise OSError("corrupt image data")

    monkeypatch.setattr(ocr, "Image", _BoomImage)
    fake_pytesseract = object()  # anything non-None so the `is None` short-circuit doesn't apply
    monkeypatch.setattr(ocr, "pytesseract", fake_pytesseract)
    ocr._run_ocr_cached.cache_clear()
    assert ocr.run_ocr(b"some bytes that would raise") is None
    ocr._run_ocr_cached.cache_clear()


def test_a_pytesseract_call_exception_is_caught_not_propagated(monkeypatch):
    class _FakeImg:
        def convert(self, _mode):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeImageModule:
        @staticmethod
        def open(_buf):
            return _FakeImg()

    class _FakePytesseract:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(*_a, **_kw):
            raise RuntimeError("tesseract binary not found")

    monkeypatch.setattr(ocr, "Image", _FakeImageModule)
    monkeypatch.setattr(ocr, "pytesseract", _FakePytesseract)
    ocr._run_ocr_cached.cache_clear()
    assert ocr.run_ocr(b"bytes") is None
    ocr._run_ocr_cached.cache_clear()


# --- Case 16: formula/Greek-symbol content is never rejected via OCR ------


@pytest.mark.parametrize("text", ["Φ", "e = -N·dΦ/dt", "H2O", "x^2", "∫|ψ(t)|²", "F = ma", "PV = nRT"])
def test_formula_like_text_is_recognized_and_excluded(text):
    assert ocr._is_formula_like(text) is True


def test_formula_like_expected_label_is_never_compared_against_ocr():
    """A governing-equation label (formula-shaped) must never be run
    through plain-word fuzzy matching - Tesseract is not authoritative for
    this content, so it must stay completely silent on it."""
    result = _result(("e_N_dPhi_dt_garbled", 90.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["e = -N dΦ/dt"])
    assert mismatches == []


def test_formula_like_ocr_word_is_excluded_from_comparison_pool():
    result = _result(("H2O", 90.0), ("Oucupy", 88.0))
    mismatches = ocr.find_ground_truth_mismatches(result, expected_labels=["Occupy"])
    # The formula-like "H2O" token must never be the "closest match" picked
    # for an unrelated label - "Oucupy" (a genuine near-miss) is.
    assert len(mismatches) == 1
    assert mismatches[0].ocr_text == "Oucupy"


# --- Real pytesseract-shaped data parsing (still mocked, deterministic) --


def test_run_ocr_averages_confidence_and_skips_blank_entries(monkeypatch):
    class _FakeImg:
        def convert(self, _mode):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeImageModule:
        @staticmethod
        def open(_buf):
            return _FakeImg()

    class _FakePytesseract:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(_img, output_type):
            return {
                "text": ["", "Occupy", "  ", "Electron"],
                "conf": ["-1", "92", "-1", "88"],
                "left": [0, 10, 0, 50],
                "top": [0, 10, 0, 10],
                "width": [0, 40, 0, 50],
                "height": [0, 12, 0, 12],
            }

    monkeypatch.setattr(ocr, "Image", _FakeImageModule)
    monkeypatch.setattr(ocr, "pytesseract", _FakePytesseract)
    ocr._run_ocr_cached.cache_clear()
    result = ocr.run_ocr(b"some real-shaped bytes")
    assert result is not None
    assert [w.text for w in result.words] == ["Occupy", "Electron"]
    assert result.average_confidence == pytest.approx(90.0)
    ocr._run_ocr_cached.cache_clear()


def test_run_ocr_caches_by_content_so_identical_bytes_only_run_tesseract_once(monkeypatch):
    calls = {"n": 0}

    class _FakeImg:
        def convert(self, _mode):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeImageModule:
        @staticmethod
        def open(_buf):
            return _FakeImg()

    class _FakePytesseract:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(_img, output_type):
            calls["n"] += 1
            return {"text": ["Occupy"], "conf": ["90"], "left": [0], "top": [0], "width": [10], "height": [10]}

    monkeypatch.setattr(ocr, "Image", _FakeImageModule)
    monkeypatch.setattr(ocr, "pytesseract", _FakePytesseract)
    ocr._run_ocr_cached.cache_clear()
    data = b"identical bytes used twice"
    ocr.run_ocr(data)
    ocr.run_ocr(data)
    assert calls["n"] == 1  # the second call was served from cache, not a new Tesseract invocation
    ocr._run_ocr_cached.cache_clear()
