"""Structural paths MUST be ALLOWED under action=settings_pr.

Per Notion v4 line 700: structural changes are allowed but ONLY via PR.
The guard's job is to confirm the path is in structural territory and the
action class matches. It does NOT enforce "PR vs direct push" at this layer —
that's branch protection's job (Notion line 725: "branch protection" is one
of the listed enforcement layers).

This test asserts settings_pr ALLOWS structural paths in own_repo. The
runtime_write_own analogue (DENY) is in test_denied_paths_structural.py.
"""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "dept.yaml",
        "layers/1/PROMPT.md",
        "subagents/executor.md",
        "skills/echo-skill/SKILL.md",
        "tools/echo-tool/tool.py",
        ".claude/settings.json",
    ],
)
def test_settings_pr_allows_structural_paths_in_own_repo(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="settings_pr", repo="bubble-ops-fixture"
    )
    assert allowed, f"expected ALLOW under settings_pr for {path}, denied={denied}"
    assert path in ok_paths


def test_settings_pr_rejects_runtime_paths(fixture_policy_yaml):
    """The inverse: a settings_pr against a runtime path is wrong action class."""
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, denied = g.check_paths(
        ["outputs/2026-05-20/1/summary.md"],
        action="settings_pr",
        repo="bubble-ops-fixture",
    )
    assert not allowed
    assert any("structural" in d or "runtime_write_own" in d for d in denied)
