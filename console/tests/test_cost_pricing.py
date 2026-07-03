"""
test_cost_pricing.py — the /costs pricing table + unknown-model fallback (board #499).

Board card #496's cost reconciliation found the cockpit's _DEFAULT_PRICING table
had gone stale: opus was priced at the RETIRED Opus-3 rate ($15/$75) — a ~3×
overstatement on the fleet's dominant model — there was no `fable` key (so
claude-fable-5 fell through to the sonnet default), and unknown/non-Anthropic
models (deepseek) were also billed at the sonnet rate instead of $0.

These tests pin the corrected per-1M rates + the zero-cost fallback and prove the
table flows through the real cost math (_cost_split).
"""
from __future__ import annotations

import pytest

from console.services import cost_tracker
from console.services.cost_tracker import _DEFAULT_PRICING, _price_for_model, _cost_split


def test_opus_priced_at_current_rate_not_retired_opus3():
    """claude-opus-4-8 → $5/$25 (Opus-4), NOT the retired Opus-3 $15/$75."""
    r = _price_for_model("claude-opus-4-8", _DEFAULT_PRICING)
    assert r["input"] == 5.0
    assert r["output"] == 25.0
    # explicitly guard against the stale rate regressing back in
    assert r["input"] != 15.0 and r["output"] != 75.0


def test_fable_has_its_own_rate_not_sonnet_fallback():
    """claude-fable-5 → $10/$50, not the sonnet ($3/$15) default it used to hit."""
    r = _price_for_model("claude-fable-5", _DEFAULT_PRICING)
    assert r["input"] == 10.0
    assert r["output"] == 50.0


def test_sonnet_and_haiku_unchanged():
    s = _price_for_model("claude-sonnet-5", _DEFAULT_PRICING)
    assert (s["input"], s["output"]) == (3.0, 15.0)
    s46 = _price_for_model("claude-sonnet-4-6", _DEFAULT_PRICING)
    assert (s46["input"], s46["output"]) == (3.0, 15.0)
    h = _price_for_model("claude-haiku-4-5", _DEFAULT_PRICING)
    assert (h["input"], h["output"]) == (1.0, 5.0)


def test_unknown_model_falls_to_zero_not_sonnet():
    """deepseek / any non-matching model → all-zero rate, so it's never
    silently over-billed at an Anthropic rate."""
    r = _price_for_model("deepseek-v4-pro", _DEFAULT_PRICING)
    assert r == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def test_cache_rates_derived_from_input():
    """cache_read = 0.1 × input, cache_write = 1.25 × input, per key."""
    for key, rates in _DEFAULT_PRICING.items():
        assert rates["cache_read"] == pytest.approx(0.1 * rates["input"], abs=1e-9), key
        assert rates["cache_write"] == pytest.approx(1.25 * rates["input"], abs=1e-9), key


def test_no_pricing_key_is_a_substring_of_another():
    """_price_for_model returns the FIRST key that is a substring of the model
    name — so no key may be a substring of another or the lookup is ambiguous."""
    keys = list(_DEFAULT_PRICING.keys())
    for a in keys:
        for b in keys:
            if a != b:
                assert a not in b, f"{a!r} is a substring of {b!r}"


def test_cost_split_flows_new_opus_rate_end_to_end():
    """Feed a known opus usage dict through _cost_split and assert the computed
    real-$ and cache-$ match the hand-calc at the CURRENT opus rate ($5/$25 in,
    $0.50/$6.25 cache) — proves the table actually reaches the cost math."""
    model_usage = {
        "claude-opus-4-8": {
            "input": 200_000,       # 0.2M × $5   = $1.00
            "output": 100_000,      # 0.1M × $25  = $2.50   -> real = $3.50
            "cache_read": 50_000,   # 0.05M × $0.50 = $0.025
            "cache_create": 40_000, # 0.04M × $6.25 = $0.25  -> cache = $0.275
        }
    }
    split = _cost_split(model_usage, _DEFAULT_PRICING)
    assert split["real"] == pytest.approx(3.50, abs=1e-6)
    assert split["cache"] == pytest.approx(0.275, abs=1e-6)


def test_cost_split_zeroes_unknown_model_end_to_end():
    """An unknown model contributes $0 to both real and cache through the full
    _cost_split path (not a phantom sonnet charge)."""
    model_usage = {
        "deepseek-v4-pro": {
            "input": 1_000_000,
            "output": 1_000_000,
            "cache_read": 1_000_000,
            "cache_create": 1_000_000,
        }
    }
    split = _cost_split(model_usage, _DEFAULT_PRICING)
    assert split == {"real": 0.0, "cache": 0.0}
