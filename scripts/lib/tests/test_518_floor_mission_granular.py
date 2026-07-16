"""Tests for select_due_missions_for_forced_layer (card #518).

CORE BUG FIXED: the LAYER-FLOOR path (`loop-backup.sh --layer N`, the static
per-layer cron that guarantees a layer fires even when the live /loop is
dead) never called into mission-centric dispatch at all. It handed a fresh
Claude session a generic "read layers/<N>/PROMPT.md, run Layer N" prompt, and
the legacy monolithic layer prompt (e.g. agents/ben/layers/4/PROMPT.md) gates
on a single LAYER-level `outputs/<today>/<N>/.last-run` marker ("once per
day, no parallelism"). So a SECOND same-layer mission with a later `time:`
(e.g. ben's risk_control@21:00 vs a hypothetical market_wrapup@22:30, both
L4) was invisible to the floor: once risk_control fired and stamped the
layer marker, a 23:00 late floor tick would see the layer as "done" and never
dispatch market_wrapup — even though the live-loop dispatch primitive
(select_due_missions, #261/#277) has supported per-mission idempotence for
weeks.

FIX: select_due_missions_for_forced_layer(repo_dir, layer, now_utc=...) reads
dept.yaml's recurring_missions, filters to the forced layer, and returns only
the missions that are still due per their OWN per-mission
`outputs/<today>/missions/<id>/.last-run` marker — reusing is_mission_due()
and _mission_last_fired() so the idempotence model can never diverge from
the live-loop path's.

Coverage:
  1. Two same-layer missions, one already fired (per-mission marker), one
     still due at a later time → ONLY the pending one returned. This is the
     PRIMARY correctness test — proves market_wrapup dispatches specifically,
     not "the layer re-runs generically".
  2. No dept.yaml / no recurring_missions on this layer → [] (back-compat:
     caller falls back to legacy generic floor tick).
  3. A mission already fired today is excluded (no re-fire / no fire-spin).
  4. A mission not yet at its time: today is excluded.
  5. Read-only: does not stamp any .last-run marker as a side effect.
"""
from __future__ import annotations

import yaml
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.dispatch_helpers import (
    select_due_missions_for_forced_layer,
    write_last_run,
)

# 22:31 Paris (UTC+2 in June) / 21:00 Paris — mirrors test_select_due_missions.py's
# three-L4-mission scenario time anchors so the fixtures are directly comparable.
AT_22_31_UTC = datetime(2026, 6, 23, 20, 31, tzinfo=timezone.utc)  # 22:31 Paris
AT_21_00_UTC = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)   # 21:00 Paris


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "inbox" / "decisions").mkdir(parents=True)
    return tmp_path


def _write_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _mk_daily(mid: str, layer: int, time: str) -> dict:
    return {
        "id": mid,
        "layer": layer,
        "cadence": "daily",
        "time": time,
        "output_queue": "queues/research/",
        "creates": [],
    }


def _stamp_mission_lastrun(repo: Path, mid: str, when: datetime) -> None:
    today = when.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "missions" / mid, when)


def _fire_prereqs(repo: Path, when: datetime) -> None:
    """Stamp L1/L2/L3 layer markers so an L4 probe's prerequisite gate passes."""
    today = when.strftime("%Y-%m-%d")
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), when)


# ── 1. PRIMARY correctness test: market_wrapup dispatches, not risk_control ──

def test_late_floor_tick_dispatches_only_the_pending_second_mission(tmp_path: Path):
    """risk_control@21:00 already fired (per-mission marker present);
    market_wrapup@22:30 has not. At 22:31 Paris, the L4 floor tick's mission
    enumeration must return market_wrapup ONLY — proving the late floor tick
    dispatches the SECOND mission specifically, not "L4 generically" (which
    would either re-run risk_control or return nothing because the layer
    marker already exists).
    """
    repo = _mk_repo(tmp_path)
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])

    _fire_prereqs(repo, AT_21_00_UTC)

    # risk_control fired at 21:00 — per-mission marker present (as the real
    # missions/risk_control/PROMPT.md STEP 1 would stamp on its own run).
    _stamp_mission_lastrun(repo, "risk_control", AT_21_00_UTC)
    # Also simulate the legacy layer-level marker some primaries still write
    # (agents/ben/layers/4/PROMPT.md's STEP 1) — the floor selector must NOT
    # be fooled by this into thinking L4 is "done" for the day.
    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "4", AT_21_00_UTC)

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    ids = [m["id"] for m in due]

    assert ids == ["market_wrapup"], (
        f"expected the late floor tick (22:31 Paris) to select ONLY "
        f"market_wrapup (still pending, time reached); got {ids}. "
        f"A layer-level marker from risk_control's earlier run must not "
        f"mask a second, still-pending, same-layer mission."
    )


def test_early_floor_tick_before_second_mission_time_selects_nothing_pending(tmp_path: Path):
    """At 21:01 Paris (just after risk_control fires, before market_wrapup's
    22:30 slot), the floor selector must return risk_control (still pending
    at 21:01 the instant its own time is reached, before any marker exists)
    and must NOT return market_wrapup (its time has not arrived yet)."""
    repo = _mk_repo(tmp_path)
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)

    at_21_01 = datetime(2026, 6, 23, 19, 1, tzinfo=timezone.utc)  # 21:01 Paris
    due = select_due_missions_for_forced_layer(repo, 4, now_utc=at_21_01)
    ids = [m["id"] for m in due]

    assert "market_wrapup" not in ids, "market_wrapup's 22:30 slot has not arrived at 21:01"
    assert "risk_control" in ids, "risk_control's 21:00 slot has arrived and it has never fired"


# ── 2. Back-compat: no dept.yaml / no missions on this layer → [] ───────────

def test_no_dept_yaml_returns_empty_list_for_legacy_fallback(tmp_path: Path):
    """A dept with no dept.yaml at all (or one the caller can't find) must
    return [] so loop-backup.sh falls back to the legacy generic 'run Layer N'
    tick — zero regression for depts that haven't migrated to recurring_missions."""
    repo = _mk_repo(tmp_path)  # no dept.yaml written
    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    assert due == []


def test_no_missions_on_forced_layer_returns_empty_list(tmp_path: Path):
    """dept.yaml exists but has no recurring_missions on the forced layer
    (e.g. --layer 4 for a dept whose recurring_missions are all L1/L2) → []."""
    repo = _mk_repo(tmp_path)
    l1_mission = _mk_daily("data_update", layer=1, time="07:00")
    _write_dept_yaml(repo, [l1_mission])
    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    assert due == []


# ── 3. No re-fire: a fully-fired layer returns [] ────────────────────────────

def test_all_missions_already_fired_returns_empty_list(tmp_path: Path):
    """Both L4 missions already have per-mission markers today → the late
    floor tick must select NOTHING (no re-fire / no fire-spin)."""
    repo = _mk_repo(tmp_path)
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)
    _stamp_mission_lastrun(repo, "risk_control", AT_21_00_UTC)
    _stamp_mission_lastrun(repo, "market_wrapup", AT_22_31_UTC)

    later = datetime(2026, 6, 23, 21, 0, tzinfo=timezone.utc)  # 23:00 Paris
    due = select_due_missions_for_forced_layer(repo, 4, now_utc=later)
    assert due == [], "both missions already fired today — a later floor tick must not re-dispatch either"


# ── 4. Read-only: no marker is stamped as a side effect ──────────────────────

def test_selector_is_read_only_no_marker_stamped(tmp_path: Path):
    """The floor selector is an ENUMERATION, not a dispatch — it must not
    write any .last-run marker itself (mirrors the #454 discipline:
    materialize=False for any read-only gate/probe caller). Only the
    mission's real run may stamp its own marker."""
    repo = _mk_repo(tmp_path)
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    assert [m["id"] for m in due] == ["market_wrapup"]

    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    marker = repo / "outputs" / today / "missions" / "market_wrapup" / ".last-run"
    assert not marker.exists(), (
        "select_due_missions_for_forced_layer must be read-only — it must not "
        "stamp the per-mission marker itself as a side effect of enumeration"
    )


# ── 6. LEGACY-SHIM-MARKER regression (independent-reviewer finding, post-merge) ──
#
# `_mission_last_fired` deliberately does NOT fall back to the layer-level
# marker (see its own docstring) because `materialize_due_missions_for_tick`
# is supposed to guarantee every due L4 mission gets its own per-mission
# marker EVERY tick (#277) — but that guarantee only holds when the
# materializer actually RUNS, i.e. build_dispatch_ctx(materialize=True), the
# live-loop default. select_due_missions_for_forced_layer deliberately calls
# build_dispatch_ctx(materialize=False) (the #454 read-only-probe discipline
# — an enumeration must never stamp a marker as a side effect), so that
# guarantee does NOT hold on the floor path. A mission resolving to the
# LEGACY layers/<N>/PROMPT.md shim (no dedicated missions/<id>/PROMPT.md —
# Ben's ACTUAL dept.yaml shape today: neither risk_control nor weekly_review
# has one) fires via that shim, whose STEP 1 stamps ONLY the shared layer
# marker, never the per-mission one. Without a fallback, a floor tick
# running AFTER the live loop already ran risk_control via the shim would
# see "never fired" and wrongly re-select it.
#
# _mission_last_fired_with_shim_fallback closes this gap, gated on
# resolve_mission_prompt's OWN legacy-shim test (so the two can never
# diverge) AND disambiguated by comparing the layer marker's timestamp
# against EACH mission's own scheduled time: (so a marker stamped at 21:00
# cannot be mistaken for a LATER same-layer shim mission's own fire — the
# defect an earlier draft of this fix had, where BOTH risk_control and
# market_wrapup resolved to the shim and the layer marker satisfied both).

def test_legacy_shim_marker_excludes_already_fired_shim_mission(tmp_path: Path):
    """PRIMARY regression test: risk_control has NO dedicated
    missions/risk_control/PROMPT.md (Ben's real shape) — it fires via the
    legacy layers/4/PROMPT.md shim, whose STEP 1 stamps ONLY the layer
    marker (outputs/<today>/4/.last-run), never
    outputs/<today>/missions/risk_control/.last-run. At 22:31 Paris, with
    ONLY the layer marker present (stamped at 21:00, risk_control's own
    slot), the floor selector must exclude risk_control (it fired, via the
    shim) and still return market_wrapup (still pending) — NOT both, and
    NOT neither.
    """
    repo = _mk_repo(tmp_path)
    (repo / "layers" / "4").mkdir(parents=True)
    (repo / "layers" / "4" / "PROMPT.md").write_text("legacy L4 shim")
    # Deliberately NO missions/ dir at all — mirrors Ben's real dept.yaml,
    # where BOTH L4 missions resolve to the shim (no per-mission prompts).
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)

    # ONLY the layer marker is stamped (the shim's real STEP 1 behavior) —
    # deliberately NOT stamping outputs/<today>/missions/risk_control/.last-run.
    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "4", AT_21_00_UTC)

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    ids = [m["id"] for m in due]

    assert ids == ["market_wrapup"], (
        f"expected ONLY market_wrapup (risk_control fired via the legacy shim "
        f"— its per-mission marker is absent by design, but the layer marker "
        f"at 21:00 covers it); got {ids}. If risk_control appears, the "
        f"shim-marker fallback is missing. If market_wrapup is ALSO missing, "
        f"the fallback is wrongly bleeding across same-layer shim missions."
    )


def test_legacy_shim_marker_does_not_mask_a_later_pending_shim_mission(tmp_path: Path):
    """Narrower isolation of the disambiguation logic: at 21:01 Paris (just
    after risk_control's shim run stamps the layer marker, well before
    market_wrapup's 22:30 slot), the floor selector must return risk_control
    (its own slot just opened, shim not yet run) and NOT prematurely treat
    market_wrapup as fired just because SOME layer marker exists."""
    repo = _mk_repo(tmp_path)
    (repo / "layers" / "4").mkdir(parents=True)
    (repo / "layers" / "4" / "PROMPT.md").write_text("legacy L4 shim")
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)
    # No layer marker yet at all (nothing has fired) — risk_control's slot
    # just opened.
    at_21_01 = datetime(2026, 6, 23, 19, 1, tzinfo=timezone.utc)  # 21:01 Paris

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=at_21_01)
    ids = [m["id"] for m in due]
    assert ids == ["risk_control"], f"expected only risk_control due at 21:01; got {ids}"


def test_dedicated_prompt_mission_unaffected_by_shim_fallback(tmp_path: Path):
    """If market_wrapup HAS its own missions/market_wrapup/PROMPT.md (a dept
    that migrated it off the shim), the shim fallback must never apply to
    it — resolve_mission_prompt resolves it to the dedicated prompt, not the
    shim, so _mission_last_fired_with_shim_fallback short-circuits to the
    plain per-mission-marker behavior regardless of any layer marker."""
    repo = _mk_repo(tmp_path)
    (repo / "layers" / "4").mkdir(parents=True)
    (repo / "layers" / "4" / "PROMPT.md").write_text("legacy L4 shim")
    (repo / "missions" / "market_wrapup").mkdir(parents=True)
    (repo / "missions" / "market_wrapup" / "PROMPT.md").write_text("dedicated prompt")
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)

    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "4", AT_21_00_UTC)  # shim marker (risk_control)

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_22_31_UTC)
    assert [m["id"] for m in due] == ["market_wrapup"], (
        "dedicated-prompt market_wrapup must still be selected — its own "
        "resolve_mission_prompt path bypasses the shim fallback entirely"
    )


def test_shim_marker_stamped_after_second_missions_slot_covers_it_too(tmp_path: Path):
    """If the layer marker's timestamp is AFTER market_wrapup's own slot
    (e.g. the shim happened to run at 22:35, after BOTH slots had opened),
    the fallback correctly treats market_wrapup as fired too (the marker
    COULD represent either mission having fired via the shared shim — this
    is the accepted ambiguity of two same-layer shim missions; a dept
    needing true disambiguation should use dedicated prompts). This test
    documents that boundary rather than asserting a specific "right"
    mission — it is the shim's own structural limit, not a selector bug.
    """
    repo = _mk_repo(tmp_path)
    (repo / "layers" / "4").mkdir(parents=True)
    (repo / "layers" / "4" / "PROMPT.md").write_text("legacy L4 shim")
    risk_control = _mk_daily("risk_control", layer=4, time="21:00")
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30")
    _write_dept_yaml(repo, [risk_control, market_wrapup])
    _fire_prereqs(repo, AT_21_00_UTC)

    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    after_both_slots = datetime(2026, 6, 23, 20, 35, tzinfo=timezone.utc)  # 22:35 Paris
    write_last_run(repo / "outputs" / today / "4", after_both_slots)

    later = datetime(2026, 6, 23, 21, 0, tzinfo=timezone.utc)  # 23:00 Paris
    due = select_due_missions_for_forced_layer(repo, 4, now_utc=later)
    assert due == [], (
        "a shim marker stamped AFTER both missions' slots opened plausibly "
        "covers both — this is the shim's structural ambiguity, not a bug"
    )
