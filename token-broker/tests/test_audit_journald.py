"""Audit journald output — opt-in journald emission for cross-service correlation.

Per Joris's Q3 review feedback on Step 3b: when Layer 4 mandate-guardian needs
to correlate broker events with other systemd-managed services on Morty
(claude-agent-morty, telegram-watchdog, etc.), file-only JSONL is insufficient.
journald gives `journalctl -u bubble-token-broker` filtering + structured
search via `journalctl _COMM=bubble-token-broker --output=json`.

Design (3 modes via `journal` constructor arg):
  - False (default): file-only, unchanged backward-compat
  - True:            file AND journald (dual, redundant)
  - "only":          journald only (no file), for ephemeral hosts

Graceful degradation: if `systemd.journal` lib isn't installed, journal=True
or "only" raises a clear ImportError at __init__ time (fail-loud, not silent).
This avoids the worst case "operator thinks journald is on but nothing lands".

Notion v4 doesn't mandate journald — it's a v1 → v2 hardening flag, opt-in
only. The default stays file-only so existing deployments don't break.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch


# Canonical journald identifier — appears in `journalctl _COMM=...` filter
EXPECTED_COMM = "bubble-token-broker"


def test_default_is_file_only_no_journald_call(tmp_path):
    """journal=False (default) → no systemd.journal import, no send() call."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    # Pretend systemd.journal exists but spy on it — should NOT be called
    fake_journal = MagicMock()
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal}):
        a = Audit(log_path=log_path)  # default: journal=False
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_read",
            permissions=["contents:read"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
        )
    fake_journal.send.assert_not_called()
    # File still written
    assert log_path.read_text().strip()


def test_journal_true_writes_to_both_file_and_journald(tmp_path):
    """journal=True → file write AND journald send()."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    fake_journal_module = MagicMock()
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal_module
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal_module}):
        a = Audit(log_path=log_path, journal=True)
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_write_own",
            permissions=["contents:write"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
        )
    # File written
    assert log_path.read_text().strip()
    # journald.send() called exactly once
    fake_journal_module.send.assert_called_once()


def test_journal_only_skips_file_writes(tmp_path):
    """journal='only' → no file write, only journald send()."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    fake_journal_module = MagicMock()
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal_module
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal_module}):
        a = Audit(log_path=log_path, journal="only")
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_read",
            permissions=["contents:read"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
        )
    # File NOT written (or empty if pre-created)
    assert not log_path.exists() or log_path.read_text() == ""
    # journald.send() called
    fake_journal_module.send.assert_called_once()


def test_journal_send_carries_structured_fields(tmp_path):
    """journald send() receives MESSAGE + structured fields per audit schema."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    fake_journal_module = MagicMock()
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal_module
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal_module}):
        a = Audit(log_path=log_path, journal=True)
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="maya",
            repo="bubble-ops-maya",
            action="runtime_write_own",
            permissions=["contents:write"],
            actor="ops-loop-maya",
            token_ttl_minutes=60,
            status="issued",
        )
    # Inspect what was passed to journal.send()
    call_args = fake_journal_module.send.call_args
    # send() takes a positional MESSAGE + kwargs for structured fields
    args, kwargs = call_args
    # Convention: MESSAGE is human-readable; kwargs are structured
    assert "MESSAGE" in kwargs or len(args) >= 1
    # Structured fields use SYSLOG_IDENTIFIER convention to make journalctl
    # filtering work: `journalctl SYSLOG_IDENTIFIER=bubble-token-broker`
    assert kwargs.get("SYSLOG_IDENTIFIER") == EXPECTED_COMM
    # Audit schema fields are exposed as upper-case journald fields
    assert kwargs.get("DEPT") == "maya"
    assert kwargs.get("REPO") == "bubble-ops-maya"
    assert kwargs.get("ACTION") == "runtime_write_own"
    assert kwargs.get("STATUS") == "issued"


def test_journal_never_contains_token_value(tmp_path):
    """Defense-in-depth: same FORBIDDEN_FIELDS guard applies to journald path."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    fake_journal_module = MagicMock()
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal_module
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal_module}):
        a = Audit(log_path=log_path, journal=True)
        # Caller passes a forbidden field — should be dropped before journal.send()
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_write_own",
            permissions=["contents:write"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
            token="ghs_THISsecretMUSTnotEVERappearINlogs",  # caller mistake
        )
    call_args = fake_journal_module.send.call_args
    args, kwargs = call_args
    # token field must be dropped — no key, no value anywhere
    assert "TOKEN" not in kwargs
    assert "token" not in kwargs
    for v in kwargs.values():
        if isinstance(v, str):
            assert "ghs_" not in v


def test_journal_send_failure_does_not_kill_audit(tmp_path):
    """If journald is enabled but send() raises, file write still happens.

    The broker must not crash when journald is misconfigured (e.g. socket
    not reachable in a non-systemd dev environment). File audit is the
    durable channel.
    """
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    fake_journal_module = MagicMock()
    fake_journal_module.send.side_effect = OSError("no journald socket")
    fake_systemd = MagicMock()
    fake_systemd.journal = fake_journal_module
    with patch.dict(sys.modules, {"systemd": fake_systemd, "systemd.journal": fake_journal_module}):
        a = Audit(log_path=log_path, journal=True)
        # Should NOT raise — degrade gracefully
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_read",
            permissions=["contents:read"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
        )
    # File write happened despite journald failure
    line = log_path.read_text().strip()
    row = json.loads(line)
    assert row["dept"] == "fixture"


def test_journal_import_error_at_init_when_enabled(tmp_path):
    """If systemd.journal is missing AND journal=True, fail loud at __init__."""
    from src.audit import Audit

    # Force ImportError by removing systemd from sys.modules
    saved = {k: sys.modules[k] for k in list(sys.modules) if k.startswith("systemd")}
    for k in saved:
        del sys.modules[k]
    # Also block the import attempt
    import builtins
    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "systemd.journal" or name == "systemd":
            raise ImportError(f"No module named {name!r}")
        return orig_import(name, *args, **kwargs)

    try:
        with patch.object(builtins, "__import__", fake_import):
            try:
                Audit(log_path=tmp_path / "audit.jsonl", journal=True)
                raise AssertionError("expected ImportError")
            except ImportError as e:
                assert "systemd" in str(e).lower() or "journal" in str(e).lower()
    finally:
        # Restore
        sys.modules.update(saved)
