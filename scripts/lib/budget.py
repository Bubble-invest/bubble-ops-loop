#!/usr/bin/env python3
"""budget.py — the per-card/mission budget ledger (instruction-based, NOT enforced).

Layer 3 of the dollar-budget feature (board #358). The dept loop (and rnd_loop) use this
to SEE spend-so-far against a card/mission's budget and adjust behaviour — warn-first, by
the agent's judgment. There is NO enforcement here: this only records + reports. The agent
decides what to do with the number (see the dept CLAUDE.md "Budget awareness" step).

Spend is WORKER-SELF-REPORTED: when a worker subagent finishes, its real-$ (input+output,
cache-excluded) is appended here keyed by card/mission id. Approximate by design — the point
is to give the manager a number to steer by; the cockpit /costs page keeps the accurate
per-dept truth.

Ledger: state/budget-ledger.jsonl — one JSON object per line:
    {"id": "<card-or-mission>", "usd": <float>, "ts": "<iso>"}

Usage:
    python3 scripts/lib/budget.py record --card 358 --usd 0.42
    python3 scripts/lib/budget.py record --mission warming --usd 1.10
    python3 scripts/lib/budget.py consumed 358          -> prints the Paris-day spend for id 358
    python3 scripts/lib/budget.py consumed 358 --window week  -> Paris-week spend
    python3 scripts/lib/budget.py consumed 358 --window all   -> legacy all-time sum (debug/backfill)

Windowing (board #958): a card/mission's budget is a PER-PERIOD cap (per run /
per day / per week), not a lifetime cap. `consumed()` therefore defaults to
summing only the CURRENT Paris-local calendar day (matching the fleet's own
day-boundary convention — see `dispatch_helpers.is_mission_due`), not every row
ever recorded for that id.
"""
import argparse
import datetime
import json
import sys
from datetime import timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    # MUST stay identical to dispatch_helpers._PARIS — the fleet's single
    # source of truth for the Paris-local day/week boundary (see is_mission_due).
    # Inlined here (rather than imported) to keep budget.py dependency-free/
    # self-contained for vendoring into dept clones (scripts/vendor-dept-libs.sh).
    _PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover — fallback for ancient envs
    _PARIS = timezone.utc

LEDGER = Path("state/budget-ledger.jsonl")


def _norm(card_or_mission: str) -> str:
    """Normalize an id so record + consumed match: strip a leading '#' and
    whitespace, and lowercase (so `--mission Warming` == `consumed warming`).
    Card numbers are unaffected by lowercasing."""
    return str(card_or_mission).strip().lstrip("#").lower()


def record(item_id: str, usd: float) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": _norm(item_id),
        "usd": round(float(usd), 4),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _row_in_window(row_ts: str, *, window: str, now: datetime.datetime) -> bool:
    """True iff a ledger row's `ts` falls in the requested window, evaluated
    on the Europe/Paris calendar boundary (board #958 — must match
    dispatch_helpers.is_mission_due's day/week semantics, else a 1-2h drift
    around Paris midnight puts spend in the wrong bucket).

    window == "all" is not routed through here (see consumed()).
    """
    try:
        row_dt = datetime.datetime.fromisoformat(row_ts)
    except (TypeError, ValueError):
        return False  # missing/unparseable ts — caller skips the row
    if row_dt.tzinfo is None:
        row_dt = row_dt.replace(tzinfo=timezone.utc)
    row_paris = row_dt.astimezone(_PARIS)
    now_paris = now.astimezone(_PARIS)

    if window == "day":
        return row_paris.date() == now_paris.date()
    if window == "week":
        return row_paris.isocalendar()[:2] == now_paris.isocalendar()[:2]
    raise ValueError(f"unknown window: {window!r}")


def consumed(item_id: str, window: str = "day", *, now: "datetime.datetime | None" = None) -> float:
    """Sum the self-reported real-$ spend for a card/mission id, windowed to
    the CURRENT period (board #958 — a budget is a per-run/per-day/per-week
    cap, not a lifetime cap).

    window:
      "day"  (default) — rows whose ts falls on today's Paris-local calendar
             date. Multiple runs the same day summing together is CORRECT
             (daily spend vs. daily cap) — this is not "re-fixed" here.
      "week" — rows whose ts falls in the current Paris-local ISO week.
      "all"  — legacy behaviour: sum every row ever recorded for this id,
             regardless of ts. Kept for debug/backfill/regression use.

    `now` defaults to real UTC-now; callers (and tests) may inject a
    tz-aware datetime to pin the window boundary. Rows with a missing or
    unparseable `ts` are skipped from any windowed sum (same as malformed
    JSON lines) rather than crashing or being counted.
    """
    if window not in ("day", "week", "all"):
        raise ValueError(f"unknown window: {window!r} (expected day/week/all)")
    target = _norm(item_id)
    if now is None:
        now = datetime.datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    total = 0.0
    if not LEDGER.is_file():
        return 0.0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if _norm(row.get("id", "")) != target:
            continue
        if window != "all":
            if not _row_in_window(row.get("ts", ""), window=window, now=now):
                continue
        total += float(row.get("usd", 0) or 0)
    return round(total, 4)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Per-card/mission budget ledger (record + report).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="append a worker's self-reported spend")
    grp = pr.add_mutually_exclusive_group(required=True)
    grp.add_argument("--card", help="board card number, e.g. 358")
    grp.add_argument("--mission", help="recurring-mission id, e.g. warming")
    pr.add_argument("--usd", type=float, required=True, help="real-$ (cache-excluded) spent")

    pc = sub.add_parser("consumed", help="print windowed $ spend for a card/mission id")
    pc.add_argument("id", help="card number or mission id")
    pc.add_argument("--window", choices=("day", "week", "all"), default="day",
                     help="spend window: current Paris day (default), current "
                          "Paris ISO week, or all-time (legacy/debug)")

    args = p.parse_args(argv)
    if args.cmd == "record":
        record(args.card or args.mission, args.usd)
        return 0
    if args.cmd == "consumed":
        print(f"{consumed(args.id, args.window):.4f}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
