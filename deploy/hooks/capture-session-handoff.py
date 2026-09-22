#!/usr/bin/env python3
"""capture-session-handoff.py — write a session-agnostic handoff before a daily
session rotation (board #1195).

NOT a hook — a standalone script the rotation orchestrator runs (as the dept uid)
just before it rotates the dept to a fresh session. It captures the CURRENT
session's working state to a FIXED path:
  ~/.claude/handoff/latest.md
which post-startup-restore.py re-injects into the fresh session.

Handoff content, best → fallback:
  1. If the agent proactively wrote ~/.claude/handoff/note.md recently (the
     rotation orchestrator injects "write your handoff now" first), use it — the
     agent's own working-state note is the richest, most accurate handoff.
  2. Otherwise, deterministically extract from the newest session transcript
     (recent user asks, last assistant note, files touched) — same logic as
     pre-compact-handoff.py.

FAIL-SAFE CONTRACT: exit 0 ONLY if a non-trivial handoff was written to latest.md;
exit 1 otherwise. The orchestrator MUST NOT rotate the session unless this exits 0
(never drop the session's context to a failed/empty handoff).

Usage: capture-session-handoff.py [--note-max-age-sec 1800]
"""
import sys
import json
import glob
import os
import time
import argparse
from pathlib import Path


def _newest_transcript() -> str | None:
    fs = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl"))
    if not fs:
        return None
    return max(fs, key=os.path.getmtime)


def _extract(tpath: str):
    users, assistant_last, files = [], "", []
    try:
        with open(tpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                c = m.get("content")
                if role == "user":
                    if isinstance(c, str):
                        txt = c
                    elif isinstance(c, list):
                        txt = " ".join(b.get("text", "") for b in c
                                       if isinstance(b, dict) and b.get("type") == "text")
                    else:
                        txt = ""
                    txt = " ".join(txt.split())
                    if txt and not txt.startswith("<") and "tool_result" not in txt[:40]:
                        users.append(txt[:400])
                elif role == "assistant" and isinstance(c, list):
                    parts = []
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text", "").strip():
                            parts.append(b["text"].strip())
                        if b.get("type") == "tool_use":
                            inp = b.get("input", {}) or {}
                            p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                            if p:
                                files.append(str(p))
                    if parts:
                        assistant_last = " ".join(parts)[:800]
    except Exception:
        pass

    def tail_uniq(xs, n):
        seen, res = set(), []
        for x in reversed(xs):
            if x in seen:
                continue
            seen.add(x)
            res.append(x)
            if len(res) >= n:
                break
        return list(reversed(res))

    return tail_uniq(users, 12), assistant_last, tail_uniq(files, 15)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--note-max-age-sec", type=int, default=1800)
    args = ap.parse_args()

    hd = Path.home() / ".claude" / "handoff"
    hd.mkdir(parents=True, exist_ok=True)
    out = hd / "latest.md"
    note = hd / "note.md"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    header = (f"# Session handoff (daily rotation, #1195) — {ts}\n"
              "_Written by your previous session before the daily fresh-session "
              "rotation. Re-injected at the fresh session's start._\n")

    body_parts = []
    used_note = False
    try:
        if note.exists() and (time.time() - note.stat().st_mtime) < args.note_max_age_sec:
            nb = note.read_text(encoding="utf-8").strip()
            if nb:
                body_parts.append("## Your own handoff note (written just now)\n" + nb)
                used_note = True
                note.unlink()  # consumed
    except Exception:
        pass

    # Always also include the deterministic extract (belt-and-suspenders context).
    tpath = _newest_transcript()
    det_ok = False
    if tpath:
        users, assistant_last, files = _extract(tpath)
        det = ["## Recent user asks (most recent last)"]
        det += [f"- {u}" for u in users] or ["- (none captured)"]
        if assistant_last:
            det += ["", "## Where you left off (last assistant note)", assistant_last]
        if files:
            det += ["", "## Files touched this session"] + [f"- `{p}`" for p in files]
        # "non-trivial" = at least one real user ask or a last-assistant note
        det_ok = bool(users or assistant_last)
        body_parts.append("\n".join(det))

    # FAIL-SAFE: only succeed if we captured something real (agent note OR a
    # non-trivial deterministic extract). Otherwise leave latest.md untouched and
    # signal the orchestrator to skip the rotation.
    if not (used_note or det_ok):
        sys.stderr.write("capture-session-handoff: no non-trivial handoff captured — "
                         "NOT writing latest.md; rotation should be skipped\n")
        return 1

    try:
        out.write_text(header + "\n" + "\n\n".join(body_parts) + "\n", encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"capture-session-handoff: failed to write {out}: {e}\n")
        return 1
    sys.stderr.write(f"capture-session-handoff: wrote {out} "
                     f"(note={'yes' if used_note else 'no'}, det={'yes' if det_ok else 'no'})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
