"""
test_deploy_to_morty_os_user.py — board #1120 per-dept OS-user isolation.

Covers the --os-user flag added to scripts/deploy-to-morty.sh:
  - default (no flag) is byte-identical to pre-#1120 behaviour (User=claude,
    Group=claude, /home/claude/agents/<slug>, chown claude:claude everywhere).
  - --os-user=agent-<slug> re-points User=/Group=/WorkingDirectory=/all
    ExecStartPre chowns to the new user, and moves the workdir to
    /srv/agents/<slug>.
  - a bogus --os-user (empty, "root") is refused.
  - the real (non-dry-run) provisioning path refuses to proceed against a
    non-legacy os_user that doesn't exist yet on the remote (SSH mocked via
    a stub in PATH — never touches a real host).

SSH is never actually invoked in these tests (either --dry-run mode, which
never shells out to ssh at all, or a PATH-shadowing `ssh` stub for the one
real-path test).
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "deploy-to-morty.sh"


def _dry_run(*extra_args: str, slug: str = "eliot") -> str:
    res = subprocess.run(
        ["bash", str(SCRIPT), f"--slug={slug}", "--remote=claude@morty", "--dry-run", *extra_args],
        capture_output=True, text=True,
    )
    return res.stdout + res.stderr, res.returncode


def test_default_os_user_is_claude_legacy():
    combined, code = _dry_run()
    assert code == 0
    assert "User=claude" in combined
    assert "Group=claude" in combined
    assert "chown claude:claude /run/claude-agent-eliot" in combined
    assert "chown claude:claude /run/bubble-eliot" in combined
    # No bootstrap-os-user reminder for the legacy user.
    assert "bootstrap-os-user.sh" not in combined


def test_os_user_override_repoints_everything():
    combined, code = _dry_run("--os-user=agent-eliot")
    assert code == 0
    assert "User=agent-eliot" in combined
    assert "Group=agent-eliot" in combined
    assert "WorkingDirectory=/srv/agents/eliot" in combined
    assert "chown agent-eliot:agent-eliot /run/claude-agent-eliot" in combined
    assert "chown agent-eliot:agent-eliot /run/claude-agent-eliot/env" in combined
    assert "chown agent-eliot:agent-eliot /run/bubble-eliot" in combined
    assert "chown agent-eliot:agent-eliot /run/bubble-eliot/pem" in combined
    assert "User=claude" not in combined
    # Pre-req reminder must be present and must come before the unit install step.
    assert "bootstrap-os-user.sh --os-user=agent-eliot --workdir=/srv/agents/eliot" in combined
    assert combined.index("bootstrap-os-user.sh") < combined.index("systemctl start ops-loop-eliot.service")


def test_os_user_override_repoints_telegram_state_dir():
    combined, code = _dry_run("--os-user=agent-eliot")
    assert code == 0
    assert "/home/agent-eliot/.claude/channels/telegram-eliot" in combined
    assert "/home/claude/.claude/channels/telegram-eliot" not in combined


def test_shared_ro_runtime_path_stays_literal_claude_regardless_of_os_user():
    """The bun/node runtime PATH segment is deliberately SHARED-RO — it must
    stay /home/claude/.bun/bin even for a per-user dept (no secrets there,
    every dept user just needs read+execute on it)."""
    combined, code = _dry_run("--os-user=agent-eliot")
    assert code == 0
    assert "/home/claude/.bun/bin" in combined


@pytest.mark.parametrize("bogus", ["", "root"])
def test_rejects_bogus_os_user(bogus):
    args = ["--slug=eliot", "--dry-run"]
    if bogus:
        args.append(f"--os-user={bogus}")
    else:
        # Empty string via --os-user= (flag present, empty value).
        args.append("--os-user=")
    res = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
    assert res.returncode != 0
    assert "os-user" in (res.stdout + res.stderr).lower()


def test_help_documents_os_user_flag():
    res = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--os-user" in res.stdout


def test_real_path_refuses_missing_os_user(tmp_path, monkeypatch):
    """The (non-dry-run) real provisioning path must refuse to proceed for a
    non-legacy os_user that doesn't exist on the remote yet — verified via an
    `ssh` stub in PATH that always reports "no such user" (`id -u` exit 1),
    so this test never touches a real host."""
    ssh_stub = tmp_path / "ssh"
    ssh_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Minimal ssh stub: any "id -u <user>" probe fails (user absent).
        # Anything else exits 0 (should never be reached — the script must
        # bail out on the id -u probe before doing anything else).
        if [[ "$*" == *"id -u"* ]]; then
          exit 1
        fi
        exit 0
    """))
    ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    res = subprocess.run(
        ["bash", str(SCRIPT), "--slug=eliot", "--os-user=agent-eliot", "--remote=claude@morty"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "does not exist" in combined
    assert "bootstrap-os-user.sh" in combined
