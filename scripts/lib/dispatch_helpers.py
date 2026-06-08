"""
dispatch_helpers.py — small pure-function helpers for the /loop STEP C
dispatch decision tree.

Why this module exists
----------------------
STEP C of CLAUDE_MD_OPERATING_TEMPLATE is an LLM-driven decision tree. We
can't unit-test "what the agent decides", but we CAN unit-test the
artifacts and primitives the agent relies on:

  - .last-run file format (round-trip ISO timestamp)
  - round_counter.json file format + per-layer increment
  - cadence-due check (is mission X due RIGHT NOW given last fire?)
  - the Layer-1 idle-gate condition (all other layers have completed ≥ N
    rounds since L1's last fire)
  - a deterministic `decide_dispatch()` that locks down the C.0/C.1/C.2/C.3/C.4
    priority order — the agent's prompt describes the same tree in prose,
    so this helper is the executable contract.

Joris msg 3129 (2026-05-24): "Layer 1 = morning / data refresh subagent
whose job is to materialize recurring_missions[] from dept.yaml into
actionable queue items based on each mission's cadence field. Layer 1
fires when all 3 other layers have completed 1 (or configurable) round."

Reference: scripts/lib/scaffold.py::CLAUDE_MD_OPERATING_TEMPLATE STEP C.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time as _time, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
    _PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover — fallback for ancient envs
    _PARIS = timezone.utc


_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# ---------------------------------------------------------------------------
# .last-run file I/O
# ---------------------------------------------------------------------------

def read_last_run(layer_dir: Path) -> datetime | None:
    """Read the ISO-8601 timestamp from `<layer_dir>/.last-run`.

    Returns None if the file does not exist. Caller is expected to pass
    the per-layer dir (e.g. `outputs/2026-05-24/1/`).
    """
    f = layer_dir / ".last-run"
    if not f.exists():
        return None
    body = f.read_text(encoding="utf-8").strip()
    if not body:
        return None
    return datetime.fromisoformat(body)


def write_last_run(layer_dir: Path, when: datetime | None = None) -> None:
    """Write the ISO-8601 timestamp to `<layer_dir>/.last-run`.

    `when` defaults to "now" (tz-aware UTC) so the layer PROMPT.md call site
    `write_last_run(Path("outputs/<today>/<n>"))` runs without a TypeError —
    the documented FIRST action of any layer dispatch is simply "stamp now".
    When `when` is supplied it MUST be timezone-aware; we serialize via
    `datetime.isoformat()` so the offset is preserved (idempotence + audit
    trail).
    """
    if when is None:
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError("write_last_run requires a tz-aware datetime")
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / ".last-run").write_text(when.isoformat(), encoding="utf-8")


# ---------------------------------------------------------------------------
# round_counter.json file I/O
# ---------------------------------------------------------------------------
#
# Format: {"1": 0, "2": 3, "3": 1, "4": 0} — per-layer integer counter
# of completed dispatches for THIS UTC day. Path: outputs/<today>/round_counter.json.
# Reset semantics: file lives under outputs/<today>/, so a new UTC day = new
# dir = fresh counter (no carry-over).
# ---------------------------------------------------------------------------

_COUNTER_FILE = "round_counter.json"


def read_round_counter(today_dir: Path) -> dict[str, int]:
    """Return the per-layer round counter dict (keys are str layer numbers).

    Missing file => {}. Missing layer key => caller should treat as 0.
    """
    f = today_dir / _COUNTER_FILE
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # Coerce to {str: int} defensively.
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def increment_round_counter(today_dir: Path, *, layer: int) -> int:
    """Increment the counter for `layer` and persist. Returns the new value.

    This is the documented LAST action of any layer dispatch.
    """
    today_dir.mkdir(parents=True, exist_ok=True)
    counts = read_round_counter(today_dir)
    key = str(int(layer))
    counts[key] = counts.get(key, 0) + 1
    (today_dir / _COUNTER_FILE).write_text(
        json.dumps(counts, sort_keys=True), encoding="utf-8"
    )
    return counts[key]


_L1_BASELINE_FILE = ".l1-baseline.json"


def read_l1_baseline(today_dir: Path) -> dict[str, int]:
    """Return the round_counter snapshot captured at Layer 1's last fire today.

    Missing file => {} (L1 has not fired yet today). This is the baseline the
    cycle gate (decide_dispatch C.0 branch b) measures the other layers'
    progress against, so L1 re-fires once per completed cycle rather than every
    tick after the threshold is first crossed.
    """
    f = today_dir / _L1_BASELINE_FILE
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def write_l1_baseline(today_dir: Path, counts: dict[str, int] | None = None) -> dict[str, int]:
    """Snapshot the current round_counter as Layer 1's cycle baseline.

    Call this when Layer 1 fires (alongside increment_round_counter), so the
    next cycle gate measures the other layers' rounds from this moment. If
    `counts` is omitted, the current round_counter on disk is snapshotted.
    Returns the snapshot written.
    """
    today_dir.mkdir(parents=True, exist_ok=True)
    snap = read_round_counter(today_dir) if counts is None else dict(counts)
    (today_dir / _L1_BASELINE_FILE).write_text(
        json.dumps(snap, sort_keys=True), encoding="utf-8"
    )
    return snap


# ---------------------------------------------------------------------------
# Layer-1 idle-gate
# ---------------------------------------------------------------------------

def layer_1_gate_satisfied(today_dir: Path, *, fire_after_rounds: int = 1) -> bool:
    """Return True iff EACH of L2, L3, L4 has completed ≥ fire_after_rounds
    since the start of the UTC day.

    This is the "all 3 other layers have completed 1 (or configurable)
    round" gate from Joris msg 3129. It prevents Layer 1 from over-flooding
    downstream queues.
    """
    counts = read_round_counter(today_dir)
    for layer in ("2", "3", "4"):
        if counts.get(layer, 0) < fire_after_rounds:
            return False
    return True


# ---------------------------------------------------------------------------
# Cadence-due check
# ---------------------------------------------------------------------------

def _to_paris(dt_utc: datetime) -> datetime:
    """Convert a tz-aware UTC datetime into Paris-local time."""
    if dt_utc.tzinfo is None:
        raise ValueError("_to_paris requires a tz-aware datetime")
    return dt_utc.astimezone(_PARIS)


def _parse_hhmm(s: str) -> _time:
    h, m = s.split(":")
    return _time(int(h), int(m))


def is_mission_due(mission: dict, *, now: datetime,
                   last_fired: datetime | None) -> bool:
    """Return True iff the mission's cadence says it should fire RIGHT NOW.

    `now` must be a tz-aware UTC datetime. `last_fired` is the mission's
    own .last-run timestamp (None if never fired).

    Supported cadences (from recurring-mission.schema.yaml::cadence):
      daily            — needs `time:` HH:MM Paris. Due once per Paris-local
                         day after that time.
      weekly           — needs `time:` + `day:` (lowercase English). Due
                         once per Paris-local week on that day after time.
      hourly           — top of every Paris hour.
      every_<N>h       — every N hours since last fire.
      every_<N>m       — every N minutes since last fire.
      cron:<expr>      — escape hatch; NOT evaluated here, returns False
                         (caller / agent must handle cron expressions).
    """
    cadence = mission.get("cadence", "")
    if not cadence:
        return False

    now_paris = _to_paris(now)
    last_paris = _to_paris(last_fired) if last_fired else None

    if cadence == "daily":
        t_str = mission.get("time")
        if not t_str:
            return False
        target = _parse_hhmm(t_str)
        if now_paris.time() < target:
            return False
        # Already fired today (Paris-local day)?
        if last_paris and last_paris.date() == now_paris.date():
            return False
        return True

    if cadence == "weekly":
        t_str = mission.get("time")
        day = mission.get("day", "").lower()
        if not t_str or day not in _WEEKDAY_NAMES:
            return False
        if now_paris.weekday() != _WEEKDAY_NAMES[day]:
            return False
        target = _parse_hhmm(t_str)
        if now_paris.time() < target:
            return False
        # Already fired this week (same ISO year+week)?
        if last_paris:
            ly, lw, _ = last_paris.isocalendar()
            ny, nw, _ = now_paris.isocalendar()
            if (ly, lw) == (ny, nw):
                return False
        return True

    if cadence == "hourly":
        # Top of every Paris hour. If we already fired in this same hour,
        # skip.
        if last_paris and (last_paris.year, last_paris.month,
                           last_paris.day, last_paris.hour) == (
                              now_paris.year, now_paris.month,
                              now_paris.day, now_paris.hour):
            return False
        return True

    if cadence.startswith("every_") and cadence.endswith("h"):
        try:
            n = int(cadence[len("every_"):-1])
        except ValueError:
            return False
        if last_paris is None:
            return True
        delta_s = (now - last_fired).total_seconds()
        return delta_s >= n * 3600

    if cadence.startswith("every_") and cadence.endswith("m"):
        try:
            n = int(cadence[len("every_"):-1])
        except ValueError:
            return False
        if last_paris is None:
            return True
        delta_s = (now - last_fired).total_seconds()
        return delta_s >= n * 60

    # cron:<expr> — escape hatch. Out of scope for STEP C.0.
    return False


# ---------------------------------------------------------------------------
# Materialization helper
# ---------------------------------------------------------------------------

@dataclass
class _MaterializedItem:
    """Not exported — kept internal as a documentation aid."""
    mission_id: str
    output_queue: str
    kind: str


def materialize_due_missions(missions: Iterable[dict], *,
                             now: datetime,
                             last_fired_per_mission: dict[str, datetime]
                             ) -> list[dict]:
    """For each mission with layer in 1..3 that is due, return one queue-item
    descriptor per `creates[]` entry.

    Layers 1-3 are materialized here (WS3 growth-spearhead lock, 2026-06-08):
    recurring L2/L3 missions like Maya's `discovery` and `warming` were defined
    in dept.yaml but never woken on schedule because this used to filter
    `layer != 1`. Now any due mission on layers 1-3 produces its cards.

    The returned dicts are NOT full queue-item.schema.yaml documents — they
    are the minimum payload the agent / a downstream skill needs to write
    the actual files. Shape:
        {"mission_id": str, "output_queue": str, "kind": str}

    Layer-4 missions are filtered out (they're owned by STEP C.1).
    """
    out: list[dict] = []
    for m in missions:
        # WS3: materialize layers 1-3 (L4 is owned by the L4/debrief branch).
        if int(m.get("layer", 0)) not in (1, 2, 3):
            continue
        mid = m.get("id", "")
        last = last_fired_per_mission.get(mid)
        if not is_mission_due(m, now=now, last_fired=last):
            continue
        oq = m.get("output_queue", "")
        for kind in m.get("creates", []) or []:
            out.append({
                "mission_id": mid,
                "output_queue": oq,
                "kind": kind,
            })
    return out


# ---------------------------------------------------------------------------
# Deterministic dispatch decision (locks down the prompt's tree)
# ---------------------------------------------------------------------------

# Per-layer MINIMUM fire time (Europe/Paris local), Joris msg 3904 (2026-06-06):
# each layer becomes eligible "from this time onwards until end of day" — a
# minimum, NOT a window. A layer may fire again later the same day if there is
# work. Compared against Paris-local time so it tracks the loop-layer*.timer
# cron (also Europe/Paris) across DST.
_LAYER_MIN_TIME = {
    1: _time(7, 0),    # L1 Observe — morning floor
    2: _time(12, 0),   # L2 Research
    3: _time(16, 0),   # L3 Execution
    4: _time(19, 0),   # L4 Debrief / risk control
}


def _layer_fired_today(ctx: "dict[str, Any]", layer: int) -> bool:
    """True if layer N has fired at least once today.

    Uses the per-layer .last-run marker first (set the instant a layer starts),
    falling back to round_counter (incremented when a layer completes a round).
    """
    last = ctx.get(f"layer_{layer}_last_run_today")
    if last is not None:
        return True
    counts = ctx.get("round_counter") or {}
    return int(counts.get(str(layer), 0)) > 0


def _time_reached(now_paris_t: "Any", layer: int) -> bool:
    """True once Paris-local time has reached layer N's minimum fire time."""
    return now_paris_t >= _LAYER_MIN_TIME[layer]


def _queue_has_items(queue_dir: Path) -> bool:
    """True if `queue_dir` holds at least one actionable item.

    "Actionable" = a regular `*.yaml` file that is NOT a dotfile/hidden helper.
    Excludes `.gitkeep`, anything starting with `.`, and processed/archived
    subdirs. Missing dir -> False (fail-safe).
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return False
    for p in queue_dir.glob("*.yaml"):
        if p.name.startswith("."):
            continue
        if p.is_file():
            return True
    return False


def build_dispatch_ctx(
    repo_dir: "Path | str" = ".",
    *,
    now_utc: "datetime | None" = None,
    fire_after_rounds: int = 1,
) -> "dict[str, Any]":
    """Build the ctx dict that `decide_dispatch` consumes, by SCANNING the
    repo's queues + today's runtime markers.

    THIS is the piece that was missing (2026-06-01, Joris msg 3588):
    `decide_dispatch` is a pure decision function — without a builder, the /loop
    was calling it with a placeholder, so has_research_items/has_inbox_decisions
    were never set and the tree fell through to "heartbeat" forever — L2/L3
    never fired and work piled up in the queues.

    Queue conventions (dept template):
      - queues/research/*.yaml         -> has_research_items (Layer 2)
      - queues/inbox/decisions/*.yaml  -> has_inbox_decisions (Layer 3)
      - outputs/<today>/4/.last-run    -> layer_4_last_run_today (L4 idempotence)
      - outputs/<today>/1/.last-run    -> layer_1_last_run_today (L1 daily floor)
      - outputs/<today>/round_counter.json -> round_counter (L1 cycle gate)
      - outputs/<today>/.l1-baseline.json  -> layer_1_baseline_counter (L1 cycle gate)

    THE ebb03972 FIX (2026-06-02): earlier builds injected neither
    `layer_1_last_run_today` nor `layer_1_baseline_counter`, so decide_dispatch's
    C.0 branch always saw l1_last=None and returned "layer_1" on EVERY quiet tick
    even after L1 had already run that morning. Tony observed this live (VPS
    session ebb03972) and had to override to "heartbeat" by hand every tick. Both
    keys are now scanned from disk so the daily floor and the cycle gate work.
    """
    repo = Path(repo_dir)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    today_dir = repo / "outputs" / today
    return {
        "now_utc": now_utc,
        # Deterministic UTC date for THIS tick. The agent MUST use these for
        # all outputs/<date>/ paths + the heartbeat, instead of hand-typing
        # the date from context — that froze Maya's loop on 2026-06-02 while
        # the real date was 2026-06-04 (see docs/FLOWCHART-SPEC.md BUG-DATE).
        "today": today,
        "today_dir": str(today_dir),
        "has_research_items": _queue_has_items(repo / "queues" / "research"),
        "has_inbox_decisions": _queue_has_items(repo / "queues" / "inbox" / "decisions"),
        "layer_1_last_run_today": read_last_run(today_dir / "1"),
        "layer_2_last_run_today": read_last_run(today_dir / "2"),
        "layer_3_last_run_today": read_last_run(today_dir / "3"),
        "layer_4_last_run_today": read_last_run(today_dir / "4"),
        "round_counter": read_round_counter(today_dir),
        "layer_1_baseline_counter": read_l1_baseline(today_dir),
        "fire_after_rounds": fire_after_rounds,
    }


def decide_dispatch(ctx: dict[str, Any]) -> str:
    """Return one of "layer_1" / "layer_2" / "layer_3" / "layer_4" /
    "heartbeat" given the tick context.

    Required ctx keys (all optional, sensible defaults applied):
      now_utc: tz-aware UTC datetime — required
      has_research_items: bool
      has_inbox_decisions: bool
      layer_4_last_run_today: datetime | None — set if outputs/<today>/4/.last-run exists
      layer_1_last_run_today: datetime | None — set if outputs/<today>/1/.last-run exists
      round_counter: dict[str, int] — per-layer round counts for today
      layer_1_baseline_counter: dict[str, int] — round_counter snapshot at L1's
        last fire today ({} if L1 has not fired yet today)
      fire_after_rounds: int — rounds each other layer must complete per cycle
        (default 1)

    Priority order (highest to lowest), each gated by a Paris-local MINIMUM
    fire time (L1>=07:00, L2>=12:00, L3>=16:00, L4>=19:00) — a minimum, not a
    window; a layer stays eligible to end of day and may re-fire if there is work:
      C.1 — Layer 4 if time>=19:00 Paris AND L1+L2+L3 each fired today AND
            L4 not yet run today (prerequisite gate sequences the aggregator last)
      C.3 — Layer 3 if time>=16:00 Paris AND inbox decisions have items
      C.2 — Layer 2 if time>=12:00 Paris AND research queue has items
      C.0 — Layer 1 if time>=07:00 Paris AND (not yet run today  OR  each other
            layer has fired a fresh round since L1 last fired)
      C.4 — heartbeat (default)

    Note: C.0 has the LOWEST priority despite its number — Layer 1 is the
    last to fire, so a tick with research / decisions / an L4 window does
    that work first and the morning brief slots into a quiet tick.

    Joris flag 2026-06-01 (refined): Layer 1 fires "at least once per day, or
    whenever all other layers have fired once". So C.0 fires when EITHER:
      (a) L1 has not run today  → the daily floor (independent of dept exports:
          there are always emails + the Notion logbook to review), OR
      (b) L2, L3 and L4 have EACH completed >= fire_after_rounds rounds SINCE
          L1's last fire today  → a fresh full cycle warrants a re-consolidation.
    The cycle gate (b) measures against `layer_1_baseline_counter` (the counts
    captured when L1 last fired), so it re-fires once per completed cycle, not
    every tick once the threshold is first crossed. The legacy
    `layer_1_gate_satisfied()` helper computed the same idea against the start
    of the UTC day; it is retained for reference.
    """
    now_utc: datetime = ctx["now_utc"]
    if now_utc.tzinfo is None:
        raise ValueError("decide_dispatch: now_utc must be tz-aware")
    now_paris_t = _to_paris(now_utc).time()
    has_research = bool(ctx.get("has_research_items", False))
    has_decisions = bool(ctx.get("has_inbox_decisions", False))
    l1_last = ctx.get("layer_1_last_run_today")
    counts = ctx.get("round_counter") or {}
    baseline = ctx.get("layer_1_baseline_counter") or {}
    fire_after_rounds = int(ctx.get("fire_after_rounds", 1))

    l1_fired = _layer_fired_today(ctx, 1)
    l2_fired = _layer_fired_today(ctx, 2)
    l3_fired = _layer_fired_today(ctx, 3)
    l4_fired = _layer_fired_today(ctx, 4)

    # MODEL (Joris msg 3904, 2026-06-06): each layer has a MINIMUM fire time
    # (Paris-local), not a window — eligible from then to end of day, and may
    # re-fire later if there is work. Two layers carry a prerequisite gate ON
    # TOP of the time check:
    #   • L4 may fire only once L1, L2 and L3 have each fired >=1x today
    #     (so the aggregator/debrief always sees a full day — this is what
    #     sequences Tony AFTER the dept layers and kills the L4 read-race).
    #   • L1's re-consolidation fires once the other layers have completed a
    #     fresh cycle since L1 last ran (its morning floor fires unconditionally
    #     once 07:00 Paris is reached and it has not run yet today).
    # Priority: L4 (debrief, end of day) > L3 > L2 > L1, each guarded by time.

    # C.1 — Layer 4: time reached AND L1/L2/L3 all fired today AND not yet run.
    if (
        _time_reached(now_paris_t, 4)
        and l1_fired and l2_fired and l3_fired
        and not l4_fired
    ):
        return "layer_4"

    # C.3 — Layer 3: time reached AND inbox decisions waiting (re-fireable).
    if _time_reached(now_paris_t, 3) and has_decisions:
        return "layer_3"

    # C.2 — Layer 2: time reached AND research queue has items (re-fireable).
    if _time_reached(now_paris_t, 2) and has_research:
        return "layer_2"

    # C.0 — Layer 1: time reached AND (morning floor not yet run today OR a
    # fresh full cycle of L2/L3/L4 has completed since L1 last fired).
    if _time_reached(now_paris_t, 1):
        if not l1_fired:
            return "layer_1"
        cycle_complete = all(
            int(counts.get(str(layer), 0)) - int(baseline.get(str(layer), 0))
            >= fire_after_rounds
            for layer in (2, 3, 4)
        )
        if cycle_complete:
            return "layer_1"

    # C.4 — heartbeat (nothing eligible this tick).
    return "heartbeat"


# ─── Independent dept liveness signal (Joris flag 2026-06-01) ───────────────
#
# Joris flag 2026-06-01: "Export from dept shouldn't be your only data."
# A child's management-export is self-reported — a dept whose runtime died
# could still ship a stale "clean" export. Layer 1 needs a signal that does
# NOT depend on what the child writes about itself. We cross-check two
# things the child does not author in its export:
#   1. presence + freshness of yesterday's whitelisted export file (mtime)
#   2. recency of the child repo's last git commit (the repo's pulse)
# If the export claims health but the repo's pulse is cold, that's a liveness
# discrepancy the morning brief must surface. This stays inside the
# visibility boundary: we read the whitelisted export path and git commit
# METADATA (git log) — never the child's raw artifacts.

_LIVENESS_STALE_AFTER_H = 26.0   # ~1 day + margin: export/commit should be daily
_LIVENESS_DEAD_AFTER_H = 50.0    # ~2 days: dept has almost certainly stopped


def classify_liveness(
    export_present: bool,
    export_age_hours: "float | None",
    commit_age_hours: "float | None",
    stale_after_h: float = _LIVENESS_STALE_AFTER_H,
    dead_after_h: float = _LIVENESS_DEAD_AFTER_H,
) -> str:
    """Verdict on a child dept's liveness from signals it does not self-author.

    Pure function (no I/O) so the decision rule is unit-testable in isolation.
    Returns one of: "live" | "stale" | "dead" | "missing".

      - "missing": no export at all → dept never produced yesterday's output.
      - "dead":    freshest signal older than `dead_after_h` → runtime stopped.
      - "stale":   freshest signal older than `stale_after_h` → lagging/at risk.
      - "live":    at least one signal within the stale window.

    The "freshest signal" is the MIN age across the two inputs: a fresh commit
    OR a fresh export is enough to call the dept alive. `None` means that
    signal is unavailable (e.g. repo not checked out, no commits) and is
    ignored — unless BOTH are None, which is treated as "dead" (no pulse).
    """
    if not export_present and export_age_hours is None and commit_age_hours is None:
        return "missing"

    ages = [a for a in (export_age_hours, commit_age_hours) if a is not None]
    if not ages:
        # No measurable signal at all, but something claimed present → no pulse.
        return "dead"

    freshest = min(ages)
    if freshest >= dead_after_h:
        return "dead"
    if freshest >= stale_after_h:
        return "stale"
    return "live"


def dept_liveness(
    child_repo_dir: Path,
    now_utc: datetime,
    yesterday: str,
) -> dict:
    """Gather the independent liveness signals for one child dept.

    `child_repo_dir` is the child's repo root (e.g. ../bubble-ops-maya).
    `yesterday` is the YYYY-MM-DD whose export we expect to exist.

    Reads only:
      - the whitelisted export file's existence + mtime
        (outputs/<yesterday>/4/management-export.yaml)
      - `git -C <child_repo_dir> log -1 --format=%cI` (commit metadata only)

    Returns a dict the Layer-1 subagent folds into the morning brief:
        {
          "export_present": bool,
          "export_age_hours": float | None,
          "last_commit_iso": str | None,
          "commit_age_hours": float | None,
          "liveness": "live" | "stale" | "dead" | "missing",
        }
    Never raises on a missing repo / git failure — those degrade to None
    signals so a child that isn't checked out simply reads as low-signal.
    """
    import subprocess

    child_repo_dir = Path(child_repo_dir)
    if now_utc.tzinfo is None:
        raise ValueError("dept_liveness: now_utc must be tz-aware")

    export = child_repo_dir / "outputs" / yesterday / "4" / "management-export.yaml"
    export_present = export.exists()
    export_age_hours: "float | None" = None
    if export_present:
        mtime = datetime.fromtimestamp(export.stat().st_mtime, tz=timezone.utc)
        export_age_hours = (now_utc - mtime).total_seconds() / 3600.0

    last_commit_iso: "str | None" = None
    commit_age_hours: "float | None" = None
    try:
        res = subprocess.run(
            ["git", "-C", str(child_repo_dir), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            last_commit_iso = res.stdout.strip()
            commit_dt = datetime.fromisoformat(last_commit_iso)
            if commit_dt.tzinfo is None:
                commit_dt = commit_dt.replace(tzinfo=timezone.utc)
            commit_age_hours = (now_utc - commit_dt).total_seconds() / 3600.0
    except (OSError, ValueError, subprocess.SubprocessError):
        # Repo absent / not a git tree / unparseable date → leave as None.
        pass

    return {
        "export_present": export_present,
        "export_age_hours": export_age_hours,
        "last_commit_iso": last_commit_iso,
        "commit_age_hours": commit_age_hours,
        "liveness": classify_liveness(
            export_present, export_age_hours, commit_age_hours
        ),
    }


# ─── Auto-retry mechanism + force commit/push (Joris msg 3134) ──────────────
#
# Joris flag 2026-05-24: "We need a mechanism for agent auto retry if he
# doesn't fetch the correct input and outputs the correct expected format
# and data. Also a forced git commit and push all at each risk manager
# mission step."
#
# The agent itself drives the retry loop (its PROMPT.md will say "after
# work, call validate_layer_output(); if not OK and should_retry(): re-read
# inputs and rerun. Max retries: MAX_RETRIES_DEFAULT."). These helpers
# are the executable contract behind the prose.

MAX_RETRIES_DEFAULT = 3


def validate_layer_output(
    layer: int,
    output_dir: Path,
    expected_artifacts: list[dict],
) -> tuple[bool, list[str], list[tuple[str, str]]]:
    """Verify a layer subagent produced the expected artifacts.

    `expected_artifacts` is a list of dicts like:
        {"name": "summary.md", "kind": "markdown"}
        {"name": "logs.jsonl", "kind": "jsonl"}
        {"name": ".last-run", "kind": "iso_timestamp"}
        {"name": "risk-kpis.yaml", "kind": "yaml"}

    Returns (ok, missing_filenames, [(filename, reason), ...] malformed).
    """
    output_dir = Path(output_dir)
    missing: list[str] = []
    malformed: list[tuple[str, str]] = []

    for spec in expected_artifacts:
        name = spec["name"]
        kind = spec.get("kind", "any")
        p = output_dir / name
        if not p.exists():
            missing.append(name)
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except OSError as exc:
            malformed.append((name, f"unreadable: {exc}"))
            continue

        if kind == "iso_timestamp":
            try:
                datetime.fromisoformat(body.strip())
            except ValueError:
                malformed.append((name, "not a valid ISO timestamp"))

        elif kind == "yaml":
            try:
                import yaml as _yaml
                _yaml.safe_load(body)
            except Exception as exc:
                malformed.append((name, f"invalid yaml: {exc}"))

        elif kind == "jsonl":
            lines = [ln for ln in body.splitlines() if ln.strip()]
            if not lines:
                malformed.append((name, "empty jsonl (no lines)"))
            else:
                for ln in lines:
                    try:
                        json.loads(ln)
                    except json.JSONDecodeError as exc:
                        malformed.append((name, f"bad jsonl line: {exc}"))
                        break

        elif kind == "markdown":
            if not body.strip():
                malformed.append((name, "empty markdown file"))

        # kind == "any" or unknown: presence is enough.

    ok = not missing and not malformed
    return ok, missing, malformed


def should_retry(retry_count: int, max_retries: int = MAX_RETRIES_DEFAULT) -> bool:
    """Gate for the agent's retry loop. Returns True if another attempt
    is allowed (retry_count < max_retries)."""
    return retry_count < max_retries


def resolve_push_target(repo_dir: "Path | str") -> "tuple[str | None, str | None]":
    """Derive (dept_slug, repo_name) from a working tree's OWN git remote.

    Dept-agnostic: reads `git remote get-url origin` in repo_dir and parses
    the `bubble-ops-<slug>` repo name out of it. This is what makes the push
    isolation-safe — each dept pushes ITS OWN repo because the target is read
    from the tree we were asked to push, never hardcoded.

    Returns (None, None) if no origin remote or it doesn't look like a
    bubble-ops repo (caller falls back to a bare `git push origin`).
    """
    import re
    import subprocess

    res = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None, None
    url = res.stdout.strip()
    m = re.search(r"(bubble-ops-([A-Za-z0-9_-]+?))(?:\.git)?$", url)
    if not m:
        return None, None
    repo_name, slug = m.group(1), m.group(2)
    return slug, repo_name


def _resolve_is_structural():
    """Return the broker's `_is_structural(path)` (single source of truth for
    STRUCTURAL_PATH_GLOBS). Falls back to a built-in glob check if the broker
    module isn't importable, so the runtime push never crashes — worst case it
    behaves like the old add-all (fail-open), never blocks the loop."""
    import importlib.util, os
    for cand in (
        os.environ.get("BUBBLE_BROKER_POLICY_PY"),
        "/opt/bubble-token-broker/src/policy.py",
    ):
        if cand and os.path.isfile(cand):
            try:
                spec = importlib.util.spec_from_file_location("_bubble_policy", cand)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "_is_structural"):
                    return mod._is_structural
            except Exception:
                pass
    # Fallback: minimal built-in structural globs (kept in sync with policy.py).
    import fnmatch
    _GLOBS = (
        "dept.yaml", "CLAUDE.md", "MANDATE.md", "skills_manifest.yaml",
        "config.yaml", "gate_policy.yaml", "db/schema.sql",
        "layers/**", "missions/**", "skills/**", "tools/**", "subagents/**",
        "policies/**", "templates/**", "assets/**", ".claude/**",
    )
    def _fallback(path: str) -> bool:
        for g in _GLOBS:
            if g.endswith("/**"):
                base = g[:-3]
                if path == base or path.startswith(base + "/"):
                    return True
            elif fnmatch.fnmatch(path, g):
                return True
        return False
    return _fallback


def force_commit_and_push(
    repo_dir: Path,
    message: str,
    bubble_git_guard_path: str = "/usr/local/bin/bubble-git-guard",
    action: str = "runtime_write_own",
) -> tuple[bool, "str | None"]:
    """Stage everything, commit, and push the dept's OWN repo.

    Called by Layer 4 after EACH artifact write per Joris msg 3134 — the
    risk manager's outputs are source-of-truth for what happened today;
    we don't let them sit 20 minutes waiting for the next /loop's STEP E.

    The push target (dept slug + repo) is derived from repo_dir's OWN git
    remote via resolve_push_target() — never hardcoded — so this vendored
    helper is generic: each tree pushes its OWN repo (tony/ -> bubble-ops-tony,
    cgp/ -> bubble-ops-cgp, maya/ -> bubble-ops-maya). Set DRY_RUN=1 to resolve
    + report the push target and RETURN before any mutation — truly
    side-effect-free (no git add, no commit, no push). Used by isolation tests.

    Idempotent: if `git status --porcelain` is clean, returns (True, None)
    with no side effect (nothing is pushed).

    Returns (ok, error_message). ok=False when the push rejects (operator
    must investigate manually — we do NOT silently swallow push errors).
    """
    import os
    import shutil
    import subprocess
    repo_dir = Path(repo_dir)

    # 1. Anything to commit?
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip()[:200]}"
    if not status.stdout.strip():
        # Clean tree — nothing to do.
        return True, None

    # The push target is derived from repo_dir's OWN git remote
    # (resolve_push_target) — NEVER hardcoded to a single dept — so this same
    # vendored function pushes tony from tony/, cgp from cgp/, etc. (cross-dept
    # isolation: a dept can only push itself).
    slug, repo_name = resolve_push_target(repo_dir)

    # DRY_RUN: resolve + report the push target and RETURN *before* any mutation.
    # This MUST come before `git add`/`git commit` so a dry run is genuinely
    # side-effect-free (HEAD, index and working tree all untouched) — a dry run
    # must never mint a local commit. Lets callers/tests verify isolation (which
    # repo would be pushed) without touching the repo.
    if os.environ.get("DRY_RUN"):
        print(f"[DRY_RUN] would push dept={slug!r} repo={repo_name!r} from {repo_dir}")
        return True, None

    # 2. Stage RUNTIME paths only — never structural files (Joris 2026-06-06).
    # `git add -A` used to sweep structural files (CLAUDE.md, doctrine,
    # assets/**, db/schema.sql...) into the runtime commit; the guard then minted
    # a read-only token and the WHOLE push 403'd, so the dept's commits piled up
    # local-only and the repo drifted. We stage only the non-structural changed
    # paths here; structural changes go via propose-settings-pr (human-merged PR),
    # never a runtime push to main. _is_structural is the single source of truth
    # (same globs the guard uses), so this can't drift from the push policy.
    changed = [
        line[3:].strip().strip('"')
        for line in status.stdout.splitlines()
        if line.strip()
    ]
    # handle rename "old -> new": stage the new path
    _is_struct = _resolve_is_structural()
    runtime_paths = []
    skipped_structural = []
    for c in changed:
        path = c.split(" -> ")[-1] if " -> " in c else c
        if _is_struct(path):
            skipped_structural.append(path)
        else:
            runtime_paths.append(path)
    if skipped_structural:
        print(
            "[force_commit_and_push] NOT staging structural file(s) for the "
            "runtime push (route via propose-settings-pr): "
            + ", ".join(sorted(set(skipped_structural)))
        )
    if not runtime_paths:
        # Only structural changes pending — nothing for the runtime push to do.
        return True, None
    add = subprocess.run(
        ["git", "-C", str(repo_dir), "add", "--"] + runtime_paths,
        capture_output=True, text=True,
    )
    if add.returncode != 0:
        return False, f"git add failed: {add.stderr.strip()[:200]}"

    # 3. Commit (allow-empty=False — we already gated on porcelain output,
    # but a race could leave us empty; treat as success in that case).
    commit = subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", message],
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        # "nothing to commit" → treat as no-op success
        if "nothing to commit" in (commit.stdout + commit.stderr).lower():
            return True, None
        return False, f"git commit failed: {(commit.stderr or commit.stdout).strip()[:200]}"

    # 4. Push the dept's OWN repo (target resolved above, isolation-safe).
    guard = bubble_git_guard_path
    policy = f"/opt/bubble-token-broker/deploy/policies/{slug}-policy.yaml"
    if slug and (shutil.which(guard) or Path(guard).exists()) and Path(policy).exists():
        # Doctrine path: guarded push (path-allow-list + broker-minted token).
        # The guard scopes the token to THIS repo via --repo, derived from
        # repo_dir, so isolation is enforced at the broker too.
        push = subprocess.run(
            [guard, "push", "--dept", slug, "--repo", repo_name,
             "--repo-dir", str(repo_dir), "--action", action,
             "--policy", policy],
            capture_output=True, text=True,
        )
    elif repo_name:
        # Generic fallback: mint a short-lived GitHub App token via the
        # credential helper and push repo_dir's OWN remote. credential.helper=""
        # disables the helper chain so the inline URL auth is used directly.
        cred = subprocess.run(
            ["sudo", "-n", "/usr/local/bin/bubble-gh-credential-helper.sh", "get"],
            input=(
                "protocol=https\nhost=github.com\n"
                f"path=Bubble-invest/{repo_name}.git\n"
            ),
            capture_output=True, text=True,
        )
        token = ""
        for line in cred.stdout.splitlines():
            if line.startswith("password="):
                token = line.split("=", 1)[1].strip()
        if not token.startswith("ghs_"):
            return False, (
                f"failed to mint GitHub App token for {repo_name}: "
                f"{(cred.stderr or cred.stdout).strip()[:200]}"
            )
        push = subprocess.run(
            ["git", "-C", str(repo_dir), "-c", "credential.helper=",
             "push",
             f"https://x-access-token:{token}@github.com/Bubble-invest/{repo_name}.git",
             "main"],
            capture_output=True, text=True,
        )
    else:
        # Last resort: bare push (relies on a credential helper in git config).
        push = subprocess.run(
            ["git", "-C", str(repo_dir), "push", "origin", "main"],
            capture_output=True, text=True,
        )
    if push.returncode != 0:
        return False, (
            f"git push failed (rc={push.returncode}): "
            f"{(push.stderr or push.stdout).strip()[:200]}"
        )
    return True, None



# ─── safe_pull: dirty-tree-proof in-loop sync (Joris msg 3979, 2026-06-06) ──
#
# WHY: the loop's tick step 1 was `git pull --quiet --rebase || echo
# 'pull-failed-continuing'`. On a dirty working tree git refuses
# ("cannot pull with rebase: you have unstaged changes") and the tick just
# continues — so MERGED structural changes (a CLAUDE.md/skill PR a human just
# merged) NEVER land on the box. That's the auto-redeploy gap: merge != live.
# Reproduced on tony/ben/maya 2026-06-06 (28/6/2 dirty files).
#
# Option A (Joris's pick): make the pull RELIABLE without ever losing work.
#   1. Land legit RUNTIME changes via force_commit_and_push (outputs/queues/
#      inbox/WORKING_MEMORY... — runtime-only, structural is skipped there).
#   2. Stash whatever remains (leftover structural edits the agent shouldn't
#      have, untracked tooling, cruft) INCLUDING untracked, so the tree is clean.
#   3. git pull --rebase  (now succeeds — merged PRs land).
#   4. git stash pop (best-effort). On conflict we KEEP the stash (never drop)
#      and report — a human/agent can recover it; we never destroy work.
#
# Returns (ok, summary). ok=False only on a genuine pull failure the caller
# should surface; a clean no-op or a kept-stash-on-conflict still returns True
# with a descriptive summary (the tick must not crash on sync).
def safe_pull(
    repo_dir: "Path | str",
    bubble_git_guard_path: str = "/usr/local/bin/bubble-git-guard",
) -> "tuple[bool, str]":
    import subprocess
    repo_dir = Path(repo_dir).resolve()

    def _git(*args, **kw):
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, **kw,
        )

    notes = []

    # 1. Land legit runtime changes first (clears them from the tree). This
    #    reuses the single-source runtime/structural split — structural files
    #    are NOT committed here (they go via propose-settings-pr), they'll be
    #    stashed in step 2 instead.
    status = _git("status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        ok, err = force_commit_and_push(
            repo_dir,
            "loop: auto-commit runtime state before sync",
            bubble_git_guard_path=bubble_git_guard_path,
        )
        if ok:
            notes.append("runtime committed+pushed")
        else:
            # Push may legitimately fail (e.g. nothing-but-structural, or a
            # transient broker issue) — not fatal to the pull. Keep going; the
            # stash in step 2 still clears the tree so the pull can run.
            notes.append(f"runtime push skipped/failed: {err}")

    # 2. Stash anything still dirty (leftover structural edits + untracked), so
    #    the rebase has a clean tree. -u includes untracked; keep-index off.
    stashed = False
    status = _git("status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        st = _git("stash", "push", "--include-untracked",
                  "-m", "safe_pull: pre-rebase autostash")
        if st.returncode == 0 and "No local changes" not in (st.stdout + st.stderr):
            stashed = True
            notes.append("stashed leftovers")

    # 3. Pull --rebase (now the tree is clean → it succeeds).
    pull = _git("pull", "--quiet", "--rebase", "origin", "main")
    if pull.returncode != 0:
        # Abort a half-applied rebase so we don't wedge the tree.
        _git("rebase", "--abort")
        if stashed:
            _git("stash", "pop")  # restore the agent's work
        return False, (
            "pull --rebase FAILED: "
            + (pull.stderr or pull.stdout).strip()[:200]
            + " | " + "; ".join(notes)
        )
    notes.append("pulled")

    # 4. Restore the stash (best-effort). On conflict, KEEP the stash (do not
    #    drop) so nothing is lost; report it for human/agent recovery.
    if stashed:
        pop = _git("stash", "pop")
        if pop.returncode != 0:
            # Conflict or other issue — leave the stash in place, reset the
            # working-tree merge state so the tick can continue cleanly.
            _git("checkout", "--", ".")
            notes.append(
                "STASH KEPT (pop conflicted) — recover with `git stash list`/"
                "`git stash pop`; merged changes ARE applied"
            )
        else:
            notes.append("stash restored")

    return True, "; ".join(notes)

# ─── Gate-card YAML validation (Joris msg 3919, 2026-06-06) ─────────────────
#
# Agents hand-author rich gate cards under queues/gates/*.yaml (comments,
# multi-line scalars, expressive fields). A recurring footgun: an unquoted
# colon inside a scalar — e.g. `instrument: iShares ... (NASDAQ: TLT)` — makes
# the whole document invalid YAML, and the cockpit then can't render the card,
# so the human never sees the gate to approve it (TLT/ROBO/SMH/URA, 2026-06-06).
# This validator is the WRITE-TIME guard: a layer MUST call it right after
# writing a gate card; it fails LOUD so the bad card is fixed before the tick
# ends, instead of silently disappearing from the UI.

def validate_gate_card(path) -> "tuple[bool, str]":
    """Re-parse a just-written gate card. Returns (ok, message).

    ok=False with a precise message (line/col + the offending text) when the
    YAML is invalid or not a mapping, so the caller can fix-and-rewrite. The
    most common cause is an unquoted value containing a colon — wrap such values
    in double quotes (e.g. instrument: "iShares ... (NASDAQ: TLT)").
    """
    import yaml as _yaml
    p = Path(path)
    if not p.exists():
        return False, f"gate card not found: {p}"
    text = p.read_text(encoding="utf-8")
    try:
        doc = _yaml.safe_load(text)
    except _yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            lines = text.splitlines()
            bad = lines[mark.line] if 0 <= mark.line < len(lines) else ""
            return False, (
                f"invalid YAML at line {mark.line + 1} col {mark.column + 1}: "
                f"{str(getattr(e, 'problem', e)).strip()} -> `{bad.strip()[:100]}` "
                "(likely an unquoted colon — wrap the value in double quotes)"
            )
        return False, f"invalid YAML: {str(e).splitlines()[0]}"
    if not isinstance(doc, dict):
        return False, "gate card parsed but is not a YAML mapping (top level must be key: value)"
    if not doc.get("id") or not doc.get("kind"):
        return False, "gate card is missing required keys `id` and/or `kind`"
    return True, "ok"
