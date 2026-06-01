"""settings_pr policy class tests — PR mandatory for structural changes.

Notion v4 line 622: settings_pr required for dept.yaml, prompts, subagents,
skills, tools, policies.
"""

from __future__ import annotations

import pytest


SETTINGS_PATHS = [
    "dept.yaml",
    "layers/1/PROMPT.md",
    "layers/4/PROMPT.md",
    ".claude/agents/data-curator.md",
    "skills/echo-skill/SKILL.md",
    "tools/echo-tool/tool.py",
    ".claude/settings.json",
]


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_settings_pr_required_for_structural_paths(path, ops_policy_yaml):
    """settings_pr must be the action used for any of these paths.

    The policy verifies the action class itself is settings_pr, regardless of
    whether the path matches runtime allowed_paths."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    # Use the ops-loop actor on its own repo, with action=settings_pr
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="settings_pr",
        paths=[path],
    )
    assert allowed, f"settings_pr on {path} should be allowed: {reasons}"


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_runtime_write_own_rejects_structural_paths(path, ops_policy_yaml):
    """Counterpart: structural paths CANNOT be written via runtime_write_own."""
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, _ = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        paths=[path],
    )
    assert not allowed, f"{path} must NOT be writable via runtime_write_own"


def test_unknown_action_class_is_rejected(ops_policy_yaml):
    from src.policy import Policy

    p = Policy.from_yaml(ops_policy_yaml)
    allowed, reasons = p.enforce(
        actor="ops-loop-fixture",
        repo="bubble-ops-fixture",
        action="totally_made_up_class",
    )
    assert not allowed
    assert any("unknown action class" in r for r in reasons)
