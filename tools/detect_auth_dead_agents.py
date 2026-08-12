#!/usr/bin/env python3
"""detect_auth_dead_agents.py — dry-run detector for auth-dead fleet agents.

Board #832. Miranda's (M1) Claude auth expired 2026-07-25 but her tmux
session, `claude` process, and transcript file all stayed alive and
"healthy" by every liveness signal we had — the transcript even kept
growing, because every failed turn appends an auth-failure line to it. No
watchdog tripped. It took a human noticing she'd gone quiet for two days.
This is the same invisible-failure class as #588 (orphaned pollers): the
system reports success (a live process, a growing file) while doing
nothing useful.

REPORTING ONLY. This script never touches a session, never runs `/login`,
never restarts anything. `/login` is an interactive browser OAuth flow on
the OWNER'S OWN MACHINE — there is no SSH or server-side remediation path.
The alert's only job is to name the machine + agent + exact remediation
fast, not to self-heal. (Mirrors the `detect_orphan_sessions.py` posture:
report + human-run command, never act.)

## The core design problem: why whole-file grep is WRONG

Measured directly on M1's real live transcript
(``~/.claude/projects/-Users-jadethi-viet-lanhoang-claude-workspaces-bubble-ops-content/3b286d85-*.jsonl``,
3525 lines, 2026-07-27):

  - ``Please run /login``   -> 34 hits (matches the 34 known failed turns)
  - ``authentication_error`` -> 0 hits (this string ALONE would have missed
    the entire two-day outage — never rely on a single signature)
  - last auth-failure hit at line 3247 of 3525; the agent was re-authed
    later that morning
  - ``tail -50``  -> 0 hits
  - ``tail -200`` -> 0 hits  => correctly reports HEALTHY post-re-auth

A whole-file grep for these strings would find the 34 historical hits
FOREVER, on every run, for the rest of this transcript's life, even though
the agent has been healthy and answering since re-auth. An alert that
always fires gets muted by the humans who see it — and then the NEXT real
outage (which looks identical in a whole-file grep: "hits found") is
invisible again. That is exactly the failure #832 exists to fix, so this
detector must not reproduce it.

## Chosen window: last N=200 lines (tail), not whole-file

Rationale, tied to the measurement above:
  - N=200 is empirically past the last known auth-failure line (3247) by a
    wide margin relative to session length, AND it's what Joris/the board
    verified by hand (`tail -200` -> 0 hits on the re-authed session) —
    using the same window as the manual verification keeps this detector's
    verdict auditable against that reference point.
  - A genuinely dead agent fails auth on EVERY turn from the moment it
    dies onward — so a live outage produces failure lines continuously,
    including in whatever the most recent N lines are, for any reasonable
    N. A single historical incident does not.
  - Line-count (not a timestamp cutoff) is used because line count is
    cheap (no JSON parsing required — see below) and turn-dense: a dead
    agent that's still receiving Telegram traffic accumulates failed-turn
    lines quickly, so N=200 lines is a short REAL-TIME window in practice,
    not a long one. A timestamp cutoff would need to parse every line as
    JSON to find a `timestamp` field, which the whole-file-vs-tail
    complexity of this script is deliberately avoiding (a parse failure on
    one malformed line must never blind the detector to the lines around
    it — plain substring grep is robust to any single corrupt line).

**Accepted false-positive case:** an agent that fails auth, then in the
SAME tail window before N more lines accumulate, gets re-authed and starts
succeeding again, will still show DEAD until enough healthy turns push the
failure lines out of the window. With N=200 and Miranda's own
traffic density this is a small window (the incident needed 34 lines to
fill), but on a much quieter agent it could take longer to self-clear.
This is treated as acceptable: a stale-DEAD alert after re-auth degrades
gracefully to "recheck in a bit", not a missed outage — the opposite
failure (whole-file grep alarming forever) is the one that actually cost
two days.

**Accepted false-negative case:** an agent that fails auth on every turn
but receives so LITTLE traffic that 200 lines of non-auth-failure content
(tool calls, system entries, etc. from BEFORE the failure started) still
sit in the tail window would be reported HEALTHY even though it is
currently dead, until enough failed turns accumulate to fill the window.
This trades against the opposite failure (alerting forever on a
re-authed agent); a low-traffic agent that goes fully silent is also
more likely to be caught by existing liveness/heartbeat checks
elsewhere in the fleet, since #832 targets agents that keep LOOKING busy.

Widening the window or switching to a timestamp cutoff is a reasonable
future iteration — flagged here, not built, to keep this change small and
testable against the one incident we actually have ground truth for.

## Finding the LIVE transcript, not just the newest .jsonl

Agents resume via `--continue --fork-session`, which starts a NEW
`.jsonl` file and leaves the old one in place, unwritten from then on.
"Newest .jsonl by mtime in the project dir" is the same heuristic used
elsewhere in this repo for "the live session" and is what this script
uses. It CAN pick the wrong file if a background/inactive session in the
same project dir was touched more recently than the dept's real live
session (e.g. a one-off debugging session run by a human against the same
project path) — in that case the detector reports on the wrong
transcript's tail and its verdict says nothing about the real agent. This
is a known, accepted limitation (documented, not solved) — see PR body.

## Fleet-wide, no hardcoded paths

Different machines, different `$HOME`, different usernames (Jade's Macs
use `/Users/jadethi.viet.lanhoang/...`, Joris's use `/Users/joris/...`).
This script never hardcodes a home dir. It takes one or more **project
transcript root directories** (the `~/.claude/projects/<escaped-cwd>/`
dirs) as input — via `--projects-root` (a dir containing multiple such
project dirs, e.g. `~/.claude/projects` itself) or `--project-dir`
(one project dir directly) — and an optional `--agent-name` label per
root purely for readable alert text. Discovering WHICH roots exist on
WHICH machine (the actual fleet roster: Miranda/M1, Ellie+Geraldine/M5,
Tonio+DeepSeek/M4, VPS depts) is deliberately left to the caller
(cron/watchdog config), same as `detect_orphan_sessions.py` leaves
"which box am I on" to whatever invokes it — this script's job is the
per-transcript classification, not fleet discovery.

Usage:
  # scan every project dir under a `~/.claude/projects`-shaped root
  python3 tools/detect_auth_dead_agents.py --projects-root ~/.claude/projects

  # scan one specific project dir (e.g. from a per-agent cron)
  python3 tools/detect_auth_dead_agents.py --project-dir ~/.claude/projects/-Users-x-claude-workspaces-y --agent-name miranda

  # machine-readable
  python3 tools/detect_auth_dead_agents.py --projects-root ~/.claude/projects --json

  # classify a fixture transcript directly (testing)
  python3 tools/detect_auth_dead_agents.py --fixture-transcript tests/fixtures/auth_dead_tail.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Signatures observed to appear ONLY on an auth failure, never in healthy
# operation. #832 explicitly measured that `authentication_error` ALONE
# scores 0 hits against a real 34-failure incident (it's present in some
# error payloads but not the ones actually emitted here) — so this list is
# deliberately redundant. Do not slim it down to "the one that matched
# last time"; different SDK/CLI versions surface different strings.
AUTH_FAILURE_SIGNATURES = (
    "Please run /login",
    "authentication_error",
    "OAuth token has expired",
    "Invalid API key",
)

# Empirically justified in the module docstring: matches the manual
# `tail -200` verification already performed against the real M1 incident.
DEFAULT_WINDOW_LINES = 200


@dataclass
class Verdict:
    transcript_path: str
    agent_name: str
    status: str  # "DEAD" | "HEALTHY" | "UNKNOWN"
    reason: str
    matched_signatures: list = field(default_factory=list)
    hit_lines: list = field(default_factory=list)  # 1-indexed line numbers within the window
    lines_scanned: int = 0


def _tail_lines(path: Path, window: int) -> Optional[list]:
    """Read the last `window` lines of a text file without loading the
    whole file into memory for huge transcripts. Returns None (not []) if
    the file cannot be read at all, so callers can tell "empty file" apart
    from "unreadable file"."""
    try:
        with open(path, "rb") as f:
            # Simple, robust tail: seek from the end in chunks. Transcripts
            # are JSONL (one JSON object per line) so this never needs to
            # split mid-multibyte-char at a line boundary in practice, but
            # we decode with errors="replace" as a belt-and-suspenders
            # guard against a corrupt byte sequence blinding the whole read.
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            block_size = 8192
            data = b""
            lines_found = 0
            pos = file_size
            while pos > 0 and lines_found <= window:
                read_size = min(block_size, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
                lines_found = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return lines[-window:] if len(lines) > window else lines
    except FileNotFoundError:
        return None
    except OSError:
        return None


def classify_transcript(path: Path, agent_name: str = "", window: int = DEFAULT_WINDOW_LINES) -> Verdict:
    """Classify one transcript file. Never raises — a bad transcript
    (missing, empty, unreadable, garbage bytes) must not crash whatever
    cron/timer calls this across the whole fleet in one pass.

    Unknown (missing/empty/unreadable) transcripts are reported HEALTHY,
    not DEAD — a detector that can't read the file has no evidence of a
    failure, and #832's whole point is that ALERT FATIGUE from false
    positives is what let the real incident hide for two days. Silence
    from an unreadable-transcript case would otherwise be indistinguishable
    from silence-because-genuinely-broken; see PR body for why this
    default was chosen over the alternative (alert on unknown)."""
    label = agent_name or str(path)

    if not path.exists():
        return Verdict(
            transcript_path=str(path), agent_name=label, status="UNKNOWN",
            reason=f"transcript not found at {path} — cannot evaluate, reporting HEALTHY "
                   f"(conservative default: no evidence of failure is not evidence of failure, "
                   f"but this also cannot become a permanent false alarm)",
        )

    lines = _tail_lines(path, window)
    if lines is None:
        return Verdict(
            transcript_path=str(path), agent_name=label, status="UNKNOWN",
            reason=f"transcript at {path} could not be read (permission/IO error) — reporting HEALTHY "
                   f"(conservative default, see missing-file case)",
        )

    if not lines:
        return Verdict(
            transcript_path=str(path), agent_name=label, status="HEALTHY",
            reason="transcript exists but is empty (new/never-run session) — no failure signal possible",
            lines_scanned=0,
        )

    matched = set()
    hit_lines = []
    for i, line in enumerate(lines, start=1):
        for sig in AUTH_FAILURE_SIGNATURES:
            if sig in line:
                matched.add(sig)
                hit_lines.append(i)
                break

    if matched:
        return Verdict(
            transcript_path=str(path), agent_name=label, status="DEAD",
            reason=(
                f"auth-failure signature(s) found in the last {len(lines)} line(s) "
                f"(window={window}): {sorted(matched)!r} — agent cannot complete turns, "
                f"needs a human to run /login on the owning machine (interactive OAuth, "
                f"no server-side remediation)"
            ),
            matched_signatures=sorted(matched),
            hit_lines=hit_lines,
            lines_scanned=len(lines),
        )

    return Verdict(
        transcript_path=str(path), agent_name=label, status="HEALTHY",
        reason=f"no auth-failure signature in the last {len(lines)} line(s) (window={window})",
        lines_scanned=len(lines),
    )


def find_live_transcript(project_dir: Path) -> Optional[Path]:
    """"Live" session heuristic: newest-by-mtime `.jsonl` directly inside a
    `~/.claude/projects/<escaped-cwd>/` project dir. A `--continue
    --fork-session` resume starts a NEW file and leaves the old one
    unwritten, so mtime (not filename/creation order) is what actually
    tracks "which file is still being appended to right now".

    KNOWN LIMITATION (see module docstring): this can pick the wrong file
    if some other session under the same project dir (e.g. a human's
    one-off debugging session) was touched more recently than the dept's
    real live session. When that happens this function has no way to tell
    the difference — it returns the wrong path, silently, and the caller's
    verdict is about the wrong session. Not solved here; flagged for the
    PR body and for whoever wires this into a cron to be aware of."""
    if not project_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in project_dir.glob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def discover_project_dirs(projects_root: Path) -> list:
    """Every immediate subdirectory of a `~/.claude/projects`-shaped root
    that looks like a project dir (contains at least one .jsonl,
    directly or we wouldn't know it's relevant). Cheap, read-only glob —
    no hardcoded dept list, matches the `bubble-ops-*` glob-discovery
    convention used elsewhere in this repo (e.g. fleet-drift-report.sh)."""
    if not projects_root.is_dir():
        return []
    return sorted(d for d in projects_root.iterdir() if d.is_dir())


def run_fleet_scan(project_dirs: list, window: int = DEFAULT_WINDOW_LINES) -> list:
    verdicts = []
    for pd in project_dirs:
        transcript = find_live_transcript(pd)
        agent_name = pd.name
        if transcript is None:
            verdicts.append(Verdict(
                transcript_path="", agent_name=agent_name, status="UNKNOWN",
                reason=f"no .jsonl transcript found under {pd} — reporting HEALTHY (conservative default)",
            ))
            continue
        verdicts.append(classify_transcript(transcript, agent_name=agent_name, window=window))
    return verdicts


def _print_report(verdicts: list) -> int:
    dead = [v for v in verdicts if v.status == "DEAD"]
    unknown = [v for v in verdicts if v.status == "UNKNOWN"]
    healthy = [v for v in verdicts if v.status == "HEALTHY"]

    print(f"detect_auth_dead_agents.py — {len(verdicts)} transcript(s) checked "
          f"({len(healthy)} healthy, {len(dead)} DEAD, {len(unknown)} unknown)")
    print()

    for v in healthy:
        print(f"  [healthy] {v.agent_name} — {v.reason}")
    for v in unknown:
        print(f"  [unknown] {v.agent_name} — {v.reason}")

    if dead:
        print()
        print(f"AUTH-DEAD — {len(dead)} agent(s) need a human to run /login:")
        for v in dead:
            print(f"  [DEAD] {v.agent_name}")
            print(f"         transcript: {v.transcript_path}")
            print(f"         signatures: {v.matched_signatures}")
            print(f"         {v.reason}")
            print(f"         REMEDIATION: on the machine running this agent, run `/login` "
                  f"in its Claude Code session (interactive browser OAuth — cannot be done "
                  f"remotely/server-side). No auto-heal is possible.")
    else:
        print()
        print("AUTH-DEAD — none.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects-root", metavar="DIR",
                     help="a `~/.claude/projects`-shaped dir; every subdir with a .jsonl is scanned")
    ap.add_argument("--project-dir", action="append", default=[], metavar="DIR",
                     help="one project dir to scan directly (repeatable)")
    ap.add_argument("--agent-name", metavar="NAME",
                     help="label for --project-dir's alert text (only meaningful with exactly one --project-dir)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_LINES,
                     help=f"tail window in lines (default {DEFAULT_WINDOW_LINES})")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the text report")
    ap.add_argument("--fixture-transcript", metavar="PATH",
                     help="classify one transcript file directly, bypassing discovery (testing/one-off)")
    args = ap.parse_args()

    verdicts = []

    if args.fixture_transcript:
        verdicts.append(classify_transcript(Path(args.fixture_transcript), agent_name="fixture", window=args.window))
    else:
        project_dirs = []
        if args.projects_root:
            project_dirs.extend(discover_project_dirs(Path(args.projects_root).expanduser()))
        for pd in args.project_dir:
            project_dirs.append(Path(pd).expanduser())

        if not project_dirs:
            print("No --projects-root, --project-dir, or --fixture-transcript given — nothing to scan.",
                  file=sys.stderr)
            return 2

        if len(project_dirs) == 1 and args.agent_name:
            transcript = find_live_transcript(project_dirs[0])
            if transcript is None:
                verdicts.append(Verdict(
                    transcript_path="", agent_name=args.agent_name, status="UNKNOWN",
                    reason=f"no .jsonl transcript found under {project_dirs[0]} — reporting HEALTHY",
                ))
            else:
                verdicts.append(classify_transcript(transcript, agent_name=args.agent_name, window=args.window))
        else:
            verdicts.extend(run_fleet_scan(project_dirs, window=args.window))

    if args.json:
        print(json.dumps([{
            "transcript_path": v.transcript_path, "agent_name": v.agent_name, "status": v.status,
            "reason": v.reason, "matched_signatures": v.matched_signatures,
            "hit_lines": v.hit_lines, "lines_scanned": v.lines_scanned,
        } for v in verdicts], indent=2))
        return 1 if any(v.status == "DEAD" for v in verdicts) else 0

    _print_report(verdicts)
    return 1 if any(v.status == "DEAD" for v in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
