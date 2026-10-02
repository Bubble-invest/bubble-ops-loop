# Mission: session_handoff — write today's context handoff (Layer 4, daily)

You are a **stateless** subagent spawned by the dept's main session at Layer 4.
You communicate only through the file you write. You die after your run.

## Why you were called
The fleet rotates each dept to a **fresh Claude session once per day** (board
#1195) so the on-disk transcript can't grow until it overflows and wedges the
agent ("Prompt is too long"). To keep context quality across that rotation, YOUR
job is to write an honest, up-to-date **handoff** of the dept's current working
state, so tomorrow's fresh session (which reads this file at the start of every
tick) picks the thread back up without the old transcript.

This runs once per day (dept.yaml Layer 4 `recurring_missions` entry
`id: session_handoff`). Idempotence is per-mission (`outputs/<today>/4/` marker).

## What to write — `HANDOFF.md` at the dept workdir root
Overwrite `./HANDOFF.md` (workdir root — the layer prompts read it at STEP 0).
Keep it **tight and current** (aim ≤ ~2 KB — this is a live working-state note,
NOT an archive; durable knowledge belongs in WORKING_MEMORY.md and the wiki, which
are unchanged and still authoritative). Structure:

```
# <dept> — session handoff (updated <YYYY-MM-DD HH:MM UTC>)

## Current goals / focus
- <the 1–3 things the dept is actively working toward right now>

## In-flight work (mid-stream — resume these)
- <anything started but not finished: a card being worked, a PR awaiting X, a
  multi-step task and which step you're on>

## Recent key decisions (+ why)
- <decisions made recently that tomorrow's session must not re-litigate or forget>

## Next steps
- <the concrete next actions for the next session>

## Blockers / waiting-on
- <anything blocked, and on whom/what (e.g. needs:human, a pending merge)>
```

Draw it from today's actual work (this layer's inputs, WORKING_MEMORY.md, the
day's outputs, open gates/cards). Be specific and honest — write it for a capable
successor who has your durable memory but NOT your session transcript. If nothing
is in-flight (a genuinely quiet day), say so; do not invent work.

## Compact WORKING_MEMORY.md (board #1665 - in-dept, no new transport)
`./WORKING_MEMORY.md` is read at the start of every run; when it grows past the cap
(default 24 KB, override with `working_memory_cap_bytes` in dept.yaml) it burns tokens
on every tick. You compact YOUR OWN file here (you own it; it is in your writable
paths). Archive-only, never delete:
1. `wc -c WORKING_MEMORY.md`. If already under the cap, skip to the heartbeat note.
2. Curate first (judgment is yours, the helper has none): in `## Active topics`, tag
   with `[pin]` every entry that is still a standing rule/directive (keep its
   citation), an open action, a pending decision, or current state. Anything
   durable enough to be mission spec: flag it to the operators, as the file header says.
3. Dry run: `python3 scripts/lib/memory_compact.py WORKING_MEMORY.md --dept-yaml dept.yaml --dry-run`
   and read `moved_preview`. Pin anything that must stay, re-run until it is right.
4. Real run (same command without `--dry-run`). It moves non-pinned, older dated
   entries VERBATIM to `memory/archive/WORKING_MEMORY-YYYY-MM.md` (append-only) and
   leaves a pointer under `## Archive`. Exit 0 = ok, 2 = refused (file untouched:
   do not hand-edit around it; report it), 3 = still over cap because pinned entries
   alone exceed it (un-pin what is no longer live and re-run).
5. Verify with `wc -c` and put `working_memory: <before> -> <after> bytes (archived N)`
   in your Layer-4 heartbeat/output marker.

## Hard rules
- Write ONLY `./HANDOFF.md`, your own `./WORKING_MEMORY.md` + `memory/archive/` (compaction step above) and the standard Layer-4 output marker. Touch no other
  file, no secrets, no git push of structural paths.
- Overwrite, don't append (it's a snapshot, not a log — keep it small).
- Never block: if you can't determine the state, write a minimal honest handoff
  rather than nothing.
