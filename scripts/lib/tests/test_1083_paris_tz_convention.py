"""test_1083_paris_tz_convention.py — board #1083: the fleet's `<today>` date
convention is Europe/Paris, and it is ONE source of truth shared by the writer
(the live L1 gatherer, via `ctx['today']`) and every reader (the #898
`queues/research/<today>/` research gate AND the #346/#1080 `outputs/<today>/`
output-evidence / idempotence gate).

Root cause (#1083): `build_dispatch_ctx` used to key `<today>` off
`now_utc.strftime("%Y-%m-%d")` — the UTC date — while the layer timers fire on
Paris wall-clock and the live agent named its dated dirs by "today" in an
unpinned tz. Across the Paris↔UTC offset (~1–2h/day) writer and reader could
land on different date dirs → a just-written research item wasn't counted, and
an `outputs/<paris-day>/` run wasn't seen. Joris's call: align the fleet on
Europe/Paris.

Fix: `paris_today()` is THE single source of truth; `build_dispatch_ctx` derives
`ctx['today']` from it, so both gates and the writer share one Paris date.

Run: python3 -m pytest scripts/lib/tests/test_1083_paris_tz_convention.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone

import yaml

from scripts.lib import dispatch_helpers as dh
from scripts.lib.dispatch_helpers import build_dispatch_ctx, paris_today, write_last_run

UTC = timezone.utc

# 23:30 UTC on a WINTER day: Paris is UTC+1, so Paris-local is already the NEXT
# calendar day (00:30). This is the exact offset window #1083 is about.
WINTER_BOUNDARY = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
WINTER_UTC_DATE = "2026-01-15"      # what the OLD (UTC) convention produced
WINTER_PARIS_DATE = "2026-01-16"    # what the NEW (Paris) convention produces

# 23:30 UTC on a SUMMER day: Paris is UTC+2 (CEST) — DST-correct, not a hardcoded
# offset — so Paris-local is 01:30 the next day.
SUMMER_BOUNDARY = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)
SUMMER_PARIS_DATE = "2026-07-16"

# Midday: same date in both zones (no boundary) — the common case must be a no-op.
MIDDAY = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _has_paris_tzdata() -> bool:
    """True iff this box has the Europe/Paris tz database (zoneinfo). Without it
    `_PARIS` falls back to UTC and paris_today degrades to the UTC date — the
    documented fail-safe. The boundary assertions only hold with real tzdata."""
    return dh._PARIS is not timezone.utc


import pytest

requires_tzdata = pytest.mark.skipif(
    not _has_paris_tzdata(),
    reason="no Europe/Paris tz database on this box (paris_today degrades to UTC)",
)


def _research_item(repo, date_str, name="lead-1.yaml", kind="research_item"):
    d = repo / "queues" / "research" / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(yaml.safe_dump({"kind": kind, "id": name}), encoding="utf-8")


# ── the helper itself ────────────────────────────────────────────────────────

@requires_tzdata
def test_paris_today_crosses_utc_day_boundary_winter():
    """23:30 UTC in winter is already the next day in Paris."""
    assert paris_today(WINTER_BOUNDARY) == WINTER_PARIS_DATE
    assert paris_today(WINTER_BOUNDARY) != WINTER_UTC_DATE


@requires_tzdata
def test_paris_today_is_dst_aware_summer():
    """DST is read from the tz database, not a hardcoded +1 (UTC+2 in July)."""
    assert paris_today(SUMMER_BOUNDARY) == SUMMER_PARIS_DATE


@requires_tzdata
def test_paris_today_midday_same_date():
    """Away from the boundary, Paris and UTC dates coincide (no surprise shift)."""
    assert paris_today(MIDDAY) == "2026-01-15"


def test_paris_today_naive_input_assumed_utc():
    """A naive datetime is treated as UTC (mirrors _to_paris' #713 tolerance),
    so a stray naive `now` can never crash the date computation."""
    naive = datetime(2026, 1, 15, 12, 0)
    assert paris_today(naive) == paris_today(MIDDAY)


# ── build_dispatch_ctx uses the Paris date (single source of truth) ───────────

@requires_tzdata
def test_ctx_today_is_paris_not_utc(tmp_path):
    ctx = build_dispatch_ctx(tmp_path, now_utc=WINTER_BOUNDARY, materialize=False)
    assert ctx["today"] == WINTER_PARIS_DATE
    assert ctx["today"] != WINTER_UTC_DATE
    assert ctx["today_dir"].endswith(f"outputs/{WINTER_PARIS_DATE}")


# ── writer/reader agree across the boundary: the #898 research gate ───────────

@requires_tzdata
def test_research_gate_reads_the_paris_subdir(tmp_path):
    """An item the (Paris) writer drops into queues/research/<paris-today>/ IS
    counted by the gate — because the gate now scans the same Paris subdir."""
    _research_item(tmp_path, WINTER_PARIS_DATE)
    ctx = build_dispatch_ctx(tmp_path, now_utc=WINTER_BOUNDARY, materialize=False)
    assert ctx["has_research_items"] is True


@requires_tzdata
def test_research_gate_ignores_the_old_utc_subdir(tmp_path):
    """An item under the OLD UTC-named subdir (the transition hazard) is NOT what
    the gate counts at the boundary — the gate keys on the Paris date. This is
    the exact miss #1083 fixes, shown from the reader side: pre-fix the gate
    scanned <utc-date> and would have (wrongly, for a Paris writer) looked here."""
    _research_item(tmp_path, WINTER_UTC_DATE)
    ctx = build_dispatch_ctx(tmp_path, now_utc=WINTER_BOUNDARY, materialize=False)
    assert ctx["has_research_items"] is False


# ── the SAME Paris date drives the #346/#1080 outputs-evidence gate ───────────

@requires_tzdata
def test_outputs_last_run_gate_uses_the_same_paris_today(tmp_path):
    """L1's .last-run written under the Paris day IS read back as
    layer_1_last_run_today; a marker under the UTC day is invisible — proving the
    idempotence/evidence gate and the research gate share one clock."""
    write_last_run(tmp_path / "outputs" / WINTER_PARIS_DATE / "1", when=WINTER_BOUNDARY)
    write_last_run(tmp_path / "outputs" / WINTER_UTC_DATE / "1", when=WINTER_BOUNDARY)
    ctx = build_dispatch_ctx(tmp_path, now_utc=WINTER_BOUNDARY, materialize=False)
    # Reader looked in the Paris dir → found the marker.
    assert ctx["layer_1_last_run_today"] is not None
    # And it's the Paris dir specifically that today_dir points at.
    assert ctx["today_dir"].endswith(f"outputs/{WINTER_PARIS_DATE}")
