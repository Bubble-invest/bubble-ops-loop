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

{{OPERATOR}} msg 3129 (2026-05-24): "Layer 1 = morning / data refresh subagent
whose job is to materialize recurring_missions[] from dept.yaml into
actionable queue items based on each mission's cadence field. Layer 1
fires when all 3 other layers have completed 1 (or configurable) round."

Reference: scripts/lib/scaffold.py::CLAUDE_MD_OPERATING_TEMPLATE STEP C.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import yaml
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
# .last-mgmt-scan file I/O  (issue #176 — mgmt-note heartbeat coverage)
# ---------------------------------------------------------------------------
#
# Track when the dept last consumed its queues/management/*.yaml inbound notes.
# The file lives at `<repo_dir>/queues/management/.last-mgmt-scan` (alongside
# the note files themselves) so it is only ever written by Layer 1 (after its
# STEP 0-ter reads the notes) and read by build_dispatch_ctx on every tick.
#
# Using a timestamp-marker (Option A) rather than a consumed-flag on each YAML:
#  • no mutation of the note files (no churn, no consumed_at field sprawl)
#  • atomic: one file written once per L1 tick
#  • naturally handles batches of notes — all notes with created_at ≤ marker
#    are considered seen, regardless of how many files exist
#
# Interaction with .consumed.json (dept-level): .consumed.json tracks which
# note IDs L1 has acted on in its reasoning loop (the dept's own bookkeeping).
# .last-mgmt-scan is the DISPATCHER's signal: "has ANY new note arrived since
# the last time a real layer ran STEP 0-ter?" — it does not track actions.

_MGMT_SCAN_MARKER = ".last-mgmt-scan"


def read_last_mgmt_scan(repo_dir: "Path | str") -> "datetime | None":
    """Read the ISO-8601 timestamp from `<repo_dir>/queues/management/.last-mgmt-scan`.

    Returns None if the marker does not exist (never scanned → any note is new).
    """
    f = Path(repo_dir) / "queues" / "management" / _MGMT_SCAN_MARKER
    if not f.exists():
        return None
    body = f.read_text(encoding="utf-8").strip()
    if not body:
        return None
    try:
        return datetime.fromisoformat(body)
    except ValueError:
        return None


def write_last_mgmt_scan(repo_dir: "Path | str", when: "datetime | None" = None) -> None:
    """Write the ISO-8601 timestamp to `<repo_dir>/queues/management/.last-mgmt-scan`.

    Called by Layer 1 at the end of STEP 0-ter (after reading inbound notes),
    so the dispatcher knows the notes have been seen and stops routing to L1
    on heartbeat ticks. `when` defaults to now (UTC).
    """
    if when is None:
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError("write_last_mgmt_scan requires a tz-aware datetime")
    mgmt_dir = Path(repo_dir) / "queues" / "management"
    mgmt_dir.mkdir(parents=True, exist_ok=True)
    (mgmt_dir / _MGMT_SCAN_MARKER).write_text(when.isoformat(), encoding="utf-8")


def _load_consumed_ids(mgmt_dir: "Path") -> "set[str]":
    """Return the set of note IDs already recorded in `.consumed.json`.

    `.consumed.json` is the dept-level bookkeeping file written by L1's STEP
    0-ter to record which note IDs it has acted on. It may be a JSON object
    (keys are note IDs, values are metadata) or a JSON array (a list of IDs).
    Returns an empty set if the file is absent, unreadable, or malformed.
    """
    consumed_file = mgmt_dir / ".consumed.json"
    if not consumed_file.is_file():
        return set()
    try:
        raw = json.loads(consumed_file.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(raw, dict):
        return set(raw.keys())
    if isinstance(raw, list):
        return {str(x) for x in raw if x}
    return set()


def _scan_mgmt_notes(repo_dir: "Path | str", since: "datetime | None") -> bool:
    """Return True if `queues/management/` contains at least one inbound note
    with `created_at` strictly after `since` (or any note if `since` is None)
    that has NOT already been consumed.

    "Inbound" = a regular `*.yaml` file whose `audience` includes the dept slug
    OR whose `created_by`/`from` field is a manager (non-dept author). Because
    the dispatcher cannot know the dept slug here, we take the conservative
    approach: ANY `*.yaml` in `queues/management/` that is NOT a dotfile and is
    NOT written by the dept itself (i.e. not a Rick-request escalation from the
    dept — those have `audience: [rick, operator]` and are outbound) counts as a
    potential inbound note.

    Practical heuristic (matches the deployed PROMPT.md STEP 0-ter wording):
      - Exclude dotfiles (`.last-mgmt-scan`, `.consumed.json`, `.gitkeep`, …)
      - Exclude subdirectories (e.g. `.processed/`)
      - For each remaining file:
          1. Check `.consumed.json` FIRST — if the note's `id` is already in
             the consumed set, skip it regardless of its timestamp. This is the
             fix for issue #198: a note with a bad/missing `created_at` that
             hits the fail-open path would previously re-trigger L1 every tick
             until removed. Now an already-consumed note is silently skipped
             no matter what its timestamp looks like.
          2. If `since` is None → treat as unconsumed.
          3. Parse `created_at`; if `created_at > since` → unconsumed.
          4. Files still unparseable or missing `created_at` after the consumed
             check → fail-open (better to fire L1 once than to silently miss a
             note that hasn't been acted on yet).

    The scan intentionally does NOT read `audience` or `created_by` to avoid
    false negatives from structural variation across note kinds (directive vs
    management_note). A dept's own outbound Rick-request files live in the same
    directory but are typically processed quickly; the rare false positive (L1
    fired when only a Rick-request is in the queue and L1 was already going to run
    anyway) is acceptable.
    """
    mgmt_dir = Path(repo_dir) / "queues" / "management"
    if not mgmt_dir.is_dir():
        return False

    # Load the consumed-IDs set ONCE before the file loop (O(1) per note).
    consumed_ids = _load_consumed_ids(mgmt_dir)

    for p in mgmt_dir.glob("*.yaml"):
        if p.name.startswith("."):
            continue
        if not p.is_file():
            continue

        # Parse the note to get its id and created_at. We need the data dict
        # for both the consumed-check and the timestamp-check below, so we
        # parse it now. Fail-open on unreadable files (treat as unconsumed).
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            # Unreadable YAML → we can't determine id, so we can't consumed-
            # check it. Fall through to unconsumed (fail-open).
            return True

        # Fix #198 — consumed check BEFORE created_at parse:
        # If the note's id appears in .consumed.json, L1 has already acted on
        # it. Skip it unconditionally — a bad/missing created_at on a consumed
        # note must not cause an infinite re-trigger.
        note_id = data.get("id")
        if note_id and str(note_id) in consumed_ids:
            continue  # already consumed → not unconsumed, skip this note

        if since is None:
            # Marker absent → any non-consumed note is considered new
            return True

        # Parse created_at; fail-open on missing or unparseable timestamp.
        raw_ts = data.get("created_at")
        if raw_ts is None:
            return True  # no timestamp → treat as unconsumed
        try:
            note_ts = datetime.fromisoformat(str(raw_ts))
            if note_ts.tzinfo is None:
                note_ts = note_ts.replace(tzinfo=timezone.utc)
            if note_ts > since:
                return True
        except (ValueError, TypeError):
            return True  # unparseable timestamp → treat as unconsumed

    return False


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
    round" gate from {{OPERATOR}} msg 3129. It prevents Layer 1 from over-flooding
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
        # `day` may be a single string ("monday") or a list (["tuesday", "friday"]).
        # Normalise to a set of lowercase weekday names so the membership test
        # works uniformly for both shapes.  An absent or empty value produces an
        # empty set, which correctly falls through to the "not in _WEEKDAY_NAMES"
        # guard below and returns False (no valid day specified).
        raw_day = mission.get("day", "")
        if isinstance(raw_day, list):
            day_names = {d.lower() for d in raw_day if isinstance(d, str)}
        else:
            day_names = {raw_day.lower()} if raw_day else set()
        # Filter to only known weekday names (guards against typos / empty strings).
        valid_days = {d for d in day_names if d in _WEEKDAY_NAMES}
        if not t_str or not valid_days:
            return False
        if now_paris.weekday() not in {_WEEKDAY_NAMES[d] for d in valid_days}:
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

    input_kinds allow-list (fix #175):
    If ANY mission in the dept declares ``input_kinds: [...]``, only kinds
    that appear in at least one mission's ``input_kinds`` list are emitted.
    Kinds not consumed by any downstream layer (e.g. ``warming_outcome``,
    ``sent_confirmation``) are silently skipped — they are output-only
    artefacts, not queue items for the cockpit.

    Backward-compatibility: depts without any ``input_kinds`` declaration
    get the original behaviour (all creates[] are emitted).
    """
    # Consume the iterable once; we need two passes.
    missions_list = list(missions)

    # Build the global allowed_kinds set from all missions' input_kinds.
    allowed_kinds: set[str] = set()
    for m in missions_list:
        for k in m.get("input_kinds", []) or []:
            allowed_kinds.add(k)
    # Empty set → no mission declared input_kinds → backward-compat mode.
    gate_active = bool(allowed_kinds)

    out: list[dict] = []
    for m in missions_list:
        # WS3: materialize layers 1-3 (L4 is owned by the L4/debrief branch).
        if int(m.get("layer", 0)) not in (1, 2, 3):
            continue
        mid = m.get("id", "")
        last = last_fired_per_mission.get(mid)
        if not is_mission_due(m, now=now, last_fired=last):
            continue
        oq = m.get("output_queue", "")
        for kind in m.get("creates", []) or []:
            if gate_active and kind not in allowed_kinds:
                # Output-only kind — no layer consumes it as input.
                continue
            out.append({
                "mission_id": mid,
                "output_queue": oq,
                "kind": kind,
            })
    return out


def materialize_due_missions_for_tick(
    repo_dir: Path,
    today_dir: Path,
    now_utc: datetime,
) -> list[dict]:
    """Materialize due recurring missions into queue items (idempotent).

    Reads dept.yaml → recurring_missions, checks per-mission .last-run
    timestamps, and creates queue item YAML files for missions whose
    cadence says they are due right now.

    Idempotent via mission_id dedup: scans the output queue for existing
    items with the same mission_id (excluding .processed/ and dotfiles).
    If found, skips creation — same mission is never double-queued.

    Layer-4 missions are NOT materialized here (they fire via C.1).

    Returns the list of items that were actually created (empty if none).
    """
    dept_yaml_path = repo_dir / "dept.yaml"
    if not dept_yaml_path.is_file():
        return []

    try:
        dept = yaml.safe_load(dept_yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []

    missions = dept.get("recurring_missions") or []
    if not missions:
        return []

    # Per-mission .last-run timestamps: outputs/<today>/missions/<id>/.last-run
    last_fired_per_mission: dict[str, datetime] = {}
    for m in missions:
        mid = m.get("id", "")
        if mid:
            last = read_last_run(today_dir / "missions" / mid)
            if last:
                last_fired_per_mission[mid] = last

    # Anti-fire-spin (issue #261 / #235-#237 class): stamp the per-mission
    # .last-run for EVERY due layer-1..3 mission, BEFORE the creates[]
    # materialization loop below. A "pure report" mission (creates: [], e.g.
    # ben's market_wrapup) is is_mission_due()=True but yields NO queue-item
    # descriptors, so it never enters the `for item in due` loop and its marker
    # would never be stamped → _mission_last_fired() returns None forever →
    # select_due_missions re-selects it on EVERY tick (fire-spin). Stamping here
    # closes the leak at the source, independent of whether the dept's subagent
    # remembers to stamp it. Idempotent: the loop below re-stamps with the same
    # now_utc value, which is a no-op overwrite. Only missions already fired
    # today (in last_fired_per_mission, same Paris day) are skipped via
    # is_mission_due returning False.
    for m in missions:
        if int(m.get("layer", 0)) not in (1, 2, 3):
            continue  # L4 missions fire via C.1, not the materializer
        mid = m.get("id", "")
        if not mid:
            continue
        if is_mission_due(m, now=now_utc, last_fired=last_fired_per_mission.get(mid)):
            write_last_run(today_dir / "missions" / mid, when=now_utc)

    due = materialize_due_missions(
        missions, now=now_utc, last_fired_per_mission=last_fired_per_mission
    )

    if not due:
        return []

    created: list[dict] = []
    for item in due:
        mid = item["mission_id"]
        oq = item["output_queue"]
        kind = item["kind"]
        queue_dir = repo_dir / oq
        queue_dir.mkdir(parents=True, exist_ok=True)

        # Idempotent: skip if any existing queue item already has this mission_id.
        already_queued = False
        for existing in sorted(queue_dir.glob("*.yaml")):
            if existing.name.startswith("."):
                continue
            if not existing.is_file():
                continue
            try:
                existing_data = yaml.safe_load(
                    existing.read_text(encoding="utf-8")
                ) or {}
            except Exception:
                continue
            if existing_data.get("mission_id") == mid:
                already_queued = True
                break

        if already_queued:
            # The mission's card is still live in the queue — it has effectively
            # "fired" for today, so stamp .last-run too. Without this, a mission
            # whose card lingers (e.g. an orphan kind with no draining layer, or
            # work awaiting a later layer's min-time) stays last_fired=None →
            # due=True on every tick → the dispatcher fire-spins on it hourly.
            # That fire-spin is what drove the 2026-06-16 Maya deaf-restart storm
            # (heavy re-dispatch every tick → CC-core notification drop #61797).
            # See maya rick-requests: research-queue-poison-routing /
            # mission-lastrun-never-stamped (2026-06-16).
            write_last_run(today_dir / "missions" / mid, when=now_utc)
            continue

        # Create the queue item.
        ts = now_utc.isoformat()
        item_id = f"{kind}-{mid}-{now_utc.strftime('%Y%m%d-%H%M%S')}"
        queue_item = {
            "id": item_id,
            "mission_id": mid,
            "kind": kind,
            "created_at": ts,
            "created_by": "materialize_due_missions",
        }
        item_path = queue_dir / f"{item_id}.yaml"
        item_path.write_text(
            yaml.dump(queue_item, allow_unicode=True, default_flow_style=False)
        )
        # Stamp the mission .last-run so it is not re-materialized next tick.
        write_last_run(today_dir / "missions" / mid, when=now_utc)
        created.append(item)

    return created


# ---------------------------------------------------------------------------
# Deterministic dispatch decision (locks down the prompt's tree)
# ---------------------------------------------------------------------------

# Per-layer MINIMUM fire time (Europe/Paris local), {{OPERATOR}} msg 3904 (2026-06-06):
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


def _queue_has_items(queue_dir: Path,
                     drainable_kinds: "set[str] | None" = None) -> bool:
    """True if `queue_dir` holds at least one actionable item.

    "Actionable" = a regular `*.yaml` file that is NOT a dotfile/hidden helper.
    Excludes `.gitkeep`, anything starting with `.`, and processed/archived
    subdirs. Missing dir -> False (fail-safe).

    Kind-aware quarantine (2026-06-17, Maya rick-requests): if
    `drainable_kinds` is given, only count items whose `kind` is in that set.
    An "orphan kind" — a card no layer can drain (e.g. a `discovery_sweep`
    materialized into queues/research but only handled by L3, or a kind with no
    handler at all) — used to make this return True forever, so the dispatcher
    fire-spun on that layer every tick. That fire-spin tripped the CC-core
    notification drop (#61797) and drove the deaf-restart storm. With
    `drainable_kinds`, an unrecognised-kind card no longer pins the layer.
    `drainable_kinds=None` keeps the old kind-blind behaviour (fail-open: an
    item with no readable `kind` is still counted so we never silently starve
    a real item).
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return False
    for p in queue_dir.glob("*.yaml"):
        if p.name.startswith("."):
            continue
        if not p.is_file():
            continue
        if drainable_kinds is None:
            return True
        # Kind-aware: only an item this layer can actually drain counts.
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            # Unparseable → fail-open, count it (don't silently drop real work).
            return True
        kind = data.get("kind")
        if kind is None or kind in drainable_kinds:
            return True
    return False


def _drainable_kinds_for_layer(repo_dir: Path, layer: int) -> "set[str]":
    """Kinds a given layer can drain = the `creates[]` of that layer's
    recurring missions in dept.yaml. Empty set if dept.yaml is absent/unreadable
    (caller then falls back to kind-blind, fail-open)."""
    dept_yaml = Path(repo_dir) / "dept.yaml"
    if not dept_yaml.is_file():
        return set()
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    kinds: set[str] = set()
    for m in dept.get("recurring_missions") or []:
        if int(m.get("layer", 0)) == layer:
            for k in m.get("creates", []) or []:
                kinds.add(k)
    return kinds


def _drainable_kinds_for_queue(repo_dir: Path, queue_rel_path: str) -> "set[str]":
    """Kinds that legitimately land in an input queue = union of `creates[]`
    from all missions whose `output_queue` matches `queue_rel_path`.

    This is the producer-side view: "what kinds do upstream missions deposit
    into this queue?" It replaces the incorrect consumer-side view
    (`_drainable_kinds_for_layer`) for INPUT queue checks.

    Context — Issue #204 (2026-06-20): `build_dispatch_ctx` was computing
    `has_research_items` using `_drainable_kinds_for_layer(repo, 2)`, which
    returns L2's OWN creates[] (`{investment_case, proposal}` for Ben).
    But items in `queues/research/` are PRODUCED by L1 (kind: `research_item`)
    and CONSUMED by L2. `research_item` is in L1's creates[], not L2's, so
    `_queue_has_items` always returned False → L2 starved.

    The correct model for an INPUT queue check:
      drainable_kinds = union of creates[] of all missions with
                        output_queue == that queue

    This naturally includes cross-layer producer→queue relationships (L1→research,
    L2→gates, etc.) without knowing which layer consumes the queue.

    Anti-fire-spin guarantee (issue #61797): a kind produced by NO mission's
    `creates[]` (an orphan kind — e.g. a manually dropped YAML of unknown type)
    is still excluded because it appears in no mission's `creates[]`. The
    quarantine added in June-2026 is preserved: only kinds that SOME mission
    actively produces count as drainable.

    `queue_rel_path` should be the raw `output_queue` string from dept.yaml
    (e.g. `"queues/research/"` or `"queues/gates/"`). Trailing slashes are
    stripped for comparison so both forms match.

    Returns an empty set if dept.yaml is absent or unreadable (caller falls back
    to kind-blind, fail-open behaviour via the `drainable_kinds=None` path in
    `_queue_has_items`).
    """
    dept_yaml = Path(repo_dir) / "dept.yaml"
    if not dept_yaml.is_file():
        return set()
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    target = queue_rel_path.rstrip("/")
    kinds: set[str] = set()
    for m in dept.get("recurring_missions") or []:
        oq = (m.get("output_queue") or "").rstrip("/")
        if oq == target:
            for k in m.get("creates", []) or []:
                kinds.add(k)
    return kinds


def build_dispatch_ctx(
    repo_dir: "Path | str" = ".",
    *,
    now_utc: "datetime | None" = None,
    fire_after_rounds: int = 1,
) -> "dict[str, Any]":
    """Build the ctx dict that `decide_dispatch` consumes, by SCANNING the
    repo's queues + today's runtime markers.

    THIS is the piece that was missing (2026-06-01, {{OPERATOR}} msg 3588):
    `decide_dispatch` is a pure decision function — without a builder, the /loop
    was calling it with a placeholder, so has_research_items/has_inbox_decisions
    were never set and the tree fell through to "heartbeat" forever — L2/L3
    never fired and work piled up in the queues.

    Queue conventions (dept template):
      - dept.yaml::recurring_missions[] -> materialized into queue items
        by materialize_due_missions_for_tick() BEFORE the queue scan below.
        Uses mission_id dedup so the same mission is never double-queued.
      - queues/research/*.yaml         -> has_research_items (Layer 2)
      - queues/inbox/decisions/*.yaml  -> has_inbox_decisions (Layer 3)
      - outputs/<today>/4/.last-run    -> layer_4_last_run_today (L4 idempotence)
      - outputs/<today>/1/.last-run    -> layer_1_last_run_today (L1 daily floor)
      - outputs/<today>/round_counter.json -> round_counter (L1 cycle gate)
      - outputs/<today>/.l1-baseline.json  -> layer_1_baseline_counter (L1 cycle gate)
      - queues/management/.last-mgmt-scan  -> has_unconsumed_mgmt_notes (issue #176)

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

    # Materialize due recurring missions BEFORE scanning queues — newly
    # created items become visible to the queue scanners below and can
    # trigger the appropriate layer this same tick.
    materialize_due_missions_for_tick(repo, today_dir, now_utc)

    return {
        "now_utc": now_utc,
        # Deterministic UTC date for THIS tick. The agent MUST use these for
        # all outputs/<date>/ paths + the heartbeat, instead of hand-typing
        # the date from context — that froze Maya's loop on 2026-06-02 while
        # the real date was 2026-06-04 (see docs/FLOWCHART-SPEC.md BUG-DATE).
        "today": today,
        "today_dir": str(today_dir),
        # Kind-aware: only kinds PRODUCED into queues/research/ count, so an
        # orphan kind (a card no mission produces) can no longer pin L2 every
        # tick. Uses the producer-side view (_drainable_kinds_for_queue) rather
        # than the broken consumer-side view (_drainable_kinds_for_layer(repo,2))
        # — issue #204 fix: research_item is in L1's creates[], not L2's.
        # Falls back to kind-blind (drainable_kinds=None) if dept.yaml is absent
        # so a repo without a dept.yaml is fail-open (never silently starves).
        "has_research_items": _queue_has_items(
            repo / "queues" / "research",
            drainable_kinds=(_drainable_kinds_for_queue(repo, "queues/research/") or None),
        ),
        # Approved decisions land in the dept top-level `inbox/decisions/`
        # — that is where the cockpit approve-click writes (console
        # github_reader.write_decision) and where Layer 3 reads + archives to
        # `.processed/`. The original `queues/inbox/decisions/` path exists on
        # NO dept on disk, so has_inbox_decisions was ALWAYS False and C.3
        # (Layer 3) NEVER fired from the live loop — every approved trade was
        # stranded until the once-daily backup floor cron forced an L3 tick
        # (root cause of the recurring DTLA "missed its window", 2026-06-04 to 09).
        # Check both paths so the fix is robust to either inbox layout.
        "has_inbox_decisions": (
            _queue_has_items(repo / "inbox" / "decisions")
            or _queue_has_items(repo / "queues" / "inbox" / "decisions")
        ),
        "layer_1_last_run_today": read_last_run(today_dir / "1"),
        "layer_2_last_run_today": read_last_run(today_dir / "2"),
        "layer_3_last_run_today": read_last_run(today_dir / "3"),
        "layer_4_last_run_today": read_last_run(today_dir / "4"),
        "round_counter": read_round_counter(today_dir),
        "layer_1_baseline_counter": read_l1_baseline(today_dir),
        "fire_after_rounds": fire_after_rounds,
        # Issue #176: flag unconsumed inbound management notes so the dispatcher
        # routes to L1 even on heartbeat ticks (not just when L1's daily floor
        # or cycle gate fires). Layer 1 writes .last-mgmt-scan at the end of its
        # STEP 0-ter; any note with created_at strictly after that marker is new.
        "has_unconsumed_mgmt_notes": _scan_mgmt_notes(
            repo, since=read_last_mgmt_scan(repo)
        ),
    }


def decide_dispatch(ctx: dict[str, Any]) -> str:
    """Return one of "layer_1" / "layer_2" / "layer_3" / "layer_4" /
    "heartbeat" given the tick context.

    Required ctx keys (all optional, sensible defaults applied):
      now_utc: tz-aware UTC datetime — required
      has_research_items: bool
      has_inbox_decisions: bool
      has_unconsumed_mgmt_notes: bool — True if queues/management/ has notes with
        created_at > queues/management/.last-mgmt-scan (or any notes if marker absent)
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
      C.1 — Layer 4 if time>=19:00 Paris AND L1 fired today AND (L2 fired
            OR no research items) AND (L3 fired OR no inbox decisions —
            no-trade day) AND L4 not yet run (aggregator last)
      C.3 — Layer 3 if time>=16:00 Paris AND inbox decisions have items
      C.2 — Layer 2 if time>=12:00 Paris AND research queue has items
      C.mgmt — Layer 1 if time>=07:00 Paris AND unconsumed management notes
               exist AND L1 not already fired this same tick's daily-floor slot
               (issue #176: a directive arriving in a quiet stretch is not
               delayed until the next natural L1 floor or cycle gate)
      C.0 — Layer 1 if time>=07:00 Paris AND (not yet run today  OR  each other
            layer has fired a fresh round since L1 last fired)
      C.4 — heartbeat (default)

    Note: C.0 has the LOWEST priority despite its number — Layer 1 is the
    last to fire, so a tick with research / decisions / an L4 window does
    that work first and the morning brief slots into a quiet tick.

    {{OPERATOR}} flag 2026-06-01 (refined): Layer 1 fires "at least once per day, or
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
    has_mgmt_notes = bool(ctx.get("has_unconsumed_mgmt_notes", False))
    l1_last = ctx.get("layer_1_last_run_today")
    counts = ctx.get("round_counter") or {}
    baseline = ctx.get("layer_1_baseline_counter") or {}
    fire_after_rounds = int(ctx.get("fire_after_rounds", 1))

    l1_fired = _layer_fired_today(ctx, 1)
    l2_fired = _layer_fired_today(ctx, 2)
    l3_fired = _layer_fired_today(ctx, 3)
    l4_fired = _layer_fired_today(ctx, 4)

    # MODEL ({{OPERATOR}} msg 3904, 2026-06-06): each layer has a MINIMUM fire time
    # (Paris-local), not a window — eligible from then to end of day, and may
    # re-fire later if there is work. Two layers carry a prerequisite gate ON
    # TOP of the time check:
    #   • L4 may fire once L1 has fired >=1x today AND (L2 has fired
    #     OR no research items — quiet day) AND (L3 has fired OR no inbox
    #     decisions — no-trade day). This sequences the aggregator after all
    #     work is done — with or without research or trades.)
    #   • L1's re-consolidation fires once the other layers have completed a
    #     fresh cycle since L1 last ran (its morning floor fires unconditionally
    #     once 07:00 Paris is reached and it has not run yet today).
    # Priority: L4 (debrief, end of day) > L3 > L2 > L1, each guarded by time.

    # C.1 — Layer 4: time reached AND L1 fired today AND (L2 fired OR no research items — quiet day) AND (L3 fired OR no inbox decisions — no-trade day) AND not yet run.
    if (
        _time_reached(now_paris_t, 4)
        and l1_fired and (l2_fired or not has_research) and (l3_fired or not has_decisions)
        and not l4_fired
    ):
        return "layer_4"

    # C.3 — Layer 3: an APPROVED decision is waiting. Eligible from L1's
    # morning floor (07:00 Paris, _LAYER_MIN_TIME[1]) rather than the fixed
    # 16:00 L3 gate, so an approved trade reaches the executor at ITS market's
    # open — not hours later. Safe because L3 runs its OWN arm-state fence +
    # market-open + slippage/news pre-flight (PROMPT STEP 0ter / validate) and
    # defers WITHOUT a broker call when the venue is shut. The old 16:00 Paris
    # gate left almost no window for LSE names (LSE closes 17:30 Paris) which —
    # together with the has_inbox_decisions path bug fixed above — is why
    # approved trades kept lapsing unexecuted. C.3 still ranks ABOVE C.2, so an
    # approved trade always outranks the research queue.
    if _time_reached(now_paris_t, 1) and has_decisions:
        return "layer_3"

    # C.2 — Layer 2: time reached AND research queue has items (re-fireable).
    if _time_reached(now_paris_t, 2) and has_research:
        return "layer_2"

    # C.mgmt — Layer 1: unconsumed management notes exist AND morning floor
    # reached. Routes to L1 so its STEP 0-ter consumes the notes on THIS tick,
    # instead of waiting for the next natural L1 floor or cycle gate (issue #176).
    # Gated at L1's 07:00 Paris floor (not fired during night-quiet hours where
    # no normal L1 would run either). Does NOT require L1 to be unfired today:
    # if a new note arrives AFTER L1 ran this morning, this branch re-fires L1
    # so the note is read within the next tick (up to ~1h), not deferred to
    # tomorrow's floor or the next full cycle gate (which could be many hours).
    if _time_reached(now_paris_t, 1) and has_mgmt_notes:
        return "layer_1"

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


# ---------------------------------------------------------------------------
# Mission-centric dispatch (issue #261, 2026-06-23)
# ---------------------------------------------------------------------------
#
# BACKGROUND: decide_dispatch() returns ONE phase string ("layer_1" … "layer_4").
# The runtime was then loading ONE monolithic layers/<N>/PROMPT.md and spawning
# ONE subagent — so only the PRIMARY mission per phase ever ran. Secondary
# missions declared in dept.yaml::recurring_missions[] were ORPHANED: ben's
# `market_wrapup`, content's `newsletter_redaction`, etc. never fired.
#
# FIX: select_due_missions(ctx, missions) enumerates ALL due missions on the
# highest-priority eligible phase, respecting every existing gate (time floors,
# queue non-emptiness for consumers, L4 prerequisites, L1 cycle gate). The
# runtime spawns ONE subagent per returned mission via resolve_mission_prompt().
#
# BACK-COMPAT: decide_dispatch() is UNCHANGED. Existing callers + all tests
# pass without modification. select_due_missions is purely ADDITIVE.

_LAYER_PRIORITY = [4, 3, 2, 1]   # highest to lowest (mirrors decide_dispatch)


def _mission_layer_eligible(ctx: "dict[str, Any]", layer: int) -> bool:
    """Return True if the given layer's time gate AND prerequisites are met.

    Mirrors the exact conditions decide_dispatch uses per branch, so the two
    functions always agree on which layer is eligible. Only the PRIMARY
    mission per phase was ever checked before; we now expose this as a
    reusable predicate.
    """
    now_paris_t = _to_paris(ctx["now_utc"]).time()
    has_research = bool(ctx.get("has_research_items", False))
    has_decisions = bool(ctx.get("has_inbox_decisions", False))
    l1_fired = _layer_fired_today(ctx, 1)
    l2_fired = _layer_fired_today(ctx, 2)
    l3_fired = _layer_fired_today(ctx, 3)
    l4_fired = _layer_fired_today(ctx, 4)

    if layer == 4:
        return (
            _time_reached(now_paris_t, 4)
            and l1_fired
            and (l2_fired or not has_research)
            and (l3_fired or not has_decisions)
            and not l4_fired
        )
    if layer == 3:
        return _time_reached(now_paris_t, 1) and has_decisions
    if layer == 2:
        return _time_reached(now_paris_t, 2) and has_research
    if layer == 1:
        if not _time_reached(now_paris_t, 1):
            return False
        # C.mgmt: unconsumed management notes (always eligible when floor reached)
        if bool(ctx.get("has_unconsumed_mgmt_notes", False)):
            return True
        # C.0a: daily floor
        if not _layer_fired_today(ctx, 1):
            return True
        # C.0b: cycle gate
        counts = ctx.get("round_counter") or {}
        baseline = ctx.get("layer_1_baseline_counter") or {}
        fire_after_rounds = int(ctx.get("fire_after_rounds", 1))
        return all(
            int(counts.get(str(lyr), 0)) - int(baseline.get(str(lyr), 0))
            >= fire_after_rounds
            for lyr in (2, 3, 4)
        )
    return False


def _mission_input_ready(ctx: "dict[str, Any]", mission: dict) -> bool:
    """Return True if the mission's input queue condition is satisfied.

    Producer missions (those that WRITE TO a queue without reading FROM one —
    no `input_queue` key) are always considered ready; they generate new work
    regardless of queue state.

    Consumer missions declare an `input_queue` key. They are only eligible when
    that queue is non-empty (with the same kind-aware quarantine used by
    build_dispatch_ctx so orphan kinds don't pin the dispatcher).

    `ctx` must have been built by build_dispatch_ctx (so `now_utc` and
    `today_dir` are present), or at minimum must carry `_repo_dir` if the
    caller injects it. When no `_repo_dir` is available we fail-open (return
    True) so a misconfigured ctx never silently starves a mission.
    """
    input_queue = mission.get("input_queue")
    if not input_queue:
        # Producer mission — no input-queue gate.
        return True

    repo_dir = ctx.get("_repo_dir")
    if not repo_dir:
        # No repo_dir injected → fail-open (never starve).
        return True

    queue_path = Path(repo_dir) / input_queue
    drainable = _drainable_kinds_for_queue(Path(repo_dir), input_queue) or None
    return _queue_has_items(queue_path, drainable_kinds=drainable)


def _mission_last_fired(ctx: "dict[str, Any]", mission: dict) -> "datetime | None":
    """Return the per-mission .last-run timestamp, or None if absent or stamped
    THIS tick.

    Per-mission marker path: outputs/<today>/missions/<id>/.last-run

    This is written either by:
      a) materialize_due_missions_for_tick — runs at the TOP of build_dispatch_ctx
         and stamps the marker at the CURRENT tick's now_utc for every due
         layer-1..3 mission (anti-fire-spin, issue #261). Because this runs in the
         SAME tick as the dispatch decision, the marker it writes equals
         ctx['now_utc'].
      b) The mission's own subagent — on a LATER tick, after it completes, stamps
         this path so the next tick's select_due_missions skips it.

    Critical "this-tick" exclusion (fixes the tick-1-vs-tick-2 ambiguity):
    a marker whose timestamp == ctx['now_utc'] was written by the materializer
    THIS tick — the dispatch has NOT happened yet, so we return None so the
    mission IS still selected this tick. A marker from a PRIOR tick (< now_utc)
    means the mission already ran today → return it so is_mission_due() vetoes
    a re-dispatch. Without this distinction the materializer's own same-tick
    stamp would exclude the mission on the very tick it became due (it would
    never run at all).

    Deliberately does NOT fall back to the layer-level marker. The layer marker
    tracks whether a LAYER ran; the per-mission marker tracks whether THIS MISSION
    ran. A mission with no per-mission marker → last_fired=None → is_mission_due
    evaluates purely on cadence (time/day) without a "fired today" veto.
    """
    today_dir_str = ctx.get("today_dir")
    if not today_dir_str:
        # No today_dir in ctx → treat as never fired (mission is due).
        return None

    mid = mission.get("id", "")
    if not mid:
        return None

    marker = read_last_run(Path(today_dir_str) / "missions" / mid)
    if marker is None:
        return None

    # A marker stamped THIS tick (by the materializer at ctx['now_utc']) does not
    # mean the dispatch already ran — it means the mission became due this tick.
    # Treat it as "not yet fired" so select_due_missions still returns it now;
    # next tick the marker will be < now_utc and correctly veto a re-dispatch.
    now_utc = ctx.get("now_utc")
    if now_utc is not None and marker == now_utc:
        return None

    return marker


def select_due_missions(
    ctx: "dict[str, Any]",
    missions: "list[dict]",
) -> "list[dict]":
    """Return all due missions for the highest-priority eligible phase this tick.

    This is the mission-centric dispatch primitive. The caller (the /loop
    runtime) uses it to spawn ONE subagent per returned mission instead of ONE
    per phase — so every secondary mission (ben's `market_wrapup`, content's
    `newsletter_redaction`, etc.) runs, not just the primary one.

    Input:
      ctx     — the same dict that decide_dispatch consumes (built by
                build_dispatch_ctx). May optionally carry `_repo_dir` (str or
                Path) for input-queue checks on consumer missions.
      missions — the dept's `recurring_missions` list from dept.yaml.

    Selection criteria per mission (ALL must hold):
      1. Its `layer` is on the highest-priority eligible phase (mirrors
         decide_dispatch's priority order: L4 > L3 > L2 > L1).
      2. The layer's time floor (Paris-local) is reached AND its
         prerequisites are met (same gates as decide_dispatch).
      3. `is_mission_due()` returns True using the mission's own per-mission
         last-run timestamp (with layer-marker fallback for regression safety).
      4. Its input is ready: producer missions always pass; consumer missions
         (those with `input_queue`) require that queue to be non-empty.

    Returns missions in layer-priority order, then by mission id for
    determinism. Returns [] when no phase is eligible (heartbeat tick) or no
    mission in the eligible phase is due.

    Back-compat guarantee: decide_dispatch(ctx) returns the same phase string
    as before — this function does NOT mutate ctx or alter any on-disk state.
    The relationship is:
      phase = decide_dispatch(ctx)
      due   = select_due_missions(ctx, missions)
      ∀ m ∈ due: m["layer"] == _layer_from_phase(phase)  (when due is non-empty)
    """
    now_utc: datetime = ctx.get("now_utc")
    if now_utc is None or now_utc.tzinfo is None:
        return []

    # Determine the highest-priority eligible layer (same logic as decide_dispatch).
    eligible_layer: "int | None" = None
    for layer in _LAYER_PRIORITY:
        if _mission_layer_eligible(ctx, layer):
            eligible_layer = layer
            break

    if eligible_layer is None:
        return []

    # Among all missions on that layer, keep those that are due and have ready input.
    due: list[dict] = []
    for m in missions:
        if int(m.get("layer", 0)) != eligible_layer:
            continue
        last_fired = _mission_last_fired(ctx, m)
        if not is_mission_due(m, now=now_utc, last_fired=last_fired):
            continue
        if not _mission_input_ready(ctx, m):
            continue
        due.append(m)

    # Sort by mission id for determinism (layer is uniform across the result).
    due.sort(key=lambda m: m.get("id", ""))
    return due


def resolve_mission_prompt(repo_dir: "Path | str", mission: dict) -> Path:
    """Return the Path to the PROMPT.md for a given mission.

    Convention (mission-centric):
      missions/<id>/PROMPT.md  — per-mission prompt (preferred when it exists)

    Legacy shim (zero-regression):
      layers/<layer>/PROMPT.md — monolithic layer prompt (used when no
        per-mission prompt exists, so depts that have not yet migrated to
        per-mission prompts keep running their existing layer prompt via the
        primary mission — exactly the same behaviour as before this change).

    The caller uses the returned Path as the task description for the Agent
    tool invocation. A mission whose prompt is the legacy layer prompt behaves
    identically to the old phase-centric dispatch.
    """
    repo = Path(repo_dir)
    mid = mission.get("id", "")
    layer = int(mission.get("layer", 0))

    if mid:
        per_mission = repo / "missions" / mid / "PROMPT.md"
        if per_mission.exists():
            return per_mission

    # Legacy shim: fall back to the layer's monolithic PROMPT.md.
    return repo / "layers" / str(layer) / "PROMPT.md"


# ─── Independent dept liveness signal ({{OPERATOR}} flag 2026-06-01) ───────────────
#
# {{OPERATOR}} flag 2026-06-01: "Export from dept shouldn't be your only data."
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


# ─── Auto-retry mechanism + force commit/push ({{OPERATOR}} msg 3134) ──────────────
#
# {{OPERATOR}} flag 2026-05-24: "We need a mechanism for agent auto retry if he
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


# ─── Vendored, canonical-sourced paths a dept must NEVER push (2026-06-11) ──
#
# `scripts/lib/**` (dispatch_helpers.py + its sibling tests) is VENDORED into
# every dept from the framework repo by scripts/sync-dispatch-lib.sh and
# re-synced at restart. It is NOT in any dept's `allowed_paths`
# (outputs/queues/inbox/...) — by design: the canonical copy lives in
# bubble-ops-loop and changes there via a human-merged PR, never a dept push.
#
# The gap this closes: `_is_structural` (the runtime/structural split below)
# only flags the SHARED mission globs. `scripts/lib/**` is structural ONLY in
# the framework repo (FRAMEWORK_STRUCTURAL_PATH_GLOBS), so in a DEPT repo it is
# neither structural NOR runtime-allowed. force_commit_and_push therefore used
# to classify a re-vendored scripts/lib/ change as "runtime", stage+commit it,
# and the guard then DENIED the dept's whole next push ("path scripts/lib/... not
# in allowed_paths") — stranding every runtime commit behind it. Poisoned Tony's
# loop on 2026-06-11. We skip these paths from the runtime push entirely; the
# vendored files stay correct on disk (the loop imports them) and are owned by
# the framework PR flow, not the dept.
_VENDORED_NONPUSHABLE_GLOBS = (
    "scripts/lib/**",
)


def _is_vendored_nonpushable(path: str) -> bool:
    """True if `path` is a vendored, canonical-sourced file a dept must not push
    (currently scripts/lib/**). Kept separate from _is_structural because the
    remedy differs: structural -> propose-settings-pr; vendored -> leave it to the
    framework sync (no dept action at all)."""
    for g in _VENDORED_NONPUSHABLE_GLOBS:
        if g.endswith("/**"):
            base = g[:-3]
            if path == base or path.startswith(base + "/"):
                return True
        elif path == g:
            return True
    return False


def force_commit_and_push(
    repo_dir: Path,
    message: str,
    bubble_git_guard_path: str = "/usr/local/bin/bubble-git-guard",
    action: str = "runtime_write_own",
) -> tuple[bool, "str | None"]:
    """Stage everything, commit, and push the dept's OWN repo.

    Called by Layer 4 after EACH artifact write per {{OPERATOR}} msg 3134 — the
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

    # Ensure broker PATH is always available for subprocess calls
    # (sandbox/bwrap may strip the systemd Environment PATH).
    _broker_bin = "/opt/bubble-token-broker/bin"
    _env = os.environ.copy()
    if _broker_bin not in _env.get("PATH", ""):
        _env["PATH"] = _broker_bin + ":" + _env.get("PATH", "")

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

    # Local-bare: skip broker mint + guarded push; plain git push works for file://
    _remote_url = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout.strip()
    if _remote_url.startswith("file://"):
        push = subprocess.run(
            ["git", "-C", str(repo_dir), "push", "origin", "main"],
            capture_output=True, text=True,
        )
        return (push.returncode == 0, None if push.returncode == 0 else f"git push failed (rc={push.returncode}): {(push.stderr or push.stdout).strip()[:200]}")

    # DRY_RUN: resolve + report the push target and RETURN *before* any mutation.
    # This MUST come before `git add`/`git commit` so a dry run is genuinely
    # side-effect-free (HEAD, index and working tree all untouched) — a dry run
    # must never mint a local commit. Lets callers/tests verify isolation (which
    # repo would be pushed) without touching the repo.
    if os.environ.get("DRY_RUN"):
        print(f"[DRY_RUN] would push dept={slug!r} repo={repo_name!r} from {repo_dir}")
        return True, None

    # 2. Stage RUNTIME paths only — never structural files ({{OPERATOR}} 2026-06-06).
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
    skipped_vendored = []
    for c in changed:
        path = c.split(" -> ")[-1] if " -> " in c else c
        if _is_struct(path):
            skipped_structural.append(path)
        elif _is_vendored_nonpushable(path):
            # Vendored canonical lib (scripts/lib/**) — owned by the framework
            # sync, never a dept push. Skipping it here is what stops the
            # "scripts/lib/... not in allowed_paths" guard DENY that strands the
            # whole runtime push behind it (Tony, 2026-06-11).
            skipped_vendored.append(path)
        else:
            runtime_paths.append(path)
    if skipped_structural:
        print(
            "[force_commit_and_push] NOT staging structural file(s) for the "
            "runtime push (route via propose-settings-pr): "
            + ", ".join(sorted(set(skipped_structural)))
        )
    if skipped_vendored:
        print(
            "[force_commit_and_push] NOT staging vendored canonical file(s) for "
            "the runtime push (owned by scripts/sync-dispatch-lib.sh): "
            + ", ".join(sorted(set(skipped_vendored)))
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
            env=_env,
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
            env=_env,
        )
    if push.returncode != 0:
        return False, (
            f"git push failed (rc={push.returncode}): "
            f"{(push.stderr or push.stdout).strip()[:200]}"
        )
    return True, None



# ─── safe_pull: dirty-tree-proof in-loop sync ({{OPERATOR}} msg 3979, 2026-06-06) ──
#
# WHY: the loop's tick step 1 was `git pull --quiet --rebase || echo
# 'pull-failed-continuing'`. On a dirty working tree git refuses
# ("cannot pull with rebase: you have unstaged changes") and the tick just
# continues — so MERGED structural changes (a CLAUDE.md/skill PR a human just
# merged) NEVER land on the box. That's the auto-redeploy gap: merge != live.
# Reproduced on tony/ben/maya 2026-06-06 (28/6/2 dirty files).
#
# Option A ({{OPERATOR}}'s pick): make the pull RELIABLE without ever losing work.
#   1. Land legit RUNTIME changes via force_commit_and_push (outputs/queues/
#      inbox/WORKING_MEMORY... — runtime-only, structural is skipped there).
#   2. Stash whatever remains (leftover structural edits the agent shouldn't
#      have, untracked tooling, cruft) INCLUDING untracked, so the tree is clean.
#   3. Tag HEAD as a safety checkpoint (Maya 2026-06-15 data-loss fix: a
#      fast-forward to a merge commit can delete runtime files the dept pushed
#      moments earlier because the merged PR branch was forked before the push).
#   4. git pull --rebase  (now succeeds — merged PRs land).
#   4a. Detect and restore any runtime files the pull deleted (diff against the
#       safety tag; checkout deleted outputs/queues/... back from the tag).
#   5. git stash pop (best-effort). On conflict we KEEP the stash (never drop)
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

    # 3. Snapshot the current HEAD before the pull so we can detect and
    #    recover any runtime files the pull deletes (Maya data-loss bug,
    #    2026-06-15: a fast-forward to a merge commit deleted subagent
    #    output files — the merge branch was forked before the outputs
    #    were pushed, so the merge commit never included them).
    from datetime import datetime, timezone
    backup_tag = f"safe_pull_pre_pull_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    _git("tag", backup_tag)

    # 4. Pull --rebase (now the tree is clean → it succeeds).
    pull = _git("pull", "--quiet", "--rebase", "origin", "main")
    if pull.returncode != 0:
        # Abort a half-applied rebase so we don't wedge the tree.
        _git("rebase", "--abort")
        _git("tag", "-d", backup_tag)
        if stashed:
            _git("stash", "pop")  # restore the agent's work
        return False, (
            "pull --rebase FAILED: "
            + (pull.stderr or pull.stdout).strip()[:200]
            + " | " + "; ".join(notes)
        )
    notes.append("pulled")

    # 4a. Detect and restore any runtime files the pull deleted.
    #     This closes the Maya 2026-06-15 data-loss bug: when a merge
    #     commit on origin removes files the dept pushed moments earlier
    #     (because the merged branch was forked before the dept's push),
    #     the fast-forward silently drops them.  We recover them from
    #     the pre-pull backup tag.
    diff = _git("diff", "--name-only", "--diff-filter=D",
                backup_tag, "HEAD")
    if diff.returncode == 0 and diff.stdout.strip():
        deleted = diff.stdout.strip().splitlines()
        # Only restore runtime paths (outputs/queues/inbox/WORKING_MEMORY
        # etc.) — never structural files the human intentionally deleted.
        runtime_deleted = [
            p for p in deleted
            if (p.startswith("outputs/") or p.startswith("queues/")
                or p.startswith("inbox/") or p == "WORKING_MEMORY.md"
                or p.startswith("missions/"))
        ]
        if runtime_deleted:
            for p in runtime_deleted:
                _git("checkout", backup_tag, "--", p)
            notes.append(
                f"RESTORED {len(runtime_deleted)} runtime file(s) "
                "dropped by pull: " + ", ".join(runtime_deleted)
            )
    _git("tag", "-d", backup_tag)

    # 5. Restore the stash (best-effort). On conflict, KEEP the stash (do not
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

# ─── Gate-card YAML validation ({{OPERATOR}} msg 3919, 2026-06-06) ─────────────────
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
