"""Real-money cost estimation for AI model usage (admin-only - see
app.api.admin's model-usage endpoint and app.schemas.admin.ModelUsageRecord).

This is a COST ESTIMATE, built from each provider's own published list
pricing applied to this app's own recorded usage (app.core.metrics.CallRecord)
- not a reconciled invoice. Three honest limitations, stated once here rather
than re-litigated at every call site:

1. List prices change. `TEXT_MODEL_RATES_USD_PER_1M`/`EMBEDDING_RATES_USD_PER_1M`
   /`FLUX_RATE_USD_PER_MEGAPIXEL`/`USD_TO_INR_RATE` are plain, named constants
   specifically so whoever maintains this can update a number here without
   hunting through call sites - they are NOT re-fetched from a live pricing API
   (deliberately: a static, documented rate beats a new runtime dependency on
   an external pricing/FX service for this).
2. MAI-Image-2.6's real per-image cost CANNOT be computed exactly from this
   app's own data. Microsoft Foundry bills MAI-Image-2.6 per TOKEN (text
   input / image input / image output), but its generation response never
   returns a token-usage field (confirmed directly against the documented
   response shape in app.services.azure_mai_image_provider - only
   `{"data": [{"b64_json": ...}]}`, no `usage`), so this app has no way to
   know the real billed token count. `MAI_IMAGE_ESTIMATED_USD_PER_IMAGE` is a
   documented ESTIMATE (see its own comment for the derivation), not a
   verified flat rate - flagged as such in every response that includes it
   (`estimated=True` on that line of the breakdown).
3. An unrecognised `model` string (e.g. a new deployment not yet added here,
   or "mock" from MockAIClient) returns `None`, never a fabricated 0 or a
   guessed rate - a cost estimate that silently omits a real model would be
   worse than one that visibly admits it doesn't know this model yet.

USD_TO_INR_RATE needs periodic manual review (FX rates move daily) - it is
not pinned to any specific date on purpose, so updating it is a one-line
change here, not a search through the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- FX ------------------------------------------------------------------

# Manually-maintained USD->INR rate. Update this constant directly when it
# drifts meaningfully from the real market rate - deliberately NOT a live FX
# API call (see module docstring, point 1).
USD_TO_INR_RATE = 95.0

# --- Text/embedding model rates, USD per 1,000,000 tokens -----------------
# Keyed by the LOGICAL model name this app itself uses (app.core.config's
# writer_model/fallback_model/deep_research_model etc., e.g. "gpt-5") - NOT
# the Azure deployment name, which is a per-environment detail these calls
# never see (see OpenAIClient._client_for's own docstring on that split).
# Source: each provider's own published list pricing at the time this table
# was written - verify against the live pricing page before trusting an old
# copy of this file for a real budget decision (see module docstring, point 1).

TEXT_MODEL_RATES_USD_PER_1M: dict[str, tuple[float, float]] = {
    # (input_rate, output_rate), USD per 1,000,000 tokens.
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5.5": (5.0, 30.0),
}

EMBEDDING_RATES_USD_PER_1M: dict[str, float] = {
    # Embeddings have no output tokens - a single input-only rate.
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
}

# --- Image model rates -----------------------------------------------------

# FLUX.2-flex on Azure AI Foundry (Global Standard, pay-as-you-go): billed
# per output megapixel, rounded UP to the next whole megapixel (1 megapixel
# == 1024x1024) - see OpenAIClient._megapixels_for, which computes the real
# billed quantity the same way before this rate is ever applied.
FLUX_RATE_USD_PER_MEGAPIXEL = 0.05

# MAI-Image-2.6 (Microsoft Foundry) - see point 2 above: this app cannot see
# the real billed token count, so this is a documented ESTIMATE, not a
# verified flat rate. Derivation: Microsoft's own published per-token rates
# put the standard (non-Flash) model's image-OUTPUT token rate at exactly 2x
# the Flash variant's ($38 vs $19 per 1M tokens); a third-party benchmark
# (Artificial Analysis) measured the Flash variant at ~$19.50 per 1,000
# generated images on Microsoft Foundry. Assuming a comparable token count
# per image across both variants (the only assumption this estimate makes),
# the standard model's real-world per-image cost should be in the same
# neighbourhood as 2x that Flash figure: ~$0.039/image. This number should be
# replaced with a real measurement (or Microsoft's own flat per-image price,
# if one is ever published) rather than trusted indefinitely.
MAI_IMAGE_ESTIMATED_USD_PER_IMAGE = 0.039


@dataclass
class CallCost:
    """One call's estimated cost - `usd` is always a real computation when
    not None; `estimated` is True only for the MAI-Image-2.6 per-image
    approximation (point 2 above), so a caller can visibly flag it rather
    than present it with the same confidence as an exact token-rate
    calculation."""

    usd: float
    estimated: bool = False

    @property
    def inr(self) -> float:
        return self.usd * USD_TO_INR_RATE


def estimate_call_cost_usd(
    *,
    model: str,
    kind: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    megapixels: float = 0.0,
) -> CallCost | None:
    """Estimated USD cost for one real AI call, or None when `model` isn't a
    recognised, priced model (an unrecognised model, or MockAIClient's
    "mock" - never fabricated as 0, see module docstring point 3)."""
    if kind == "image":
        if model == "FLUX.2-flex":
            return CallCost(usd=megapixels * FLUX_RATE_USD_PER_MEGAPIXEL)
        if model == "MAI-Image-2.6":
            return CallCost(usd=MAI_IMAGE_ESTIMATED_USD_PER_IMAGE, estimated=True)
        return None
    if kind == "embedding":
        rate = EMBEDDING_RATES_USD_PER_1M.get(model)
        if rate is None:
            return None
        return CallCost(usd=(input_tokens / 1_000_000) * rate)
    rates = TEXT_MODEL_RATES_USD_PER_1M.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    usd = (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
    return CallCost(usd=usd)


@dataclass
class CostBreakdown:
    """Per-model cost totals for a course's full set of recorded calls (see
    app.api.admin's model-usage endpoint, the only real caller) - INR
    throughout, since that's what the admin-facing UI shows; `by_model` is
    keyed by the same `model` string the raw call list already uses, so a
    reader can cross-reference the two without a name-mapping step.
    `has_unpriced_models`/`has_estimated_models` are flagged separately so
    the UI can show an honest caveat instead of presenting `total_inr` as if
    it accounted for every call."""

    by_model: dict[str, float]
    total_inr: float
    has_unpriced_models: bool
    has_estimated_models: bool


def summarize_cost(calls: list[dict]) -> CostBreakdown:
    """Builds the full per-model INR breakdown for a course's call list (each
    item shaped like `ModelUsageRecord`/`CallRecord.__dict__` - only the
    `model`/`kind`/`input_tokens`/`output_tokens`/`megapixels`/`failed`
    fields are read). A FAILED call is still counted - a real request that
    the provider attempted and rejected mid-way (not merely retried before
    ever reaching the provider) can still be billed by the provider; this
    app has no reliable way to distinguish "never reached the provider" from
    "reached it and failed after real token/image usage" from the fields it
    records today, so a failed call is priced the same as a successful one
    rather than assumed free - the conservative (never-undercount) choice."""
    by_model: dict[str, float] = {}
    has_unpriced = False
    has_estimated = False
    for call in calls:
        cost = estimate_call_cost_usd(
            model=call.get("model", ""),
            kind=call.get("kind", ""),
            input_tokens=call.get("input_tokens", 0) or 0,
            output_tokens=call.get("output_tokens", 0) or 0,
            megapixels=call.get("megapixels", 0.0) or 0.0,
        )
        if cost is None:
            has_unpriced = True
            continue
        if cost.estimated:
            has_estimated = True
        model = call.get("model", "") or "unknown"
        by_model[model] = by_model.get(model, 0.0) + cost.inr
    return CostBreakdown(
        by_model={model: round(amount, 2) for model, amount in by_model.items()},
        total_inr=round(sum(by_model.values()), 2),
        has_unpriced_models=has_unpriced,
        has_estimated_models=has_estimated,
    )
