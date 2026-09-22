#!/usr/bin/env python3
"""SessionStart(startup) hook — re-inject the cross-session handoff after a daily
session rotation (board #1195).

The fleet rotates each dept to a FRESH session once per day to stop the on-disk
transcript .jsonl growing unbounded (auto-compaction bounds only the in-memory
context per turn, not the .jsonl; on --continue the whole file is reconstructed,
so a forever-session eventually overflows -> 'Prompt is too long' -> wedge).

To preserve context quality across that rotation, the OUTGOING session writes a
session-agnostic handoff to a FIXED path (~/.claude/handoff/latest.md). This hook
fires at the START of the fresh session and returns that handoff as
additionalContext so the new session picks the thread back up.

Why a separate hook from post-compact-restore.py: that one keys the handoff on
session_id and only fires on matcher:"compact" (survives compaction WITHIN a
session, not across a session boundary). This one is fixed-name + fires on
matcher:"startup"/"resume" — the cross-session case.

Reads SessionStart event JSON on stdin ({session_id, source, ...}); emits
additionalContext JSON; always exit 0 (never break session start).
"""
import sys
import json
import time
from pathlib import Path

# Don't inject a handoff older than this — a stale one (e.g. days old after a
# long outage) is more likely to mislead than help; better to start clean.
MAX_AGE_HOURS = 36.0


def main():
    try:
        json.load(sys.stdin)  # event consumed; we don't need its fields (fixed-name file)
    except Exception:
        pass  # be tolerant: still try to restore even if the event is malformed

    f = Path.home() / ".claude" / "handoff" / "latest.md"
    if not f.exists():
        return 0
    try:
        age_h = (time.time() - f.stat().st_mtime) / 3600.0
        if age_h > MAX_AGE_HOURS:
            return 0  # too old to trust — let the session start clean
        body = f.read_text(encoding="utf-8").strip()
    except Exception:
        return 0
    if not body:
        return 0

    ctx = ("This session was just started FRESH by the daily session-rotation "
           "(#1195), to keep the transcript from growing until it wedges. Below "
           "is the handoff your previous session wrote before it ended — use it "
           "to recover your working state (goals, in-flight work, decisions, next "
           "steps), then continue. Your durable memory (WORKING_MEMORY.md, the "
           "wiki) is unchanged and still authoritative.\n\n" + body)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
