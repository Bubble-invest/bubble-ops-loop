"""
test_dept_cutover.py — board #1150 per-dept cutover recipe helper.

Exercises scripts/dept-cutover.sh in --dry-run mode ONLY — this test suite
must NEVER invoke the script for real (that would call useradd/runuser/
systemctl against the CI runner). --dry-run prints every command/write it
would perform without executing any of it; these tests assert on that
printed plan.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "dept-cutover.sh"


def _dry_run(*args: str) -> tuple[str, int]:
    res = subprocess.run(
        ["bash", str(SCRIPT), *args, "--dry-run"],
        capture_output=True, text=True,
    )
    return res.stdout + res.stderr, res.returncode


def test_script_exists_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_dry_run_defaults_os_user_and_workdir():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "os_user=agent-maya" in combined
    assert "workdir=/srv/agents/maya" in combined


def test_dry_run_delegates_to_bootstrap_os_user():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "bootstrap-os-user.sh --os-user=agent-maya --workdir=/srv/agents/maya" in combined
    # bootstrap-os-user.sh itself must ALSO be invoked in --dry-run, never for real.
    assert "useradd --system" in combined
    assert "chown agent-maya:agent-maya" in combined


def test_skip_user_bootstrap_flag_skips_step_a():
    combined, code = _dry_run("--slug=maya", "--skip-user-bootstrap")
    assert code == 0
    assert "skip-user-bootstrap set" in combined
    assert "useradd" not in combined


def test_dry_run_seeds_claude_json_with_correct_shape():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "/home/agent-maya/.claude.json" in combined
    assert "hasCompletedOnboarding" in combined
    assert "hasTrustDialogAccepted" in combined


def test_dry_run_registers_marketplace_and_installs_telegram_plugin():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "runuser -l agent-maya -c 'claude plugin marketplace add anthropics/claude-plugins-official'" in combined
    assert "runuser -l agent-maya -c 'claude plugin install telegram@claude-plugins-official --scope user -y'" in combined


def test_dry_run_strips_settings_env_step_present():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "/srv/agents/maya/.claude/settings.json" in combined


def test_dry_run_clears_stale_shared_tmp_logs():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "rm -f /tmp/install-boot-rearm-build.log /tmp/install-channel-patches-inject-build.log" in combined


def test_dry_run_disables_old_watchdog_timer_reference():
    combined, code = _dry_run("--slug=maya")
    assert code == 0
    assert "telegram-watchdog-maya.timer" in combined


def test_ben_specific_extra_state_paths_mentioned():
    combined, code = _dry_run("--slug=ben")
    assert code == 0
    assert "os_user=agent-ben" in combined


def test_refuses_claude_as_os_user():
    combined, code = _dry_run("--slug=maya", "--os-user=claude")
    assert code != 0
    assert "refusing" in combined.lower()


def test_refuses_non_kebab_case_slug():
    combined, code = _dry_run("--slug=Maya")
    assert code != 0
    assert "kebab-case" in combined.lower()


def test_requires_slug():
    combined, code = _dry_run()
    assert code != 0
    assert "--slug required" in combined
