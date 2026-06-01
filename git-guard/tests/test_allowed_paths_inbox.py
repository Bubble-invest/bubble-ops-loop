"""runtime_write_own MUST allow inbox/** for the fixture dept (Notion line 620)."""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "inbox/decisions/d-001.yaml",
        "inbox/decisions/d-001/processed/result.yaml",
        "inbox/notifications/n-042.md",
    ],
)
def test_runtime_write_own_allows_inbox_subdir(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert allowed, f"expected ALLOW for {path}, got denied={denied}"
    assert path in ok_paths
