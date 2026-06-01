"""
test_morty_security_audit.py — Security/backup sprint, deliverable D.

Tests `scripts/morty-security-audit.sh` — read-only, no-mutations
inventory of Morty's security posture. Output is structured markdown,
pipe-friendly (mail / telegram).

The script invokes SSH to fetch:
  - SSH posture (PasswordAuthentication, PermitRootLogin, authorized_keys count)
  - Sudo posture (sudoers.d, NOPASSWD)
  - Firewall + listening ports
  - Secret files (paths + sizes + mtimes; NEVER contents)
  - Restic backup status (last snapshot timestamp, count, retention check)
  - Recent SSH logins
  - Bubble systemd units
  - Disk + memory + apt-upgradable count

SSH is fully mocked — no real connection.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "morty-security-audit.sh"


def test_audit_script_exists_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK)


# ---------- Test 1: invokes the right commands ----------


def test_audit_script_invokes_required_probes():
    body = SCRIPT.read_text(encoding="utf-8")
    # SSH posture: needs sshd_config or sshd -T.
    assert "sshd" in body or "PasswordAuthentication" in body
    # Sudoers.
    assert "sudoers" in body or "NOPASSWD" in body
    # Listening ports.
    assert "ss " in body or "netstat" in body or "ss -" in body
    # Recent logins.
    assert "last " in body or "lastlog" in body or "journalctl" in body and "sshd" in body
    # Disk usage.
    assert "df " in body or "df -" in body
    # Memory.
    assert "free " in body or "free -" in body
    # Apt updates.
    assert "apt list" in body or "apt-get -s upgrade" in body or "/var/lib/update-notifier" in body


# ---------- Test 2: markdown structure has the expected sections ----------


def test_audit_script_emits_required_markdown_sections():
    """
    Inspect the script body for the markdown section headers it emits
    via echo/printf. Each expected section must appear at least once as
    a heading marker.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    required_headings = [
        "SSH",  # ## SSH posture
        "Sudo",  # ## Sudo posture
        "Firewall",  # ## Firewall / listening ports
        "Secret",  # ## Secret files
        "Backup",  # ## Backup status (Restic)
        "Login",  # ## Recent SSH logins
        "Systemd",  # ## Systemd units (bubble-*)
        "Disk",  # ## Disk usage
        "Memory",  # ## Memory
        "Update",  # ## Updates pending
    ]
    for heading in required_headings:
        assert heading in body, f"audit script must emit a '{heading}' section"


# ---------- Test 3: NEVER prints secret contents ----------


def test_audit_script_never_prints_secret_contents():
    """
    Regex check across the entire script body: no command that would
    print the CONTENTS of a known secret file. The script may print
    paths, sizes, mtimes — never bytes.
    """
    body = SCRIPT.read_text(encoding="utf-8")

    # Forbidden patterns: cat/head/tail/less on known-secret paths.
    forbidden_paths = [
        r"/etc/age/key\.txt",
        r"/etc/bubble/.*\.env",
        r"/etc/bubble/restic-password",
        r"/srv/bubble-secrets/.*\.pem",
        r"/srv/bubble-secrets/.*key",
        r"\.sops\.env",
        r"\.sops\.pem",
    ]
    # Forbidden read commands: things that emit file contents.
    forbidden_read_cmds = [r"cat", r"head", r"tail", r"less", r"more", r"hexdump", r"xxd", r"od"]

    for path_pat in forbidden_paths:
        for cmd in forbidden_read_cmds:
            # Look for `cmd ... <path>` patterns. Tolerant of sudo prefix.
            full_pat = rf"\b{cmd}\b[^|\n]*{path_pat}"
            m = re.search(full_pat, body)
            assert not m, (
                f"audit script must NEVER read contents of secret files. "
                f"Offending match: {m.group(0)!r}"
            )

    # Also forbid `grep` on secret files (would print matching lines).
    for path_pat in forbidden_paths:
        full_pat = rf"\bgrep\b[^|\n]*{path_pat}"
        m = re.search(full_pat, body)
        assert not m, (
            f"audit script must not grep into secret files. "
            f"Offending match: {m.group(0)!r}"
        )


def test_audit_script_secret_listing_uses_stat_or_ls_only():
    """
    The secret-files section must list paths + sizes + mtimes via
    `stat` or `ls -la`, never via content-reading commands.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Either `stat` or `ls -la` must be present (the safe listing).
    assert "stat " in body or "ls -" in body or "ls --" in body


# ---------- Test 4: leak-detection regex on the output template ----------


def test_audit_script_does_not_template_known_leak_markers():
    """
    Independent of which files are read, the script body itself must
    not contain any literal that LOOKS like a leaked secret marker.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    leak_patterns = [
        r"-----BEGIN [A-Z ]*KEY-----",  # PEM headers
        r"\bghp_[A-Za-z0-9]{30,}",  # GitHub PAT
        r"\bsk_live_[A-Za-z0-9]{20,}",  # Stripe live key
        r"\bAGE-SECRET-KEY-1[A-Z0-9]{50,}",  # age secret key
        r"hostname.*token\s*=\s*['\"]?[A-Za-z0-9]{20,}",  # generic token
    ]
    for pat in leak_patterns:
        m = re.search(pat, body)
        assert not m, (
            f"audit script template contains what looks like a leaked secret: {m.group(0)!r}"
        )


# ---------- Test 5: read-only, no mutations ----------


def test_audit_script_is_read_only():
    """
    The audit must never mutate Morty's state. Forbid common
    mutation verbs in active (non-echo, non-comment) lines.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    forbidden_mutating = [
        r"systemctl\s+(start|stop|restart|enable|disable)",
        r"apt(-get)?\s+(install|remove|purge|upgrade)",
        r"\brm\b\s+",
        r"\bmv\b\s+",
        r"\bcp\b\s+",
        r"\bchmod\b\s+",
        r"\bchown\b\s+",
        r"\binstall\b\s+-m",
        r"\bdd\b\s+if=",
        r"\bmkfs",
        r">\s*/etc/",  # any redirect into /etc/
    ]
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("echo ") or stripped.startswith("printf "):
            continue
        # ssh-wrapped commands could still be mutations; check inside.
        for pat in forbidden_mutating:
            if re.search(pat, raw):
                pytest.fail(
                    f"audit script must be read-only. "
                    f"Line {lineno} matches mutating pattern {pat!r}: {raw}"
                )


def test_audit_script_uses_ssh_with_hetzner_default():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "ssh" in body
    assert "hetzner" in body


def test_audit_script_output_is_pipe_friendly():
    """
    The output must go to stdout (markdown, easy to pipe to mail or
    telegram), not to a log file or interactive pager.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Must NOT invoke a pager.
    assert " less " not in body and " | less" not in body
    assert " more " not in body
    # Must NOT redirect stdout to a file.
    # (We allow `2>/dev/null` for suppressing stderr noise, but not
    # `>/some/path` for hiding stdout.)
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        # echo "..." > file = forbidden (would hide output).
        if re.search(r">\s*/(?!dev/null)\S+", raw) and not re.search(r"2>\s*/", raw):
            # Allow > /dev/null but not > /any/other/path on stdout side.
            # Check it's not just `2>` (stderr).
            if not re.match(r"^[^>]*2>\s*/", raw):
                pytest.fail(
                    f"audit script must write to stdout (not redirect to file). "
                    f"Line {lineno}: {raw}"
                )
