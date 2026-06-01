"""runtime_read policy class tests — read-only in declared repos."""

from __future__ import annotations


def test_runtime_read_allows_read_in_own_repo(ops_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_read",
    )
    assert allowed, f"runtime_read in own repo must be allowed: {reasons}"


def test_runtime_read_allows_shared_wiki(ops_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, _ = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-shared-wiki",
        action="runtime_read",
    )
    assert allowed


def test_runtime_read_denies_unlisted_repo(ops_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-tony",
        action="runtime_read",
    )
    assert not allowed
    assert any("not in policy read list" in r for r in reasons)
