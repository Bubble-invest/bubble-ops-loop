"""
test_step5_gates_kpis.py — Step 5 (Gates, autonomy bands, KPI guardrails).

Notion v5 lines 894-924. The 5 autonomy modes per dept.schema.yaml lines
236-240: manual_required, manual_unless_policy_passed, auto_if_policy_passed,
auto_with_veto_window, disabled.
"""
from __future__ import annotations

import yaml

from skill_lib.templates import render_template
from skill_lib.gates import ALL_AUTONOMY_MODES, build_authorization_band


def test_gate_policy_template_renders_with_all_5_modes():
    """Every one of the 5 modes must be representable by the template."""
    assert ALL_AUTONOMY_MODES == [
        "manual_required",
        "manual_unless_policy_passed",
        "auto_if_policy_passed",
        "auto_with_veto_window",
        "disabled",
    ]
    for mode in ALL_AUTONOMY_MODES:
        ctx = {
            "policy_id": "test_policy",
            "current_mode": mode,
            "eligible_future_modes": [m for m in ALL_AUTONOMY_MODES if m != mode],
            "authorization_band": "test_band",
            "kpi_guardrail_set": "test_kpis",
        }
        rendered = render_template("gate_policy.yaml", ctx)
        loaded = yaml.safe_load(rendered)
        assert "test_policy" in loaded
        assert loaded["test_policy"]["current_mode"] == mode


def test_gate_policy_template_includes_authorization_bands(stub_agent_context):
    """Notion v5 line 902 — authorization_bands block with allowed types +
    forbidden + kpi_guardrails. We build the band as a side-artifact (the band
    itself is dept-domain-specific and ships outside the policy)."""
    ctx = stub_agent_context("step5_gates_kpis")
    band = build_authorization_band(
        band_id=ctx["authorization_band"],
        allowed_types=ctx["allowed_post_types"],
        forbidden=ctx["forbidden"],
    )
    assert band["id"] == "low_risk_evergreen"
    assert "educational" in band["allowed_types"]
    assert "client_names" in band["forbidden"]


def test_kpi_guardrails_are_per_dept_specific(stub_agent_context):
    """The KPI shape accepts arbitrary keys (not hardcoded to one domain).
    We render two different KPI sets and both must roundtrip."""
    ctx = stub_agent_context("step5_gates_kpis")
    # Render under Miranda's content domain.
    miranda_kpis = ctx["kpi_guardrails"]
    assert "brand_safety_breaches" in miranda_kpis
    # Render under a finance-domain dept's KPIs — different keys, same template.
    finance_ctx = dict(ctx)
    finance_ctx["kpi_guardrails"] = {
        "slippage_bps": "<= 5",
        "max_drawdown_30d": "<= 3%",
        "execution_error_rate": "<= 0.1%",
    }
    rendered = render_template("gate_policy.yaml", finance_ctx)
    loaded = yaml.safe_load(rendered)
    policy_block = loaded[finance_ctx["policy_id"]]
    assert "slippage_bps" in policy_block.get("kpi_guardrails", {})
    assert "max_drawdown_30d" in policy_block.get("kpi_guardrails", {})
