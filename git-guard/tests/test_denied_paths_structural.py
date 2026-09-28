"""Structural paths MUST be DENIED for runtime_write_own.

Per Notion v4 line 700:
  "Changer dept.yaml, prompts, subagents, skills, tools, policies → PR obligatoire"

So under action=runtime_write_own these paths must all fail-closed.
"""

from __future__ import annotations

import pytest

from src.guard import Guard
from src.policy_loader import load_policy


@pytest.mark.parametrize(
    "path",
    [
        "dept.yaml",
        "MANDATE.md",  # mandate is structural (governs the dept)
        "CLAUDE.md",
        "layers/1/PROMPT.md",
        "layers/2/PROMPT.md",
        "layers/3/PROMPT.md",
        "layers/4/PROMPT.md",
        "subagents/executor.md",
        "subagents/data-curator.md",
        "skills/echo-skill/SKILL.md",
        "tools/echo-tool/tool.py",
        ".claude/settings.json",
        ".claude/agents/data-curator.md",
        "templates/schemas/dept.schema.yaml",
        "policies/runtime.yaml",
    ],
)
def test_runtime_write_own_denies_structural_paths(fixture_policy_yaml, path):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed, f"expected DENY for {path}, got allowed=True"
    assert path not in ok_paths
    # Denial reason must reference the path and either 'structural' or the allow-list
    assert any(path in d for d in denied), f"denied reasons should mention path: {denied}"


def test_runtime_write_own_denies_dept_yaml_specifically(fixture_policy_yaml):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, denied = g.check_paths(
        ["dept.yaml"], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed
    assert any("dept.yaml" in d for d in denied)


def test_runtime_write_own_denies_claude_md_specifically(fixture_policy_yaml):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, _ = g.check_paths(
        ["CLAUDE.md"], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed


def test_runtime_write_own_denies_dot_claude(fixture_policy_yaml):
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy)
    allowed, _, _ = g.check_paths(
        [".claude/settings.json"],
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert not allowed


@pytest.mark.parametrize(
    "path",
    [
        "mandate.md",
        "Mandate.MD",
        "LAYERS/1/prompt.md",
        ".CLAUDE/SETTINGS.JSON",
    ],
)
def test_structural_matching_is_case_insensitive_even_if_allowlist_matches(
    tmp_path, path
):
    policy_path = tmp_path / "case-policy.yaml"
    policy_path.write_text(
        """github_access:
  actor: ops-loop-fixture
  own_repo: bubble-ops-fixture
  read: [bubble-ops-fixture]
  write:
    - repo: bubble-ops-fixture
      allowed_paths: ["**"]
      mode: direct_runtime_commit
  pull_requests:
    can_open_to: []
"""
    )
    g = Guard(policy=load_policy(policy_path))
    allowed, ok_paths, denied = g.check_paths(
        [path], action="runtime_write_own", repo="bubble-ops-fixture"
    )
    assert not allowed
    assert path not in ok_paths
    assert any("structural" in reason for reason in denied)
