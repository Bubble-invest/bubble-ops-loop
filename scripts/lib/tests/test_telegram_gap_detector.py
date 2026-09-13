"""test_telegram_gap_detector.py — board #1284 pt E: turn a SILENT Telegram
inbound loss into a LOUD, visible one.

The 2026-09-10→11 Claudette incident in one fixture: grammy delivered update_ids
up to 7526, the harness crashed, the fresh poller resumed from Telegram's acked
offset at 7546 — so update_ids 7527–7545 (19 of them) were acked-but-never-
delivered and lost with no trace. The ledger records …7526 then 7546; the
detector MUST see that jump and alert.

Run: python3 -m pytest scripts/lib/tests/test_telegram_gap_detector.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from scripts.lib.telegram_gap_detector import (  # noqa: E402
    Decision,
    Gap,
    LedgerEntry,
    State,
    decide,
    find_gaps,
)


def _ledger(*ids):
    return [LedgerEntry(update_id=i, ts=f"2026-09-11T00:00:{i % 60:02d}Z") for i in ids]


# ── the incident, exactly ────────────────────────────────────────────────────

def test_claudette_incident_gap_is_detected_and_alerts():
    entries = _ledger(*range(7520, 7527), *range(7546, 7550))  # 7527..7545 missing
    d = decide("claudette", entries, State(), bot_alive=True)
    assert d.alert is True
    assert len(d.new_gaps) == 1
    g = d.new_gaps[0]
    assert g.after == 7526 and g.before == 7546
    assert g.missing_count == 19          # RED if the off-by-one range is wrong
    assert g.missing_range == (7527, 7545)
    assert d.total_missing == 19
    txt = d.alert_text()
    assert "7527" in txt and "7545" in txt and "SILENT LOSS" in txt


def test_contiguous_stream_never_alerts():
    entries = _ledger(*range(100, 140))
    d = decide("claudette", entries, State(), bot_alive=True)
    assert d.alert is False
    assert d.new_gaps == []


def test_out_of_order_and_duplicate_updates_do_not_create_false_gaps():
    # grammy is in-order, but a defensive detector must not trip on dupes/reorder.
    entries = _ledger(5, 5, 6, 7, 7, 8, 9)
    assert find_gaps(entries) == []
    d = decide("x", entries, State(), bot_alive=True)
    assert d.alert is False


# ── de-dupe across ticks (no alert-storm) ────────────────────────────────────

def test_same_gap_not_realerted_next_tick():
    entries = _ledger(1, 2, 5, 6)  # gap 3-4
    first = decide("x", entries, State(), bot_alive=True)
    assert len(first.new_gaps) == 1
    # Next tick: same ledger, carry the persisted state forward.
    second = decide("x", entries, first.state, bot_alive=True)
    assert second.new_gaps == []
    assert second.alert is False


def test_a_brand_new_gap_after_an_old_one_still_alerts():
    e1 = _ledger(1, 2, 5, 6)          # gap 3-4
    d1 = decide("x", e1, State(), bot_alive=True)
    e2 = _ledger(1, 2, 5, 6, 9, 10)   # old gap 3-4 + NEW gap 7-8
    d2 = decide("x", e2, d1.state, bot_alive=True)
    assert [g.key for g in d2.new_gaps] == ["6-9"]
    assert d2.alert is True


# ── wedge (pending updates the alive poller isn't consuming) ──────────────────

def test_wedge_requires_two_consecutive_stuck_probes():
    entries = _ledger(1, 2, 3)
    d1 = decide("x", entries, State(), bot_alive=True, pending_count=4)
    assert d1.wedged is False                     # one probe is not enough
    d2 = decide("x", entries, d1.state, bot_alive=True, pending_count=4)
    assert d2.wedged is True and d2.alert is True


def test_pending_zero_resets_the_wedge_counter():
    entries = _ledger(1, 2, 3)
    d1 = decide("x", entries, State(), bot_alive=True, pending_count=4)
    d2 = decide("x", entries, d1.state, bot_alive=True, pending_count=0)
    assert d2.wedged is False
    assert d2.state.consecutive_pending_stuck == 0


# ── dead poller ──────────────────────────────────────────────────────────────

def test_dead_poller_alerts_and_is_not_reported_as_wedged():
    entries = _ledger(1, 2, 3)
    d = decide("x", entries, State(), bot_alive=False, pending_count=9)
    assert d.dead is True
    assert d.wedged is False
    assert d.alert is True
    assert "POLLER DEAD" in d.alert_text()


# ── state round-trips + is bounded ───────────────────────────────────────────

def test_state_load_dump_roundtrip_and_cap():
    s = State(alerted_gaps=[f"{i}-{i+2}" for i in range(600)], consecutive_pending_stuck=1)
    dumped = s.dump()
    assert len(dumped["alerted_gaps"]) == 500      # capped, newest kept
    reloaded = State.load(dumped)
    assert reloaded.consecutive_pending_stuck == 1


def test_bad_ledger_lines_are_skipped():
    assert LedgerEntry.from_line("") is None
    assert LedgerEntry.from_line("not json") is None
    assert LedgerEntry.from_line('{"ts":"x"}') is None        # no update_id
    assert LedgerEntry.from_line('{"update_id":"7"}') is None  # not an int
    ok = LedgerEntry.from_line('{"update_id":7,"ts":"z"}')
    assert ok is not None and ok.update_id == 7


# ── discovery: bare `telegram/` (morty/main) must be found + slug from home ────

def test_default_discover_finds_bare_telegram_dir_and_slugs_from_home(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_gapdet_driver",
        os.path.join(os.path.dirname(__file__), "..", "..", "telegram-gap-detector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dirs = [
        "/home/agent-morty/.claude/channels/telegram/",        # bare — the bug
        "/home/agent-claudette/.claude/channels/telegram-claudette/",
        "/home/agent-ben/.claude/channels/telegram-ben/",
    ]
    import glob as _glob
    monkeypatch.setattr(_glob, "glob", lambda pat: list(dirs))
    monkeypatch.setattr(os.path, "exists", lambda p: p.endswith("bot.pid"))

    specs = mod._default_discover("vps")
    by_slug = {s["slug"]: s["state_dir"] for s in specs}
    assert set(by_slug) == {"morty", "claudette", "ben"}   # morty NOT "main"
    assert by_slug["morty"] == "/home/agent-morty/.claude/channels/telegram"
