"""Tests for mission-file-guard.py PreToolUse hook.

Feeds the exact PreToolUse stdin JSON and asserts the deny/allow decision.
Governance fix 2026-06-01 ({{OPERATOR}} msg 3597/3599).

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


# ── #961: framework-repo-scoped protection (scripts/lib/, scripts/ broadly) ─
#
# Board incident: an agent edited+committed scripts/lib/budget.py directly in
# the bubble-ops-loop working tree (commit 5513824) with no deny from this
# hook, because it only consulted the repo-agnostic STRUCTURAL_PATH_GLOBS.
# These tests build a REAL git repo (so `git config --get remote.origin.url`
# resolves) with an `origin` remote named after the repo under test, mirroring
# token-broker/tests/test_is_structural_push.py's `_repo_with_named_remote`.


def _git_repo_with_remote(tmp_path: Path, remote_name: str) -> Path:
    bare = tmp_path / f"{remote_name}.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    clone = tmp_path / f"clone-{remote_name}"
    clone.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "HOME": str(tmp_path), "PATH": __import__("os").environ.get("PATH", "")}
    subprocess.run(["git", "init"], cwd=str(clone), env=env, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(clone), env=env,
                    check=True, capture_output=True)
    (clone / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=str(clone), env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(clone), env=env, check=True, capture_output=True)
    return clone


def test_edit_scripts_lib_on_framework_repo_is_denied(tmp_path):
    """scripts/lib/budget.py, edited with cwd inside a bubble-ops-loop clone
    -> DENIED. This is the exact #961 incident, caught pre-commit."""
    repo = _git_repo_with_remote(tmp_path, "bubble-ops-loop")
    code, parsed = _run("Edit", {"file_path": "scripts/lib/budget.py"}, cwd=str(repo))
    assert _is_deny(parsed), "scripts/lib/ edit in the framework repo must be denied"


def test_edit_scripts_broadly_on_framework_repo_is_denied(tmp_path):
    """scripts/vendor-dept-libs.sh (NOT under lib/) on bubble-ops-loop -> DENIED
    (scripts/** broadened in #961, not just scripts/lib/)."""
    repo = _git_repo_with_remote(tmp_path, "bubble-ops-loop")
    code, parsed = _run("Edit", {"file_path": "scripts/vendor-dept-libs.sh"}, cwd=str(repo))
    assert _is_deny(parsed), "scripts/ (broadly) edit in the framework repo must be denied"


def test_edit_scripts_lib_on_dept_repo_is_still_allowed(tmp_path):
    """The SAME scripts/lib/ path, cwd inside a DEPT repo (bubble-ops-maya)
    -> stays ALLOWED. Depts vendor & sync the lib at runtime (sync-dispatch-
    lib.sh); the framework-only glob must not 403 that legitimate workflow."""
    repo = _git_repo_with_remote(tmp_path, "bubble-ops-maya")
    code, parsed = _run("Edit", {"file_path": "scripts/lib/dispatch_helpers.py"}, cwd=str(repo))
    assert parsed is None, f"scripts/lib/ edit in a dept repo must stay allowed, got {parsed}"


def test_edit_mandate_on_dept_repo_still_denied_with_repo_awareness(tmp_path):
    """Sanity: repo-aware lookup must not regress the shared mission-file lock."""
    repo = _git_repo_with_remote(tmp_path, "bubble-ops-maya")
    code, parsed = _run("Edit", {"file_path": "MANDATE.md"}, cwd=str(repo))
    assert _is_deny(parsed), "MANDATE.md must stay denied regardless of repo"


def test_repo_lookup_failure_falls_back_to_shared_globs_only(tmp_path):
    """cwd is not a git repo at all (git config lookup fails) -> repo_name is
    None -> framework-only globs simply don't apply (fail-open on the REPO
    NAME lookup, not on structural detection) -> scripts/lib/ allowed, but
    the shared MANDATE.md lock is untouched."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    code, parsed = _run("Edit", {"file_path": "scripts/lib/budget.py"}, cwd=str(plain))
    assert parsed is None
    code, parsed = _run("Edit", {"file_path": "MANDATE.md"}, cwd=str(plain))
    assert _is_deny(parsed)
