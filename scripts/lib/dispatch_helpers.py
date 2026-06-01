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


def write_last_run(layer_dir: Path, when: datetime) -> None:
    """Write the ISO-8601 timestamp to `<layer_dir>/.last-run`.

    `when` MUST be timezone-aware; we serialize via `datetime.isoformat()`
    so the offset is preserved. This is the documented FIRST action of any
    layer dispatch (idempotence + audit trail).
    """
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
    """For each mission with layer==1 that is due, return one queue-item
    descriptor per `creates[]` entry.

    The returned dicts are NOT full queue-item.schema.yaml documents — they
    are the minimum payload the agent / a downstream skill needs to write
    the actual files. Shape:
        {"mission_id": str, "output_queue": str, "kind": str}

    Layer-4 missions are filtered out (they're owned by STEP C.1).
    """
    out: list[dict] = []
    for m in missions:
        if int(m.get("layer", 0)) != 1:
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

# The L4 window is the same as the existing C.1 rule in the template.
_L4_WINDOW_START = _time(22, 0)
_L4_WINDOW_END = _time(22, 30)


def decide_dispatch(ctx: dict[str, Any]) -> str:
    """Return one of "layer_1" / "layer_2" / "layer_3" / "layer_4" /
    "heartbeat" given the tick context.

    Required ctx keys (all optional, sensible defaults applied):
      now_utc: tz-aware UTC datetime — required
      has_research_items: bool
      has_inbox_decisions: bool
      layer_4_last_run_today: datetime | None — set if outputs/<today>/4/.last-run exists
      round_counter: dict[str, int] — per-layer round counts for today
      fire_after_rounds: int — Layer-1 gate threshold (default 1)

    Priority order (highest to lowest):
      C.1 — Layer 4 if in 22:00-22:30 UTC window AND not yet run today
      C.2 — Layer 2 if research queue has items
      C.3 — Layer 3 if inbox decisions have items
      C.0 — Layer 1 if idle gate satisfied (round counter ≥ N)
      C.4 — heartbeat (default)

    Note: C.0 has the LOWEST priority despite its number — the naming is
    "Layer 1 is the last to fire" by design. Joris's "all other layers
    idle" gate.
    """
    now_utc: datetime = ctx["now_utc"]
    if now_utc.tzinfo is None:
        raise ValueError("decide_dispatch: now_utc must be tz-aware")
    has_research = bool(ctx.get("has_research_items", False))
    has_decisions = bool(ctx.get("has_inbox_decisions", False))
    l4_last = ctx.get("layer_4_last_run_today")
    counts = ctx.get("round_counter") or {}
    fire_after_rounds = int(ctx.get("fire_after_rounds", 1))

    # C.1 — L4 window (UTC).
    in_window = _L4_WINDOW_START <= now_utc.time() < _L4_WINDOW_END
    if in_window and l4_last is None:
        return "layer_4"

    # C.2 — research queue.
    if has_research:
        return "layer_2"

    # C.3 — inbox decisions.
    if has_decisions:
        return "layer_3"

    # C.0 — Layer 1 idle gate.
    gate_ok = all(int(counts.get(str(layer), 0)) >= fire_after_rounds
                  for layer in (2, 3, 4))
    if gate_ok:
        return "layer_1"

    # C.4 — heartbeat.
    return "heartbeat"


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


def force_commit_and_push(
    repo_dir: Path,
    message: str,
    bubble_git_guard_path: str = "/opt/bubble-git-guard/bin/bubble-git-guard",
    action: str = "runtime_write_own",
) -> tuple[bool, "str | None"]:
    """Stage everything, commit, and push via bubble-git-guard.

    Called by Layer 4 after EACH artifact write per Joris msg 3134 — the
    risk manager's outputs are source-of-truth for what happened today;
    we don't let them sit 20 minutes waiting for the next /loop's STEP E.

    Idempotent: if `git status --porcelain` is clean, returns (True, None)
    with no side effect (the guard isn't even invoked).

    Returns (ok, error_message). ok=False when bubble-git-guard push
    rejects (operator must investigate manually — we do NOT silently
    swallow push errors).
    """
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

    # 2. Stage everything.
    add = subprocess.run(
        ["git", "-C", str(repo_dir), "add", "-A"],
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

    # 4. Push via bubble-git-guard (mints short-lived broker token).
    push = subprocess.run(
        [bubble_git_guard_path, "push", "--action", action],
        capture_output=True, text=True,
        cwd=str(repo_dir),
    )
    if push.returncode != 0:
        return False, (
            f"bubble-git-guard push failed (rc={push.returncode}): "
            f"{(push.stderr or push.stdout).strip()[:200]}"
        )
    return True, None
