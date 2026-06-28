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
    python3 scripts/lib/budget.py consumed 358          -> prints the summed $ for id 358
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

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


def consumed(item_id: str) -> float:
    """Sum the self-reported real-$ spend for a card/mission id."""
    target = _norm(item_id)
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
        if _norm(row.get("id", "")) == target:
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

    pc = sub.add_parser("consumed", help="print summed $ for a card/mission id")
    pc.add_argument("id", help="card number or mission id")

    args = p.parse_args(argv)
    if args.cmd == "record":
        record(args.card or args.mission, args.usd)
        return 0
    if args.cmd == "consumed":
        print(f"{consumed(args.id):.4f}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
