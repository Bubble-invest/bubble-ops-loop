"""open_priority_pr policy class tests — Tony class, branch-prefixed PRs only.

Notion v4 line 621: branch prefix `tony/directive/`, target path
`queues/management/**`, no direct push to main.
"""

from __future__ import annotations


def test_tony_can_open_pr_to_child_dept_queues_management(tony_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(tony_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-tony",
        repo="bubble-ops-ben",
        action="open_priority_pr",
        paths=["queues/management/directive-2026-05-20-001.yaml"],
    )
    assert allowed, f"Tony->Ben directive PR must be allowed: {reasons}"


def test_tony_cannot_open_priority_pr_outside_queues_management(tony_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(tony_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-tony",
        repo="bubble-ops-ben",
        action="open_priority_pr",
        paths=["outputs/2026-05-20/1/summary.md"],
    )
    assert not allowed
    assert any("queues/management" in r for r in reasons)


def test_tony_cannot_open_priority_pr_to_unauthorized_repo(tony_policy_yaml):
    """can_open_to does not include 'bubble-ops-loop' (template repo)."""
    from src.policy import Policy

    p = Policy.from_yaml(tony_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-tony",
        repo="bubble-ops-loop",
        action="open_priority_pr",
        paths=["queues/management/foo.yaml"],
    )
    assert not allowed
    assert any("not in can_open_to" in r for r in reasons)


def test_ops_dept_cannot_open_priority_pr(ops_policy_yaml):
    """Per Notion v4 §'Ops department standard': can_open_to = []."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-tony",
        action="open_priority_pr",
        paths=["queues/management/x.yaml"],
    )
    assert not allowed
