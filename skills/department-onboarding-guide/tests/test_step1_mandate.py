"""
test_step1_mandate.py — Step 1 of the 7-step eclosure (Mandate).

Notion v5 lines 803-829: the agent clarifies role / owner / outputs / forbidden,
emits a `department:` block. Status: `onboarding` (Notion v5 line 818).
"""
from __future__ import annotations

import yaml

from skill_lib.templates import render_template


def test_mandate_template_renders_with_required_fields(stub_agent_context, schema_validator):
    ctx = stub_agent_context("step1_mandate")
    rendered = render_template("dept.yaml", ctx)
    doc = yaml.safe_load(rendered)
    # Just the department block surfaced at top-level wrapper.
    assert "department" in doc
    dept = doc["department"]
    assert dept["slug"] == "miranda"
    assert dept["level"] == "ops"
    assert dept["mandate"].startswith("Produire")
    # Schema-valid as the department sub-block.
    v = schema_validator("dept")
    # We validate only the department sub-tree by constructing a minimal full
    # doc that satisfies the other required keys with empty/default shells.
    full = {
        "department": dept,
        "layers": {"subscribed": [1]},
        "recurring_missions": [],
        "skills": {},
        "tools": [],
        "gate_policies": {},
        "hierarchy": {
            "level": "ops",
            "parent": None,
            "children": [],
            "visibility": {
                "read_outputs": [],
                "read_risk_kpis": False,
                "read_risk_briefs": False,
                "read_raw_artifacts": False,
                "read_secrets": False,
            },
            "directive_policy": {
                "can_open_priority_prs": False,
                "target_queue": "queues/management/",
                "requires_human_gate_for": [],
            },
        },
        "optional_domain_ledger": None,
    }
    errors = sorted(v.iter_errors(full), key=lambda e: e.path)
    assert not errors, [e.message for e in errors]


def test_mandate_template_status_is_onboarding(stub_agent_context):
    """Notion v5 line 818 says `status: onboarding` during eclosure."""
    ctx = stub_agent_context("step1_mandate")
    rendered = render_template("dept.yaml", ctx)
    doc = yaml.safe_load(rendered)
    assert doc["department"].get("status") == "onboarding"


def test_mandate_template_includes_forbidden_list_when_provided(stub_agent_context):
    ctx = stub_agent_context("step1_mandate")
    rendered = render_template("dept.yaml", ctx)
    doc = yaml.safe_load(rendered)
    forb = doc["department"].get("forbidden", [])
    assert "publier informations confidentielles" in forb
    assert len(forb) == 3


def test_mandate_template_documents_budget_weekly_usd(stub_agent_context):
    """Board #466 (child of #404): the scaffold's dept.yaml template must
    document the new per-dept `budget_weekly_usd:` field (commented-out,
    like `brief_artifacts:` above it) so operators onboarding a new dept know
    it exists — same convention as mission `budget_usd:` but for the WHOLE
    dept's week. It's OPTIONAL: rendering with the stub context (which
    doesn't set it) must still parse to valid YAML with no such key present
    (nothing forces it on)."""
    ctx = stub_agent_context("step1_mandate")
    rendered = render_template("dept.yaml", ctx)
    assert "budget_weekly_usd" in rendered
    doc = yaml.safe_load(rendered)
    # Commented-out by default → not actually a live key in the parsed doc.
    assert "budget_weekly_usd" not in doc["department"]
