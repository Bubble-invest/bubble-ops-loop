#!/usr/bin/env python3
"""memory_hygiene_notify.py — detect cluttered agent memory, nudge the OWNING agent.

DOCTRINE (Joris, 2026-06-19; reaffirmed #1223, 2026-09-12)
---------------------------------------------------------
The wiki-compile job prunes the SHARED wiki, but nothing grooms each agent's
PRIVATE memory:
  - MEMORY.md          — the private memory INDEX + its reference files.
  - WORKING_MEMORY.md  — the agent's live scratch/working state (grows unbounded).
A central pruner would be wrong: only the owning agent knows which entries are
still load-bearing vs. truly stale, so a blind length-cap would silently delete
memory the agent relies on ("agentic not deterministic", #103).

So this job DETECTS (cheap, mechanical evidence) and NUDGES (the agent grooms its
own memory — kept "in the consciousness flow"). It NEVER edits private memory and
NEVER hard-deletes anything: the nudge asks the agent to ARCHIVE (move-only) stale
material, never delete it.

Split of responsibility (the #103 line):
  - cron / this script  -> COLLECT EVIDENCE: size vs budget, over-long lines,
                           dup slugs, file staleness (mtime), line count.
  - the agent (on nudge) -> JUDGE & ACT: re-read, dedupe, archive-move, trim.

COVERAGE (#1223)
----------------
Two private stores are watched fleet-wide, each against its LIVE file:
  1. MEMORY.md         — the index (24 KB budget; over-long lines; dup slugs).
  2. WORKING_MEMORY.md — the working scratchpad (larger soft budget; also a
                         staleness trigger, since abandoned scratch is the
                         common bloat mode). This closed a ZERO-coverage gap:
                         nothing scanned WORKING_MEMORY.md, so agents' working
                         memory grew unbounded and had to be cleaned by hand.

DELIVERY (the hard part — same-machine constraint)
--------------------------------------------------
A nudge must land in the AGENT'S SESSION, not in a human's chat. A Telegram bot
`sendMessage` reaches the human, NOT the agent (Telegram protocol — see the
`telegram-message-A2A` skill). The only path that reaches a running --channels
session is **bubble-inject**: write the agent's inject file on the machine it
runs on. So we route per agent:
  - VPS depts (tony/ben/maya/accountant)  -> write inject on the VPS.
  - Mac-resident agents (rnd/claudette/security/content) -> per-Mac outbox that
    the Mac's own sync run drains and injects locally (trust arrow = laptop->cloud).
An agent with no live inject target (no running session) is skipped with a note;
its clutter will be caught on a later run once it's up.

This runs on the VPS inside the weekly cloud-wiki-compile@pruning cron.

Idempotency + closed loop: a per-(agent,kind) stamp suppresses re-nudging within
`NUDGE_COOLDOWN_DAYS`, and records the size at nudge time. On a later eligible
run, if the file is STILL cluttered and has NOT shrunk since the last nudge, the
new nudge is ESCALATED ("nudged Nd ago, hasn't shrunk") — the feedback signal the
job previously lacked.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# --- tunables: MEMORY.md (the index) --------------------------------------
SIZE_BUDGET_BYTES = 24576        # ~24KB — matches the MEMORY.md loader truncation warning
INDEX_LINE_MAX = 200             # index entries longer than this get truncated by the loader
# A nudge fires only on a PRIMARY trigger (real clutter). Secondary signals
# (long lines / dups) are reported to help the agent, but a couple of them in an
# otherwise-small, healthy index must NOT trigger a nudge — only egregious counts do.
LONG_LINES_TRIGGER = 20          # this many over-long index entries is itself clutter
DUP_SLUG_TRIGGER = 3             # this many duplicate slugs is itself clutter

# --- tunables: WORKING_MEMORY.md (the scratchpad) -------------------------
# Working memory is a scratch/working-state file, legitimately larger than the
# index — but it is the file that grows unbounded (ben/maya/tony seen at
# 170-305KB, 7-12x the index budget) because completed scope is never archived.
# The soft budget is generous; the staleness trigger catches abandoned scratch
# that is under budget but clearly no longer active.
WM_SIZE_BUDGET_BYTES = 65536     # ~64KB soft budget for the working scratchpad
WM_STALE_DAYS = 45               # untouched this long => likely abandoned scope
WM_STALE_MIN_BYTES = 16384       # ...but only nag if it's non-trivial (>16KB)

NUDGE_COOLDOWN_DAYS = 6          # don't re-nudge the same (agent,kind) within this window
STAMP_DIR = Path("/home/claude/.claude/agent-memory/.memory-hygiene-stamps")

# --- WHERE THIS RUNS: on the VPS, inside the weekly pruning cron --------------
# Compile/maintenance is VPS-only (Joris confirmed 2026-06-19). The canonical
# private memories live on the two Macs, so mac-transcript-sync.sh pushes each
# Mac's ~/.claude/agent-memory/ up to a per-Mac cache here. We scan those caches
# PLUS the VPS-native agent-memory, then route a grooming nudge to the live session.
SCAN_BASES = [
    Path("/home/claude/.claude/projects/_mac-joris-agent-memory"),
    Path("/home/claude/.claude/projects/_mac-jade-agent-memory"),
    Path("/home/claude/.claude/agent-memory"),  # VPS-native (claudette/morty/rnd)
]

# --- VPS dept live memory location (fix, #874, 2026-08-12) ------------------
# VPS-native depts (ben/maya/tony/accountant) migrated their canonical MEMORY.md
# to ~/.claude/projects/-home-claude-agents-bubble-ops-<dept>/memory/MEMORY.md.
# SCAN_BASES above never learned this — for these depts it either finds
# nothing (path doesn't exist under agent-memory/<dept>/ anymore) or falls
# back to a frozen per-Mac transcript cache that predates the migration.
# This location is authoritative when present: see canonical_memories().
VPS_PROJECTS_DIR = Path("/home/claude/.claude/projects")
VPS_DEPT_PREFIX = "-home-claude-agents-bubble-ops-"
VPS_DEPT_ALIAS = {"tony": "main-strategist"}  # dir slug -> ROUTING/report key

# --- VPS dept LIVE working-memory location (#1223) --------------------------
# WORKING_MEMORY.md lives at the agent's workspace ROOT, not under memory/. On
# the VPS the LIVE homes are /srv/agents/<slug>/ (post-#1120 uid isolation).
# There are ALSO pre-migration mirrors under /home/claude/agents/bubble-ops-<slug>/
# that are DAYS STALE (the exact frozen-mirror trap that bit #874) — we do NOT
# scan those; we only trust the live /srv/agents homes and, among candidates for
# one agent, the NEWEST mtime wins (never "largest", which is how #874 went wrong).
WM_SCAN_BASES = [
    Path("/srv/agents"),
]
# Normalize a workspace dir name to a ROUTING/report key.
WM_DIR_ALIAS = {"tony": "main-strategist"}


def _norm_slug(name: str) -> str | None:
    """Workspace dir name -> ROUTING/report key, or None to skip."""
    if name.startswith(".") or name.startswith("_retired"):
        return None
    slug = name[len("bubble-ops-"):] if name.startswith("bubble-ops-") else name
    return WM_DIR_ALIAS.get(slug, slug)

# --- agent -> live-session delivery routing (FROM THE VPS) -------------------
# The trust arrow is laptop -> cloud ONLY (the VPS must not hold SSH login rights
# INTO a laptop). So the VPS canNOT inject into a Mac agent's session directly.
# Therefore:
#   - VPS-native depts -> inject LOCALLY (write the inject file here).
#   - Mac-resident agents -> drop the nudge in a per-Mac OUTBOX here; the Mac's
#     own mac-transcript-sync run (laptop->cloud) PULLS its outbox and injects
#     locally on the Mac. Respects the trust arrow; reuses the existing channel.
# value for "local" routes = absolute inject path on the VPS.
# value for "*-mac" routes = the channel slug to inject on that Mac.
MAC_OUTBOX = Path("/home/claude/.claude/projects/_memory-nudge-outbox")

ROUTING = {
    # memory-dir name -> (host, inject_path_on_vps | channel_slug_on_mac)
    "ben":             ("local",     "/home/claude/.claude/channels/telegram-ben/inject"),
    "maya":            ("local",     "/home/claude/.claude/channels/telegram-maya/inject"),
    "main-strategist": ("local",     "/home/claude/.claude/channels/telegram-tony/inject"),
    "accountant":      ("local",     "/home/claude/.claude/channels/telegram-accountant/inject"),
    "content":         ("jade-mac",  "telegram-socials"),
    "rnd":             ("joris-mac", "telegram-rnd"),
    "claudette":       ("joris-mac", "telegram-claudette"),
    "security":        ("joris-mac", "telegram-security"),
}


# ---------------------------------------------------------------------------
# EVIDENCE COLLECTION (mechanical only — no judgment about what to keep)
# ---------------------------------------------------------------------------
def analyze_index(memory_md: Path) -> dict:
    """Mechanical clutter signals for one MEMORY.md index."""
    text = memory_md.read_text(encoding="utf-8", errors="replace")
    size = len(text.encode("utf-8"))
    lines = text.splitlines()
    long_lines = [l for l in lines if len(l) > INDEX_LINE_MAX]
    # index entries look like:  - [Title](slug.md) — hook
    slugs = re.findall(r"\]\(([^)]+?\.md)\)", text)
    seen, dups = set(), set()
    for s in slugs:
        (dups if s in seen else seen).add(s)
    # PRIMARY triggers — any one of these means the index genuinely needs grooming.
    triggers = []
    if size > SIZE_BUDGET_BYTES:
        triggers.append(f"index is {size//1024}KB (budget ~{SIZE_BUDGET_BYTES//1024}KB) — "
                        f"entries past the budget stop loading")
    if len(long_lines) >= LONG_LINES_TRIGGER:
        triggers.append(f"{len(long_lines)} index entries exceed {INDEX_LINE_MAX} chars "
                        f"(should be one short line each)")
    if len(dups) >= DUP_SLUG_TRIGGER:
        triggers.append(f"{len(dups)} duplicate slug(s) in the index: "
                        f"{', '.join(sorted(dups)[:5])}")

    cluttered = bool(triggers)
    # Once cluttered, fold in secondary detail so the agent has the full picture,
    # even if a given signal was below its own standalone trigger threshold.
    reasons = list(triggers)
    if cluttered:
        if 0 < len(long_lines) < LONG_LINES_TRIGGER:
            reasons.append(f"(also: {len(long_lines)} over-long index entries to shorten)")
        if 0 < len(dups) < DUP_SLUG_TRIGGER:
            reasons.append(f"(also: {len(dups)} duplicate slug(s): {', '.join(sorted(dups)[:5])})")
    return {"size": size, "reasons": reasons, "cluttered": cluttered}


def analyze_working(wm_md: Path) -> dict:
    """Mechanical bloat/staleness signals for one WORKING_MEMORY.md scratchpad."""
    text = wm_md.read_text(encoding="utf-8", errors="replace")
    size = len(text.encode("utf-8"))
    n_lines = len(text.splitlines())
    age_days = (time.time() - wm_md.stat().st_mtime) / 86400.0

    triggers = []
    if size > WM_SIZE_BUDGET_BYTES:
        triggers.append(
            f"working memory is {size//1024}KB (soft budget ~{WM_SIZE_BUDGET_BYTES//1024}KB, "
            f"{n_lines} lines) — likely holds completed/stale scope to archive")
    if age_days >= WM_STALE_DAYS and size >= WM_STALE_MIN_BYTES:
        triggers.append(
            f"not updated in {int(age_days)}d ({size//1024}KB) — likely abandoned scratch "
            f"to archive")

    cluttered = bool(triggers)
    reasons = list(triggers)
    # Secondary context once cluttered: give the agent the age even if size fired.
    if cluttered and age_days >= 14 and f"not updated" not in " ".join(reasons):
        reasons.append(f"(also: last touched {int(age_days)}d ago)")
    return {"size": size, "reasons": reasons, "cluttered": cluttered}


# ---------------------------------------------------------------------------
# STAMPS + CLOSED-LOOP FEEDBACK
# ---------------------------------------------------------------------------
def _stamp_path(key: str) -> Path:
    return STAMP_DIR / f"{key}.json"


def read_stamp(key: str) -> dict | None:
    stamp = _stamp_path(key)
    if not stamp.exists():
        return None
    try:
        return json.loads(stamp.read_text())
    except Exception:
        return None


def recently_nudged(key: str) -> bool:
    st = read_stamp(key)
    if not st:
        return False
    return (time.time() - st.get("ts", 0)) < NUDGE_COOLDOWN_DAYS * 86400


def write_stamp(key: str, size: int) -> None:
    STAMP_DIR.mkdir(parents=True, exist_ok=True)
    _stamp_path(key).write_text(
        json.dumps({"ts": time.time(), "size": size}), encoding="utf-8")


def escalation_note(key: str, cur_size: int) -> str:
    """If the last nudge didn't lead to a shrink, say so (closed-loop signal)."""
    st = read_stamp(key)
    if not st:
        return ""
    prev = st.get("size", 0)
    days = (time.time() - st.get("ts", 0)) / 86400.0
    if prev and cur_size >= prev:
        return (f"⚠️ You were nudged ~{int(days)}d ago and this hasn't shrunk "
                f"(was {prev//1024}KB, still {cur_size//1024}KB) — please prioritize. ")
    return ""


# ---------------------------------------------------------------------------
# NUDGE TEXT
# ---------------------------------------------------------------------------
def build_index_nudge(agent: str, info: dict, path: Path, escalate: str) -> str:
    bullets = "\n".join(f"  - {r}" for r in info["reasons"])
    return (
        f"🧹 Memory-hygiene nudge (automated, weekly). {escalate}Your private memory index "
        f"({path}) looks cluttered:\n{bullets}\n"
        f"Please groom it when convenient: re-read your index, shorten over-long "
        f"entries to one line, dedupe repeated slugs, and archive (move, don't delete) "
        f"reference files that are stale. Keep load-bearing memories — only YOU know which "
        f"those are, which is why this is a nudge, not an automatic edit. "
        f"(Source: memory-hygiene job. No action needed from a human.)"
    )


def build_working_nudge(agent: str, info: dict, path: Path, escalate: str) -> str:
    bullets = "\n".join(f"  - {r}" for r in info["reasons"])
    return (
        f"🧹 Working-memory hygiene nudge (automated, weekly). {escalate}Your working "
        f"scratchpad ({path}) looks bloated/stale:\n{bullets}\n"
        f"Please groom it when convenient: keep only ACTIVE, in-flight state; ARCHIVE "
        f"completed or stale sections by MOVING them into a WORKING_MEMORY.archive.md "
        f"beside it (move, never delete — nothing is thrown away). Only YOU know what is "
        f"still load-bearing, which is why this is a nudge, not an automatic edit. "
        f"(Source: memory-hygiene job. No action needed from a human.)"
    )


# ---------------------------------------------------------------------------
# DELIVERY
# ---------------------------------------------------------------------------
def deliver(agent: str, msg: str, dry_run: bool) -> tuple[bool, str]:
    route = ROUTING.get(agent)
    if not route:
        return False, "no routing entry (unknown live session)"
    host, target = route
    one_line = msg.replace("\n", " ⏎ ")  # one inbound turn = one line

    if dry_run:
        where = f"local inject {target}" if host == "local" else f"{host} outbox ({target})"
        return True, f"DRY-RUN -> {where}"

    if host == "local":
        # VPS-native dept: inject straight into its live session.
        try:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(one_line + "\n")
            return True, f"injected (local {target})"
        except Exception as e:
            return False, f"local inject failed: {e}"

    # Mac-resident agent: drop into the per-Mac outbox. The Mac's own sync run
    # pulls it and injects locally (trust arrow = laptop->cloud only). One JSON
    # file per nudge; channel slug + line. mac-transcript-sync drains it.
    mac = "joris" if host == "joris-mac" else "jade"
    outbox = MAC_OUTBOX / mac
    try:
        outbox.mkdir(parents=True, exist_ok=True)
        fn = outbox / f"{agent}-{int(time.time())}.json"
        fn.write_text(json.dumps({"channel": target, "line": one_line}), encoding="utf-8")
        return True, f"queued in {mac} outbox ({target}); Mac sync will inject"
    except Exception as e:
        return False, f"outbox write failed: {e}"


# ---------------------------------------------------------------------------
# LIVE-FILE RESOLUTION (live path wins; never a frozen/stale mirror — #874)
# ---------------------------------------------------------------------------
def canonical_memories() -> dict[str, Path]:
    """Pick each agent's canonical MEMORY.md.

    VPS-native depts' live memory now lives under VPS_PROJECTS_DIR (see fix
    note above) — when that path exists for a dept it is authoritative and
    wins outright, with NO size comparison against SCAN_BASES. Bug fixed here
    (#874): the old code picked the *largest* MEMORY.md across all bases as
    "canonical", so a frozen, stale, pre-migration Mac cache (bigger, because
    it was never pruned) silently outranked a smaller, healthy, live index.
    A live path must never lose to a frozen cache regardless of size.

    SCAN_BASES remains the source for agents that haven't migrated (Mac-
    resident rnd/claudette/security/content) and as a fallback for any VPS
    dept without a live path yet.
    """
    best: dict[str, Path] = {}
    live: set[str] = set()
    if VPS_PROJECTS_DIR.is_dir():
        for proj_dir in sorted(VPS_PROJECTS_DIR.glob(f"{VPS_DEPT_PREFIX}*/")):
            slug = proj_dir.name[len(VPS_DEPT_PREFIX):]
            md = proj_dir / "memory" / "MEMORY.md"
            if md.exists():
                agent = VPS_DEPT_ALIAS.get(slug, slug)
                best[agent] = md
                live.add(agent)

    for base in SCAN_BASES:
        if not base.is_dir():
            continue
        for mem_dir in sorted(base.glob("*/")):
            agent = mem_dir.name
            if agent.startswith(".") or agent.startswith("shared-wiki"):
                continue
            if agent in live:
                continue  # live VPS path already authoritative for this dept
            md = mem_dir / "MEMORY.md"
            if not md.exists():
                continue
            cur = best.get(agent)
            if cur is None or md.stat().st_size > cur.stat().st_size:
                best[agent] = md
    return best


def working_memories() -> dict[str, Path]:
    """Pick each agent's canonical WORKING_MEMORY.md.

    Scans ONLY the live agent homes (WM_SCAN_BASES = /srv/agents). The stale
    pre-migration mirrors under /home/claude/agents/bubble-ops-<slug>/ are
    deliberately NOT scanned — they are days behind the live home and would
    reproduce the #874 frozen-cache mis-measurement for working memory.
    Among multiple candidate dirs that normalize to the same agent (e.g.
    /srv/agents/ben and /srv/agents/bubble-ops-ben), the NEWEST mtime wins —
    never "largest" (that is exactly how #874 went wrong: size != freshness).
    """
    best: dict[str, Path] = {}
    for base in WM_SCAN_BASES:
        if not base.is_dir():
            continue
        for wm in sorted(base.glob("*/WORKING_MEMORY.md")):
            agent = _norm_slug(wm.parent.name)
            if agent is None:
                continue
            cur = best.get(agent)
            if cur is None or wm.stat().st_mtime > cur.stat().st_mtime:
                best[agent] = wm
    return best


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def _run_pass(kind: str, files: dict[str, Path], analyze, build_nudge,
              dry_run: bool, report: list[str]) -> None:
    label = "MEMORY.md" if kind == "index" else "WORKING_MEMORY.md"
    report.append(f"{label} scan:")
    if not files:
        report.append("  (no live files found)")
        return
    for agent, path in sorted(files.items()):
        info = analyze(path)
        if not info["cluttered"]:
            report.append(f"  {agent:16} ok ({info['size']//1024}KB)")
            continue
        stamp_key = agent if kind == "index" else f"{agent}.working"
        if recently_nudged(stamp_key) and not dry_run:
            report.append(f"  {agent:16} CLUTTERED but nudged <{NUDGE_COOLDOWN_DAYS}d ago — skip")
            continue
        escalate = escalation_note(stamp_key, info["size"])
        ok, detail = deliver(agent, build_nudge(agent, info, path, escalate), dry_run)
        if ok and not dry_run:
            write_stamp(stamp_key, info["size"])
        tag = "CLUTTERED" + (" [escalated]" if escalate else "")
        report.append(f"  {agent:16} {tag} ({info['size']//1024}KB) -> {detail}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    report: list[str] = []
    _run_pass("index", canonical_memories(), analyze_index, build_index_nudge,
              dry_run, report)
    report.append("")
    _run_pass("working", working_memories(), analyze_working, build_working_nudge,
              dry_run, report)
    print("memory-hygiene scan:")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
