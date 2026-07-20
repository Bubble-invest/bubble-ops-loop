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
# Tolerant ISO-8601 parser
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z' (UTC) which
    ``datetime.fromisoformat`` rejects before Python 3.11, AND tolerating a
    NAIVE timestamp (no offset/'Z' at all) by assuming UTC.

    On Python 3.9 and 3.10, ``fromisoformat('2026-06-26T08:03:39Z')`` raises
    ``ValueError``.  This helper normalises the 'Z' to '+00:00' before
    parsing so agent-written timestamps are accepted on all supported Python
    versions.  For non-'Z' input the behaviour is identical to the bare call.

    #713: an agent-written `.last-run` marker occasionally has no tz info at
    all (e.g. ``2026-07-19T19:06:19.897511``) — ``fromisoformat`` parses this
    fine but returns a NAIVE datetime, which later poisons ``_to_paris`` and
    crashes dispatch. This helper normalises any naive result to tz-aware
    UTC before returning, so a naive marker on disk never produces a naive
    datetime in memory. For an input that already carries an offset (or
    'Z'), this is a pure no-op — behaviour is unchanged.
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
    return _parse_iso(body)


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
        return _parse_iso(body)
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
          1. Check `.consumed.json` FIRST — if the note's `id` (or, absent
             that, its `directive_id` — fix #468, directive-shaped notes never
             carry a top-level `id`) is already in the consumed set, skip it
             regardless of its timestamp. This is the fix for issue #198: a
             note with a bad/missing `created_at` that hits the fail-open path
             would previously re-trigger L1 every tick until removed. Now an
             already-consumed note is silently skipped no matter what its
             timestamp looks like.
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
        # #593: a top-level-LIST (or other non-dict) YAML document is truthy,
        # so `or {}` does not catch it — the `.get()` calls below would then
        # raise AttributeError. Treat it like the unreadable case above:
        # fail-open, treat as unconsumed.
        if not isinstance(data, dict):
            return True

        # Fix #198 — consumed check BEFORE created_at parse:
        # If the note's id appears in .consumed.json, L1 has already acted on
        # it. Skip it unconditionally — a bad/missing created_at on a consumed
        # note must not cause an infinite re-trigger.
        #
        # Fix #468 — directive-shaped notes (scripts/dispatch_directives.py)
        # carry `directive_id` and NEVER a top-level `id`. Without this
        # fallback such a note can never match `.consumed.json`, so an
        # already-consumed directive keeps failing open on its timestamp and
        # re-fires L1 forever (root cause suspected for #235). Mirror this
        # exactly in console/services/mgmt_note_state.py:_note_id() — the two
        # must never drift (see that module's docstring / PR #189 note).
        note_id = data.get("id") or data.get("directive_id")
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
            note_ts = _parse_iso(str(raw_ts))
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
    """Convert a UTC datetime into Paris-local time.

    #713: a naive datetime (no tzinfo) used to raise ``ValueError`` here — a
    single bad `.last-run` marker (e.g. an agent-written naive timestamp)
    was enough to crash `build_dispatch_ctx` for the rest of the day. Now we
    assume UTC for naive input instead of raising, matching `_parse_iso`'s
    read-boundary tolerance (defense in depth for any other call site that
    hands this a naive datetime). For an already tz-aware input this is a
    pure no-op — behaviour is unchanged.
    """
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(_PARIS)


def _parse_hhmm(s: str) -> _time:
    h, m = s.split(":")
    return _time(int(h), int(m))


def _normalize_weekday_set(raw_day: "str | list | None") -> "set[str]":
    """Normalise a mission's `day:` field into a set of valid lowercase
    weekday names, for the weekday-membership test.

    `raw_day` may be a single weekday string ("monday") or a list of weekday
    strings (["monday", "thursday"]); either shape is normalised uniformly.
    Unknown names (typos) and an absent/empty value are filtered out, so a
    caller checking `now_paris.weekday() not in {_WEEKDAY_NAMES[d] for d in
    valid_days}` for an empty result correctly treats it as "no valid day
    specified".
    """
    if isinstance(raw_day, list):
        day_names = {d.lower() for d in raw_day if isinstance(d, str)}
    else:
        day_names = {raw_day.lower()} if raw_day else set()
    return {d for d in day_names if d in _WEEKDAY_NAMES}


def is_mission_due(mission: dict, *, now: datetime,
                   last_fired: datetime | None) -> bool:
    """Return True iff the mission's cadence says it should fire RIGHT NOW.

    `now` must be a tz-aware UTC datetime. `last_fired` is the mission's
    own .last-run timestamp (None if never fired).

    Supported cadences (from recurring-mission.schema.yaml::cadence):
      daily            — needs `time:` HH:MM Paris. Due once per Paris-local
                         day after that time.
      weekly           — needs `time:` + `day:` (lowercase English). `day`
                         may be a single weekday string ("monday") or a
                         list of weekday strings (["monday", "thursday"]).
                         Due once per Paris-local day on each listed day
                         after time — a single-day mission fires once/week;
                         a multi-day mission fires once per listed day.
      hourly           — top of every Paris hour.
      every_<N>h       — every N hours since last fire.
      every_<N>m       — every N minutes since last fire.
      event            — trigger-gated, not time-gated. is_mission_due always
                         returns True; whether the mission actually fires is
                         decided downstream by phase eligibility (has_inbox_decisions
                         gates L3) and input-readiness (_mission_input_ready) and
                         the per-mission same-tick marker. Do NOT add a time-based
                         "already fired" veto here — a second approved item the same
                         day must be processable. Idempotence is provided by the
                         per-mission .last-run marker (stamped this tick by the
                         materializer → treated as "not yet dispatched" by
                         _mission_last_fired) and by the approved-item being
                         consumed/archived out of inbox/decisions by the subagent.
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
        raw_day = mission.get("day", "")
        valid_days = _normalize_weekday_set(raw_day)
        if not t_str or not valid_days:
            return False
        if now_paris.weekday() not in {_WEEKDAY_NAMES[d] for d in valid_days}:
            return False
        target = _parse_hhmm(t_str)
        if now_paris.time() < target:
            return False
        # Already fired today (same Paris-local date)? A single-day weekly
        # still fires at most once/week because the weekday-membership check
        # above already excludes every other day; a multi-day `day:` list
        # (e.g. ["monday", "thursday"]) fires at most once per listed day.
        if last_paris and last_paris.date() == now_paris.date():
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

    if cadence == "event":
        # Event missions are trigger-gated, not time-gated.  "Is it time?" is
        # ALWAYS yes from is_mission_due's perspective — the REAL question
        # (is there a trigger to process?) is answered downstream:
        #
        #   • Phase eligibility (_mission_layer_eligible, L3 branch):
        #       requires has_inbox_decisions=True, so an L3 event mission
        #       is only in scope when approved decisions actually exist.
        #   • Input readiness (_mission_input_ready):
        #       for missions with no input_queue (producer-style, e.g.
        #       publish_execution) this always returns True — the phase gate
        #       above is the real guard.
        #   • Per-mission same-tick marker (_mission_last_fired):
        #       the materializer stamps outputs/<today>/missions/<id>/.last-run
        #       at now_utc this tick; _mission_last_fired treats a same-tick
        #       marker as "not yet dispatched", so the mission fires this tick
        #       and is excluded on the NEXT tick (same Paris day).  If the
        #       subagent archives the inbox/decisions item, has_inbox_decisions
        #       becomes False → L3 no longer eligible → the mission is not
        #       re-selected anyway.
        #
        # Why no time-based "fired today" veto here:
        #   A second legitimately-approved item arriving the same day (different
        #   gate card) must still be processable.  Adding a "same Paris day →
        #   False" check would wrongly block that second item.  The per-mission
        #   marker + inbox consumption provide sufficient idempotence.
        return True

    # cron:<expr> — escape hatch. Out of scope for STEP C.0.
    return False


# ---------------------------------------------------------------------------
# Mission-granular "pending later today" (board #508, 2026-07-03)
# ---------------------------------------------------------------------------

def next_pending_mission_time_today(
    missions: "list[dict]",
    *,
    now: datetime,
    last_run_lookup,
) -> "datetime | None":
    """Return the earliest Paris-local `time:` slot still ahead of `now` TODAY
    for any daily/weekly mission that has NOT yet fired for its current
    period, or None if nothing is pending later today.

    WHY this exists (#508): the live /loop's wake-arming is LAYER-granular —
    it arms "tomorrow morning" once every LAYER has fired at least once today.
    A mission sharing that layer but with a LATER `time:` (e.g. Ben's L4
    `market_wrapup`@22:30 vs `risk_control`@21:00, both L4) is then never
    re-checked: the layer already looks "done" so the loop sleeps until
    tomorrow and the later mission is silently starved every day the live
    loop doesn't happen to still be awake at its slot.

    FIX: before arming the next-morning one-shot, the loop calls this helper.
    If it returns a datetime, the loop arms an INTERIM one-shot wake for that
    time instead of jumping straight to tomorrow morning — so the mission
    gets its own re-check at its own slot, independent of its layer's state.

    Args:
      missions        — the dept's `recurring_missions` list from dept.yaml
                         (or any subset — callers may pre-filter by layer).
      now             — tz-aware UTC "now".
      last_run_lookup — callable(mission_id) -> datetime | None, the
                         mission's own last-fired timestamp (tz-aware UTC),
                         e.g. a thin wrapper around `_mission_last_fired`/
                         `read_last_run` for the caller's ctx. Kept as an
                         injected callable (not `ctx`) so this helper stays a
                         pure function of its inputs, independent of the
                         on-disk `.last-run` layout.

    Scope — ONLY `daily` and `time:`-bearing `weekly` cadences are considered.
    These are exactly the cadences starved by the layer-granular bug (a fixed
    time-of-day slot that can fall later than its layer's other missions).
    `hourly`/`every_Nh`/`every_Nm`/`event`/`cron:` missions have no single
    "later today" slot to arm an interim wake for — they are intentionally
    excluded, not a gap: an hourly mission is picked up again within the hour
    regardless of layer state, and event missions are trigger-gated.

    A mission whose slot has ALREADY PASSED today and has NOT fired is NOT
    "pending later today" — it is overdue NOW. That case belongs to the
    existing live-tick / `_due_scheduled_catchup_layer` path, not to interim-
    wake arming (arming a wake for a time already in the past would be a
    no-op). This helper only ever returns a time STRICTLY AFTER `now`.
    """
    now_paris = _to_paris(now)
    candidates: "list[datetime]" = []

    for m in missions:
        cadence = m.get("cadence")
        if cadence not in ("daily", "weekly"):
            continue
        t_str = m.get("time")
        if not t_str:
            continue
        try:
            target_t = _parse_hhmm(t_str)
        except (ValueError, AttributeError):
            continue

        if cadence == "weekly":
            raw_day = m.get("day", "")
            valid_days = _normalize_weekday_set(raw_day)
            if not valid_days:
                continue
            if now_paris.weekday() not in {_WEEKDAY_NAMES[d] for d in valid_days}:
                continue  # not today's weekday — no slot today at all

        # The candidate slot is TODAY at target_t (Paris-local).
        slot_paris = now_paris.replace(
            hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0
        )
        if slot_paris <= now_paris:
            continue  # already passed (or exactly now) — overdue, not "later today"

        mid = m.get("id", "")
        last_fired = last_run_lookup(mid) if mid else None
        if is_mission_due(m, now=slot_paris.astimezone(timezone.utc), last_fired=last_fired):
            candidates.append(slot_paris)

    if not candidates:
        return None
    return min(candidates)


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


# ---------------------------------------------------------------------------
# Per-item dispatched-id ledger for cadence:event missions (#282)
# ---------------------------------------------------------------------------
#
# WHY: an event mission's trigger is the inbox ITEM, not the clock. is_mission_due
# is always True for event, so the per-mission .last-run marker cannot encode
# "which trigger did we already act on" — it either re-fires every tick (R2 fail)
# or blocks a 2nd same-day item (R3 fail). The fix keys idempotence on the trigger
# item's id: each dispatched id gets a marker under
#   outputs/<today>/missions/<id>/dispatched-items/<trigger_id>
# An event mission is "due" only while at least one present trigger id is NOT yet
# ledgered. The ledger is written by the materializer at dispatch time (crash-safe:
# it does not depend on the subagent archiving the item) and only READ by
# select_due_missions (keeping the selector non-mutating). Daily-scoped under
# outputs/<today>/ like .last-run; trigger ids (gate filenames) are date-stamped
# and unique, so cross-midnight re-dispatch is not a practical concern.


def _event_trigger_dir(repo_dir: Path, mission: dict) -> Path:
    """The directory whose *.yaml files are an event mission's trigger items.

    Generic: the mission's `input_queue` if declared, else `inbox/decisions`
    (the L3 approved-gate queue that content's publish_execution consumes). Not
    hardcoded to any mission id.
    """
    iq = mission.get("input_queue")
    return Path(repo_dir) / (iq if iq else "inbox/decisions")


def _present_trigger_ids(repo_dir: Path, mission: dict) -> "set[str]":
    """Ids (filename stems) of the event mission's current trigger items."""
    d = _event_trigger_dir(repo_dir, mission)
    if not d.is_dir():
        return set()
    return {p.stem for p in d.glob("*.yaml") if p.is_file() and not p.name.startswith(".")}


def _dispatched_trigger_ids(today_dir: Path, mission_id: str, *, before: datetime) -> "set[str]":
    """Ids dispatched for this event mission on a PRIOR tick (marker ts < before).

    Each ledger marker stores the dispatch tick's ISO timestamp. Mirroring the
    `_mission_last_fired` same-tick rule: a marker stamped THIS tick (ts ==
    before) does NOT count as already-dispatched — otherwise the materializer,
    which records ids at the top of build_dispatch_ctx, would hide a brand-new
    item from select_due_missions in the very same tick (the item would never be
    dispatched at all). A prior-tick marker (ts < before) DOES block re-dispatch
    — that's what closes the per-tick re-fire loop.
    """
    led = Path(today_dir) / "missions" / mission_id / "dispatched-items"
    if not led.is_dir():
        return set()
    out: set[str] = set()
    for p in led.iterdir():
        if not p.is_file():
            continue
        body = p.read_text(encoding="utf-8").strip()
        if not body:
            # legacy/empty marker — treat as prior (blocking), safest default
            out.add(p.name)
            continue
        try:
            ts = _parse_iso(body)
        except ValueError:
            out.add(p.name)
            continue
        if ts < before:
            out.add(p.name)
    return out


def _event_pending_trigger_ids(repo_dir: Path, today_dir: Path, mission: dict,
                               *, now_utc: datetime) -> "set[str]":
    """Trigger ids present but NOT already-dispatched-on-a-prior-tick — the event
    mission is due iff this is non-empty. Pure read (selector-safe)."""
    mid = mission.get("id", "")
    if not mid:
        return set()
    return _present_trigger_ids(repo_dir, mission) - _dispatched_trigger_ids(
        today_dir, mid, before=now_utc
    )


def _record_dispatched_trigger_ids(today_dir: Path, mission_id: str, ids: "set[str]",
                                   *, now_utc: datetime) -> None:
    """Stamp trigger ids as dispatched THIS tick (marker body = now_utc ISO).

    CALLER CONTRACT: only ever called with `ids` that are genuinely pending (not
    yet in the ledger) — the materializer computes `pending = _event_pending_…`
    and only calls this when `pending` is non-empty. So an already-dispatched id
    is NEVER re-stamped here. That matters: re-stamping an existing id with the
    current now_utc would set `ts == this tick`, which the same-tick rule treats
    as NOT-blocking → the re-fire loop would reopen. The pending-guard upstream is
    what keeps this safe; do not call this with already-ledgered ids.

    Cross-UTC-midnight note: the ledger lives under outputs/<today>/, so a new UTC
    day starts a fresh ledger. An item left UN-archived past midnight would
    re-dispatch on day 2. This is acceptable because decision filenames (the
    trigger ids) are date-unique in practice; if that ever changes, key the ledger
    on a content hash or move it out of the daily dir."""
    led = Path(today_dir) / "missions" / mission_id / "dispatched-items"
    led.mkdir(parents=True, exist_ok=True)
    stamp = now_utc.isoformat()
    for tid in ids:
        # sanitize: ids are filename stems already, but guard against path parts
        safe = str(tid).replace("/", "_")
        (led / safe).write_text(stamp, encoding="utf-8")


def _mission_authors_own_marker(repo_dir: "Path | str", mission: dict) -> bool:
    """True if this mission has a DEDICATED prompt (`missions/<id>/PROMPT.md`)
    whose STEP 0 stamps the per-mission `.last-run` itself when it ACTUALLY runs.

    WHY this gate exists (the weekly-newsletter silent-failure bug, #428):
    The materializer used to stamp `outputs/<today>/missions/<id>/.last-run` as a
    SIDE EFFECT of the dispatch DECISION — before the mission's subagent had run.
    For a mission that hand-authors its real output in a subagent (every dedicated
    `missions/<id>/PROMPT.md` — e.g. `newsletter_redaction`, `linkedin_sage_batch`,
    the storytellers), the materializer produces NOTHING (gate cards are suppressed,
    see the queues/gates guard below), so that premature stamp was a lie: it made
    the idempotence guard believe the mission "already ran today / this week" → the
    real run was NEVER dispatched. Silent failure every newsletter day (Tue/Fri) and
    every sage day (Sun).

    The fix: for missions that author their OWN marker at STEP 0, the materializer
    must NOT pre-stamp `.last-run`. The marker is then written ONLY by the mission's
    real run — so a missed slot (e.g. Mac asleep at 18:03) stays un-marked and the
    mission remains DUE on the next tick (catch-up), instead of being silently
    skipped for the week.

    Missions WITHOUT a dedicated prompt (the legacy layer-shim primaries:
    `content_daily_rotation`→L1, `research_draft`→L2, `synthesizing_content_feedback`
    →L4) only stamp the LAYER marker at STEP 0, NOT the per-mission marker, and
    `_mission_last_fired` does NOT fall back to the layer marker. For those, the
    materializer MUST keep stamping the per-mission marker as the sole per-mission
    idempotence source — removing it there would re-introduce the #261/#277
    fire-spin. So the stamp is preserved for shim-resolved missions and removed only
    for dedicated-prompt ones.
    """
    mid = mission.get("id", "")
    if not mid:
        return False
    return (Path(repo_dir) / "missions" / mid / "PROMPT.md").exists()


def materialize_due_missions_for_tick(
    repo_dir: Path,
    today_dir: Path,
    now_utc: datetime,
    *,
    fire_after_rounds: int = 1,
) -> list[dict]:
    """Materialize due recurring missions into queue items (idempotent).

    Reads dept.yaml → recurring_missions, checks per-mission .last-run
    timestamps, and creates queue item YAML files for missions whose
    cadence says they are due right now.

    Idempotent via mission_id dedup: scans the output queue for existing
    items with the same mission_id (excluding .processed/ and dotfiles).
    If found, skips creation — same mission is never double-queued.

    Layer-4 missions are NOT materialized here (they fire via C.1).

    `fire_after_rounds` is threaded through to _highest_eligible_layer_from_signals
    so L1's cycle gate uses the same threshold as build_dispatch_ctx / decide_dispatch.
    Defaults to 1 (the system-wide default) and is passed from build_dispatch_ctx.

    Returns the list of items that were actually created (empty if none).
    """
    dept_yaml_path = repo_dir / "dept.yaml"
    if not dept_yaml_path.is_file():
        return []

    try:
        dept = yaml.safe_load(dept_yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    # #593: a top-level-LIST (or other non-dict) dept.yaml is truthy, so
    # `or {}` does not catch it — `.get()` would then raise AttributeError.
    # Treat it the same as the unparseable case above.
    if not isinstance(dept, dict):
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

    # Anti-fire-spin (issue #261 / #235-#237 class, extended to L4 in #277):
    # stamp the per-mission .last-run for EVERY due mission on layers 1-4,
    # BEFORE the creates[] materialization loop below. A "pure report" mission
    # (creates: [], e.g. ben's market_wrapup) is is_mission_due()=True but
    # yields NO queue-item descriptors, so it never enters the `for item in due`
    # loop and its marker would never be stamped → _mission_last_fired() returns
    # None forever → select_due_missions re-selects it on EVERY tick (fire-spin).
    # Stamping here closes the leak at the source, independent of whether the
    # dept's subagent remembers to stamp it. Idempotent: the loop below
    # re-stamps with the same now_utc value, which is a no-op overwrite. Only
    # missions already fired today (in last_fired_per_mission, same Paris day)
    # are skipped via is_mission_due returning False.
    #
    # L4 inclusion rationale (#277): L4 missions previously relied on the
    # once-per-day LAYER cap (`not l4_fired` in _mission_layer_eligible) as
    # their sole fire-spin guard. That cap blocked ALL L4 missions once any one
    # fired — preventing secondary L4 missions (market_wrapup 22:30,
    # weekly_review Fri) from ever running. The cap has been removed from
    # _mission_layer_eligible; per-mission idempotence now applies uniformly to
    # ALL layers. Note: queue-item creation (materialize_due_missions below) still
    # skips L4 — only the per-mission .last-run stamp is extended here.
    #
    # FIX #432 (DEFECT B) shared signal computation: compute the highest-priority
    # eligible layer ONCE for this tick, the same way the EVENT branch already
    # does (and the same way select_due_missions decides) — reused by BOTH the
    # event branch below AND the daily (non-event) pre-stamp branch, so the two
    # branches can never diverge on what "this tick's eligible layer" means.
    _mat_has_decisions = (
        _queue_has_items(repo_dir / "inbox" / "decisions")
        or _queue_has_items(repo_dir / "queues" / "inbox" / "decisions")
    )
    _mat_has_research = _queue_has_items(
        repo_dir / "queues" / "research",
        drainable_kinds=(_drainable_kinds_for_queue(repo_dir, "queues/research/") or None),
    )
    # Layer fired-today signals come from today_dir markers. Read round_counter
    # FIRST so _mat_layer_fired can mirror _layer_fired_today's fallback
    # (.last-run OR round_counter>0 OR FIX #432: any per-mission marker for a
    # layer-N mission today). Without the fallback the materializer's
    # l{1,2,3}_fired can diverge from select_due_missions' (via ctx
    # _layer_fired_today) in the state where round_counter[n]>0 but .last-run
    # is absent (external cleanup or a subagent that incremented the counter
    # without the marker), or where a layer ran entirely via its per-mission
    # path (#261, no layer-level marker written at all) — either divergence
    # breaks the highest-eligible-layer choice → silent #375/#432-class loss.
    _mat_counts = read_round_counter(today_dir)
    def _mat_layer_fired(n: int) -> bool:
        if read_last_run(today_dir / str(n)) is not None:
            return True
        if int(_mat_counts.get(str(n), 0)) > 0:
            return True
        return _any_mission_fired_today_for_layer(repo_dir, today_dir, n, now_utc=now_utc)
    _mat_l1_fired = _mat_layer_fired(1)
    _mat_l2_fired = _mat_layer_fired(2)
    _mat_l3_fired = _mat_layer_fired(3)
    # has_mgmt_notes: conservative read (same as build_dispatch_ctx).
    _mat_has_mgmt_notes = _scan_mgmt_notes(repo_dir, since=read_last_mgmt_scan(repo_dir))
    _mat_now_paris_t = _to_paris(now_utc).time()
    _mat_signals = dict(
        has_research=_mat_has_research,
        has_decisions=_mat_has_decisions,
        has_mgmt_notes=_mat_has_mgmt_notes,
        l1_fired=_mat_l1_fired,
        l2_fired=_mat_l2_fired,
        l3_fired=_mat_l3_fired,
        counts=_mat_counts,
        baseline=read_l1_baseline(today_dir),
        fire_after_rounds=fire_after_rounds,
    )
    # The highest-priority eligible layer this tick — the one select_due_missions
    # will actually dispatch. Both the event branch and the #432 daily pre-stamp
    # gate below stamp a mission's marker ONLY when its own layer IS this value.
    highest_eligible = _highest_eligible_layer_from_signals(
        _mat_now_paris_t, **_mat_signals
    )

    for m in missions:
        layer_n = int(m.get("layer", 0))
        if layer_n not in (1, 2, 3, 4):
            continue
        mid = m.get("id", "")
        if not mid:
            continue
        # EVENT cadence (#282): the trigger identity is the inbox item, not the
        # clock. is_mission_due(event) is always True, so the unconditional
        # re-stamp below would refresh the marker to now_utc every tick →
        # _mission_last_fired returns None every tick → re-spawn every tick while
        # an item sits unprocessed (the re-fire loop). Instead, gate on a per-ITEM
        # dispatched-id ledger: only "fire" (and record the ids) when there is a
        # trigger item NOT yet dispatched. Once every present id is ledgered, do
        # nothing — the loop closes structurally, independent of the subagent
        # archiving the item (crash-safe). A new (different-id) item re-arms it.
        #
        # FIX #375 v3 — HIGHEST-ELIGIBLE-LAYER invariant via _highest_eligible_layer_from_signals:
        # Only stamp the ledger when the mission's layer IS the highest-priority
        # eligible layer this tick — exactly the layer select_due_missions will
        # actually dispatch. This closes the invariant:
        #
        #     materializer stamps ledger ⟺ select_due_missions would dispatch
        #
        # Why "highest eligible" (not just "eligible in isolation"):
        #   _layer_eligible_from_signals(layer_n, ...) returns True for L3 whenever
        #   time>=07:00 AND has_decisions, but select_due_missions iterates
        #   _LAYER_PRIORITY [4,3,2,1] and picks the FIRST eligible layer. If L4 is
        #   also eligible (time>=19:00 + prerequisites), select picks L4 — L3 is
        #   NEVER dispatched this tick. Stamping L3's trigger here marks it as
        #   dispatched (ts < next tick's now_utc) → permanently blocked →
        #   SILENT DATA LOSS (bug #375 reintroduced in new form, found by adversarial
        #   review of a8ed933).
        #
        # Fix: compute the highest eligible layer via _highest_eligible_layer_from_signals
        # (which iterates _LAYER_PRIORITY exactly as select does), then stamp ONLY
        # if layer_n == that result. Structural divergence is impossible: both paths
        # call the same underlying predicate in the same priority order.
        #
        # fire_after_rounds: now threaded through from build_dispatch_ctx (was
        # hardcoded to 1). An L1 event mission with fire_after_rounds!=1 would have
        # diverged if the threshold ever changed. Threading it through ensures the
        # materializer and selector always agree on the L1 cycle gate.
        if m.get("cadence") == "event":
            # highest_eligible was computed ONCE above (shared with the daily
            # branch below) using the exact same signal extraction. Stamp only
            # when this mission's layer IS that layer.
            if highest_eligible == layer_n:
                pending = _event_pending_trigger_ids(repo_dir, today_dir, m, now_utc=now_utc)
                if pending:
                    _record_dispatched_trigger_ids(today_dir, mid, pending, now_utc=now_utc)
                    write_last_run(today_dir / "missions" / mid, when=now_utc)
            continue
        if is_mission_due(m, now=now_utc, last_fired=last_fired_per_mission.get(mid)):
            # #428 — do NOT pre-stamp at DECISION time for missions that author
            # their own per-mission marker at STEP 0 (dedicated prompt). Stamping
            # here before the subagent runs is the weekly silent-failure bug:
            # the idempotence guard would treat the mission as "already ran" and
            # the real run would never be dispatched. Shim-resolved missions (no
            # dedicated prompt) still get the stamp — it's their only per-mission
            # idempotence source (anti-fire-spin #261/#277).
            #
            # FIX #432 (DEFECT B) — ADDITIONAL gate, mirroring the EVENT branch's
            # #375-v3 fix: only stamp when this mission's layer IS the
            # highest-eligible layer this tick (i.e. it is actually being
            # dispatched now). Without this gate, a daily mission that is
            # is_mission_due=True but whose layer is NOT the highest-eligible
            # phase this tick gets stamped-as-done WITHOUT being dispatched —
            # silently killed for the rest of the day (LIVE evidence 2026-06-30:
            # synthesizing_content_feedback, L4 shim, stamped at 19:33 with zero
            # work because L1 — not L4 — was the highest-eligible layer that
            # tick). A mission whose layer IS eligible is being dispatched now,
            # so stamping it is the correct idempotence (anti-fire-spin
            # #261/#277) — it must not be re-materialized next tick.
            if not _mission_authors_own_marker(repo_dir, m) and layer_n == highest_eligible:
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
            # #593: a top-level-LIST (or other non-dict) YAML document is
            # truthy, so `or {}` does not catch it — `.get()` would then raise
            # AttributeError and kill the whole tick. Treat a non-dict document
            # the same as an unparseable one: skip and continue.
            if not isinstance(existing_data, dict):
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
            # #428: skip for dedicated-prompt missions — their subagent stamps the
            # per-mission marker at STEP 0 when it actually runs.
            if not _mission_authors_own_marker(repo_dir, {"id": mid}):
                write_last_run(today_dir / "missions" / mid, when=now_utc)
            continue

        # Guard (#302): never write a bare stub into queues/gates/ — gate cards
        # are always hand-authored by the agent layers (rich payload, no
        # created_by). A bare stub would create a phantom ghost decision card
        # whenever the upstream funnel is dry (confirmed across ben/maya/content).
        # Still stamp .last-run so the mission doesn't fire-spin on the next tick.
        if oq.rstrip("/") == "queues/gates":
            print(
                f"[materialize] suppressed bare gate stub for mission={mid!r}"
                f" (output_queue={oq!r} — gates are hand-authored by agents)"
            )
            # #428: gate-card missions (newsletter, sage, …) hand-author their gate
            # in a subagent and stamp the per-mission marker at STEP 0 themselves.
            # Stamping here at decision time is the silent-failure bug — only the
            # real run may write the marker. Shim-resolved gate missions (none on
            # queues/gates today) would still be stamped to avoid fire-spin.
            if not _mission_authors_own_marker(repo_dir, {"id": mid}):
                write_last_run(today_dir / "missions" / mid, when=now_utc)
            continue

        # Guard (#442, defense-in-depth GENERALIZATION of #302): never write a
        # bare stub for a DEDICATED-PROMPT mission, regardless of its output_queue.
        #
        # Ground truth on the board-#442 missions (verified against the LIVE content
        # dept.yaml, 2026-07-05): BOTH offending missions target `queues/gates`, so
        # the #302 guard ABOVE already suppresses their bare stub today:
        #   - research_draft      → output_queue=queues/gates, SHIM-resolved
        #                           (NO missions/research_draft/PROMPT.md) — covered
        #                           by the #302 `queues/gates` guard above.
        #   - linkedin_sage_batch → output_queue=queues/gates, DEDICATED-prompt
        #                           (has missions/linkedin_sage_batch/PROMPT.md) —
        #                           also covered by the #302 guard above.
        # The two board-#442 phantom cards (draft-research_draft-20260620,
        # draft-linkedin_sage_batch-20260621, both EMPTY, created_by=
        # materialize_due_missions) were written BEFORE #302 landed (2026-06-26);
        # they were archived to queues/gates/.processed/ and are now also excluded
        # from the cockpit view (console .processed exclusion). So the ROOT MECHANISM
        # for the board-#442 cards is already closed by #302 for both missions.
        #
        # This guard adds coverage for a case #302 does NOT reach: a dedicated-prompt
        # mission whose output_queue is something OTHER than queues/gates (e.g.
        # queues/drafts, queues/research). For any such mission the real card is
        # ALWAYS hand-authored by the subagent that runs its PROMPT.md; the bare
        # descriptor written below (id/mission_id/kind/created_at/created_by, no
        # payload) is only ever meant to be a placeholder the subagent overwrites.
        # When that run yields no content (dry funnel), nothing overwrites it and a
        # phantom empty card would be left behind. No LIVE mission is in that shape
        # today, so this is a preventative invariant, not a repro fix — kept because
        # it is cheap, harmless (it never fires for shim-resolved missions, whose
        # stub IS the legitimate hand-off), and mirrors the #302 pattern exactly.
        if _mission_authors_own_marker(repo_dir, {"id": mid}):
            print(
                f"[materialize] suppressed bare draft stub for mission={mid!r}"
                f" (dedicated-prompt mission — its subagent authors the real"
                f" card only when it has content; #442)"
            )
            # Dedicated-prompt missions stamp their OWN per-mission marker at
            # STEP 0 when they actually run (see #428) — do NOT stamp it here,
            # that would be the same premature-stamp silent-failure bug #428
            # fixed for the queues/gates case.
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
        # #428: skip for dedicated-prompt missions — their subagent stamps the
        # per-mission marker at STEP 0 when it actually runs (the created card is a
        # stub the subagent fills). Idempotence for the materialized stub still
        # holds via the mission_id dedup scan above.
        if not _mission_authors_own_marker(repo_dir, {"id": mid}):
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
    falling back to round_counter (incremented when a layer completes a round),
    falling back to FIX #432 (DEFECT A): any per-mission .last-run marker for
    a mission belonging to this layer stamped today (`layer_N_mission_fired_today`
    in ctx, populated by build_dispatch_ctx via _any_mission_fired_today_for_layer).
    This third fallback covers layers that ran ENTIRELY via the per-mission
    dispatch path (#261), which never writes the layer-level marker — without
    it, a layer that plainly ran is reported as not-fired all day, e.g. the L4
    debrief gate (`l1_fired AND ...`) never opens. ADDITIVE only: does not
    change behaviour when the layer-marker or round_counter paths already
    report True.
    """
    last = ctx.get(f"layer_{layer}_last_run_today")
    if last is not None:
        return True
    counts = ctx.get("round_counter") or {}
    if int(counts.get(str(layer), 0)) > 0:
        return True
    return bool(ctx.get(f"layer_{layer}_mission_fired_today", False))


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
        # #593: a top-level-LIST (or other non-dict) YAML document is truthy,
        # so `or {}` does not catch it. Treat it like the unparseable case
        # above — fail-open, count it — rather than let `.get()` raise.
        if not isinstance(data, dict):
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
    # #593: a top-level-LIST (or other non-dict) dept.yaml is truthy, so `or {}`
    # does not catch it — `.get()` below would then raise AttributeError. Treat
    # it the same as the absent/unreadable case above.
    if not isinstance(dept, dict):
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
    # #593: a top-level-LIST (or other non-dict) dept.yaml is truthy, so `or {}`
    # does not catch it — `.get()` below would then raise AttributeError. Treat
    # it the same as the absent/unreadable case above.
    if not isinstance(dept, dict):
        return set()
    target = queue_rel_path.rstrip("/")
    kinds: set[str] = set()
    for m in dept.get("recurring_missions") or []:
        oq = (m.get("output_queue") or "").rstrip("/")
        if oq == target:
            for k in m.get("creates", []) or []:
                kinds.add(k)
    return kinds


def _any_mission_fired_today_for_layer(
    repo_dir: "Path | str", today_dir: "Path | str", layer: int,
    *, now_utc: "datetime | None" = None,
) -> bool:
    """True if ANY recurring mission belonging to `layer` has a per-mission
    `.last-run` marker stamped today (`outputs/<today>/missions/<id>/.last-run`)
    from a PRIOR tick (strictly before `now_utc`, when given).

    FIX #432 (DEFECT A): after the per-mission dispatch migration (#261), a
    layer can run ENTIRELY via its per-mission path — e.g. content's
    `content_daily_rotation` (a shim-resolved L1 mission, no dedicated
    PROMPT.md) stamps `outputs/<today>/missions/content_daily_rotation/.last-run`
    when it runs, but the LAYER-level marker `outputs/<today>/1/.last-run` is
    only written by the loop runner / a layer's STEP 0 — which the per-mission
    path bypasses entirely. `dispatch_helpers.py` itself never writes a
    layer-level marker (confirmed: only `missions/<id>/.last-run` writes
    exist), so this is a FALLBACK signal, not a duplicate of an existing write.

    LIVE evidence (2026-06-30): `outputs/2026-06-30/1/.last-run` MISSING while
    `outputs/2026-06-30/missions/content_daily_rotation/.last-run` = 16:22:35.
    Without this fallback, `_layer_fired_today(ctx, 1)` reports False all day
    -> the L4 gate (`l1_fired AND ...`) never opens -> L4 silently never fires.

    Same-tick exclusion (mirrors `_mission_last_fired`'s tick-1-vs-tick-2
    rule): `materialize_due_missions_for_tick` runs at the TOP of
    `build_dispatch_ctx`, in the SAME tick as the dispatch decision. If it
    just stamped a layer-N mission's marker at `now_utc` THIS tick, that must
    NOT make `_layer_fired_today(ctx, N)` report True for THIS tick's own
    decision — the layer hasn't actually run yet, it's only ABOUT to be
    dispatched. Without this exclusion, a fresh L1 mission's same-tick stamp
    would make `_mission_layer_eligible(ctx, 1)` see "L1 already fired" on the
    very tick it became due, starving it permanently (self-cannibalizing
    same-tick loop). Only markers strictly BEFORE `now_utc` (a real prior
    tick) count as "fired today" for this fallback. When `now_utc` is not
    given, no exclusion is applied (any marker counts) — callers that build
    ctx via `build_dispatch_ctx` always pass `now_utc`.

    Returns False if dept.yaml is absent/unreadable (fail toward the
    pre-existing behaviour — no new fallback signal, layer-marker /
    round_counter paths still apply).
    """
    dept_yaml = Path(repo_dir) / "dept.yaml"
    if not dept_yaml.is_file():
        return False
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    # #593: a top-level-LIST (or other non-dict) dept.yaml is truthy, so `or {}`
    # does not catch it — `.get()` below would then raise AttributeError. Treat
    # it the same as the absent/unreadable case above.
    if not isinstance(dept, dict):
        return False
    today_dir = Path(today_dir)
    for m in dept.get("recurring_missions") or []:
        if int(m.get("layer", 0)) != layer:
            continue
        mid = m.get("id", "")
        if not mid:
            continue
        stamped = read_last_run(today_dir / "missions" / mid)
        if stamped is None:
            continue
        if now_utc is not None and stamped >= now_utc:
            continue
        return True
    return False


def build_dispatch_ctx(
    repo_dir: "Path | str" = ".",
    *,
    now_utc: "datetime | None" = None,
    fire_after_rounds: int = 1,
    materialize: bool = True,
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

    `materialize` (#454 FIX): when True (the default — used by the REAL
    dispatch decision, e.g. scaffold.py's documented
    `ctx = build_dispatch_ctx('.')` call in the live /loop), the usual
    `materialize_due_missions_for_tick()` side effect runs first, exactly as
    before (#428/#432 behaviour unchanged). Set `materialize=False` for any
    caller that only needs READ-ONLY ctx signals — e.g. a pre-flight
    eligibility GATE CHECK (loop-backup.sh's FORCE_LAYER "is this layer due
    yet" probe) that decides whether to wake the live session but does NOT
    itself dispatch. `build_dispatch_ctx` is a context BUILDER; only the
    mission's real run may write its `.last-run` marker (the #428/#375
    invariant) — a read-only gate check must not stamp it as a side effect.

    THE #454 BUG (found live on Ben, 2026-07-01/02): `loop-backup.sh`'s
    FORCE_LAYER gate calls `build_dispatch_ctx(...)` purely to read
    `_layer_fired_today` signals, ~9s BEFORE waking the live session via
    `inject_live_loop`. With `materialize` unconditionally True, that
    read-only probe pre-stamped `data_update`'s per-mission marker
    (`outputs/<today>/missions/data_update/.last-run`) as a side effect. The
    same-tick exclusion in `_mission_last_fired` /
    `_any_mission_fired_today_for_layer` only protects a marker from
    cannibalizing the SAME `build_dispatch_ctx` call that wrote it — it does
    NOT protect against a marker written by an EARLIER, separate call. So
    when the live session started 9s later and called `build_dispatch_ctx`
    for the real decision, the marker was already a prior-tick stamp →
    `layer_1_mission_fired_today`=True → `l1_fired`=True → `decide_dispatch`
    fell through to "heartbeat" — L1/data_update silently vetoed every tick.
    Ben was hand-archiving the marker to un-stick it. Fix: gate/probe call
    sites now pass `materialize=False`.
    """
    repo = Path(repo_dir)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    today_dir = repo / "outputs" / today

    # Materialize due recurring missions BEFORE scanning queues — newly
    # created items become visible to the queue scanners below and can
    # trigger the appropriate layer this same tick. Pass fire_after_rounds
    # so the materializer's event-ledger gate uses the same L1 cycle threshold
    # as decide_dispatch (was hardcoded to 1 in a8ed933; now threaded through).
    #
    # #454 FIX: this is the ONLY mutating step in build_dispatch_ctx (it
    # writes queue items + per-mission .last-run markers). Skip it entirely
    # when materialize=False (read-only gate/probe callers) so build_dispatch_ctx
    # never stamps a run-marker as a side effect of a call that isn't the real
    # dispatch decision — see the #454 docstring section above.
    if materialize:
        materialize_due_missions_for_tick(repo, today_dir, now_utc,
                                          fire_after_rounds=fire_after_rounds)

    return {
        "now_utc": now_utc,
        # Repo root, so consumer/event checks (_mission_input_ready, the #282
        # event dispatched-id ledger read in select_due_missions) can resolve
        # queue/ledger paths in PRODUCTION — not only when a test injects it.
        # Without this the selector fail-opens and the event re-fire-loop guard
        # never engages live (#282).
        "_repo_dir": str(repo),
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
        # FIX #432 (DEFECT A): fallback signal for _layer_fired_today — True
        # if ANY recurring mission belonging to layer N has a per-mission
        # .last-run marker stamped today, even when the LAYER-level marker
        # (outputs/<today>/N/.last-run) was never written (the per-mission
        # dispatch path bypasses it entirely). See
        # _any_mission_fired_today_for_layer's docstring for the live evidence.
        "layer_1_mission_fired_today": _any_mission_fired_today_for_layer(repo, today_dir, 1, now_utc=now_utc),
        "layer_2_mission_fired_today": _any_mission_fired_today_for_layer(repo, today_dir, 2, now_utc=now_utc),
        "layer_3_mission_fired_today": _any_mission_fired_today_for_layer(repo, today_dir, 3, now_utc=now_utc),
        "layer_4_mission_fired_today": _any_mission_fired_today_for_layer(repo, today_dir, 4, now_utc=now_utc),
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


def _layer_eligible_from_signals(
    layer: int,
    now_paris_t: "_time",
    *,
    has_research: bool,
    has_decisions: bool,
    has_mgmt_notes: bool,
    l1_fired: bool,
    l2_fired: bool,
    l3_fired: bool,
    counts: "dict[str, int]",
    baseline: "dict[str, int]",
    fire_after_rounds: int,
) -> bool:
    """Pure eligibility predicate — SINGLE SOURCE OF TRUTH for layer eligibility.

    Takes explicit boolean signals rather than a ctx dict so it can be called
    both by _mission_layer_eligible (which reads signals from ctx) AND by
    materialize_due_missions_for_tick (which computes signals itself from disk
    before the full ctx is assembled). This guarantees the invariant:

        materializer stamps the event ledger  ⟺  select_due_missions dispatches

    Without this shared predicate the materializer previously used a weaker gate
    (_time_reached(now_paris_t, layer_n)) that diverged from the real eligibility:
    for L3, select uses _time_reached(now_paris_t, 1) (07:00 floor) AND
    has_decisions, while the materializer gated on 16:00 — so between 07:00 and
    16:00 with has_decisions=True, select DISPATCHED but the materializer did NOT
    stamp → next tick re-dispatched → double-publish loop (bug #375).

    Layer semantics (mirrors decide_dispatch / _mission_layer_eligible exactly):
      L4: time>=19:00 AND l1_fired AND (l2_fired OR not has_research)
                       AND (l3_fired OR not has_decisions)
      L3: time>=07:00 AND has_decisions          ← 07:00, NOT 16:00
      L2: time>=12:00 AND has_research
      L1: time>=07:00 AND (not l1_fired         ← daily floor
                           OR has_mgmt_notes    ← C.mgmt
                           OR cycle gate met)   ← C.0b
    """
    if layer == 4:
        return (
            _time_reached(now_paris_t, 4)
            and l1_fired
            and (l2_fired or not has_research)
            and (l3_fired or not has_decisions)
        )
    if layer == 3:
        return _time_reached(now_paris_t, 1) and has_decisions
    if layer == 2:
        return _time_reached(now_paris_t, 2) and has_research
    if layer == 1:
        if not _time_reached(now_paris_t, 1):
            return False
        if has_mgmt_notes:
            return True
        if not l1_fired:
            return True
        return all(
            int(counts.get(str(lyr), 0)) - int(baseline.get(str(lyr), 0))
            >= fire_after_rounds
            for lyr in (2, 3, 4)
        )
    return False


def _highest_eligible_layer_from_signals(
    now_paris_t: "_time",
    *,
    has_research: bool,
    has_decisions: bool,
    has_mgmt_notes: bool,
    l1_fired: bool,
    l2_fired: bool,
    l3_fired: bool,
    counts: "dict[str, int]",
    baseline: "dict[str, int]",
    fire_after_rounds: int,
) -> "int | None":
    """Return the highest-priority eligible layer this tick, or None.

    Iterates _LAYER_PRIORITY [4, 3, 2, 1] and returns the first layer for
    which _layer_eligible_from_signals is True — EXACTLY mirroring the
    selection logic in select_due_missions:

        for layer in _LAYER_PRIORITY:
            if _mission_layer_eligible(ctx, layer):
                eligible_layer = layer; break

    SHARED HELPER: both the materializer (materialize_due_missions_for_tick)
    and select_due_missions use this function so they can never diverge on
    which layer is the "highest eligible" for a given tick.

    WHY this is needed (bug #375 v3): _layer_eligible_from_signals tells you
    whether a layer is eligible IN ISOLATION. But select dispatches only the
    SINGLE HIGHEST-PRIORITY eligible layer. If the materializer stamps an
    event trigger for a lower-priority layer that is eligible-in-isolation
    but NOT the highest eligible, select skips it — and the stamp means the
    trigger is permanently blocked on the next tick (ts < now). This helper
    enforces the invariant:

        materializer stamps event ledger ⟺ select_due_missions would dispatch
    """
    for layer in _LAYER_PRIORITY:
        if _layer_eligible_from_signals(
            layer,
            now_paris_t,
            has_research=has_research,
            has_decisions=has_decisions,
            has_mgmt_notes=has_mgmt_notes,
            l1_fired=l1_fired,
            l2_fired=l2_fired,
            l3_fired=l3_fired,
            counts=counts,
            baseline=baseline,
            fire_after_rounds=fire_after_rounds,
        ):
            return layer
    return None


def _mission_layer_eligible(ctx: "dict[str, Any]", layer: int) -> bool:
    """Return True if the given layer's time gate AND prerequisites are met.

    Delegates to _layer_eligible_from_signals (the single-source-of-truth
    predicate) after extracting the signals from ctx. See that function's
    docstring for the layer semantics.

    Mirrors the exact conditions decide_dispatch uses per branch, so the two
    functions always agree on which layer is eligible. Only the PRIMARY
    mission per phase was ever checked before; we now expose this as a
    reusable predicate.

    L4 note (fix #277): the old `and not l4_fired` once-per-day LAYER cap has
    been removed. decide_dispatch still keeps it (it returns ONE phase string
    and uses the cap as a once-daily gate for the primary debrief). Here, by
    contrast, we want to know whether the L4 WINDOW is open — i.e. whether L4
    time and prerequisites are met — so that EACH L4 mission can be evaluated
    individually by is_mission_due() + its own per-mission .last-run marker
    (stamped by materialize_due_missions_for_tick, which now covers L4 too).
    Removing the layer cap here is safe because:
      • per-mission idempotence: materialize_due_missions_for_tick stamps
        outputs/<today>/missions/<id>/.last-run for every due L4 mission
        (same same-tick semantics as L1-3). is_mission_due then vetoes
        re-selection on the next tick for that specific mission.
      • risk_control (primary L4 / legacy layer-shim) gets its own per-mission
        marker stamped by the materializer, so it stays once-daily just like
        any other daily mission — without relying on the layer-wide cap.
      • decide_dispatch is UNCHANGED — it still returns 'layer_4' only when
        not l4_fired, so its phase-string semantics are unaffected. The
        invariant ∀ m ∈ select_due_missions: m['layer'] == phase_layer(decide)
        holds on the first L4 tick (when l4_fired is False); on subsequent L4
        ticks select_due_missions returns only missions whose own marker says
        they haven't fired yet, which may be a non-empty subset — this is fine
        because select_due_missions is ADDITIVE to decide_dispatch, not a
        replacement for it.
    """
    now_paris_t = _to_paris(ctx["now_utc"]).time()
    counts = ctx.get("round_counter") or {}
    baseline = ctx.get("layer_1_baseline_counter") or {}
    fire_after_rounds = int(ctx.get("fire_after_rounds", 1))
    return _layer_eligible_from_signals(
        layer,
        now_paris_t,
        has_research=bool(ctx.get("has_research_items", False)),
        has_decisions=bool(ctx.get("has_inbox_decisions", False)),
        has_mgmt_notes=bool(ctx.get("has_unconsumed_mgmt_notes", False)),
        l1_fired=_layer_fired_today(ctx, 1),
        l2_fired=_layer_fired_today(ctx, 2),
        l3_fired=_layer_fired_today(ctx, 3),
        counts=counts,
        baseline=baseline,
        fire_after_rounds=fire_after_rounds,
    )


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

    Does NOT fall back to the layer-level marker. The layer marker tracks whether
    a LAYER ran (or more precisely, whether any one mission in that layer has
    stamped the shared layer .last-run); the per-mission marker tracks whether
    THIS MISSION ran. For L4: materialize_due_missions_for_tick now stamps the
    per-mission marker for every due L4 mission (fix #277), so all L4 missions
    have their own per-mission marker and do not need a layer-marker fallback.
    A mission with no per-mission marker → last_fired=None → is_mission_due
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


def _mission_last_fired_with_shim_fallback(
    repo_dir: "Path | str",
    ctx: "dict[str, Any]",
    mission: dict,
) -> "datetime | None":
    """`_mission_last_fired`, PLUS a fallback to the layer-level marker for a
    mission that resolves to the legacy `layers/<N>/PROMPT.md` shim (card #518
    follow-up).

    WHY this exists (the gap `_mission_last_fired` documents but does not
    close): `_mission_last_fired`'s docstring claims "For L4:
    materialize_due_missions_for_tick now stamps the per-mission marker for
    every due L4 mission (fix #277), so all L4 missions have their own
    per-mission marker and do not need a layer-marker fallback." That
    guarantee depends on `materialize_due_missions_for_tick` having actually
    RUN this tick (`build_dispatch_ctx(..., materialize=True)`, the live-loop
    default). The LAYER-FLOOR path's read-only enumeration
    (`select_due_missions_for_forced_layer`) deliberately calls
    `build_dispatch_ctx(..., materialize=False)` (the #454 discipline — an
    enumeration/gate probe must never stamp a marker as a side effect), so
    the materializer never runs there and the guarantee does not hold.

    Concretely: a mission with NO dedicated `missions/<id>/PROMPT.md` (e.g.
    Ben's `risk_control`, which resolves to the legacy `layers/4/PROMPT.md`
    shim via `resolve_mission_prompt`) fires through that shim, whose own
    STEP 1 stamps ONLY the shared layer marker
    (`outputs/<today>/<N>/.last-run`) — it has no per-mission awareness and
    never writes `outputs/<today>/missions/<id>/.last-run`. Without this
    fallback, a floor tick that runs AFTER the live loop already ran that
    mission via the shim would see `_mission_last_fired` return None (no
    per-mission marker) and wrongly re-select an already-fired mission.

    Fallback rule (mirrors `resolve_mission_prompt`'s OWN legacy-shim test,
    so the two functions can never diverge on "is this mission on the
    shim"): if `resolve_mission_prompt(repo_dir, mission)` resolves to the
    layer shim path (i.e. no dedicated per-mission prompt file exists) AND
    the per-mission marker is absent, fall back to the layer-level marker
    (via `_layer_fired_today_marker`, reading the same ctx key
    `_layer_fired_today` uses — no same-tick exclusion needed here, since
    nothing on the floor path stamps the layer marker mid-tick).

    Multiple shim missions on the SAME layer (Ben's actual dept.yaml shape
    TODAY: neither `risk_control` nor `weekly_review` has a dedicated
    `missions/<id>/PROMPT.md`, so BOTH resolve to the shim): the shared layer
    marker alone cannot say WHICH shim mission fired — a naive "any shim
    mission on this layer, marker present -> fired" would wrongly exclude a
    still-pending later mission too (this was caught by the M-section test
    below: a first draft of this fallback returned [] instead of
    [market_wrapup] because BOTH risk_control and market_wrapup resolved to
    the same shim and the layer marker satisfied both). The fix: gate the
    fallback on the marker's OWN timestamp vs. THIS mission's scheduled
    `time:` (Paris-local, daily/weekly cadence only) — the layer marker can
    only plausibly represent THIS mission having fired if the marker was
    stamped AT OR AFTER this mission's slot opened today. A marker stamped
    at 21:00 cannot be "for" a mission whose slot is 22:30 (that slot hadn't
    opened yet), so the fallback correctly does NOT apply to market_wrapup
    even though it does apply to risk_control (21:00 marker >= risk_control's
    own 21:00 slot). Two same-layer shim missions with the SAME `time:` are
    still ambiguous (a structural limit of the shim — a generic prompt has no
    mission identity to stamp); a dept that needs to disambiguate that case
    should give each mission its own `missions/<id>/PROMPT.md` instead.

    A per-mission marker (even a stale one) always wins over the layer
    fallback — this function only consults the layer marker when the
    per-mission marker is genuinely absent.
    """
    per_mission = _mission_last_fired(ctx, mission)
    if per_mission is not None:
        return per_mission

    prompt_path = resolve_mission_prompt(repo_dir, mission)
    layer = int(mission.get("layer", 0))
    is_shim = prompt_path == Path(repo_dir) / "layers" / str(layer) / "PROMPT.md"
    if not is_shim:
        return None

    layer_marker = _layer_fired_today_marker(ctx, layer)
    if layer_marker is None:
        return None

    # Disambiguation: the layer marker can only represent THIS mission if its
    # own scheduled slot (daily/weekly `time:`, Paris-local) had already
    # opened by the time the marker was stamped. hourly/every_Nh/every_Nm/
    # event cadences have no fixed daily slot to compare against, so for
    # those we conservatively apply the fallback unconditionally (matches
    # is_mission_due's own "date-only" daily check — see below).
    t_str = mission.get("time")
    cadence = mission.get("cadence", "")
    if cadence in ("daily", "weekly") and t_str:
        try:
            target = _parse_hhmm(t_str)
        except Exception:
            return layer_marker
        marker_paris_t = _to_paris(layer_marker).time()
        if marker_paris_t < target:
            # The marker was stamped BEFORE this mission's slot opened today
            # — it cannot be this mission's own fire, so no fallback signal.
            return None

    return layer_marker


def _layer_fired_today_marker(ctx: "dict[str, Any]", layer: int) -> "datetime | None":
    """The layer-level `.last-run` timestamp, returned as a value (not a bool)
    so `_mission_last_fired_with_shim_fallback` can feed it straight into
    `is_mission_due`'s `last_fired` parameter.

    Reads the SAME ctx key `_layer_fired_today` uses
    (`layer_{layer}_last_run_today`, populated by `build_dispatch_ctx` from
    `outputs/<today>/<N>/.last-run`) — no same-tick exclusion, because unlike
    the per-mission materializer, nothing on the floor path stamps this
    marker mid-tick as a side effect; it only ever reflects a PRIOR run
    (the mission's own legacy-shim STEP 1, on an earlier tick).
    """
    return ctx.get(f"layer_{layer}_last_run_today")


def _due_scheduled_catchup_layer(
    ctx: "dict[str, Any]",
    missions: "list[dict]",
) -> "int | None":
    """Catch-up safeguard (#428, Fix 2) — anti "Mac asleep at the scheduled slot".

    Returns the highest-priority layer that has a SCHEDULED producer mission whose
    slot has already passed today and which has NOT actually run today (no real
    per-mission STEP 0 marker), or None.

    WHY: a producer mission like `newsletter_redaction` (weekly Tue/Fri 18:03) or
    `linkedin_sage_batch` (weekly Sun 18:30) only gets dispatched when its LAYER is
    the highest-priority eligible layer — and L2's eligibility is gated by
    `has_research`. If the Mac was asleep at 18:03 and the research queue happens to
    be empty when it wakes, the layer is not eligible and the scheduled slot is
    silently lost for the WEEK. This safeguard lets a missed scheduled slot wake its
    OWN layer so the mission becomes DUE on the next tick (catch-up).

    Deliberately narrow / safe:
      • ONLY producer missions (no `input_queue`) — consumers are correctly gated
        by their input queue being non-empty.
      • ONLY dedicated-prompt missions (`missions/<id>/PROMPT.md`) — these author
        their own per-mission marker at STEP 0, so the marker is an honest "ran"
        signal and the catch-up self-terminates once the real run stamps it. (This
        also means it never triggers for the legacy layer-shim primaries, whose
        behaviour is left exactly as before.)
      • ONLY daily/weekly cadences with an explicit `time:` — `is_mission_due`
        enforces "never before the scheduled time" and "once per day/week", so this
        cannot fire early or double-fire.

    Used by select_due_missions ONLY as a fallback when no layer is eligible the
    normal way (an otherwise-heartbeat tick), so it can never out-rank or steal a
    tick from a higher-priority layer that has real work.
    """
    now_utc: datetime = ctx.get("now_utc")
    if now_utc is None or now_utc.tzinfo is None:
        return None
    repo_dir = ctx.get("_repo_dir")
    if not repo_dir:
        return None
    for layer in _LAYER_PRIORITY:
        for m in missions:
            if int(m.get("layer", 0)) != layer:
                continue
            if m.get("input_queue"):
                continue  # consumer — gated by its input queue, not catch-up
            if m.get("cadence") not in ("daily", "weekly"):
                continue
            if not m.get("time"):
                continue
            if not _mission_authors_own_marker(repo_dir, m):
                continue
            last_fired = _mission_last_fired(ctx, m)
            if is_mission_due(m, now=now_utc, last_fired=last_fired):
                return layer
    return None


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
         last-run timestamp (stamped by materialize_due_missions_for_tick for
         all layers 1-4, so every mission has its own idempotence marker).
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

    # Determine the highest-priority eligible layer.
    # Uses _highest_eligible_layer_from_signals (the shared helper also used
    # by the materializer) to guarantee structural identity: both paths iterate
    # _LAYER_PRIORITY and call _layer_eligible_from_signals in the same order.
    now_paris_t = _to_paris(now_utc).time()
    counts = ctx.get("round_counter") or {}
    baseline = ctx.get("layer_1_baseline_counter") or {}
    fire_after_rounds = int(ctx.get("fire_after_rounds", 1))
    eligible_layer = _highest_eligible_layer_from_signals(
        now_paris_t,
        has_research=bool(ctx.get("has_research_items", False)),
        has_decisions=bool(ctx.get("has_inbox_decisions", False)),
        has_mgmt_notes=bool(ctx.get("has_unconsumed_mgmt_notes", False)),
        l1_fired=_layer_fired_today(ctx, 1),
        l2_fired=_layer_fired_today(ctx, 2),
        l3_fired=_layer_fired_today(ctx, 3),
        counts=counts,
        baseline=baseline,
        fire_after_rounds=fire_after_rounds,
    )

    if eligible_layer is None:
        # Catch-up safeguard (#428, Fix 2): no layer is eligible the normal way
        # (heartbeat tick), but a scheduled producer mission may have a slot that
        # passed today and never ran (e.g. Mac asleep at 18:03). Let it wake its own
        # layer so it is not silently skipped for the week. Only a fallback — it can
        # never out-rank a higher-priority layer that has real work this tick.
        eligible_layer = _due_scheduled_catchup_layer(ctx, missions)
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
        # EVENT cadence (#282): due ONLY when there is a trigger item whose id is
        # not yet in the per-item dispatched ledger. This closes the per-tick
        # re-fire loop (a still-pending-but-already-dispatched item does not
        # re-select) while still firing for a NEW item (different id). Reads only;
        # the materializer writes the ledger. repo_dir comes from ctx['_repo_dir']
        # (build_dispatch_ctx injects it); if absent we fail-open to preserve the
        # old behaviour rather than silently starve.
        if m.get("cadence") == "event":
            repo_dir = ctx.get("_repo_dir")
            today_dir = Path(ctx["today_dir"]) if ctx.get("today_dir") else None
            if repo_dir and today_dir is not None:
                if not _event_pending_trigger_ids(Path(repo_dir), today_dir, m, now_utc=now_utc):
                    continue
        due.append(m)

    # Sort by mission id for determinism (layer is uniform across the result).
    due.sort(key=lambda m: m.get("id", ""))
    return due


def select_due_missions_for_forced_layer(
    repo_dir: "Path | str",
    layer: int,
    *,
    now_utc: "datetime | None" = None,
) -> "list[dict]":
    """Floor-tick counterpart to `select_due_missions` (card #518).

    `select_due_missions(ctx, missions)` answers "what's due on the layer
    `decide_dispatch` picked" — it will never enumerate Layer 4 while the
    highest-priority eligible layer is something else. The LAYER-FLOOR path
    (`loop-backup.sh --layer N`, one static cron per layer) instead FORCES a
    specific layer regardless of dispatch priority: at the L4 floor tick, L4
    is due by definition (the cron only fires at/after its own min-time), so
    the floor needs "which of THIS layer's missions are still pending today",
    not "is this layer the dispatcher's current pick".

    Before this function, the floor path did not call into per-mission
    selection AT ALL: `loop-backup.sh` handed a fresh Claude session a
    generic "read layers/<N>/PROMPT.md, run Layer N" prompt (see
    `build_tick_prompt`), and the legacy monolithic layer prompts (e.g.
    `agents/ben/layers/4/PROMPT.md`) gate on a single LAYER-level
    `outputs/<today>/<N>/.last-run` marker — "once per day, no parallelism".
    That collapses distinct same-layer missions (e.g. a 21:00 risk debrief
    and a 22:30 market wrap-up, both L4) into one: once ANY L4 work fires,
    the layer marker is written and every other pending L4 mission is
    invisible to a later floor tick the same day. This function is the fix:
    it enumerates dept.yaml's `recurring_missions` on the forced layer and
    keeps only the ones `is_mission_due()` says are still pending, using
    each mission's OWN per-mission `outputs/<today>/missions/<id>/.last-run`
    marker — exactly the idempotence model `select_due_missions` already
    uses on the live-loop path (see its docstring; this is additive, not a
    parallel model).

    Depts with no `recurring_missions` in dept.yaml (or none on this layer)
    return [] — the caller falls back to the legacy generic "run Layer N"
    tick, so a dept that hasn't migrated to the mission-centric model is
    completely unaffected (zero-regression, mirrors `resolve_mission_prompt`'s
    per-mission-first/legacy-shim-fallback contract).

    Read-only: builds its own ctx with `materialize=False` (same #454
    discipline as the loop-backup.sh eligibility probe — a gate/enumeration
    call must never stamp a `.last-run` marker as a side effect; only the
    mission's real run may do that).
    """
    repo = Path(repo_dir)
    dept_yaml = repo / "dept.yaml"
    if not dept_yaml.exists():
        return []
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    missions = dept.get("recurring_missions") or []
    layer_missions = [m for m in missions if int(m.get("layer", 0)) == int(layer)]
    if not layer_missions:
        return []

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    ctx = build_dispatch_ctx(repo, now_utc=now_utc, materialize=False)

    due: list[dict] = []
    for m in layer_missions:
        # Shim-fallback idempotence (card #518 follow-up): unlike the
        # live-loop path (build_dispatch_ctx(materialize=True), which
        # guarantees every due L4 mission gets its own per-mission marker
        # via materialize_due_missions_for_tick — see _mission_last_fired's
        # docstring), this floor enumeration runs with materialize=False (the
        # #454 read-only-probe discipline), so that guarantee does NOT hold
        # here. A mission with no dedicated missions/<id>/PROMPT.md (e.g.
        # Ben's risk_control) fires via the legacy layers/<N>/PROMPT.md shim,
        # whose STEP 1 stamps ONLY the layer marker — never the per-mission
        # one. Plain _mission_last_fired would then report "never fired" for
        # an already-fired shim mission, re-selecting it on a later floor
        # tick. _mission_last_fired_with_shim_fallback closes that gap.
        last_fired = _mission_last_fired_with_shim_fallback(repo, ctx, m)
        if not is_mission_due(m, now=now_utc, last_fired=last_fired):
            continue
        if not _mission_input_ready(ctx, m):
            continue
        if m.get("cadence") == "event":
            today_dir = Path(ctx["today_dir"]) if ctx.get("today_dir") else None
            if today_dir is not None:
                if not _event_pending_trigger_ids(repo, today_dir, m, now_utc=now_utc):
                    continue
        due.append(m)

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
            commit_dt = _parse_iso(last_commit_iso)
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
                _parse_iso(body.strip())
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


# ─── Sandbox-aware git helpers (#453, 2026-07-02) ──────────────────────────
#
# WHY: the agent OS-sandbox (bwrap fs-jail, see
# deploy/templates/managed-settings.sandbox.json) can leave specific tracked
# paths unreadable/unwritable to the sandboxed subprocess even though the OS
# user owns them fine outside the jail — `.gitmodules` (and any path under a
# submodule dir) is the recurring offender because submodule content sits
# outside the sandbox's narrow `allowWrite` allowlist. `sudo -n` is a second,
# independent failure axis: no controlling tty / no cached credential inside
# the sandbox → `sudo: a password is required`, rc!=0, no `password=` line.
#
# Both used to be treated as fatal by force_commit_and_push/safe_pull: one
# unreadable path in a batched `git add`/`git stash push` poisons the WHOLE
# operation (git aborts the entire index update, not just that path), and an
# un-degraded sudo call surfaces a cryptic mint failure. Depts worked around
# this by pushing manually, unsandboxed, every tick — defeating the point of
# the sandboxed sync. These helpers detect the condition and let callers
# degrade gracefully (skip ONLY the unreachable path/substep, WARN loudly,
# keep going) instead of aborting the tick.
#
# Never removes the never-lose-work guarantee. Restore-from-index
# (`git checkout -- <path>`) is DESTRUCTIVE — it discards whatever is on
# disk in favor of the committed version — so it is only ever safe for a
# path we can positively identify as sandbox-owned jail-fs content, never
# for "any tracked path that happens to be unreadable right now" (an
# earlier, unsandboxed step — e.g. a human/root drop-in between ticks —
# could have legitimately modified an ordinary tracked file that then
# becomes unreadable to *this* sandboxed subprocess specifically; restoring
# it would silently destroy that real edit). Restore is therefore SCOPED to
# a known-safe allowlist (`.gitmodules` + submodule directory paths — see
# `_is_restore_allowlisted`). Any OTHER unreadable-but-modified tracked path
# is never restored: it is EXCLUDED from the add/stash batch instead (so the
# batch still succeeds) and a WARN is emitted — excluding preserves the
# file untouched, restoring can destroy it, and the prime directive is
# never-lose-work. Untracked/new files are never touched by either path,
# and a stash is never silently dropped.

def _is_sudo_available() -> bool:
    """True if `sudo -n true` succeeds (a cached credential / no-password
    NOPASSWD rule is usable right now) — False under a sandbox with no tty
    and no cached credential. Cheap, side-effect-free probe."""
    import subprocess
    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, text=True, timeout=5,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _find_unreadable_tracked_paths(repo_dir, status_stdout: str) -> "list[str]":
    """From `git status --porcelain` output, return tracked paths that are
    modified-per-git but actually unreadable on disk right now (the sandbox
    fs-jail signature: git sees a stat/content mismatch it can't explain
    because it can't open() the file). Untracked ('??') entries are never
    returned here — those are real new content, not a jail artifact."""
    import os
    unreadable = []
    for line in status_stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        if "?" in code:
            continue  # untracked — not a "tracked file the jail hid" case
        path = line[3:].strip().strip('"')
        path = path.split(" -> ")[-1] if " -> " in path else path
        full = Path(repo_dir) / path
        if full.exists() and not os.access(full, os.R_OK):
            unreadable.append(path)
    return unreadable


def _submodule_paths(repo_dir) -> "set[str]":
    """Return the set of submodule directory paths for repo_dir, derived
    from `.gitmodules` (preferred — pure text parse, no subprocess needed)
    or `git submodule status` if `.gitmodules` itself is unreadable. Best
    effort: returns an empty set (never raises) if neither source is
    available, so callers fall back to the static `.gitmodules`-only
    allowlist rather than crashing."""
    import configparser
    import os
    import subprocess

    repo_dir = Path(repo_dir)
    gitmodules = repo_dir / ".gitmodules"
    if gitmodules.exists():
        try:
            if os.access(gitmodules, os.R_OK):
                parser = configparser.ConfigParser()
                # git's .gitmodules uses `submodule "name"` sections, which
                # configparser can read as-is (it's git-config-format, a
                # superset configparser tolerates for our purposes: we only
                # need the `path = ...` values).
                parser.read(str(gitmodules))
                paths = set()
                for section in parser.sections():
                    if parser.has_option(section, "path"):
                        paths.add(parser.get(section, "path").strip())
                if paths:
                    return paths
        except Exception:
            pass  # fall through to `git submodule status`

    # `.gitmodules` unreadable or unparsable — ask git directly (works even
    # when the file itself is jailed, since this reads git's own index/config
    # state rather than the working-tree file).
    try:
        status = subprocess.run(
            ["git", "-C", str(repo_dir), "submodule", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if status.returncode == 0:
            paths = set()
            for line in status.stdout.splitlines():
                # Format: " <sha> <path> (<describe>)" (leading space/+/- flag).
                parts = line.strip().split()
                if len(parts) >= 2:
                    paths.add(parts[1])
            if paths:
                return paths
    except Exception:
        pass
    return set()


def _is_restore_allowlisted(repo_dir, path: str) -> bool:
    """True if `path` is safe to restore-from-index: `.gitmodules` itself,
    or any path under a submodule directory. This is the ONLY class of
    tracked path this module will ever `git checkout --` on the sandbox's
    behalf — see the module docstring for why the allowlist exists (an
    unreadable path outside it might be a real, unsandboxed edit, and
    restoring it would silently discard that work)."""
    if path == ".gitmodules":
        return True
    for sm in _submodule_paths(repo_dir):
        sm = sm.rstrip("/")
        if path == sm or path.startswith(sm + "/"):
            return True
    return False


def _restore_unreadable_tracked_paths(repo_dir, paths: "list[str]") -> "tuple[list[str], list[str]]":
    """`git checkout -- <path>` each unreadable tracked path that is on the
    known-safe allowlist (`.gitmodules` + submodule dirs — see
    `_is_restore_allowlisted`): this restores the committed content from the
    index, discarding the working-tree copy the sandbox can't read. Safe
    ONLY for allowlisted paths, because those are sandbox-jailed submodule
    plumbing an agent cannot have edited in-sandbox — the "modification" git
    reports is a jail fs-visibility artifact, not lost work.

    Any unreadable path NOT on the allowlist is never touched here — it is
    returned in `excluded` instead, so the caller can drop it from the
    add/stash batch (never restore, never discard real work).

    Returns (restored, excluded): `restored` is the allowlisted subset
    actually restored (rc==0); `excluded` is every path that was not
    restored (either not allowlisted, or the checkout itself failed —
    directory-level sandbox denial can make `git checkout --` fail with
    rc!=0 even for an allowlisted path, e.g. an unwritable parent dir; that
    path is safely excluded from the batch too, never left half-restored)."""
    import subprocess
    restored = []
    excluded = []
    for p in paths:
        if not _is_restore_allowlisted(repo_dir, p):
            excluded.append(p)
            continue
        co = subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", "--", p],
            capture_output=True, text=True,
        )
        if co.returncode == 0:
            restored.append(p)
        else:
            # Allowlisted but checkout itself failed (e.g. the containing
            # directory — not just the file — is unwritable under the
            # sandbox: unlink/recreate needs directory write, not just file
            # read/write). Exclude from the batch rather than crash or
            # silently drop; the path is left exactly as-is on disk.
            excluded.append(p)
    return restored, excluded


def _resolve_push_branch(repo_dir: "Path | str") -> str:
    """Which branch does the runtime push land on?

    host:vps (default): always "main" — the doctrine/guard push target,
    unchanged (card #620 forbids touching this).

    host:local (BUBBLE_HOST=local, set by deploy/local/lib/local_loop_lib.sh
    on the dept's loop wrapper): a host:local dept works on a feature branch,
    not main, so pushing "main" either fails (branch doesn't exist locally)
    or silently pushes the wrong ref. Push the CURRENTLY CHECKED-OUT branch
    instead — that's what the dept actually has commits on.
    """
    import os
    import subprocess

    if os.environ.get("BUBBLE_HOST") != "local":
        return "main"
    cur = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    branch = cur.stdout.strip()
    # Fall back to "main" if HEAD is detached or the lookup failed — never
    # push an empty/garbage ref.
    if cur.returncode != 0 or not branch or branch == "HEAD":
        return "main"
    return branch


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
        push_branch = _resolve_push_branch(repo_dir)
        push = subprocess.run(
            ["git", "-C", str(repo_dir), "push", "origin", push_branch],
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

    # 2a. Sandbox-aware: a single unreadable TRACKED path (e.g. `.gitmodules`
    # hidden by the bwrap fs-jail — #453) poisons the WHOLE `git add` batch
    # (git aborts the entire operation, not just that path). ALWAYS exclude
    # such paths from the batch (never stage a path we can't even read) so
    # the rest of the batch still succeeds. Only the ALLOWLISTED subset
    # (`.gitmodules` + submodule dirs — see `_is_restore_allowlisted`) is
    # ALSO restored from the index, because only that subset is provably
    # sandbox-jail plumbing rather than a possible real edit from an
    # earlier, unsandboxed step. Everything else unreadable is excluded but
    # left exactly as-is on disk (never restored, never lost). WARN either
    # way; never abort.
    unreadable = _find_unreadable_tracked_paths(repo_dir, status.stdout)
    addable_paths = [p for p in runtime_paths if p not in unreadable]
    skipped_unreadable = [p for p in runtime_paths if p in unreadable]
    if skipped_unreadable:
        restored, excluded_only = _restore_unreadable_tracked_paths(repo_dir, skipped_unreadable)
        print(
            "[force_commit_and_push] WARN sandbox-unreadable tracked path(s) "
            "excluded from runtime add (not staged): "
            + ", ".join(sorted(set(skipped_unreadable)))
            + (f" (restored from index — allowlisted jail-fs artifact: {', '.join(sorted(set(restored)))})" if restored else "")
            + (f" (left untouched on disk — not allowlisted, may be real unsandboxed work, NOT restored: {', '.join(sorted(set(excluded_only)))})" if excluded_only else "")
        )
    if not addable_paths:
        # Everything pending was structural/vendored/unreadable-jail-artifact.
        return True, None
    add = subprocess.run(
        ["git", "-C", str(repo_dir), "add", "--"] + addable_paths,
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
        #
        # Sandbox-aware (#453): probe sudo availability FIRST — under the
        # bwrap jail (no tty, no cached credential) `sudo -n ...` fails with
        # "a password is required" and no `password=` line, which used to
        # surface as an opaque "failed to mint GitHub App token" error. Fail
        # fast with an unambiguous WARN instead; caller (safe_pull) already
        # treats this branch's failure as non-fatal (a note, not an abort).
        if not _is_sudo_available():
            return False, (
                "sudo unavailable in this environment (no cached credential/"
                "tty — expected under the agent sandbox); skipped GitHub App "
                f"token mint for {repo_name} (WARN, non-fatal — caller "
                "continues without the runtime push)"
            )
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
             _resolve_push_branch(repo_dir)],
            capture_output=True, text=True,
        )
    else:
        # Last resort: bare push (relies on a credential helper in git config).
        # This is the branch host:local depts actually take — they push via
        # the Mac's own ambient gh/git credential (no broker, no sudo helper;
        # deploy/local/lib/local_loop_lib.sh: "NO SOPS / NO token-broker").
        push = subprocess.run(
            ["git", "-C", str(repo_dir), "push", "origin", _resolve_push_branch(repo_dir)],
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

    # 1a. Sandbox-aware (#453): handle any TRACKED path the sandbox fs-jail
    # hides from read (`.gitmodules` is the recurring offender — submodule
    # content sits outside the sandbox's narrow allowWrite allowlist). git
    # CANNOT stash a path it cannot open() — `git stash push` aborts the
    # ENTIRE stash (not just that path) the moment it hits one, which is what
    # made every tick abort for Ben/Tony/Accountant.
    #
    # Only the ALLOWLISTED subset (`.gitmodules` + submodule dirs — see
    # `_is_restore_allowlisted`) is restored from the index here: that
    # subset is provably sandbox-jail plumbing an agent cannot have edited
    # in-sandbox, so the "modification" git reports is a jail-fs artifact,
    # never real work. Any OTHER unreadable path is left untouched on disk
    # (never restored — it could be a real edit from an earlier, unsandboxed
    # step) and instead excluded from step 2's stash pathspec below, so the
    # stash still succeeds without ever discarding it.
    unreadable_excluded: "list[str]" = []
    status = _git("status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        unreadable = _find_unreadable_tracked_paths(repo_dir, status.stdout)
        if unreadable:
            restored, excluded_only = _restore_unreadable_tracked_paths(repo_dir, unreadable)
            if restored:
                notes.append(
                    "WARN sandbox-unreadable tracked path(s) restored from "
                    "index (allowlisted jail-fs artifact — submodule/"
                    ".gitmodules substep skipped this tick, not lost): "
                    + ", ".join(sorted(set(restored)))
                )
            if excluded_only:
                unreadable_excluded = sorted(set(excluded_only))
                notes.append(
                    "WARN unreadable tracked path(s) not on the restore "
                    "allowlist — left untouched on disk (may be real "
                    "unsandboxed work, NOT restored) and excluded from this "
                    "tick's stash: " + ", ".join(unreadable_excluded)
                )

    # 2. Stash anything still dirty (leftover structural edits + untracked), so
    #    the rebase has a clean tree. -u includes untracked; keep-index off.
    #    Any still-unreadable, non-allowlisted path from 1a is EXCLUDED via
    #    pathspec magic (`:(exclude)<path>`) rather than restored — this is
    #    what keeps the batch from being poisoned without ever touching a
    #    path we can't prove is safe to discard.
    stashed = False
    status = _git("status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        stash_args = ["stash", "push", "--include-untracked",
                      "-m", "safe_pull: pre-rebase autostash"]
        if unreadable_excluded:
            stash_args += ["--"] + ["."] + [
                f":(exclude){p}" for p in unreadable_excluded
            ]
        st = _git(*stash_args)
        if st.returncode == 0 and "No local changes" not in (st.stdout + st.stderr):
            stashed = True
            notes.append("stashed leftovers")
        elif st.returncode != 0:
            # Stash itself failed (e.g. another unreadable path we couldn't
            # restore, or an unrelated git error) — surface it as a WARN in
            # the summary rather than silently losing the note; step 4's
            # pull will fail loudly on its own if the tree is still dirty,
            # and that failure path already restores the stash/aborts the
            # rebase safely (never a silent data loss).
            notes.append(
                f"WARN stash failed, continuing: "
                f"{(st.stderr or st.stdout).strip()[:200]}"
            )

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
