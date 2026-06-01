"""runtime_write_own MUST allow queues/** for the fixture dept (Notion line 620)."""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "queues/research/test-001.yaml",
        "queues/gates/gate-1234.yaml",
        "queues/improvements/i-005.yaml",
        "queues/management/directive-001.yaml",  # writable for the dept itself
    ],
)
def test_runtime_write_own_allows_queues_subdir(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert allowed, f"expected ALLOW for {path}, got denied={denied}"
    assert path in ok_paths


def test_tony_can_target_management_via_priority_pr(tony_policy_yaml):
    """Tony's open_priority_pr targets queues/management/** in a child repo
    (Notion line 621: 'Branch prefix tony/directive/, target path queues/management/**')."""
    policy = load_policy(tony_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        ["queues/management/directive-001.yaml"],
        action="open_priority_pr",
        repo="bubble-ops-fixture",
    )
    assert allowed, f"expected ALLOW, got denied={denied}"


def test_tony_cannot_target_management_in_random_repo(tony_policy_yaml):
    """If repo is not in can_open_to, deny even if target_path matches."""
    policy = load_policy(tony_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, denied = g.check_paths(
        ["queues/management/directive-001.yaml"],
        action="open_priority_pr",
        repo="bubble-ops-stranger",
    )
    assert not allowed
    assert any("can_open_to" in d for d in denied)
