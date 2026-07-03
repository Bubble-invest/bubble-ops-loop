"""Tests for the floor-timer ladder covering the fleet's latest mission time
(board #508, Fix B, 2026-07-03).

BUG: `loop-layer{1..4}.timer` (the safety-net floor that runs a forced OODA
layer if the live /loop is stale) fired at 09:00/14:00/18:00/21:00 Europe/Paris
(each layer's min-time + a fixed +2h backup offset). The L4 floor at 21:00 is
BEFORE a daily@22:30 mission (e.g. Ben's `market_wrapup`) — so on a day the
live loop went stale before 22:30, there was NO safety net left to catch that
mission at all; the effective reliable-daily ceiling was ~21:00 Paris.

FIX (#508 Fix B): add `loop-layer4-late.timer` (23:00 Europe/Paris) as a pure
FLOOR EXTENSION — same template service (`loop-layer@4.service`), same
staleness/prereq gates in loop-backup.sh, just one more chance for L4 later
in the evening. The floor-timer schedule is generated from STATIC systemd
unit templates under deploy/templates/ (installed verbatim by
scripts/install-loop-backup.sh — there is no dept.yaml-driven timer renderer
in this repo), so the minimal correct fix matching that mechanism is a 5th
static template + registering it in the installer's LAYER_TIMERS array.

These tests parse the actual template files + installer script (no live
systemd) to prove:
  1. loop-layer4-late.timer exists, fires Unit=loop-layer@4.service (reuses
     the SAME per-layer instance as the base L4 floor — no new layer, no new
     service logic).
  2. Its OnCalendar time is >= the latest currently-known fleet mission time
     (22:30 Paris, the documented board #508 example / test_select_due_missions
     fixture value) — the floor now has a tick AT OR AFTER the latest
     configured mission.
  3. install-loop-backup.sh's LAYER_TIMERS array includes the new unit (so a
     fresh `install-loop-backup.sh` run actually installs+enables it — adding
     the template file alone would be silently inert).
  4. Persistent=true + Install/WantedBy=timers.target are present (matches
     every other floor timer's crash/sleep-recovery contract — the late timer
     must behave identically to its siblings, not be a weaker one-off).
"""
from __future__ import annotations

import re
from datetime import time as _time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = PROJECT_ROOT / "deploy" / "templates"
INSTALLER = PROJECT_ROOT / "scripts" / "install-loop-backup.sh"

# The latest daily mission time documented/tested elsewhere in this repo
# (Ben's market_wrapup, board #508's worked example — see
# scripts/lib/tests/test_select_due_missions.py and
# scripts/lib/tests/test_508_mission_granular_wake.py).
LATEST_KNOWN_MISSION_TIME = _time(22, 30)

BASE_LAYER_TIMERS = [
    TEMPLATE_DIR / "loop-layer1.timer",
    TEMPLATE_DIR / "loop-layer2.timer",
    TEMPLATE_DIR / "loop-layer3.timer",
    TEMPLATE_DIR / "loop-layer4.timer",
]
LATE_TIMER = TEMPLATE_DIR / "loop-layer4-late.timer"


def _oncalendar_times(body: str) -> "list[_time]":
    """Extract every Europe/Paris OnCalendar wall-clock time from a unit body."""
    times = []
    for m in re.finditer(
        r"OnCalendar=\*-\*-\*\s+(\d{2}):(\d{2}):\d{2}\s+Europe/Paris", body
    ):
        times.append(_time(int(m.group(1)), int(m.group(2))))
    return times


def test_late_timer_template_exists():
    assert LATE_TIMER.is_file(), (
        "deploy/templates/loop-layer4-late.timer is missing — Fix B (#508) "
        "requires a floor tick at/after the latest configured mission time"
    )


def test_late_timer_reuses_layer4_service_instance():
    body = LATE_TIMER.read_text(encoding="utf-8")
    assert "Unit=loop-layer@4.service" in body, (
        "the late floor must reuse the SAME loop-layer@4.service instance as "
        "the base L4 floor (pure schedule extension, not a new layer/service)"
    )


def test_late_timer_fires_at_or_after_latest_known_mission_time():
    body = LATE_TIMER.read_text(encoding="utf-8")
    times = _oncalendar_times(body)
    assert times, "loop-layer4-late.timer has no parseable OnCalendar line"
    assert all(t >= LATEST_KNOWN_MISSION_TIME for t in times), (
        f"loop-layer4-late.timer OnCalendar {times} must be >= "
        f"{LATEST_KNOWN_MISSION_TIME} (the latest known fleet mission time)"
    )


def test_effective_floor_schedule_covers_latest_mission_time():
    """The FULL effective floor ladder (all loop-layer*.timer templates) must
    contain at least one tick >= the latest known mission time — this is the
    actual #508 Evaluation criterion ("a floor tick at/after 22:30")."""
    all_times: "list[_time]" = []
    for tpl in BASE_LAYER_TIMERS + [LATE_TIMER]:
        assert tpl.is_file(), f"missing floor timer template: {tpl}"
        all_times.extend(_oncalendar_times(tpl.read_text(encoding="utf-8")))

    assert any(t >= LATEST_KNOWN_MISSION_TIME for t in all_times), (
        f"no floor timer fires at/after {LATEST_KNOWN_MISSION_TIME} — the "
        f"floor ladder {sorted(all_times)} would still starve a daily@22:30 "
        f"mission on a day the live loop goes stale before 21:00"
    )

    # Regression guard: without the late timer, the OLD ladder topped out at
    # 21:00 — prove that fact stays documented/true for the base four so a
    # future edit to loop-layer4.timer's OnCalendar doesn't silently make this
    # test pass for the wrong reason (i.e. the late timer stops being needed
    # only if someone also removes it, not by accident).
    base_times: "list[_time]" = []
    for tpl in BASE_LAYER_TIMERS:
        base_times.extend(_oncalendar_times(tpl.read_text(encoding="utf-8")))
    assert max(base_times) < LATEST_KNOWN_MISSION_TIME, (
        "the base 4-timer ladder now covers 22:30 on its own — the late "
        "floor extension may be redundant; re-check this test's premise"
    )


def test_installer_registers_late_timer_in_layer_timers_array():
    body = INSTALLER.read_text(encoding="utf-8")
    m = re.search(r"LAYER_TIMERS=\(([^)]*)\)", body)
    assert m, "install-loop-backup.sh: could not find LAYER_TIMERS=(...) array"
    units = m.group(1).split()
    assert "loop-layer4-late.timer" in units, (
        "loop-layer4-late.timer must be registered in install-loop-backup.sh's "
        "LAYER_TIMERS array, or a fresh install never enables it (the template "
        "file alone is inert)"
    )
    # And still contains all 4 originals — this must be additive, not a swap.
    for base in ("loop-layer1.timer", "loop-layer2.timer",
                 "loop-layer3.timer", "loop-layer4.timer"):
        assert base in units, f"{base} must remain registered (additive change only)"


@pytest.mark.parametrize("tpl", BASE_LAYER_TIMERS + [LATE_TIMER])
def test_all_floor_timers_share_the_crash_recovery_contract(tpl: Path):
    """Every floor timer (including the new late one) must carry the same
    Persistent=true + WantedBy=timers.target contract — the late timer is a
    sibling, not a weaker one-off."""
    body = tpl.read_text(encoding="utf-8")
    assert "Persistent=true" in body, f"{tpl.name} missing Persistent=true"
    assert "WantedBy=timers.target" in body, f"{tpl.name} missing WantedBy=timers.target"
