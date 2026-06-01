"""Step-11 Finding 1 (DRIFT-6, HIGH): journald audit emission must be ACTIVE
on Morty's deployed broker. The --journal CLI flag exists and the systemd
unit was passing only file-mode by default. After the Step-11 fix, the unit
sets BUBBLE_BROKER_JOURNAL=on and the wrapper forwards `--journal $...` to
the broker on every `mint` invocation.

The assertion: after one broker mint, journald must show at least one entry
with SYSLOG_IDENTIFIER=bubble-token-broker. This is a LIVE assertion against
the deployed Morty VPS (ssh hetzner), not a unit test against an in-memory
mock. It will SKIP gracefully if the ssh connection fails or the host is
unreachable — but on a healthy deployment it MUST pass.

Notion v4 ref: §"Audit & journald correlation rationale" (lines 600-720) —
the Layer 4 mandate-guardian's planned cross-service correlation view
requires `journalctl SYSLOG_IDENTIFIER=bubble-token-broker` to return real
events. Without this, that feature is silently broken in production.

Usage:
    pytest tests/test_journald_active_on_morty.py -v
    pytest tests/test_journald_active_on_morty.py -v --no-skip-ssh   # error not skip
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest


SSH_HOST = "hetzner"
SYSLOG_ID = "bubble-token-broker"


def _ssh_available() -> bool:
    """True if `ssh` binary exists AND `ssh -o BatchMode=yes hetzner true` works."""
    if shutil.which("ssh") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_HOST, "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_SSH_OK = _ssh_available()


@pytest.mark.skipif(
    not _SSH_OK, reason=f"ssh {SSH_HOST} not reachable; this test runs only against the live VPS"
)
def test_journald_has_entries_for_broker_identifier():
    """journalctl SYSLOG_IDENTIFIER=bubble-token-broker -n 5 --output=json must
    return at least 1 entry.

    Before Step-11 fix: 0 entries (the systemd unit didn't pass --journal on
    and the wrapper didn't forward it either). After fix: >=1 entry per
    broker mint (each mint emits one structured record).
    """
    proc = subprocess.run(
        [
            "ssh",
            SSH_HOST,
            f"journalctl SYSLOG_IDENTIFIER={SYSLOG_ID} -n 5 --output=json --no-pager 2>/dev/null",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    # journalctl exits 0 even when there are no matches; output is empty in that case.
    assert proc.returncode == 0, (
        f"journalctl ssh call failed: rc={proc.returncode} stderr={proc.stderr!r}"
    )

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 1, (
        f"journalctl returned 0 entries for SYSLOG_IDENTIFIER={SYSLOG_ID}. "
        f"Expected >=1 after Step-11 fix activated journald on Morty.\n"
        f"Hint: ensure the systemd unit sets `Environment=BUBBLE_BROKER_JOURNAL=on` "
        f"AND the /opt/bubble-token-broker/bin/bubble-token-broker wrapper forwards "
        f"`--journal $BUBBLE_BROKER_JOURNAL` to the broker on mint."
    )

    # Validate at least one entry parses as JSON and carries the expected fields.
    last_entry = json.loads(lines[-1])
    assert last_entry.get("SYSLOG_IDENTIFIER") == SYSLOG_ID, (
        f"unexpected SYSLOG_IDENTIFIER in journald entry: {last_entry!r}"
    )
    # Structured field expectations (audit.py emits ACTION, DEPT, REPO, STATUS)
    # Capitalization is per journald convention (UPPER_CASE fields). audit.py
    # sends them as keyword args to journal.send(); fields surface in upper
    # because journald upper-cases unprefixed user fields.
    msg = last_entry.get("MESSAGE", "")
    assert (
        "dept=" in msg or "DEPT" in last_entry or "ACTION" in last_entry
    ), f"journald entry missing structured audit fields. entry={last_entry!r}"


@pytest.mark.skipif(not _SSH_OK, reason=f"ssh {SSH_HOST} not reachable")
def test_systemd_unit_has_journal_env():
    """The ops-loop-fixture.service unit must set BUBBLE_BROKER_JOURNAL=on.

    This is the persistence assertion: even after a reboot or unit reload,
    the journald flag must remain active. Failure here means the Step-11
    fix was applied only ephemerally and won't survive a restart.
    """
    proc = subprocess.run(
        [
            "ssh",
            SSH_HOST,
            "sudo grep -E '^Environment=BUBBLE_BROKER_JOURNAL=' "
            "/etc/systemd/system/ops-loop-fixture.service || true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"ssh call failed: {proc.stderr!r}"
    assert "BUBBLE_BROKER_JOURNAL=on" in proc.stdout, (
        f"ops-loop-fixture.service is missing `Environment=BUBBLE_BROKER_JOURNAL=on`. "
        f"grep output: {proc.stdout!r}\n"
        f"Apply Step-11 fix: add the Environment= line and `sudo systemctl daemon-reload`."
    )


@pytest.mark.skipif(not _SSH_OK, reason=f"ssh {SSH_HOST} not reachable")
def test_wrapper_forwards_journal_flag():
    """The /opt/bubble-token-broker/bin/bubble-token-broker wrapper must
    forward `--journal $BUBBLE_BROKER_JOURNAL` to mint invocations.

    Without this, the systemd Environment= line is unused — the broker is
    invoked without --journal and falls back to file-only mode."""
    proc = subprocess.run(
        [
            "ssh",
            SSH_HOST,
            "cat /opt/bubble-token-broker/bin/bubble-token-broker",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"ssh cat failed: {proc.stderr!r}"
    body = proc.stdout
    # Accept either explicit `--journal "$BUBBLE_BROKER_JOURNAL"` or
    # `--journal on` constant — but the env-var form is preferred (allows
    # toggling without re-deploying the wrapper).
    assert (
        ("--journal" in body and "BUBBLE_BROKER_JOURNAL" in body)
        or "--journal on" in body
    ), (
        f"wrapper is missing --journal pass-through to the broker. body excerpt:\n{body[:600]}"
    )
