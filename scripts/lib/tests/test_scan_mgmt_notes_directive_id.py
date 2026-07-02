"""Tests for #468 — `_scan_mgmt_notes` must resolve a note's consumption key
via `directive_id` as well as `id`.

Root cause (found during #459's independent review, PR #189):
`_scan_mgmt_notes` (scripts/lib/dispatch_helpers.py) resolved the consumed-
check key as `data.get("id")` ONLY. Directive notes delivered by
`scripts/dispatch_directives.py` (its `dispatch()` payload keeps the source
draft's `directive_id` field but never adds a top-level `id` — see
dispatch_directives.py ~L171/L201) therefore could never match an entry in
`.consumed.json` keyed by `directive_id`. Once such a note's `created_at`
passed the `.last-mgmt-scan` watermark, `_scan_mgmt_notes` would fail open on
the missing `id` and return True forever, causing the dispatcher to keep
routing to L1 on an already-actioned directive — suspected root cause of
#235 (Tony's spurious-L1 mgmt-note spin).

Fix: `_scan_mgmt_notes` resolves the key as
`data.get("id") or data.get("directive_id")`, mirroring exactly what
`console/services/mgmt_note_state.py:_note_id()` now does (see #468 fix
there too — the two must never drift, per that module's docstring).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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


def test_consumed_directive_shaped_note_is_not_refired(tmp_path: Path):
    """A directive-shaped note (directive_id only, no top-level id) that is
    already recorded in .consumed.json under its directive_id must be
    treated as consumed — _scan_mgmt_notes must return False, not fail open.
    """
    mgmt = _mgmt_dir(tmp_path)
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    (mgmt / ".consumed.json").write_text(
        json.dumps({"directive-abc123": {}}), encoding="utf-8"
    )
    # created_at AFTER the watermark — without the directive_id fallback this
    # would fail open (unparseable id -> not in consumed_ids -> timestamp
    # check -> True).
    _write_note(
        mgmt, "directive-abc123",
        directive_id="directive-abc123",
        target_dept="qtest468",
        kind="directive",
        mission_id="directive-abc123",
        title="Directive from L3",
        created_at=(watermark + timedelta(days=1)).isoformat(),
    )

    assert _scan_mgmt_notes(tmp_path, since=watermark) is False


def test_unconsumed_directive_shaped_note_still_fires(tmp_path: Path):
    """A directive-shaped note whose directive_id is NOT in .consumed.json
    and whose created_at is after the watermark is genuinely unconsumed —
    _scan_mgmt_notes must still return True."""
    mgmt = _mgmt_dir(tmp_path)
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    (mgmt / ".consumed.json").write_text(
        json.dumps({"some-other-directive": {}}), encoding="utf-8"
    )
    _write_note(
        mgmt, "directive-fresh",
        directive_id="directive-fresh",
        target_dept="qtest468",
        kind="directive",
        mission_id="directive-fresh",
        title="Directive from L3",
        created_at=(watermark + timedelta(days=1)).isoformat(),
    )

    assert _scan_mgmt_notes(tmp_path, since=watermark) is True


def test_id_still_takes_priority_over_directive_id(tmp_path: Path):
    """When a note has both `id` and `directive_id` (not expected in
    practice, but the resolution order must be deterministic), `id` wins —
    matching `data.get("id") or data.get("directive_id")` short-circuit
    order."""
    mgmt = _mgmt_dir(tmp_path)
    watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
    (mgmt / ".consumed.json").write_text(
        json.dumps({"the-real-id": {}}), encoding="utf-8"
    )
    _write_note(
        mgmt, "hybrid-note",
        id="the-real-id",
        directive_id="not-the-consumed-key",
        kind="directive",
        created_at=(watermark + timedelta(days=1)).isoformat(),
    )

    assert _scan_mgmt_notes(tmp_path, since=watermark) is False
