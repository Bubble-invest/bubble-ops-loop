"""Tests for mission-file-guard.py PreToolUse hook.

Feeds the exact PreToolUse stdin JSON and asserts the deny/allow decision.
Governance fix 2026-06-01 (Joris msg 3597/3599).

Run: python3 -m pytest deploy/hooks/test_mission_file_guard.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # bubble-ops-loop/
HOOK = REPO_ROOT / "deploy" / "hooks" / "mission-file-guard.py"
POLICY_PY = REPO_ROOT / "token-broker" / "src" / "policy.py"


def _run(tool_name: str, tool_input: dict, cwd: str = "/home/claude/agents/bubble-ops-maya"):
    """Invoke the hook with a PreToolUse payload; return (exit_code, parsed_json_or_None)."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd,
    }
    env = {"BUBBLE_BROKER_POLICY_PY": str(POLICY_PY), "PATH": __import__("os").environ.get("PATH", "")}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, check=False,
    )
    out = proc.stdout.strip()
    parsed = json.loads(out) if out else None
    return proc.returncode, parsed


def _is_deny(parsed) -> bool:
    return bool(parsed) and parsed.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ── Edit/Write on structural files → DENY ──────────────────────────────────

@pytest.mark.parametrize("rel", [
    "MANDATE.md", "CLAUDE.md", "dept.yaml", "skills_manifest.yaml",
    "layers/1/PROMPT.md", "missions/discovery.yaml", "skills/x/SKILL.md",
    "tools/t.py", ".claude/settings.json", "config.yaml", "gate_policy.yaml",
])
def test_edit_structural_is_denied_relative(rel):
    code, parsed = _run("Edit", {"file_path": rel})
    assert code == 0
    assert _is_deny(parsed), f"{rel} must be denied"


@pytest.mark.parametrize("rel", [
    "MANDATE.md", "layers/1/PROMPT.md", "missions/discovery.yaml",
])
def test_edit_structural_is_denied_absolute(rel):
    abspath = f"/home/claude/agents/bubble-ops-maya/{rel}"
    code, parsed = _run("Edit", {"file_path": abspath})
    assert _is_deny(parsed), f"absolute {abspath} must be denied"


def test_write_structural_is_denied():
    code, parsed = _run("Write", {"file_path": "MANDATE.md", "content": "x"})
    assert _is_deny(parsed)


def test_deny_reason_mentions_pr_and_working_memory():
    _, parsed = _run("Edit", {"file_path": "MANDATE.md"})
    reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
    assert "PR" in reason or "pull request" in reason.lower()
    assert "WORKING_MEMORY" in reason


# ── Edit/Write on writable files → ALLOW (no JSON) ─────────────────────────

@pytest.mark.parametrize("rel", [
    "WORKING_MEMORY.md", "whiteboard.yaml",
    "outputs/2026-06-01/1/summary.md", "queues/research/x.yaml",
    "inbox/decisions/d.yaml", "README.md", "kanban_queue.jsonl",
])
def test_edit_writable_is_allowed(rel):
    code, parsed = _run("Edit", {"file_path": rel})
    assert code == 0
    assert parsed is None, f"{rel} must pass through (no deny JSON), got {parsed}"


# ── Bash git staging of structural files → DENY ────────────────────────────

def test_bash_git_add_structural_is_denied():
    _, parsed = _run("Bash", {"command": "git add MANDATE.md && git commit -m x"})
    assert _is_deny(parsed)


def test_bash_git_add_writable_is_allowed():
    code, parsed = _run("Bash", {"command": "git add WORKING_MEMORY.md outputs/x.md"})
    assert parsed is None


def test_bash_git_mv_structural_is_denied():
    _, parsed = _run("Bash", {"command": "git mv missions/a.yaml missions/b.yaml"})
    assert _is_deny(parsed)


def test_bash_git_commit_all_is_denied():
    # -a could sweep in a structural file → block, tell agent to stage explicitly
    _, parsed = _run("Bash", {"command": "git commit -am 'update mandate'"})
    assert _is_deny(parsed)


def test_bash_non_git_is_allowed():
    code, parsed = _run("Bash", {"command": "ls -la && python3 foo.py"})
    assert parsed is None


def test_bash_git_status_is_allowed():
    code, parsed = _run("Bash", {"command": "git status && git log --oneline -3"})
    assert parsed is None


# ── Robustness ─────────────────────────────────────────────────────────────

def test_empty_stdin_fails_open():
    proc = subprocess.run([sys.executable, str(HOOK)], input="", capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_unknown_tool_is_allowed():
    code, parsed = _run("Read", {"file_path": "MANDATE.md"})
    assert parsed is None  # Read is fine — only Edit/Write/Bash gated
