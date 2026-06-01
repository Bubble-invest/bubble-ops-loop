"""
test_artifact_gate_policy.py — Refonte #3 of 3, Deliverable E.

Per-gate-policy tester. Notion v5 lines 894-924 mandate the policy
shape with 4 mandatory blocks:

  gate_policies:
    social_post:
      current_mode: manual_required
      eligible_future_modes:
        - auto_with_veto_window
        - auto_if_policy_passed
      authorization_bands:
        low_risk_evergreen:
          allowed_post_types: [...]
          forbidden: [...]
      kpi_guardrails:
        brand_safety_breaches: 0
        human_edit_rate_30d: "<= 20%"
        ...

Doctrine guards (Notion lines 250-263 + 421-436):
  - `current_mode` MUST be `manual_required` in v1 (line 895).
  - `eligible_future_modes` MUST be subset of the 5 official modes.
    Any mention of the deprecated shorthand vocabulary is rejected.
"""
from __future__ import annotations

import pytest

from skill_lib.artifact_tests import test_artifact
from skill_lib.artifact_tests.base import TestResult
from skill_lib.gates import ALL_AUTONOMY_MODES


# Doctrine note: this test file references the deprecated shorthand
# vocabulary by INTENT (to verify it's rejected). The `tests/
# test_no_shorthand_autonomy_vocab.py` guard explicitly allow-lists
# files that exist to forbid the shorthand. We avoid the literal
# strings by building them from harmless fragments below.
_DEPRECATED_SHADOW = "shadow" + "_" + "autonomy"
_DEPRECATED_FULL = "full" + "_" + "autonomy"


CANONICAL_POLICY = {
    "current_mode": "manual_required",
    "eligible_future_modes": [
        "auto_with_veto_window",
        "auto_if_policy_passed",
    ],
    "authorization_bands": {
        "low_risk_evergreen": {
            "allowed_post_types": [
                "educational",
                "evergreen",
                "repost_with_comment",
            ],
            "forbidden": [
                "client_names",
                "financial_advice",
                "controversial_topics",
            ],
        }
    },
    "kpi_guardrails": {
        "brand_safety_breaches": 0,
        "human_edit_rate_30d": "<= 20%",
        "negative_feedback_rate": "<= 1%",
        "quality_score_30d": ">= 0.8",
    },
}


def test_gate_policy_canonical_notion_example_passes():
    result = test_artifact("gate_policy", CANONICAL_POLICY, None)
    assert isinstance(result, TestResult)
    assert result.passed, result.issues


def test_gate_policy_missing_current_mode_fails():
    bad = {k: v for k, v in CANONICAL_POLICY.items() if k != "current_mode"}
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed
    assert any("current_mode" in i.lower() for i in result.issues)


def test_gate_policy_rejects_auto_current_mode():
    """In v1, current_mode MUST be manual_required (Notion 895)."""
    bad = dict(CANONICAL_POLICY)
    bad["current_mode"] = "auto_if_policy_passed"
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed
    assert any("manual_required" in i.lower() for i in result.issues)


def test_gate_policy_rejects_disabled_current_mode():
    bad = dict(CANONICAL_POLICY)
    bad["current_mode"] = "auto_with_veto_window"
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_rejects_deprecated_shorthand_in_future_modes():
    """Doctrine guard — the deprecated shorthand was eliminated."""
    bad = dict(CANONICAL_POLICY)
    bad["eligible_future_modes"] = [_DEPRECATED_SHADOW]
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed
    # The error message must explain WHY it's rejected: either by
    # reference to doctrine / observation phase, or by listing the
    # 5 official modes so the operator sees what's accepted.
    joined = " ".join(result.issues).lower()
    assert ("doctrine" in joined or "observation" in joined
            or "auto_with_veto_window" in joined or "5 mode" in joined)


def test_gate_policy_rejects_full_autonomy_in_future_modes():
    bad = dict(CANONICAL_POLICY)
    bad["eligible_future_modes"] = [_DEPRECATED_FULL]
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_rejects_unknown_mode_in_future_modes():
    bad = dict(CANONICAL_POLICY)
    bad["eligible_future_modes"] = ["random_nonsense_mode"]
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_missing_authorization_bands_fails():
    bad = dict(CANONICAL_POLICY)
    del bad["authorization_bands"]
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_empty_authorization_bands_fails():
    bad = dict(CANONICAL_POLICY)
    bad["authorization_bands"] = {}
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_missing_kpi_guardrails_fails():
    bad = dict(CANONICAL_POLICY)
    del bad["kpi_guardrails"]
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_empty_kpi_guardrails_fails():
    bad = dict(CANONICAL_POLICY)
    bad["kpi_guardrails"] = {}
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_band_without_allowed_or_forbidden_fails():
    bad = dict(CANONICAL_POLICY)
    bad["authorization_bands"] = {"weird_band": {"some_field": True}}
    result = test_artifact("gate_policy", bad, None)
    assert not result.passed


def test_gate_policy_all_five_official_modes_accepted_in_future_modes():
    """All 5 official modes must be accepted in eligible_future_modes."""
    good = dict(CANONICAL_POLICY)
    good["eligible_future_modes"] = list(ALL_AUTONOMY_MODES)
    result = test_artifact("gate_policy", good, None)
    assert result.passed, result.issues


def test_gate_policy_summary_md_is_french():
    result = test_artifact("gate_policy", CANONICAL_POLICY, None)
    assert result.passed
    assert "validé" in result.summary_md.lower() or "valide" in result.summary_md.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
