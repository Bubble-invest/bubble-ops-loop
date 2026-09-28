"""#1587 — `_scan_mgmt_notes` must use max(created_at, delivered_at), not
`created_at` alone, when comparing a note against the `.last-mgmt-scan`
watermark.

Root cause (reproduced live by the #1584 review):
`scripts/dispatch_directives.py` stamps `delivered_at` on the CHILD copy at
delivery time, which can be strictly later than `created_at` for a directive
that sat queued before being routed. `_scan_mgmt_notes` compared only
`created_at` against `since`, so a mid-day re-wake whose `since` fell between
`created_at` and `delivered_at` treated the directive as already-seen even
though it was never actually delivered — same-day handling was silently
defeated (up to ~1 day delay). Live example: directive-accountant-20260926-01
has created_at 2026-09-26T20:10:00+00:00 and delivered_at
2026-09-27T19:00:55Z; with since=2026-09-27T07:30Z the scan wrongly returned
False.

Fix: use `max(created_at, delivered_at)` (when `delivered_at` is present and
parseable) as the effective timestamp — see
`_note_effective_created_at()` in scripts/lib/dispatch_helpers.py. Must never
drift from console/services/mgmt_note_state.py:scan_mgmt_inbox() (see that
module's docstring).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import _scan_mgmt_notes  # noqa: E402


def _mgmt_dir(repo: Path) -> Path:
    d = repo / "queues" / "management"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_note(d: Path, name: str, **fields) -> None:
    (d / f"{name}.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")


# Live example shape from the #1587 card.
_CREATED_AT = "2026-09-26T20:10:00+00:00"
_DELIVERED_AT = "2026-09-27T19:00:55Z"
_SINCE = datetime(2026, 9, 27, 7, 30, tzinfo=timezone.utc)


def test_delivery_gap_created_before_since_delivered_after_is_unconsumed(tmp_path: Path):
    """(a) created_at < since < delivered_at must give unconsumed=True — the
    directive was delivered AFTER the re-wake's watermark, so it must still
    be surfaced even though created_at alone predates the watermark."""
    mgmt = _mgmt_dir(tmp_path)
    _write_note(
        mgmt, "directive-accountant-20260926-01",
        directive_id="directive-accountant-20260926-01",
        kind="directive",
        target_dept="accountant",
        created_at=_CREATED_AT,
        delivered_at=_DELIVERED_AT,
    )
    assert _scan_mgmt_notes(tmp_path, since=_SINCE) is True


def test_no_delivered_at_gives_old_created_at_only_behaviour(tmp_path: Path):
    """(b) A note with no delivered_at falls back to the old created_at-only
    comparison — created_at < since → already-seen (False)."""
    mgmt = _mgmt_dir(tmp_path)
    _write_note(
        mgmt, "directive-no-delivery-stamp",
        directive_id="directive-no-delivery-stamp",
        kind="directive",
        created_at=_CREATED_AT,
    )
    assert _scan_mgmt_notes(tmp_path, since=_SINCE) is False


def test_consumed_id_skipped_even_when_delivered_at_after_since(tmp_path: Path):
    """(c) A note whose id is already in .consumed.json is skipped
    unconditionally, even though delivered_at > since would otherwise mark
    it unconsumed — the .consumed.json check stays authoritative."""
    mgmt = _mgmt_dir(tmp_path)
    note_id = "directive-accountant-20260926-01"
    (mgmt / ".consumed.json").write_text(json.dumps({note_id: {}}), encoding="utf-8")
    _write_note(
        mgmt, note_id,
        directive_id=note_id,
        kind="directive",
        target_dept="accountant",
        created_at=_CREATED_AT,
        delivered_at=_DELIVERED_AT,
    )
    assert _scan_mgmt_notes(tmp_path, since=_SINCE) is False


def test_malformed_delivered_at_is_ignored_gracefully(tmp_path: Path):
    """(d) A malformed delivered_at must not raise and must not itself count
    — falls back to created_at alone (which is < since here → already-seen)."""
    mgmt = _mgmt_dir(tmp_path)
    _write_note(
        mgmt, "directive-bad-delivered-at",
        directive_id="directive-bad-delivered-at",
        kind="directive",
        created_at=_CREATED_AT,
        delivered_at="not-a-timestamp",
    )
    assert _scan_mgmt_notes(tmp_path, since=_SINCE) is False


def test_malformed_delivered_at_with_fresh_created_at_still_fires(tmp_path: Path):
    """(d, continued) A malformed delivered_at alongside a genuinely fresh
    created_at must not suppress the real signal — created_at alone still
    drives the result."""
    mgmt = _mgmt_dir(tmp_path)
    _write_note(
        mgmt, "directive-bad-delivered-at-fresh",
        directive_id="directive-bad-delivered-at-fresh",
        kind="directive",
        created_at="2026-09-28T00:00:00+00:00",
        delivered_at="not-a-timestamp",
    )
    assert _scan_mgmt_notes(tmp_path, since=_SINCE) is True
