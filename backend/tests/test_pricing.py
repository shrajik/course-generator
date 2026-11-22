"""Real-money cost estimation (app.core.pricing) - exact arithmetic checks
against the module's own documented rate constants, so a future edit to a
rate is caught here if it silently changes the computed cost. See that
module's own docstring for why MAI-Image-2.6 is an estimate and why an
unrecognised model returns None rather than a fabricated 0.
"""

from __future__ import annotations

from app.core.pricing import (
    FLUX_RATE_USD_PER_MEGAPIXEL,
    MAI_IMAGE_ESTIMATED_USD_PER_IMAGE,
    TEXT_MODEL_RATES_USD_PER_1M,
    USD_TO_INR_RATE,
    estimate_call_cost_usd,
    summarize_cost,
)


def test_a_text_model_call_costs_exactly_its_configured_rate():
    input_rate, output_rate = TEXT_MODEL_RATES_USD_PER_1M["gpt-5"]
    cost = estimate_call_cost_usd(
        model="gpt-5", kind="structured", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost is not None
    assert cost.estimated is False
    assert cost.usd == input_rate + output_rate
    assert cost.inr == (input_rate + output_rate) * USD_TO_INR_RATE


def test_a_fractional_token_count_is_priced_proportionally():
    input_rate, _ = TEXT_MODEL_RATES_USD_PER_1M["gpt-5-mini"]
    cost = estimate_call_cost_usd(model="gpt-5-mini", kind="structured", input_tokens=500_000)
    assert cost is not None
    assert cost.usd == input_rate / 2


def test_flux_image_cost_is_exact_megapixels_times_rate():
    cost = estimate_call_cost_usd(model="FLUX.2-flex", kind="image", megapixels=2.0)
    assert cost is not None
    assert cost.estimated is False
    assert cost.usd == 2.0 * FLUX_RATE_USD_PER_MEGAPIXEL


def test_mai_image_cost_uses_the_documented_estimate_and_is_flagged():
    cost = estimate_call_cost_usd(model="MAI-Image-2.6", kind="image", megapixels=1.0)
    assert cost is not None
    assert cost.usd == MAI_IMAGE_ESTIMATED_USD_PER_IMAGE
    assert cost.estimated is True


def test_an_unrecognised_model_returns_none_never_a_fabricated_zero():
    assert estimate_call_cost_usd(model="mock", kind="structured", input_tokens=100) is None
    assert estimate_call_cost_usd(model="some-future-model", kind="image", megapixels=1.0) is None


def test_an_embedding_call_is_priced_input_tokens_only():
    cost = estimate_call_cost_usd(model="text-embedding-3-large", kind="embedding", input_tokens=1_000_000)
    assert cost is not None
    assert cost.usd == 0.13


def test_summarize_cost_attributes_each_model_correctly_and_sums_to_the_total():
    calls = [
        {"model": "gpt-5", "kind": "structured", "input_tokens": 1_000_000, "output_tokens": 0},
        {"model": "gpt-5", "kind": "structured", "input_tokens": 0, "output_tokens": 1_000_000},
        {"model": "FLUX.2-flex", "kind": "image", "megapixels": 1.0},
    ]
    breakdown = summarize_cost(calls)
    expected_gpt5 = round((1.25 + 10.0) * USD_TO_INR_RATE, 2)
    expected_flux = round(FLUX_RATE_USD_PER_MEGAPIXEL * USD_TO_INR_RATE, 2)
    assert breakdown.by_model["gpt-5"] == expected_gpt5
    assert breakdown.by_model["FLUX.2-flex"] == expected_flux
    assert breakdown.total_inr == round(expected_gpt5 + expected_flux, 2)
    assert breakdown.has_unpriced_models is False
    assert breakdown.has_estimated_models is False


def test_summarize_cost_for_zero_usage_is_zero_not_an_error():
    breakdown = summarize_cost([])
    assert breakdown.by_model == {}
    assert breakdown.total_inr == 0.0
    assert breakdown.has_unpriced_models is False


def test_summarize_cost_flags_unpriced_and_estimated_models_separately():
    calls = [
        {"model": "mock", "kind": "structured", "input_tokens": 100},
        {"model": "MAI-Image-2.6", "kind": "image", "megapixels": 1.0},
    ]
    breakdown = summarize_cost(calls)
    assert breakdown.has_unpriced_models is True
    assert breakdown.has_estimated_models is True
    assert "mock" not in breakdown.by_model
    assert breakdown.by_model["MAI-Image-2.6"] == round(
        MAI_IMAGE_ESTIMATED_USD_PER_IMAGE * USD_TO_INR_RATE, 2
    )


def test_a_failed_call_still_counts_toward_cost_not_assumed_free():
    cost = estimate_call_cost_usd(model="gpt-5", kind="structured", input_tokens=1_000_000)
    assert cost is not None and cost.usd > 0
    # summarize_cost doesn't special-case `failed` at all (see its own
    # docstring) - a failed call with real recorded tokens is priced the
    # same as a successful one.
    breakdown = summarize_cost(
        [{"model": "gpt-5", "kind": "structured", "input_tokens": 1_000_000, "failed": True}]
    )
    assert breakdown.total_inr > 0
