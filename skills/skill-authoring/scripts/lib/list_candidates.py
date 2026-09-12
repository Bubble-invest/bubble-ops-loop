#!/usr/bin/env python3
"""list_candidates.py — ARM A evidence collector for the skill-authoring agent.

DOCTRINE + BOUNDARY (#1222, verified against cloud-wiki-compile STEP 4.6)
-------------------------------------------------------------------------------
wiki=knowledge, skills=know-how — complementary, NOT overlapping. The DETECTION
of skill-gap candidates is already done, agentically, by wiki-compile's weekly
STEP 4.6 "Skill-gap miner" (a Sonnet subagent that READS the transcript feed).
This skill is the DOWNSTREAM author/prune stage: it CONSUMES 4.6's candidate
file — it does NOT re-mine transcripts (that would duplicate 4.6, the exact
overlap Joris flagged).

STEP 4.6 writes, every Sunday, to (fixed VPS-local path, matching 4.6's SKILL):
    /home/claude/monitoring/skill-updates/{ISO_YEAR}-W{ISO_WEEK}-workaround-candidates.md
e.g. 2026-W29-workaround-candidates.md. Each surviving candidate is a block:
    ### <N>. <short title> — <signal class: MISSING|BROKEN|EXTEND|FRICTION>
    **Evidence:** "<verbatim quote>" — <agent>, <session>
    **Why it's a gap:** ...
    **Recurrence:** seen in <N> distinct session(s)/agent(s)
    **Proposed action:** new-skill | fix-existing | extend-existing — ...
    **Board check:** ...

This module MECHANICALLY locates that file and splits it into candidate blocks
(structure only — no judgment about which are real; that stays with the agent),
and enumerates the existing skill registry so the agent can dedupe (ARM A step 2:
"overlap ⇒ extend skill X, not a new skill"). Pure evidence → JSON.

Import-safe (pure helpers + argparse main) for unit testing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CANDIDATES_DIR = "/home/claude/monitoring/skill-updates"
DEFAULT_REGISTRY_DIRS = ["/home/claude/.claude/skills"]

# 4.6 candidate blocks start with a markdown H3 heading. Structure, not meaning.
BLOCK_HEADING_RE = re.compile(r"^###\s+", re.MULTILINE)


def iso_week_stamp(now: datetime | None = None) -> str:
    """Return '{ISO_YEAR}-W{ISO_WEEK:02d}' matching 4.6's `date -u +%G-W%V`."""
    now = now or datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def candidates_filename(week_stamp: str) -> str:
    return f"{week_stamp}-workaround-candidates.md"


def locate_candidates_file(candidates_dir: str, week_stamp: str) -> tuple[Path | None, str]:
    """Find THIS week's 4.6 candidates file. If the exact-week file is absent,
    fall back to the most recent *-workaround-candidates.md in the dir and say
    so (a note the agent surfaces — never silently mine something else).
    Returns (path_or_None, status_note)."""
    d = Path(candidates_dir)
    exact = d / candidates_filename(week_stamp)
    if exact.is_file():
        return exact, f"found this-week candidates file ({exact.name})"
    if not d.is_dir():
        return None, f"candidates dir {candidates_dir} does not exist — 4.6 has not run here / wrong host"
    others = sorted(d.glob("*-workaround-candidates.md"))
    if not others:
        return None, f"no *-workaround-candidates.md in {candidates_dir} — 4.6 produced nothing"
    latest = others[-1]
    return latest, (
        f"this-week file ({exact.name}) ABSENT — falling back to most recent "
        f"({latest.name}); note the staleness in your report and do NOT re-author "
        f"candidates already carded from an older week"
    )


def split_candidate_blocks(text: str) -> list[str]:
    """Split the report body into per-candidate blocks by H3 heading. Mechanical
    structure only. Preamble before the first heading is dropped. Empty/`no
    candidates` reports yield []."""
    if not text.strip():
        return []
    # Split on each '### ' heading; parts[0] is the preamble before the first
    # heading (dropped). Reattach the stripped delimiter to each real block.
    parts = BLOCK_HEADING_RE.split(text)
    return [("### " + body).strip() for body in parts[1:]]


def skill_description(skill_md: Path) -> str:
    """Extract a short description for dedupe context: the YAML frontmatter
    `description:` if present, else the first non-empty prose line. Best-effort,
    truncated. This is CONTEXT for the agent's dedupe judgment, not a decision."""
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return ""
    # frontmatter description
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")[:400]
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("---") and not s.startswith("#") and ":" not in s[:20]:
            return s[:400]
    return ""


def enumerate_registry(registry_dirs: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for d in registry_dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for sub in sorted(base.iterdir()):
            md = sub / "SKILL.md"
            if sub.is_dir() and md.is_file() and sub.name not in seen:
                seen.add(sub.name)
                out.append({
                    "name": sub.name,
                    "path": str(md),
                    "description": skill_description(md),
                })
    return out


def build_report(
    candidates_dir: str,
    registry_dirs: list[str],
    week_stamp: str | None = None,
) -> dict:
    week = week_stamp or iso_week_stamp()
    path, note = locate_candidates_file(candidates_dir, week)
    raw = ""
    blocks: list[str] = []
    if path is not None:
        try:
            raw = path.read_text(errors="replace")
        except OSError as e:
            note += f" (but read failed: {e})"
        blocks = split_candidate_blocks(raw)
    return {
        "week": week,
        "candidates_file": str(path) if path else None,
        "status": note,
        "found": path is not None,
        "candidate_count": len(blocks),
        "candidate_blocks": blocks,
        "raw": raw,
        "registered_skills": enumerate_registry(registry_dirs),
        "boundary_note": (
            "These candidates were DETECTED by wiki-compile STEP 4.6 (agentic "
            "read). This stage CONSUMES them — do NOT re-mine transcripts. Dedupe "
            "each candidate against registered_skills: overlap ⇒ propose EXTEND, "
            "not a new skill."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Locate + structure this week's 4.6 skill-gap candidates (ARM A evidence).")
    ap.add_argument("--candidates-dir", default=DEFAULT_CANDIDATES_DIR, help="Dir where 4.6 writes *-workaround-candidates.md.")
    ap.add_argument("--registry", action="append", default=[], help="Registered-skills root (repeatable) for dedupe context.")
    ap.add_argument("--week", default=None, help="ISO week stamp e.g. 2026-W29 (default: current UTC week).")
    args = ap.parse_args(argv)
    report = build_report(args.candidates_dir, args.registry or DEFAULT_REGISTRY_DIRS, args.week)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
