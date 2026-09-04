"""
test_bootstrap_os_user.py — board #1120 per-dept OS-user provisioner.

Exercises scripts/bootstrap-os-user.sh in --dry-run mode ONLY — this test
suite must NEVER invoke the script for real (that would call useradd/passwd
-l against the CI runner's own passwd database). --dry-run prints every
command it would run without executing any of them; these tests assert on
that printed command list.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "bootstrap-os-user.sh"


def _dry_run(*args: str) -> tuple[str, int]:
    res = subprocess.run(
        ["bash", str(SCRIPT), *args, "--dry-run"],
        capture_output=True, text=True,
    )
    return res.stdout + res.stderr, res.returncode


def test_script_exists_and_executable():
    import os
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_dry_run_emits_useradd_with_no_sudo_group():
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=/srv/agents/ben")
    assert code == 0
    assert "useradd --system --shell /usr/sbin/nologin --no-create-home --home-dir /home/agent-ben agent-ben" in combined
    # Must NEVER pass `-G sudo` / `--groups sudo` / any sudo-capable group.
    for line in combined.splitlines():
        if "useradd" in line:
            assert "sudo" not in line
            assert "wheel" not in line
            assert "admin" not in line


def test_dry_run_locks_password():
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=/srv/agents/ben")
    assert code == 0
    assert "passwd -l agent-ben" in combined


def test_dry_run_chowns_home_and_workdir_to_the_new_user_only():
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=/srv/agents/ben")
    assert code == 0
    assert "chown agent-ben:agent-ben /home/agent-ben" in combined
    assert "chown agent-ben:agent-ben /home/agent-ben/.claude" in combined
    assert "chown agent-ben:agent-ben /srv/agents/ben" in combined
    # Ownership must never fall back to (or additionally include) the
    # literal user/group "claude" (note: "/.claude" the DIRECTORY NAME
    # legitimately contains the substring "claude" — check for the user
    # token "claude:claude" / " claude " specifically, not a bare substring).
    assert "claude:claude" not in combined
    assert "chown claude" not in combined


def test_dry_run_sets_restrictive_modes():
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=/srv/agents/ben")
    assert code == 0
    assert "chmod 0750 /home/agent-ben" in combined
    assert "chmod 0750 /srv/agents/ben" in combined


def test_refuses_root_as_os_user():
    combined, code = _dry_run("--os-user=root", "--workdir=/srv/agents/root")
    assert code != 0
    assert "root" in combined.lower()


def test_refuses_claude_as_os_user():
    """The whole point of this migration is moving depts AWAY from the
    shared 'claude' user — refuse to (re-)provision it as a "dept user"."""
    combined, code = _dry_run("--os-user=claude", "--workdir=/srv/agents/claude")
    assert code != 0
    assert "claude" in combined.lower()


def test_refuses_workdir_under_home():
    """Per-user workdirs must live under /srv/agents, decoupled from any
    home dir (single transcript-rename point — see the design doc)."""
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=/home/agent-ben/agents/ben")
    assert code != 0
    assert "/home" in combined


def test_refuses_relative_workdir():
    combined, code = _dry_run("--os-user=agent-ben", "--workdir=srv/agents/ben")
    assert code != 0


def test_refuses_missing_args():
    res = subprocess.run(["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "os-user" in combined.lower() or "workdir" in combined.lower()


def test_help_documents_flags():
    res = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--os-user" in res.stdout
    assert "--workdir" in res.stdout
    assert "--dry-run" in res.stdout
