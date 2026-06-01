"""Atomicity: if ANY staged path is denied, the WHOLE push is denied.

Rationale (path-exfiltration mitigation): if we let partial pushes through,
an attacker could stage 9 innocuous files + 1 secret file and the guard
would push the 9, leaving the secret in the index. We must fail-closed
on the entire set.
"""

from __future__ import annotations

from src.guard import Guard
from src.policy_loader import load_policy


def test_one_denied_among_many_fails_all(fixture_policy_yaml):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    paths = [
        "outputs/2026-05-20/1/summary.md",  # OK
        "queues/research/x.yaml",            # OK
        "inbox/decisions/d.yaml",            # OK
        "MANDATE.md",                        # DENY → poisons the whole batch
    ]
    allowed, ok_paths, denied = g.check_paths(
        paths, action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed
    # The denied set must contain MANDATE.md
    assert any("MANDATE.md" in d for d in denied)
    # The atomicity contract: ok_paths is what passed individually, but the
    # OVERALL outcome is denial. Tests of guard.push() will confirm no push.


def test_all_allowed_returns_allowed(fixture_policy_yaml):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    paths = [
        "outputs/2026-05-20/1/summary.md",
        "queues/research/x.yaml",
        "inbox/decisions/d.yaml",
    ]
    allowed, ok_paths, denied = g.check_paths(
        paths, action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert allowed
    assert set(ok_paths) == set(paths)
    assert denied == []


def test_empty_paths_returns_denied(fixture_policy_yaml):
    """Empty staged set = nothing to push. Fail-closed: don't mint a token
    for a no-op (and surface the situation as denial so the loop logs it)."""
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed
    assert any("no paths" in d.lower() or "empty" in d.lower() for d in denied)
