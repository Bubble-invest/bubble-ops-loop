"""
test_backup_age_key.py — Security/backup sprint, deliverable A.

Tests the `scripts/backup-age-key.sh` + `scripts/restore-age-key.sh` pair.

These scripts back up Morty's /etc/age/key.txt by re-encrypting it with a
{{OPERATOR}}-supplied passphrase (symmetric, separate from the SOPS chain) and
writing the ciphertext to bubble-vps-data/disaster-recovery/. They do NOT
write any cleartext to disk and do NOT auto-commit.

SSH + age are fully mocked. No real connection to Morty.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = PROJECT_ROOT / "scripts" / "backup-age-key.sh"
RESTORE_SCRIPT = PROJECT_ROOT / "scripts" / "restore-age-key.sh"
DR_DOC = PROJECT_ROOT / "docs" / "DISASTER-RECOVERY-AGE-KEY.md"

# Output ciphertext lives in the private bubble-vps-data tenant repo so it
# rides on every clone + on GitHub (private).
EXPECTED_OUTPUT_PATH = (
    "projects/bubble-vps-data/disaster-recovery/age-key-morty.age"
)


# ---------- script existence ----------


def test_backup_script_exists_and_executable():
    assert BACKUP_SCRIPT.exists(), f"missing {BACKUP_SCRIPT}"
    assert os.access(BACKUP_SCRIPT, os.X_OK), f"not executable: {BACKUP_SCRIPT}"


def test_restore_script_exists_and_executable():
    assert RESTORE_SCRIPT.exists(), f"missing {RESTORE_SCRIPT}"
    assert os.access(RESTORE_SCRIPT, os.X_OK), f"not executable: {RESTORE_SCRIPT}"


def test_dr_doc_exists():
    assert DR_DOC.exists(), f"missing {DR_DOC}"
    body = DR_DOC.read_text(encoding="utf-8")
    # Must mention both scripts.
    assert "backup-age-key.sh" in body
    assert "restore-age-key.sh" in body


# ---------- backup script content checks (no execution) ----------


def test_backup_script_uses_openssl_with_keychain_passphrase():
    """Refacto 2026-05-21 (msg 2823-2825): age --passphrase reads only from
    /dev/tty (impossible to script silently). We now use openssl symmetric
    encryption (-aes-256-cbc -pbkdf2) which accepts the passphrase via a
    file descriptor (process substitution). The passphrase lives in the
    macOS Keychain (skill auth Flow 3), retrieved by bubble-get-keychain."""
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # Must invoke openssl for symmetric encryption (no longer age --passphrase)
    assert "openssl enc" in body, "must use openssl symmetric encryption"
    assert "-aes-256-cbc" in body, "must use AES-256-CBC cipher"
    assert "-pbkdf2" in body, "must use PBKDF2 key derivation"
    # Passphrase MUST come from Keychain (not /dev/tty interactive prompt)
    assert "bubble-get-keychain" in body, (
        "must retrieve passphrase from macOS Keychain via bubble-get-keychain"
    )
    # Passphrase passed via process substitution (NOT visible in `ps`)
    assert "-pass file:" in body, (
        "must pass passphrase via file:<fd> (process substitution), not pass:VALUE"
    )


def test_backup_script_targets_disaster_recovery_path():
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # Output ciphertext must land in bubble-vps-data/disaster-recovery/.
    assert EXPECTED_OUTPUT_PATH in body, (
        f"backup script must write to {EXPECTED_OUTPUT_PATH}"
    )
    assert "age-key-morty.age" in body


def test_backup_script_uses_ssh_with_hetzner_alias_default():
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # Default SSH target is the `hetzner` alias (per ~/.ssh/config). The
    # operator may override with --remote=<host>.
    assert "ssh" in body, "must use ssh"
    assert "hetzner" in body, "default remote must be 'hetzner' SSH alias"


def test_backup_script_reads_age_key_with_sudo():
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # /etc/age/key.txt is mode 400 root:root; we need sudo to read it.
    assert "sudo" in body
    assert "/etc/age/key.txt" in body


def test_backup_script_does_not_auto_commit():
    """
    The script may MENTION git commands in operator-facing instructions
    (echo "...", comments), but must NEVER EXECUTE them.

    We detect "execution" by looking for git invocations at the start of
    a line (after optional whitespace), not inside `echo "..."`, not
    inside a `#` comment.
    """
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    offending = []
    for lineno, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.lstrip()
        # Skip comments.
        if stripped.startswith("#"):
            continue
        # Skip echo/printf lines (operator instructions).
        if stripped.startswith("echo ") or stripped.startswith("printf "):
            continue
        # Skip cat <<'EOF' heredoc body markers.
        if stripped.startswith("cat <<") or stripped == "EOF" or stripped == "USAGE":
            continue
        # Bare git commands at line start = execution.
        if re.match(r"^git\s+(commit|add|push)\b", stripped):
            offending.append(f"line {lineno}: {raw}")
    assert not offending, (
        "script must NOT execute git commit/add/push — operator does this manually. "
        f"Offending lines:\n" + "\n".join(offending)
    )


def test_backup_script_does_not_write_cleartext_to_disk():
    """
    The cleartext age key must NEVER touch a file. It travels through a
    pipe (ssh | age) only.

    Forbid:
      - `cat ... > /tmp/...key.txt`-style redirects of the cleartext
      - any `mktemp` (would imply staging the cleartext on disk)
      - any `tee /tmp/` for the cleartext
    """
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert "mktemp" not in body, (
        "no mktemp allowed — cleartext age key must never hit disk"
    )
    # The cleartext payload is the output of `sudo cat /etc/age/key.txt`.
    # It must flow into age via a pipe `|`, never a redirect `>`.
    # Look for any explicit redirect of /etc/age/key.txt content.
    forbidden_patterns = [
        r"cat\s+/etc/age/key\.txt\s*>",
        r"tee\s+/tmp/.*\.txt",
        r"cp\s+/etc/age/key\.txt",
        r"scp\s+.*:/etc/age/key\.txt",
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, body), (
            f"backup script contains forbidden cleartext write pattern: {pat}"
        )


def test_backup_script_fails_loud_on_missing_dependencies():
    """Refacto 2026-05-21: now requires openssl (preinstalled macOS) AND
    bubble-get-keychain (via skill auth Flow 3). Both must be preflight-checked."""
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # Strict mode
    assert "set -euo pipefail" in body
    # Preflight checks for both binaries
    assert "command -v openssl" in body, (
        "must preflight-check for openssl and fail loud if missing"
    )
    assert "command -v bubble-get-keychain" in body, (
        "must preflight-check for bubble-get-keychain (skill auth Flow 3)"
    )


def test_backup_script_prints_passphrase_warning():
    body = BACKUP_SCRIPT.read_text(encoding="utf-8")
    # Operator-facing warning about passphrase storage.
    # The Bureau-de-Cadre voice: in French, explicit, no jargon.
    assert "1Password" in body or "gestionnaire" in body.lower(), (
        "must instruct operator to store the passphrase in a password manager"
    )


# ---------- restore script content checks ----------


def test_restore_script_decrypts_to_stdout_only():
    body = RESTORE_SCRIPT.read_text(encoding="utf-8")
    # The restore script must decrypt to stdout — operator pipes it via
    # SSH to install on a fresh Morty. Never write cleartext to a local
    # file.
    assert "age" in body
    assert "--decrypt" in body
    # No local cleartext write paths.
    forbidden = [
        r">\s*/tmp/.*key",
        r"tee\s+/tmp/.*key",
        r"mktemp",
        r"cp\s+.*key\.txt",
    ]
    for pat in forbidden:
        assert not re.search(pat, body), (
            f"restore script must not write cleartext to disk: {pat}"
        )


def test_restore_script_documents_install_command():
    body = RESTORE_SCRIPT.read_text(encoding="utf-8")
    # The operator-facing example in the script header must show the
    # canonical install pipeline `... | ssh root@new-morty 'install -m 400 /dev/stdin /etc/age/key.txt'`.
    assert "install -m 400" in body
    assert "/etc/age/key.txt" in body
    assert "/dev/stdin" in body


def test_restore_script_strict_mode_and_dependencies():
    """Refacto 2026-05-21: same as backup, now uses openssl + keychain."""
    body = RESTORE_SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in body
    assert "command -v openssl" in body
    assert "command -v bubble-get-keychain" in body
    # Must decrypt via openssl (not age)
    assert "openssl enc" in body
    assert "-aes-256-cbc" in body
    assert "-pbkdf2" in body
    assert "-d" in body  # decrypt flag
