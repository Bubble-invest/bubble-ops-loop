#!/usr/bin/env python3
"""skill_usage_count.py — ARM B evidence collector for the skill-authoring agent.

DOCTRINE (Joris, `shared/systems/cron-judgment-vs-tools.md`, #1222)
-------------------------------------------------------------------------------
"Tools = evidence-collectors. Deterministic, cheap, always-run. Output
structured data (JSON), not English verdicts. Agent = judgment layer."

This script is PURE EVIDENCE. It counts, over a rolling window, how many times
each skill was actually invoked across the fleet's centralized transcript
corpus. It emits {skill: {count, last_used, sessions, agents}} as JSON. It does
NOT decide whether any skill is "dead" — that is the AGENT's judgment (ARM B
step 2 in SKILL.md), because a bare count would prune rare-but-critical skills
like `auth`, `polymarket-vpn-bringup`, `dept-spawner` (low frequency, high
criticality) — the exact deterministic-filter failure the doctrine warns about.

HOW USAGE IS COUNTED (verified against a live transcript, #1222 investigation)
-------------------------------------------------------------------------------
Every skill invocation appears in the session `.jsonl` as a tool_use block:

    {"type":"tool_use","name":"Skill","input":{"skill":"<name>","args":"..."}}

carried inside a message line's `message.content[]`. We count one invocation per
such block, keyed by input.skill, windowed by the line's ISO-8601 `timestamp`.

CORPUS (the same centralized feed wiki-compile mines — no new scan mechanism)
-------------------------------------------------------------------------------
Default roots (on the VPS):
  - /home/claude/.claude/projects/-home-claude-agents-*/   (6 VPS-native agents)
  - /home/claude/.claude/projects/_mac-joris/              (rsync'd Mac cache)
  - /home/claude/.claude/projects/_mac-jade/               (rsync'd Mac cache)
Override with one or more --corpus DIR (repeatable) or the CORPUS env var
(os.pathsep-separated) for testing / running off-VPS.

REGISTRY (so never-invoked skills show up as count 0 — the prune targets)
-------------------------------------------------------------------------------
Pass --registry DIR (repeatable) pointing at a skills root (each subdir with a
SKILL.md is one skill). Every registered skill is emitted even at count 0, so
the agent sees the full prune-candidate set, not just skills that WERE used.
Bundled/enterprise skills that never appear in `~/.claude/skills` are out of
scope by construction (same coverage caveat as `/skill-doctor`).

This module is import-safe (pure helpers + argparse main) so it can be unit
tested without a live corpus.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- default corpus + registry roots (VPS) -----------------------------------
DEFAULT_CORPUS_GLOBS = [
    "/home/claude/.claude/projects/-home-claude-agents-*",
    "/home/claude/.claude/projects/_mac-joris",
    "/home/claude/.claude/projects/_mac-jade",
]
DEFAULT_REGISTRY_DIRS = [
    "/home/claude/.claude/skills",
]


def parse_ts(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp like '2026-08-25T15:08:55.971Z' → aware UTC
    datetime. Returns None on anything unparseable (line without a timestamp,
    e.g. a session-header line)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        # Python's fromisoformat handles offsets but not a trailing 'Z' before
        # 3.11; normalize it.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_skill_invocations(jsonl_path: Path):
    """Yield (skill_name, ts_or_None) for every Skill tool_use block in one
    transcript file. Robust to malformed lines and non-list content."""
    try:
        fh = jsonl_path.open("r", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line or '"Skill"' not in line:
                # Cheap pre-filter: skip lines that cannot contain a Skill block.
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                # A valid-JSON line that isn't an object (e.g. a bare string) —
                # not a transcript record; skip.
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            ts = parse_ts(obj.get("timestamp"))
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Skill"
                ):
                    inp = block.get("input")
                    if isinstance(inp, dict):
                        name = inp.get("skill")
                        if isinstance(name, str) and name:
                            yield name, ts


def resolve_corpus_roots(cli_corpus: list[str]) -> list[Path]:
    """Expand the corpus globs into concrete existing dirs. CLI > env > default."""
    if cli_corpus:
        patterns = cli_corpus
    elif os.environ.get("CORPUS"):
        patterns = os.environ["CORPUS"].split(os.pathsep)
    else:
        patterns = DEFAULT_CORPUS_GLOBS
    roots: list[Path] = []
    for pat in patterns:
        # Support both concrete dirs and globs.
        matched = glob.glob(pat)
        if matched:
            roots.extend(Path(m) for m in matched if Path(m).is_dir())
        elif Path(pat).is_dir():
            roots.append(Path(pat))
    return roots


def agent_slug_from_dir(project_dir: Path) -> str:
    """Best-effort human label for which agent a transcript dir belongs to, for
    the agent's judgment (e.g. 'is this skill used by only one dept?')."""
    n = project_dir.name
    if n.startswith("-home-claude-agents-"):
        return n[len("-home-claude-agents-"):] or n
    return n  # _mac-joris / _mac-jade / custom


def count_usage(
    corpus_roots: list[Path],
    window_days: int,
    now: datetime | None = None,
) -> dict:
    """Walk every *.jsonl under each corpus root, counting Skill invocations
    inside the window. Returns {skill: {count, last_used, sessions, agents}}.

    Windowing rule: a block with NO parseable timestamp is COUNTED (we don't
    silently drop evidence just because a line lacked a timestamp) but does not
    update last_used. A block with a timestamp OLDER than the cutoff is skipped.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    stats: dict[str, dict] = {}
    for root in corpus_roots:
        agent = agent_slug_from_dir(root)
        for jf in root.rglob("*.jsonl"):
            session = jf.stem
            for name, ts in iter_skill_invocations(jf):
                if ts is not None and ts < cutoff:
                    continue
                s = stats.setdefault(
                    name,
                    {"count": 0, "last_used": None, "sessions": set(), "agents": set()},
                )
                s["count"] += 1
                s["sessions"].add(session)
                s["agents"].add(agent)
                if ts is not None:
                    iso = ts.astimezone(timezone.utc).isoformat()
                    if s["last_used"] is None or iso > s["last_used"]:
                        s["last_used"] = iso
    return stats


def enumerate_registry(registry_dirs: list[str]) -> set[str]:
    """Every skill name registered under the given roots (subdir with SKILL.md)."""
    names: set[str] = set()
    for d in registry_dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for sub in base.iterdir():
            if sub.is_dir() and (sub / "SKILL.md").is_file():
                names.add(sub.name)
    return names


def build_report(
    corpus_roots: list[Path],
    registry_dirs: list[str],
    window_days: int,
    now: datetime | None = None,
) -> dict:
    stats = count_usage(corpus_roots, window_days, now=now)
    registered = enumerate_registry(registry_dirs)
    # Merge: ensure every registered skill appears (count 0 = never invoked in
    # window = a prune candidate for the AGENT to judge).
    skills_out = {}
    for name in sorted(set(stats) | registered):
        s = stats.get(name, {"count": 0, "last_used": None, "sessions": set(), "agents": set()})
        skills_out[name] = {
            "count": s["count"],
            "last_used": s["last_used"],
            "distinct_sessions": len(s["sessions"]),
            "agents": sorted(s["agents"]),
            "registered": name in registered,
        }
    return {
        "window_days": window_days,
        "generated_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "corpus_roots": [str(r) for r in corpus_roots],
        "registry_dirs": registry_dirs,
        "live_skill_count": len(registered),
        "skills": skills_out,
        "note": (
            "count == 0 means never invoked in the window (prune CANDIDATE). "
            "A count is EVIDENCE ONLY — the agent must read each low/zero-use "
            "skill's SKILL.md and judge genuinely-dead vs rare-but-critical "
            "before flagging. Never auto-remove."
        ),
    }


def record_weekly_counts(report: dict, flagged_count: int, history_dir: str) -> dict:
    """Record agent-supplied flags, never infer pruning judgments from usage.

    Compare exact ISO weeks so a missed week is not disguised as a weekly delta.
    Replacing the current week makes retries idempotent.
    """
    if flagged_count < 0:
        raise ValueError("flagged_count must be non-negative")
    if not all(Path(d).is_dir() for d in report["registry_dirs"]):
        raise ValueError("registry unavailable; cannot record a live skill count")
    now = datetime.fromisoformat(report["generated_utc"])
    week = now.strftime("%G-W%V")
    previous_week = (now - timedelta(days=7)).strftime("%G-W%V")
    history = Path(history_dir)
    previous = None
    try:
        previous = json.loads((history / f"{previous_week}.json").read_text())
        if (not isinstance(previous, dict) or previous.get("week") != previous_week
                or any(type(previous.get(k)) is not int or previous[k] < 0
                       for k in ("live_skill_count", "flagged_count"))
                or previous.get("registry_dirs") != report["registry_dirs"]
                or previous.get("window_days") != report["window_days"]):
            previous = None
    except (OSError, ValueError):
        pass
    counts = {
        "week": week,
        "previous_week": previous_week,
        "registry_dirs": report["registry_dirs"],
        "window_days": report["window_days"],
        "live_skill_count": report["live_skill_count"],
        "flagged_count": flagged_count,
        "live_skill_delta": report["live_skill_count"] - previous["live_skill_count"] if previous else None,
        "flagged_delta": flagged_count - previous["flagged_count"] if previous else None,
    }
    history.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=history, delete=False) as fh:
        tmp = Path(fh.name)
        try:
            json.dump(counts, fh, indent=2)
            fh.flush()
            os.replace(tmp, history / f"{week}.json")
        finally:
            tmp.unlink(missing_ok=True)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Count skill usage across the transcript corpus (ARM B evidence).")
    ap.add_argument("--corpus", action="append", default=[], help="Corpus dir or glob (repeatable). Default: VPS project dirs / $CORPUS.")
    ap.add_argument("--registry", action="append", default=[], help="Registered-skills root (repeatable). Default: ~/.claude/skills equivalent on VPS.")
    ap.add_argument("--window-days", type=int, default=45, help="Rolling window in days (default 45).")
    ap.add_argument("--flagged-count", type=int, help="Distinct skills judged dead by ARM B this run (including existing cards).")
    ap.add_argument("--history-dir", help="Weekly count snapshots; required with --flagged-count.")
    args = ap.parse_args(argv)
    if (args.flagged_count is None) != (args.history_dir is None):
        ap.error("--flagged-count and --history-dir must be supplied together")
    if args.flagged_count is not None and args.flagged_count < 0:
        ap.error("--flagged-count must be non-negative")

    corpus_roots = resolve_corpus_roots(args.corpus)
    registry_dirs = args.registry or DEFAULT_REGISTRY_DIRS
    if not corpus_roots:
        print(json.dumps({"error": "no corpus roots resolved", "tried": args.corpus or os.environ.get("CORPUS") or DEFAULT_CORPUS_GLOBS}), file=sys.stderr)
        # Still emit a valid (empty-usage) report so the agent step degrades
        # gracefully rather than crashing the weekly pass.
    report = build_report(corpus_roots, registry_dirs, args.window_days)
    if args.history_dir is not None:
        report["weekly_counts"] = record_weekly_counts(report, args.flagged_count, args.history_dir)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
