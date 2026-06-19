"""test_loop_backup.py — backup-execution decision logic.

Context ({{OPERATOR}} 2026-06-01): a per-dept BACKUP runner fires twice a day
(morning + afternoon). For each dept it must decide: is the persistent
/loop alive (recent heartbeat) → SKIP, or dead/parked (stale heartbeat)
→ run ONE backup dispatch tick. This module is the pure decision; the
bash wrapper does the `claude -p` + flock.

`backup_decision(latest_heartbeat_epoch, now_epoch, stale_after_sec)`
returns:
    {"action": "skip",  "reason": "loop alive (heartbeat fresh)"}      # fresh
    {"action": "run",   "reason": "loop stale (… )"}                    # stale
    {"action": "run",   "reason": "no heartbeat found"}                # never ticked

Rules:
  - latest_heartbeat_epoch is None  -> run (no evidence the loop ever ran)
  - age = now - latest;  age <= stale_after_sec -> skip (alive)
  - age >  stale_after_sec          -> run (dead/parked → back it up)
  - a heartbeat in the FUTURE (clock skew) is treated as fresh -> skip

TDD: written BEFORE the function exists. RED -> GREEN.
"""
from __future__ import annotations

import json

import pytest

from scripts.lib.loop_backup import (
    HB_BACKUP_FAILED,
    HB_BACKUP_RAN,
    HB_DEGRADED_L4,
    append_event,
    append_external_heartbeat,
    backup_decision,
    format_event,
    format_external_heartbeat,
    latest_heartbeat_epoch,
    latest_per_dept,
    read_events,
)


HOUR = 3600
STALE = 90 * 60  # 90 minutes, {{OPERATOR}}-approved threshold


def test_fresh_heartbeat_skips():
    now = 1_000_000
    hb = now - 10 * 60  # 10 min ago
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_stale_heartbeat_runs():
    now = 1_000_000
    hb = now - 120 * 60  # 2h ago > 90m
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "run"


def test_exactly_at_threshold_is_skip():
    """age == threshold → still considered alive (boundary inclusive)."""
    now = 1_000_000
    hb = now - STALE
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_one_second_past_threshold_runs():
    now = 1_000_000
    hb = now - (STALE + 1)
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "run"


def test_no_heartbeat_runs():
    d = backup_decision(None, 1_000_000, STALE)
    assert d["action"] == "run"
    assert "no heartbeat" in d["reason"].lower()


def test_future_heartbeat_treated_as_fresh():
    """Clock skew: heartbeat slightly in the future must not trigger a run."""
    now = 1_000_000
    hb = now + 30  # 30s in the future
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_reason_is_present_and_descriptive():
    now = 1_000_000
    run = backup_decision(now - 3 * HOUR, now, STALE)
    skip = backup_decision(now - 60, now, STALE)
    assert run["reason"] and "stale" in run["reason"].lower()
    assert skip["reason"] and "fresh" in skip["reason"].lower()


def test_threshold_is_respected_per_call():
    """A caller can pass a tighter/looser threshold."""
    now = 1_000_000
    hb = now - 30 * 60  # 30 min ago
    assert backup_decision(hb, now, 20 * 60)["action"] == "run"   # 20m thresh → stale
    assert backup_decision(hb, now, 60 * 60)["action"] == "skip"  # 60m thresh → fresh


# ─── Event log ({{OPERATOR}} msg 1171) ──────────────────────────────────────────
#
# Every fire appends one JSON line per dept so the cockpit can surface the
# safety-net result in the front end. Reader/writer share this module.


def test_format_event_minimal():
    ev = format_event("maya", "skip", "loop alive", ts="2026-06-01T12:00:00Z")
    assert ev == {
        "ts": "2026-06-01T12:00:00Z",
        "slug": "maya",
        "action": "skip",
        "reason": "loop alive",
    }


def test_format_event_run_carries_age_and_exit():
    ev = format_event("tony", "run", "loop stale", age_sec=9000, exit_code=0,
                      ts="2026-06-01T12:00:01Z")
    assert ev["action"] == "run"
    assert ev["age_sec"] == 9000
    assert ev["exit"] == 0


def test_append_then_read_roundtrip(tmp_path):
    path = str(tmp_path / "state" / "loop-backup.jsonl")  # parent created on write
    append_event(path, format_event("maya", "skip", "alive", ts="2026-06-01T08:00:00Z"))
    append_event(path, format_event("tony", "run", "stale", age_sec=9000,
                                    exit_code=0, ts="2026-06-01T08:00:01Z"))
    events = read_events(path)
    assert [e["slug"] for e in events] == ["maya", "tony"]      # chronological
    assert events[1]["exit"] == 0


def test_read_events_missing_file_is_empty():
    assert read_events("/no/such/path.jsonl") == []


def test_read_events_skips_blank_and_garbage_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"ts": "t1", "slug": "maya", "action": "skip", "reason": "x"}) + "\n"
        "\n"                                   # blank
        "not json at all\n"                    # garbage — must be skipped
        "[1,2,3]\n"                            # valid json but not a dict — skipped
        + json.dumps({"ts": "t2", "slug": "tony", "action": "run", "reason": "y"}) + "\n",
        encoding="utf-8",
    )
    events = read_events(str(path))
    assert [e["slug"] for e in events] == ["maya", "tony"]


def test_read_events_filter_by_slug_and_limit(tmp_path):
    path = tmp_path / "log.jsonl"
    for i in range(5):
        append_event(str(path), format_event("maya", "skip", f"r{i}", ts=f"t{i}"))
        append_event(str(path), format_event("tony", "skip", f"r{i}", ts=f"t{i}"))
    maya = read_events(str(path), slug="maya")
    assert len(maya) == 5 and all(e["slug"] == "maya" for e in maya)
    last2 = read_events(str(path), slug="maya", limit=2)
    assert [e["reason"] for e in last2] == ["r3", "r4"]


def test_latest_per_dept_keeps_most_recent():
    events = [
        {"ts": "t1", "slug": "maya", "action": "run", "reason": "a"},
        {"ts": "t2", "slug": "maya", "action": "skip", "reason": "b"},
        {"ts": "t3", "slug": "tony", "action": "skip", "reason": "c"},
    ]
    latest = latest_per_dept(events)
    assert latest["maya"]["reason"] == "b"     # second maya event wins
    assert latest["tony"]["reason"] == "c"


# ── Regression: heartbeat timestamp parser accepts both wire formats ──────
# Bug 2026-06-04: _ISO_RE required a literal "Z", so the datetime.isoformat()
# form ("...SS.ffffff+00:00") never matched and latest_heartbeat_epoch fell
# back to file mtime — making a frozen-date loop read as false-fresh.

def test_latest_heartbeat_parses_microsecond_offset_form(tmp_path):
    from scripts.lib.loop_backup import latest_heartbeat_epoch
    import datetime as _dt
    d = tmp_path / "2026-06-02"
    d.mkdir()
    # Maya-style line (microseconds + +00:00 offset, no trailing Z)
    (d / "heartbeat.log").write_text(
        "2026-06-02T13:30:35.931407+00:00 tick L2 OK\n", encoding="utf-8")
    ep = latest_heartbeat_epoch(str(tmp_path))
    assert ep is not None
    # Must be the IN-FILE time (stale), NOT the fresh file mtime.
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    assert (got.year, got.month, got.day, got.hour) == (2026, 6, 2, 13)


def test_latest_heartbeat_parses_z_suffix_form(tmp_path):
    from scripts.lib.loop_backup import latest_heartbeat_epoch
    import datetime as _dt
    d = tmp_path / "2026-06-04"
    d.mkdir()
    (d / "heartbeat.log").write_text(
        "2026-06-04T08:40:28Z tick ok\n", encoding="utf-8")
    ep = latest_heartbeat_epoch(str(tmp_path))
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    assert (got.year, got.month, got.day, got.hour) == (2026, 6, 4, 8)


def test_latest_heartbeat_picks_newest_across_formats(tmp_path):
    """Mixed-format files: the newest real timestamp wins (not mtime)."""
    from scripts.lib.loop_backup import latest_heartbeat_epoch
    import datetime as _dt
    (tmp_path / "2026-06-02").mkdir()
    (tmp_path / "2026-06-02" / "heartbeat.log").write_text(
        "2026-06-02T13:30:35.931407+00:00 tick\n", encoding="utf-8")
    (tmp_path / "2026-06-04").mkdir()
    (tmp_path / "2026-06-04" / "heartbeat.log").write_text(
        "2026-06-04T08:40:28Z tick\n", encoding="utf-8")
    ep = latest_heartbeat_epoch(str(tmp_path))
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    assert got.day == 4


# ── Truthful external heartbeat (Rick 2026-06-19) ─────────────────────────
#
# When the live loop is stale the floor writes a TRUTHFUL liveness line into
# the dept's OWN heartbeat.log encoding the real OUTCOME (ran / failed /
# degraded), collapsing the two channels (freshness vs loop-backup.jsonl) into
# one signal. The line must keep the `<iso> tick ...` shape so the existing
# freshness parser (_ISO_RE) and every consumer keep working.


def test_external_hb_backup_ran_shape():
    line = format_external_heartbeat(
        HB_BACKUP_RAN, layer=2, exit_code=0, ts="2026-06-18T20:00:00Z")
    assert line == "2026-06-18T20:00:00Z tick BACKUP-RAN-FOR-DEPT layer=2 exit=0"


def test_external_hb_backup_failed_says_dept_down():
    line = format_external_heartbeat(
        HB_BACKUP_FAILED, exit_code=1, ts="2026-06-18T20:00:00Z")
    assert line == "2026-06-18T20:00:00Z tick BACKUP-FAILED exit=1 — dept DOWN"
    assert "dept DOWN" in line  # the honest "I'm down" signal that was missing


def test_external_hb_degraded_l4():
    line = format_external_heartbeat(HB_DEGRADED_L4, ts="2026-06-18T21:00:00Z")
    assert line == "2026-06-18T21:00:00Z tick DEGRADED-L4 carried-over"


def test_external_hb_unknown_outcome_raises():
    with pytest.raises(ValueError):
        format_external_heartbeat("NOT-A-REAL-OUTCOME")


def test_external_hb_line_is_parseable_by_freshness_reader(tmp_path):
    """A BACKUP-RAN truthful line the floor writes MUST be read back as the
    latest heartbeat by latest_heartbeat_epoch — i.e. it doubles as a real
    freshness signal, not just human text (the dept WAS serviced)."""
    import datetime as _dt
    d = tmp_path / "2026-06-18"
    d.mkdir()
    hb = d / "heartbeat.log"
    # The dept's own last (stale) line, then the floor's truthful append.
    hb.write_text("2026-06-18T07:30:00Z tick L1 ok\n", encoding="utf-8")
    written = append_external_heartbeat(
        str(hb), HB_BACKUP_RAN, layer=2, exit_code=0, ts="2026-06-18T22:00:00Z")
    assert written == "2026-06-18T22:00:00Z tick BACKUP-RAN-FOR-DEPT layer=2 exit=0"
    ep = latest_heartbeat_epoch(str(tmp_path))
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    # The floor's 22:00 BACKUP-RAN line is now the freshest — parser reads it.
    assert (got.hour, got.day) == (22, 18)


def test_append_external_heartbeat_creates_parent_and_appends(tmp_path):
    hb = tmp_path / "2026-06-18" / "heartbeat.log"  # parent missing on purpose
    append_external_heartbeat(str(hb), HB_BACKUP_RAN, layer=3, exit_code=0,
                              ts="2026-06-18T18:00:00Z")
    append_external_heartbeat(str(hb), HB_DEGRADED_L4, ts="2026-06-18T21:00:00Z")
    lines = hb.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "2026-06-18T18:00:00Z tick BACKUP-RAN-FOR-DEPT layer=3 exit=0",
        "2026-06-18T21:00:00Z tick DEGRADED-L4 carried-over",
    ]


def test_external_hb_backup_ran_defaults_exit_zero():
    """The OK path always records exit=0 explicitly even if not passed."""
    line = format_external_heartbeat(HB_BACKUP_RAN, layer=1, ts="t")
    assert line.endswith("BACKUP-RAN-FOR-DEPT layer=1 exit=0")


# ── BACKUP-FAILED must NOT read as fresh (the false-fresh trap) ────────────
#
# The crux of the truthful-heartbeat fix: a BACKUP-FAILED line carries a
# CURRENT timestamp, but it means "dept DOWN". The freshness reader must NOT
# treat it as a liveness signal — otherwise the floor would stop re-firing and
# the watchdog would read the dead dept as alive (the exact false-fresh bug).


def test_backup_failed_line_does_not_read_as_fresh(tmp_path):
    """A fresh BACKUP-FAILED line must fall back to the prior REAL heartbeat,
    NOT count as a new fresh tick."""
    import datetime as _dt
    d = tmp_path / "2026-06-18"
    d.mkdir()
    (d / "heartbeat.log").write_text(
        "2026-06-18T07:30:00Z tick L1 ok\n"                          # real, stale
        "2026-06-18T22:00:00Z tick BACKUP-FAILED exit=1 — dept DOWN\n",  # fresh-but-down
        encoding="utf-8",
    )
    ep = latest_heartbeat_epoch(str(tmp_path))
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    # Must be the 07:30 REAL tick (stale), NOT the 22:00 down-marker.
    assert (got.hour, got.day) == (7, 18), (
        "BACKUP-FAILED leaked through as a fresh liveness signal (false-fresh)"
    )


def test_backup_ran_line_does_read_as_fresh(tmp_path):
    """BACKUP-RAN means the dept WAS serviced → it IS a fresh liveness signal."""
    import datetime as _dt
    d = tmp_path / "2026-06-18"
    d.mkdir()
    (d / "heartbeat.log").write_text(
        "2026-06-18T07:30:00Z tick L1 ok\n"
        "2026-06-18T18:00:00Z tick BACKUP-RAN-FOR-DEPT layer=3 exit=0\n",
        encoding="utf-8",
    )
    ep = latest_heartbeat_epoch(str(tmp_path))
    got = _dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
    assert (got.hour, got.day) == (18, 18)


def test_only_backup_failed_lines_never_fall_back_to_mtime(tmp_path):
    """A file with ONLY down-markers must NOT use mtime (false-fresh) — it
    returns None (no real liveness ever seen) so the dept stays stale."""
    d = tmp_path / "2026-06-18"
    d.mkdir()
    hb = d / "heartbeat.log"
    hb.write_text(
        "2026-06-18T22:00:00Z tick BACKUP-FAILED exit=1 — dept DOWN\n",
        encoding="utf-8",
    )
    # mtime is "now" (fresh); the down-marker must still win → None.
    assert latest_heartbeat_epoch(str(tmp_path)) is None


def test_no_iso_lines_still_falls_back_to_mtime(tmp_path):
    """Back-compat: a heartbeat with no ISO ts (and no down-marker) still falls
    back to mtime as before."""
    d = tmp_path / "2026-06-18"
    d.mkdir()
    (d / "heartbeat.log").write_text("heartbeat\n", encoding="utf-8")
    ep = latest_heartbeat_epoch(str(tmp_path))
    assert ep is not None  # mtime fallback preserved
