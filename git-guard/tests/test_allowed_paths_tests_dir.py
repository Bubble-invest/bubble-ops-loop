"""runtime_write_own MUST now allow tests/** for the fixture dept (#913,
2026-08-12, Joris-approved).

Before #913, an agent could neither commit a test file directly (denied
here, at the git-guard push boundary: tests/ absent from allowed_paths) nor
propose it via a settings PR (denied by propose-settings-pr's structural
check: tests/ correctly isn't a mission file). See #773/#891/#888.

Mirrors test_allowed_paths_outputs.py's shape/pattern.
"""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_new_feature.py",
        "tests/unit/test_widget.py",
        "tests/fixtures/data.json",
    ],
)
def test_runtime_write_own_allows_tests_subdir(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert allowed, f"expected ALLOW for {path}, got denied={denied}"
    assert path in ok_paths
    assert denied == []


def test_runtime_write_own_still_denies_forbidden_path(fixture_policy_yaml):
    """Scope check: the tests/** widening did not blind the guard — a path
    outside every allowed_paths entry (secret/config-shaped, not structural)
    is STILL denied."""
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    path = "secrets/prod-broker.sops.env"
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed, f"expected DENY for {path}, got ok_paths={ok_paths}"
    assert path not in ok_paths
    assert any("not in allowed_paths" in d for d in denied), denied
