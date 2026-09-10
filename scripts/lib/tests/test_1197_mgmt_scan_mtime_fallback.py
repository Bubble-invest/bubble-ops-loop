"""#1197 — _scan_mgmt_notes must NOT fail open forever on undated notes.

Before #1197 an undated yaml (no parseable created_at) not in .consumed.json
returned True (unconsumed) on EVERY scan, so once a dept's research queue
emptied, C.mgmt re-fired layer_1 every quiet tick fleet-wide (observed on ben
2026-09-09: 41 stale top-level yaml, 35 with no created_at). Fix: fall back to
the file MTIME — an undated note older than the .last-mgmt-scan marker counts as
already-seen; only one newer than the marker retriggers L1.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import _scan_mgmt_notes  # noqa: E402


def _mgmt(repo: Path) -> Path:
    d = repo / "queues" / "management"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_undated_note_older_than_marker_is_not_unconsumed(tmp_path: Path):
    mgmt = _mgmt(tmp_path)
    marker = datetime(2026, 6, 1, tzinfo=timezone.utc)
    p = mgmt / "old-undated.yaml"
    p.write_text(yaml.safe_dump({"kind": "management_note", "note": "no created_at"}),
                 encoding="utf-8")
    old = (marker - timedelta(days=30)).timestamp()
    os.utime(p, (old, old))  # mtime BEFORE the marker
    assert _scan_mgmt_notes(tmp_path, marker) is False, (
        "an undated note older than the marker must be treated as already-seen "
        "via the mtime fallback, not fail open forever (#1197)"
    )


def test_undated_note_newer_than_marker_still_fires_once(tmp_path: Path):
    mgmt = _mgmt(tmp_path)
    marker = datetime(2026, 6, 1, tzinfo=timezone.utc)
    p = mgmt / "new-undated.yaml"
    p.write_text(yaml.safe_dump({"kind": "management_note", "note": "just arrived"}),
                 encoding="utf-8")
    new = (marker + timedelta(days=1)).timestamp()
    os.utime(p, (new, new))  # mtime AFTER the marker
    assert _scan_mgmt_notes(tmp_path, marker) is True, (
        "a genuinely new undated note (mtime after the marker) must still fire L1 once"
    )


def test_dated_note_behavior_unchanged(tmp_path: Path):
    """created_at still wins when present (mtime is only the fallback)."""
    mgmt = _mgmt(tmp_path)
    marker = datetime(2026, 6, 1, tzinfo=timezone.utc)
    old_dated = mgmt / "old-dated.yaml"
    old_dated.write_text(yaml.safe_dump({"id": "n1", "created_at": "2026-05-01T00:00:00Z"}),
                         encoding="utf-8")
    # even if its mtime is fresh, an old created_at means already-seen
    fresh = (marker + timedelta(days=5)).timestamp()
    os.utime(old_dated, (fresh, fresh))
    assert _scan_mgmt_notes(tmp_path, marker) is False
