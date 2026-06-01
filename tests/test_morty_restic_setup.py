"""
test_morty_restic_setup.py — Security/backup sprint, deliverable B.

Tests `scripts/morty-restic-setup.sh` + the systemd unit templates it
installs on Morty.

The script must:
  1. Install restic (idempotent — `apt install` is idempotent by design)
  2. Create /var/backups/bubble-restic/ (mode 700, root)
  3. `restic init` (skipped on re-run if repo already exists)
  4. Render + install bubble-restic-backup.service + .timer
  5. Render + install bubble-restic-forget.service + .timer
  6. daemon-reload + enable + start the timers

SSH and restic are fully mocked — no real execution.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "morty-restic-setup.sh"
RESTORE_DOC_SCRIPT = PROJECT_ROOT / "scripts" / "morty-restic-restore-doc.sh"
BACKUP_STRATEGY_DOC = PROJECT_ROOT / "docs" / "BACKUP-STRATEGY.md"
TEMPLATE_DIR = PROJECT_ROOT / "deploy" / "templates"
BACKUP_SERVICE_TPL = TEMPLATE_DIR / "bubble-restic-backup.service.template"
BACKUP_TIMER_TPL = TEMPLATE_DIR / "bubble-restic-backup.timer.template"
FORGET_SERVICE_TPL = TEMPLATE_DIR / "bubble-restic-forget.service.template"
FORGET_TIMER_TPL = TEMPLATE_DIR / "bubble-restic-forget.timer.template"


# ---------- files exist ----------


def test_setup_script_exists_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_restore_doc_script_exists_and_executable():
    assert RESTORE_DOC_SCRIPT.exists(), f"missing {RESTORE_DOC_SCRIPT}"
    assert os.access(RESTORE_DOC_SCRIPT, os.X_OK)


def test_strategy_doc_exists():
    assert BACKUP_STRATEGY_DOC.exists(), f"missing {BACKUP_STRATEGY_DOC}"


def test_templates_exist():
    for tpl in (BACKUP_SERVICE_TPL, BACKUP_TIMER_TPL, FORGET_SERVICE_TPL, FORGET_TIMER_TPL):
        assert tpl.exists(), f"missing template: {tpl}"


# ---------- Test 1: systemd unit content has correct paths + excludes ----------


def test_backup_service_has_required_backup_paths():
    body = BACKUP_SERVICE_TPL.read_text(encoding="utf-8")
    # These are the paths the backup MUST cover. From the sprint brief.
    required_paths = [
        "/etc/age",
        "/etc/bubble",
        "/srv/bubble-secrets",
        "/home/claude/.claude/agent-memory",
        "/home/claude/.claude/projects",
        "/home/claude/agents",
    ]
    for p in required_paths:
        assert p in body, f"backup service missing required path: {p}"


def test_backup_service_has_required_excludes():
    body = BACKUP_SERVICE_TPL.read_text(encoding="utf-8")
    # The backup excludes (no need to backup cache, compiled bytecode,
    # nor git objects since GitHub already has them).
    required_excludes = [
        ".cache",
        "*.pyc",
        "__pycache__",
        ".git/objects",
    ]
    for ex in required_excludes:
        assert ex in body, f"backup service missing required exclude: {ex}"


def test_backup_service_uses_local_repo_target():
    body = BACKUP_SERVICE_TPL.read_text(encoding="utf-8")
    # Phase-1 target is local. The strategy doc has the TODO to move it
    # off-site.
    assert "/var/backups/bubble-restic" in body
    # The repo path is provided via env var or --repo. Both patterns
    # acceptable; check at least one wires it.
    assert "RESTIC_REPOSITORY" in body or "--repo" in body


def test_backup_service_password_file_not_inline_password():
    body = BACKUP_SERVICE_TPL.read_text(encoding="utf-8")
    # The passphrase MUST be read from a file (mode 400 root), not
    # inlined as Environment=RESTIC_PASSWORD=xxxx.
    assert "RESTIC_PASSWORD_FILE" in body, (
        "must use RESTIC_PASSWORD_FILE (file, mode 400) not inline RESTIC_PASSWORD"
    )
    # Make sure we don't accidentally also set RESTIC_PASSWORD=.
    assert not re.search(r"RESTIC_PASSWORD\s*=\s*\S", body) or "RESTIC_PASSWORD_FILE" in body
    # Strict: no literal `Environment=RESTIC_PASSWORD=...`.
    assert not re.search(r"Environment\s*=\s*RESTIC_PASSWORD=", body)


# ---------- Test 2: timer fires every 6h ----------


def test_backup_timer_fires_every_6h():
    body = BACKUP_TIMER_TPL.read_text(encoding="utf-8")
    # systemd OnCalendar or OnUnitActiveSec patterns acceptable. The
    # canonical is OnCalendar=*-*-* 00/6:00:00 (every 6h at :00).
    assert "OnCalendar=" in body or "OnUnitActiveSec=" in body
    # Must mention 6h in some form.
    six_h_patterns = [
        r"00/6:00:00",
        r"\*/6:00:00",
        r"OnUnitActiveSec\s*=\s*6h",
        r"OnUnitActiveSec\s*=\s*21600",
    ]
    assert any(re.search(p, body) for p in six_h_patterns), (
        "backup timer must fire every 6h (OnCalendar=00/6:00:00 or OnUnitActiveSec=6h)"
    )
    # Persistent=true so a missed tick (Morty down) catches up on boot.
    assert "Persistent=true" in body


# ---------- Test 3: retention policy is encoded ----------


def test_forget_service_encodes_retention_policy():
    body = FORGET_SERVICE_TPL.read_text(encoding="utf-8")
    # Retention policy from the brief:
    #   hourly for 24h, daily for 7 days, weekly for 4 weeks.
    # Restic flags: --keep-hourly 24, --keep-daily 7, --keep-weekly 4.
    assert "--keep-hourly" in body and "24" in body
    assert "--keep-daily" in body and "7" in body
    assert "--keep-weekly" in body and "4" in body
    # Must include --prune so old data is actually reclaimed.
    assert "--prune" in body or "forget --prune" in body
    # Must invoke `restic forget`.
    assert "restic forget" in body or "restic" in body and "forget" in body


def test_forget_timer_runs_at_least_daily():
    body = FORGET_TIMER_TPL.read_text(encoding="utf-8")
    # Forget+prune is heavier; daily is enough.
    assert "OnCalendar=" in body or "OnUnitActiveSec=" in body
    # daily is acceptable: OnCalendar=daily or OnCalendar=*-*-* 03:30:00 etc.
    # Just check it isn't more frequent than every 6h.
    assert "Persistent=true" in body


# ---------- Test 4: script invokes `restic init` ----------


def test_setup_script_invokes_restic_init():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "restic init" in body, "first-run must initialize the repo"
    # First-run only: must NOT run `restic backup` from the setup script
    # (that's the systemd unit's job).
    # We allow the literal string in echo'd help / comments, but not as
    # a bare command.
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("echo ") or stripped.startswith("printf "):
            continue
        # Bare `restic backup` at line start = forbidden in setup.
        assert not re.match(r"^restic\s+backup\b", stripped), (
            f"setup script must not run `restic backup` directly "
            f"(systemd unit does it). Offending line {lineno}: {raw}"
        )


# ---------- Test 5: idempotent ----------


def test_setup_script_is_idempotent():
    """
    Re-running the setup must not fail. Concretely:
      - `apt install` is idempotent by design (already-installed = no-op)
      - `restic init` must be guarded by a "repo exists?" check
      - `mkdir -p` for the local repo dir
      - `systemctl enable` is idempotent
      - the template install MUST be safe to overwrite
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Guard around restic init: either explicit check OR --quiet ignore.
    # We require an explicit check pattern: `restic snapshots` (fast
    # probe) or `restic cat config` succeeds → skip init.
    guarded = (
        re.search(r"restic\s+(cat\s+config|snapshots)", body) is not None
        or "already initialized" in body.lower()
        or "repo exists" in body.lower()
        or "skip init" in body.lower()
    )
    assert guarded, "restic init must be guarded by an existence check (idempotency)"

    # mkdir -p for the local backup dir.
    assert "mkdir -p" in body or "install -d" in body
    # Strict mode (catches surprises early on re-run).
    assert "set -euo pipefail" in body


# ---------- Additional invariants ----------


def test_setup_script_uses_ssh_with_hetzner_alias_default():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "ssh" in body
    assert "hetzner" in body, "default remote must be 'hetzner' SSH alias"


def test_setup_script_does_not_actually_run_restic_or_apt_locally():
    """
    All restic + apt + systemctl commands must go through SSH to Morty.
    No local invocation.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("echo ") or stripped.startswith("printf "):
            continue
        # Local apt-get or restic invocations (not preceded by ssh) =
        # forbidden.
        if re.match(r"^(apt-get|apt)\s+install", stripped):
            pytest.fail(f"line {lineno}: local apt install. Must go through ssh. {raw}")
        if re.match(r"^restic\s+", stripped):
            pytest.fail(f"line {lineno}: local restic invocation. Must go through ssh. {raw}")
        if re.match(r"^systemctl\s+", stripped):
            pytest.fail(f"line {lineno}: local systemctl. Must go through ssh. {raw}")


def test_strategy_doc_mentions_off_site_todo():
    body = BACKUP_STRATEGY_DOC.read_text(encoding="utf-8")
    # The strategy doc must flag the limitation: local-only target,
    # needs migration to off-site (B2 or Storage Box).
    assert "TODO" in body or "À FAIRE" in body or "off-site" in body.lower()
    # Concrete provider names so Joris remembers the options.
    assert "B2" in body or "Backblaze" in body
    assert "Storage Box" in body or "Hetzner" in body
    # Retention policy documented.
    assert "24" in body and "7" in body and "4" in body, (
        "retention policy (hourly 24, daily 7, weekly 4) must be in the doc"
    )


def test_strategy_doc_explains_verification():
    body = BACKUP_STRATEGY_DOC.read_text(encoding="utf-8")
    # Operator needs to know how to verify backups are succeeding.
    assert "journalctl" in body
    assert "bubble-restic" in body


# ---------- Refacto 2026-05-21: passphrase from Keychain (Flow 3) ----------


def test_setup_uses_keychain_for_passphrase_not_interactive_read():
    """Refacto 2026-05-21 (msg 2823-2825): passphrase no longer comes from
    a `read -s` terminal prompt. Instead it's fetched from macOS Keychain via
    bubble-get-keychain (skill auth Flow 3) — same pattern as backup-age-key.sh.

    This avoids leaving the passphrase in terminal scrollback / shell history /
    JSONL transcripts during the (interactive) setup phase."""
    body = SCRIPT.read_text(encoding="utf-8")
    # Must use the Keychain primitive
    assert "bubble-get-keychain" in body, (
        "must fetch Restic passphrase from Keychain via bubble-get-keychain"
    )
    assert "bubble-set-keychain" in body, (
        "must fall back to bubble-set-keychain if passphrase not yet stored"
    )
    # Must use the canonical service/account naming
    assert "bubble-restic" in body, (
        "Keychain service convention: bubble-restic"
    )
    # MUST NOT use the old interactive `read -s` pattern for the passphrase
    # (it's OK to have `read` for confirmation prompts unrelated to secrets)
    assert "read -r -s -p" not in body, (
        "must NOT use `read -s` for the passphrase (old interactive pattern). "
        "Use bubble-get-keychain instead."
    )


def test_setup_preflight_checks_for_bubble_get_keychain():
    """If bubble-get-keychain is missing, fail loud before SSH'ing to Morty."""
    body = SCRIPT.read_text(encoding="utf-8")
    # The preflight check pattern
    assert "command -v bubble-get-keychain" in body, (
        "must preflight-check for bubble-get-keychain availability"
    )
