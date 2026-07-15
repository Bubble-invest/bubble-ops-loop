#!/usr/bin/env python3
"""detect_orphan_sessions.py — dry-run detector for orphaned Claude Code
sessions and their Telegram/iMessage channel pollers.

Board #588. Observed 2026-07-07: after a fleet reboot/restart, a channel
poller (``bun server.ts``, spawned by the telegram/imessage plugin's ``bun
run ... start`` wrapper) can outlive the ``claude --channels`` session that
started it. The wrapper dies with the session, but the poller it forked
gets reparented to launchd (PID 1) and keeps hot-spinning — 2 orphans seen
at ~94% CPU each (~2 wasted cores), not doing anything useful since nobody
is reading their output anymore.

REPORTING ONLY. This script never sends a signal to any process. It prints
a verdict and, for each orphan found, a ready-to-copy ``kill -TERM <pid>``
line for a human to run. There is no ``--kill`` flag by design — killing
gets promoted only after a human has graded this detector's judgment on
real dry-run output (see card #588 discussion). A false positive here would
take down a live dept's Telegram/iMessage bridge.

## Real process topology this detector assumes (verified live 2026-07-15)

    claude --channels ...                      <- the session (PID we care about)
      └─ bun run --cwd .../telegram/X.Y.Z --shell=bun --silent start   <- plugin wrapper
           └─ bun server.ts                     <- the actual poller (what we classify)

So a poller is NOT a direct child of the claude session — it's a
grandchild, through a short-lived-looking ``bun run ... start`` wrapper.
Walking only one level up (checking the immediate parent) is not enough;
this detector walks the ancestor chain looking for a live ``claude``
process, and separately treats "parent is PID 1 (launchd)" as the orphan
signal, since that's what an orphaned poller is reparented to on macOS/Linux
when its whole subtree's session leader dies.

## Classification

A poller is ORPHANED iff BOTH hold:
  1. Its parent process is launchd/PID 1 (i.e. it has been reparented —
     the ``bun run ... start`` wrapper that spawned it is gone), AND
  2. No live ``claude`` process exists anywhere in what would have been
     its ancestor chain (belt-and-suspenders: condition 1 alone already
     implies this on a healthy box, since a live wrapper would still be
     between the poller and PID 1, but we check both so a partial/unusual
     reparenting — e.g. wrapper reparented but claude still alive somehow
     — is NOT misclassified as an orphan).

A poller is HEALTHY if its parent is alive and is (or descends from) a
``bun run ... start`` wrapper whose own parent is a live ``claude``
process — i.e. the full 3-level chain is intact.

Usage:
  python3 tools/detect_orphan_sessions.py              # human-readable report
  python3 tools/detect_orphan_sessions.py --json        # machine-readable
  python3 tools/detect_orphan_sessions.py --fixture F   # classify a JSON ps snapshot (testing)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional


POLLER_MARKER = "bun server.ts"
WRAPPER_MARKER = "bun run"
CLAUDE_MARKER = "claude"


@dataclass
class ProcRow:
    pid: int
    ppid: int
    pcpu: float
    command: str


@dataclass
class Verdict:
    pid: int
    ppid: int
    pcpu: float
    command: str
    orphaned: bool
    reason: str
    ancestor_chain: list = field(default_factory=list)  # list of ProcRow-like dicts, pid order nearest-first


def _live_ps_snapshot() -> list[ProcRow]:
    """Read-only: one `ps -eo pid,ppid,pcpu,command` call. Never mutates
    anything. Falls back to an empty list (never raises) so a detector bug
    can't crash whatever cron/timer calls this."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,pcpu,command"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except Exception as exc:  # noqa: BLE001 — reporting tool, must not crash the caller
        print(f"WARNING: ps snapshot failed: {exc}", file=sys.stderr)
        return []

    rows: list[ProcRow] = []
    lines = out.splitlines()
    for line in lines[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, pcpu_s, command = parts
        try:
            rows.append(ProcRow(pid=int(pid_s), ppid=int(ppid_s), pcpu=float(pcpu_s), command=command))
        except ValueError:
            continue
    return rows


def _by_pid(rows: list[ProcRow]) -> dict:
    return {r.pid: r for r in rows}


def _walk_ancestors(pid: int, by_pid: dict, max_depth: int = 10) -> list:
    """Walk up the parent chain starting at `pid`'s parent. Returns the
    chain as a list of ProcRow, nearest-parent-first. Stops at PID 1, an
    unknown/missing PID, or max_depth (cycle/runaway guard)."""
    chain = []
    seen = set()
    row = by_pid.get(pid)
    if row is None:
        return chain
    current_ppid = row.ppid
    for _ in range(max_depth):
        if current_ppid in seen:
            break  # cycle guard, should never happen but never trust ps blindly
        seen.add(current_ppid)
        parent = by_pid.get(current_ppid)
        if parent is None:
            # Parent not found in the snapshot at all — treat as unknown,
            # not orphaned (conservative: absence of proof isn't proof).
            break
        chain.append(parent)
        if parent.pid == 1:
            break
        current_ppid = parent.ppid
    return chain


def classify_poller(poller: ProcRow, by_pid: dict) -> Verdict:
    """Classify a single `bun server.ts` poller process.

    Conservative by construction: anything short of "parent chain reaches
    PID 1 with no live claude process anywhere in the walked chain" is
    reported healthy/unknown, never orphaned. When ps data is incomplete
    (parent missing from snapshot — a race during the ps call), we do NOT
    call it orphaned; we flag it unknown and let a human look.
    """
    chain = _walk_ancestors(poller.pid, by_pid)
    chain_dicts = [{"pid": r.pid, "ppid": r.ppid, "command": r.command} for r in chain]

    parent = by_pid.get(poller.ppid)
    if parent is None:
        return Verdict(
            pid=poller.pid, ppid=poller.ppid, pcpu=poller.pcpu, command=poller.command,
            orphaned=False,
            reason=f"parent PID {poller.ppid} not found in ps snapshot (race or already gone) — "
                   f"reporting as UNKNOWN, not orphaned (conservative default)",
            ancestor_chain=chain_dicts,
        )

    has_live_claude_in_chain = any(CLAUDE_MARKER in r.command and WRAPPER_MARKER not in r.command for r in chain)

    if parent.pid == 1:
        if has_live_claude_in_chain:
            # Should not happen given a single-hop wrapper topology, but
            # if it ever does, don't misclassify — surface it as unknown.
            return Verdict(
                pid=poller.pid, ppid=poller.ppid, pcpu=poller.pcpu, command=poller.command,
                orphaned=False,
                reason="parent is launchd (PID 1) but a live claude process was found further up an "
                       "unexpected chain — unusual topology, reporting UNKNOWN not orphaned",
                ancestor_chain=chain_dicts,
            )
        return Verdict(
            pid=poller.pid, ppid=poller.ppid, pcpu=poller.pcpu, command=poller.command,
            orphaned=True,
            reason="parent is launchd (PID 1) — the bun-run wrapper that spawned this poller is gone "
                   "and no live claude session owns it",
            ancestor_chain=chain_dicts,
        )

    # Parent is alive and is not launchd. Healthy iff somewhere in the
    # (short) ancestor chain there is a live claude process.
    if has_live_claude_in_chain:
        return Verdict(
            pid=poller.pid, ppid=poller.ppid, pcpu=poller.pcpu, command=poller.command,
            orphaned=False,
            reason="live claude process found in ancestor chain — healthy",
            ancestor_chain=chain_dicts,
        )

    return Verdict(
        pid=poller.pid, ppid=poller.ppid, pcpu=poller.pcpu, command=poller.command,
        orphaned=False,
        reason="parent is alive and is not launchd, but no claude process found in the walked ancestor "
               "chain (chain may have been truncated by max_depth, or this is a non-fleet bun server.ts) "
               "— reporting UNKNOWN, not orphaned (conservative default)",
        ancestor_chain=chain_dicts,
    )


def find_pollers(rows: list[ProcRow]) -> list[ProcRow]:
    return [r for r in rows if POLLER_MARKER in r.command]


def run(rows: list[ProcRow]) -> list[Verdict]:
    by_pid = _by_pid(rows)
    pollers = find_pollers(rows)
    return [classify_poller(p, by_pid) for p in pollers]


def _load_fixture(path: str) -> list[ProcRow]:
    with open(path) as f:
        data = json.load(f)
    return [ProcRow(pid=d["pid"], ppid=d["ppid"], pcpu=d.get("pcpu", 0.0), command=d["command"]) for d in data]


def _print_report(verdicts: list[Verdict]) -> int:
    orphans = [v for v in verdicts if v.orphaned]
    unknowns = [v for v in verdicts if not v.orphaned and "UNKNOWN" in v.reason]
    healthy = [v for v in verdicts if not v.orphaned and "UNKNOWN" not in v.reason]

    print(f"detect_orphan_sessions.py — {len(verdicts)} poller(s) found "
          f"({len(healthy)} healthy, {len(orphans)} orphaned, {len(unknowns)} unknown)")
    print()

    if not verdicts:
        print("No `bun server.ts` poller processes found on this box.")
        return 0

    for v in healthy:
        print(f"  [healthy] pid={v.pid} ppid={v.ppid} cpu={v.pcpu}% — {v.reason}")

    for v in unknowns:
        print(f"  [UNKNOWN] pid={v.pid} ppid={v.ppid} cpu={v.pcpu}% — {v.reason}")

    if orphans:
        print()
        print(f"ORPHANED — {len(orphans)} poller(s) reparented to launchd with no owning claude session:")
        for v in orphans:
            print(f"  [ORPHAN]  pid={v.pid} ppid={v.ppid} cpu={v.pcpu}% cmd={v.command!r}")
            print(f"            reason: {v.reason}")
            print(f"            ancestor chain: {v.ancestor_chain}")
            print(f"            would run: kill -TERM {v.pid}   # NOT executed — dry-run only, human must run this")
    else:
        print()
        print("ORPHANED — none.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the text report")
    ap.add_argument("--fixture", metavar="PATH", help="classify a JSON ps snapshot instead of the live machine "
                                                        "(testing only; see tests/fixtures/)")
    args = ap.parse_args()

    rows = _load_fixture(args.fixture) if args.fixture else _live_ps_snapshot()
    verdicts = run(rows)

    if args.json:
        print(json.dumps([{
            "pid": v.pid, "ppid": v.ppid, "pcpu": v.pcpu, "command": v.command,
            "orphaned": v.orphaned, "reason": v.reason, "ancestor_chain": v.ancestor_chain,
        } for v in verdicts], indent=2))
        return 0

    return _print_report(verdicts)


if __name__ == "__main__":
    sys.exit(main())
