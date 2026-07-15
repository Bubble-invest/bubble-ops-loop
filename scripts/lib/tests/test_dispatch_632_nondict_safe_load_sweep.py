"""Tests for #632 — the SAFE-LOAD SWEEP follow-up to #593.

#593 (test_dispatch_593_nondict_queue_yaml.py) fixed the
`yaml.safe_load(...) or {}` -> `.get()` AttributeError crash at the sites
INSIDE (or called from within) `materialize_due_missions_for_tick`. Its own
docstring (lines 38-40) names the remaining sites — 244, 1310, 1361, 1419 in
its pre-merge numbering — as "OTHER functions, out of scope for #593, left
untouched per explicit instruction (separate sweep card)." This is that card.

The anti-pattern: `data = yaml.safe_load(text) or {}`. `or {}` only rescues a
FALSY load (None / empty doc). A non-empty top-level LIST is truthy, so it
slips through, and the subsequent `.get(...)` raises
`AttributeError: 'list' object has no attribute 'get'`, propagating out of the
function.

The four functions swept here, each guarded per its OWN local fallback
convention (NOT a uniform behavior — #593 established the right fallback
differs per site):

  - `_scan_mgmt_notes`            — fail-open: a non-dict note is treated as
                                     unconsumed, mirroring `except: return True`.
  - `_drainable_kinds_for_layer`  — `if not isinstance(dept, dict): return set()`
                                     mirroring `except: return set()`.
  - `_drainable_kinds_for_queue`  — same, `return set()`.
  - `_any_mission_fired_today_for_layer` — `return False`, mirroring
                                     `except: return False`.

Every test below FAILS on main (real AttributeError from the unguarded
`.get()`) and passes with the isinstance guard. Well-formed data still
processes, and the malformed doc is skipped / failed-over per local
convention — never repaired or deleted.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    _scan_mgmt_notes,
    _drainable_kinds_for_layer,
    _drainable_kinds_for_queue,
    _any_mission_fired_today_for_layer,
)

# UTC anchor, same convention as the sibling #593 / #442 test files.
_NOW = datetime(2026, 6, 21, 10, 30, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _write_yaml(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(doc, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Site 1 — _scan_mgmt_notes  (fail-open: return True)
# --------------------------------------------------------------------------

def test_scan_mgmt_notes_list_shaped_note_does_not_crash(tmp_path: Path):
    """A queues/management/ note that is a top-level LIST must not raise
    AttributeError. Per this site's local convention (`except: return True`),
    an unreadable note fails open — treated as an unconsumed inbound note, so
    the scan returns True.

    FAILS on main: `yaml.safe_load(...) or {}` lets the truthy list through,
    then `data.get("id")` raises AttributeError('list' object has no attribute
    'get').
    """
    repo = tmp_path / "repo"
    mgmt = repo / "queues" / "management"
    _write_yaml(
        mgmt / "poison-list-note.yaml",
        [{"id": "note-1", "created_at": "2026-06-21T09:00:00+00:00"}],
    )

    # since=None → any non-consumed note counts. Must not raise.
    result = _scan_mgmt_notes(repo, None)

    assert result is True, (
        "a list-shaped note is unreadable-as-a-note → fail open (unconsumed), "
        "matching the adjacent `except Exception: return True`"
    )


def test_scan_mgmt_notes_wellformed_note_still_detected(tmp_path: Path):
    """The guard must not blind the scan to legitimate dict notes: a
    well-formed, unconsumed inbound note sitting next to a poison list note is
    still detected (returns True). The poison note is skipped (fail-open per
    this site's convention), not crashed on."""
    repo = tmp_path / "repo"
    mgmt = repo / "queues" / "management"

    # Poison list note — must be skipped, not crash.
    _write_yaml(mgmt / "poison-list-note.yaml", [{"id": "x"}])
    # Well-formed note, timestamp AFTER `since` → unconsumed.
    _write_yaml(
        mgmt / "real-note.yaml",
        {"id": "note-real", "created_at": "2026-06-21T10:00:00+00:00"},
    )

    since = datetime(2026, 6, 21, 9, 0, 0, tzinfo=timezone.utc)
    assert _scan_mgmt_notes(repo, since) is True


def test_scan_mgmt_notes_old_wellformed_note_not_counted(tmp_path: Path):
    """A well-formed dict note whose timestamp is OLDER than `since` is
    correctly NOT counted as unconsumed (returns False). No poison note here:
    this isolates the dict-parsing path from the fail-open poison path, which
    on its own would always return True. Confirms the guard didn't alter the
    normal dict-note timestamp logic."""
    repo = tmp_path / "repo"
    mgmt = repo / "queues" / "management"
    _write_yaml(
        mgmt / "old-note.yaml",
        {"id": "note-old", "created_at": "2026-06-21T08:00:00+00:00"},
    )

    since = datetime(2026, 6, 21, 9, 0, 0, tzinfo=timezone.utc)
    assert _scan_mgmt_notes(repo, since) is False


# --------------------------------------------------------------------------
# Site 2 — _drainable_kinds_for_layer  (return set())
# --------------------------------------------------------------------------

def test_drainable_kinds_for_layer_list_shaped_dept_yaml_does_not_crash(tmp_path: Path):
    """A list-shaped dept.yaml must not raise; per local convention (`except:
    return set()`) it yields an empty set — the caller then falls back to
    kind-blind, fail-open behaviour.

    FAILS on main: `dept.get("recurring_missions")` raises AttributeError on
    the truthy list.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", [{"id": "m1", "layer": 1}])

    assert _drainable_kinds_for_layer(repo, 1) == set()


def test_drainable_kinds_for_layer_wellformed_still_resolves(tmp_path: Path):
    """A well-formed dict dept.yaml still resolves creates[] for the layer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", {
        "recurring_missions": [
            {"id": "m1", "layer": 1, "creates": ["research_item"]},
            {"id": "m2", "layer": 2, "creates": ["proposal"]},
        ]
    })

    assert _drainable_kinds_for_layer(repo, 1) == {"research_item"}
    assert _drainable_kinds_for_layer(repo, 2) == {"proposal"}


# --------------------------------------------------------------------------
# Site 3 — _drainable_kinds_for_queue  (return set())
# --------------------------------------------------------------------------

def test_drainable_kinds_for_queue_list_shaped_dept_yaml_does_not_crash(tmp_path: Path):
    """A list-shaped dept.yaml must not raise; yields an empty set.

    FAILS on main: `dept.get("recurring_missions")` raises AttributeError.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", [{"id": "m1", "output_queue": "queues/research/"}])

    assert _drainable_kinds_for_queue(repo, "queues/research/") == set()


def test_drainable_kinds_for_queue_wellformed_still_resolves(tmp_path: Path):
    """A well-formed dict dept.yaml still resolves creates[] for the queue."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", {
        "recurring_missions": [
            {"id": "m1", "output_queue": "queues/research/", "creates": ["research_item"]},
            {"id": "m2", "output_queue": "queues/gates/", "creates": ["gate_card"]},
        ]
    })

    assert _drainable_kinds_for_queue(repo, "queues/research/") == {"research_item"}
    assert _drainable_kinds_for_queue(repo, "queues/gates") == {"gate_card"}


# --------------------------------------------------------------------------
# Site 4 — _any_mission_fired_today_for_layer  (return False)
# --------------------------------------------------------------------------

def test_any_mission_fired_list_shaped_dept_yaml_does_not_crash(tmp_path: Path):
    """A list-shaped dept.yaml must not raise; per local convention (`except:
    return False`) it returns False — fail toward pre-existing behaviour (no
    new fallback signal).

    FAILS on main: `dept.get("recurring_missions")` raises AttributeError.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", [{"id": "m1", "layer": 1}])
    today_dir = repo / "outputs" / _TODAY
    today_dir.mkdir(parents=True, exist_ok=True)

    assert _any_mission_fired_today_for_layer(repo, today_dir, 1, now_utc=_NOW) is False


def test_any_mission_fired_wellformed_still_resolves(tmp_path: Path):
    """A well-formed dict dept.yaml still resolves: with no prior marker the
    function returns False, and with a prior-tick marker it returns True."""
    from scripts.lib.dispatch_helpers import write_last_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_yaml(repo / "dept.yaml", {
        "recurring_missions": [
            {"id": "m1", "layer": 1, "creates": ["research_item"]},
        ]
    })
    today_dir = repo / "outputs" / _TODAY
    today_dir.mkdir(parents=True, exist_ok=True)

    # No marker yet → False.
    assert _any_mission_fired_today_for_layer(repo, today_dir, 1, now_utc=_NOW) is False

    # Stamp a PRIOR-tick marker (strictly before _NOW) for m1 → True.
    prior = datetime(2026, 6, 21, 9, 0, 0, tzinfo=timezone.utc)
    write_last_run(today_dir / "missions" / "m1", prior)
    assert _any_mission_fired_today_for_layer(repo, today_dir, 1, now_utc=_NOW) is True
