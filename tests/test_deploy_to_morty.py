"""
test_deploy_to_morty.py — UX-5 task 4.

Tests the `scripts/deploy-to-morty.sh` flow. SSH is fully mocked — no
real connection to Morty. The script must:
  1. Refuse to run without --slug
  2. In --dry-run mode, print the would-be SSH commands and exit 0
  3. Verify the systemd template exists
  4. NEVER touch /etc/systemd/system/claude-agent-morty.service
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "deploy-to-morty.sh"
TEMPLATE = PROJECT_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"


def test_script_exists_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_template_exists():
    assert TEMPLATE.exists(), f"missing template {TEMPLATE}"


def test_template_uses_dept_slug_placeholder():
    body = TEMPLATE.read_text(encoding="utf-8")
    # All three placeholders must be present.
    assert "${DEPT_SLUG}" in body
    assert "${TELEGRAM_STATE_DIR}" in body or "telegram-${DEPT_SLUG}" in body
    assert "${ENV_FILE}" in body or "/run/claude-agent-${DEPT_SLUG}" in body


def test_template_does_not_reference_morty_unit():
    body = TEMPLATE.read_text(encoding="utf-8")
    # Doctrine: NEVER replace morty's own unit. Template must reference
    # only ops-loop-<slug>, not claude-agent-morty.
    assert "claude-agent-morty" not in body, \
        "template must not reference morty's unit"


def test_template_does_not_use_tmux():
    body = TEMPLATE.read_text(encoding="utf-8")
    # Doctrine: tmux is forbidden in ACTIVE systemd directives
    # (ExecStart, ExecStartPre, Environment, etc.) per STEP-4 404
    # regression. Comments explaining the ban MAY mention "tmux"; we
    # only forbid it in executable directives.
    offending = []
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#") or not stripped:
            continue
        if "tmux" in raw.lower():
            offending.append(f"line {lineno}: {raw}")
    assert not offending, (
        "tmux is forbidden in active directives "
        "(Step 4 documented 404 regression). Offending lines:\n"
        + "\n".join(offending)
    )


def test_template_uses_script_qfc_pattern():
    body = TEMPLATE.read_text(encoding="utf-8")
    # Per STEP-7-DEPLOYMENT-RESULTS.md the ExecStart pattern is
    #   /usr/bin/script -qfc "/usr/bin/claude --dangerously-skip-permissions ..."
    assert "/usr/bin/script" in body or "script -qfc" in body
    assert "--dangerously-skip-permissions" in body


def test_template_uses_plugin_telegram_channel():
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "plugin:telegram@claude-plugins-official" in body


def test_dry_run_prints_ssh_commands(tmp_path):
    """In --dry-run mode the script must print the SSH commands it WOULD
    have run without actually running them."""
    res = subprocess.run(
        [
            "bash", str(SCRIPT),
            "--slug=fixture",
            "--remote=claude@morty.example",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, \
        f"expected 0, got {res.returncode}\nstdout={res.stdout}\nstderr={res.stderr}"
    combined = res.stdout + res.stderr
    # We expect the dry-run to print:
    #   - the systemd unit it would render
    #   - the ssh + scp + systemctl commands it would run
    assert "ops-loop-fixture.service" in combined
    assert "claude@morty.example" in combined or "morty.example" in combined
    assert "/etc/systemd/system" in combined


def test_template_has_claude_model_placeholder():
    body = TEMPLATE.read_text(encoding="utf-8")
    # Per-dept model pin (fleet cost-optimization, 2026-06-19): ExecStart must
    # carry --model "${CLAUDE_MODEL}" so deploy-to-morty.sh can substitute it.
    assert "${CLAUDE_MODEL}" in body
    assert '--model \\"${CLAUDE_MODEL}\\"' in body


def test_dry_run_default_model_is_opus_1m():
    """No --model flag -> ExecStart pins opus[1m] (unchanged prior behaviour)."""
    res = subprocess.run(
        ["bash", str(SCRIPT), "--slug=fixture", "--dry-run"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    combined = res.stdout + res.stderr
    assert '/usr/bin/claude --model \\"opus[1m]\\"' in combined


def test_dry_run_model_override_to_sonnet_1m():
    """--model=sonnet[1m] -> ExecStart pins sonnet[1m] (cost-optimization dept)."""
    res = subprocess.run(
        ["bash", str(SCRIPT), "--slug=fixture", "--model=sonnet[1m]", "--dry-run"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    combined = res.stdout + res.stderr
    assert '/usr/bin/claude --model \\"sonnet[1m]\\"' in combined
    assert 'opus[1m]' not in combined.split("ExecStart=/bin/sh")[1].split("\n")[0]


def test_dry_run_does_not_touch_morty_unit(tmp_path):
    res = subprocess.run(
        [
            "bash", str(SCRIPT),
            "--slug=fixture",
            "--remote=claude@morty.example",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    combined = res.stdout + res.stderr
    # Must NOT mention writing morty's own unit.
    forbidden = "/etc/systemd/system/claude-agent-morty.service"
    if forbidden in combined:
        # Allowed only as a comment "DO NOT touch ..." — assert it's a
        # warning, not an action.
        for line in combined.splitlines():
            if forbidden in line:
                lower = line.lower()
                assert ("do not" in lower or "never" in lower or
                        "warning" in lower or "forbidden" in lower), \
                    f"line touches morty unit without warning prefix: {line!r}"


def test_help_documents_args():
    res = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert "--slug" in res.stdout
    assert "--remote" in res.stdout
    assert "--dry-run" in res.stdout


def test_rejects_missing_slug():
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "slug" in (res.stderr + res.stdout).lower()


def test_dry_run_uses_env_default_remote(monkeypatch):
    """When --remote not given, defaults to $BUBBLE_MORTY_HOST or claude@morty."""
    env = os.environ.copy()
    env["BUBBLE_MORTY_HOST"] = "claude@override.example"
    res = subprocess.run(
        ["bash", str(SCRIPT), "--slug=fixture", "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0
    assert "claude@override.example" in (res.stdout + res.stderr)


def test_dry_run_renders_template_substitutions():
    """Template placeholders must be substituted with the dept slug."""
    res = subprocess.run(
        [
            "bash", str(SCRIPT),
            "--slug=eliot",
            "--remote=claude@morty",
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    combined = res.stdout + res.stderr
    # Substituted unit must reference eliot, not literal ${DEPT_SLUG}.
    assert "${DEPT_SLUG}" not in combined or "${DEPT_SLUG}" not in combined.split("would have run")[0]
    assert "ops-loop-eliot" in combined
    assert "/home/claude/.claude/channels/telegram-eliot" in combined
    assert "/run/claude-agent-eliot" in combined
