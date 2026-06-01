"""
test_step7_activation.py — Step 7 (Activation).

Notion v5 lines 947-977. Status flips onboarding -> live; the agent opens a PR
titled "Activate <DisplayName> department" whose body summarises every
validated step.
"""
from __future__ import annotations

import yaml

from skill_lib.activation import flip_status_to_live, build_activation_pr_body
from skill_lib.templates import render_template


def test_activation_flips_status_to_live(stub_agent_context, tmp_dept_repo):
    """The dept.yaml's `department.status` field flips onboarding -> live."""
    # Build a starter dept.yaml with status=onboarding.
    ctx = stub_agent_context("step1_mandate")
    rendered = render_template("dept.yaml", ctx)
    dept_file = tmp_dept_repo / "dept.yaml"
    dept_file.write_text(rendered, encoding="utf-8")
    pre = yaml.safe_load(dept_file.read_text(encoding="utf-8"))
    assert pre["department"]["status"] == "onboarding"
    # Flip.
    flip_status_to_live(dept_file)
    post = yaml.safe_load(dept_file.read_text(encoding="utf-8"))
    assert post["department"]["status"] == "live"


def test_activation_creates_pr_with_summary_of_all_steps(stub_agent_context):
    ctx = stub_agent_context("step7_activation")
    body = build_activation_pr_body(
        display_name=ctx["display_name"],
        slug=ctx["slug"],
        validated_steps=ctx["validated_steps"],
    )
    # Title format per Notion v5 line 975.
    assert ctx["display_name"] in body
    # All 7 step labels surfaced.
    for step in ctx["validated_steps"]:
        assert step in body
    # Branch convention per Notion v5 line 964.
    assert "onboarding/miranda" in body
