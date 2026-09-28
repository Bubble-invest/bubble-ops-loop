"""#1587 — `console/services/mgmt_note_state.py:scan_mgmt_inbox()` must use
max(created_at, delivered_at), not `created_at` alone, mirroring the fix in
`scripts/lib/dispatch_helpers.py:_scan_mgmt_notes` (that module's docstring:
the two must never drift).

Root cause / live example: see
scripts/lib/tests/test_1587_mgmt_scan_delivered_at.py. This file covers the
same cases against `scan_mgmt_inbox()` (pending_rows / consumed_count
instead of a bool), plus a parity test asserting both modules agree.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import _scan_mgmt_notes  # noqa: E402
from console.services.mgmt_note_state import scan_mgmt_inbox  # noqa: E402


def _mgmt_dir(repo: Path) -> Path:
    d = repo / "queues" / "management"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_note(d: Path, name: str, **fields) -> None:
    (d / f"{name}.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")


# Live example shape from the #1587 card.
_CREATED_AT = "2026-09-26T20:10:00+00:00"
_DELIVERED_AT = "2026-09-27T19:00:55Z"
_SINCE_ISO = "2026-09-27T07:30:00+00:00"
_SINCE_DT = datetime(2026, 9, 27, 7, 30, tzinfo=timezone.utc)


def _pending_ids(state) -> list:
    return [i.id for row in state.pending_rows for i in row.items]


def test_delivery_gap_created_before_since_delivered_after_is_pending():
    """(a) created_at < since < delivered_at gives a pending row."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
        _write_note(
            mgmt, "directive-accountant-20260926-01",
            directive_id="directive-accountant-20260926-01",
            kind="directive", target_dept="accountant",
            created_at=_CREATED_AT, delivered_at=_DELIVERED_AT,
        )
        state = scan_mgmt_inbox(root)
        assert "directive-accountant-20260926-01" in _pending_ids(state)


def test_no_delivered_at_gives_old_behaviour():
    """(b) No delivered_at falls back to created_at-only: created_at < since
    → hidden (counted as consumed)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
        _write_note(
            mgmt, "directive-no-delivery-stamp",
            directive_id="directive-no-delivery-stamp",
            kind="directive", created_at=_CREATED_AT,
        )
        state = scan_mgmt_inbox(root)
        assert "directive-no-delivery-stamp" not in _pending_ids(state)
        assert state.pending_rows == []


def test_consumed_id_skipped_even_when_delivered_at_after_since():
    """(c) .consumed.json stays authoritative even when delivered_at > since."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        note_id = "directive-accountant-20260926-01"
        (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
        (mgmt / ".consumed.json").write_text(json.dumps({note_id: {}}), encoding="utf-8")
        _write_note(
            mgmt, note_id,
            directive_id=note_id, kind="directive", target_dept="accountant",
            created_at=_CREATED_AT, delivered_at=_DELIVERED_AT,
        )
        state = scan_mgmt_inbox(root)
        assert note_id not in _pending_ids(state)
        assert state.consumed_count == 1


def test_malformed_delivered_at_is_ignored_gracefully():
    """(d) A malformed delivered_at doesn't raise, and doesn't itself make
    the note pending — falls back to created_at alone (< since → hidden)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
        _write_note(
            mgmt, "directive-bad-delivered-at",
            directive_id="directive-bad-delivered-at",
            kind="directive", created_at=_CREATED_AT,
            delivered_at="not-a-timestamp",
        )
        state = scan_mgmt_inbox(root)
        assert "directive-bad-delivered-at" not in _pending_ids(state)


def test_parity_with_dispatch_helpers_scan_mgmt_notes():
    """(e) Parity: dispatch_helpers._scan_mgmt_notes and
    console.services.mgmt_note_state.scan_mgmt_inbox must agree across every
    case above."""
    cases = [
        # (fields, expect_unconsumed)
        (dict(directive_id="c1", kind="directive", target_dept="accountant",
              created_at=_CREATED_AT, delivered_at=_DELIVERED_AT), True),
        (dict(directive_id="c2", kind="directive",
              created_at=_CREATED_AT), False),
        (dict(directive_id="c4", kind="directive",
              created_at=_CREATED_AT, delivered_at="not-a-timestamp"), False),
        (dict(directive_id="c5", kind="directive",
              created_at="2026-09-28T00:00:00+00:00",
              delivered_at="not-a-timestamp"), True),
    ]
    for fields, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgmt = _mgmt_dir(root)
            (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
            note_id = fields["directive_id"]
            _write_note(mgmt, note_id, **fields)

            dispatcher_result = _scan_mgmt_notes(root, since=_SINCE_DT)
            console_result = note_id in _pending_ids(scan_mgmt_inbox(root))

            assert dispatcher_result is expected, (note_id, "dispatcher")
            assert console_result is expected, (note_id, "console")
            assert dispatcher_result == console_result, (
                f"{note_id}: dispatch_helpers._scan_mgmt_notes and "
                f"mgmt_note_state.scan_mgmt_inbox disagree "
                f"({dispatcher_result} vs {console_result})"
            )

    # (c) consumed case, parity in a separate dir since .consumed.json is
    # global to the mgmt dir.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgmt = _mgmt_dir(root)
        note_id = "c3-consumed"
        (mgmt / ".last-mgmt-scan").write_text(_SINCE_ISO, encoding="utf-8")
        (mgmt / ".consumed.json").write_text(json.dumps({note_id: {}}), encoding="utf-8")
        _write_note(
            mgmt, note_id,
            directive_id=note_id, kind="directive",
            created_at=_CREATED_AT, delivered_at=_DELIVERED_AT,
        )
        dispatcher_result = _scan_mgmt_notes(root, since=_SINCE_DT)
        console_result = note_id in _pending_ids(scan_mgmt_inbox(root))
        assert dispatcher_result is False
        assert console_result is False
