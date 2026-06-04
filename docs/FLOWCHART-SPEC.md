# /health Carnet de bord — flowchart spec (from 2026-06-03 meeting transcript)

Source: raw transcript of the architecture meeting (Notion page 374cfc52…).
This is the canonical wishlist for the live org flowchart on `/health`.
Built incrementally; check items off as shipped.

## Goal
A **useful live visual** to: monitor the org in real time, understand the flow,
explain it to others, and improve on it. The visual must also **surface backend
bugs** (stale/missing layers, broken readers, wrong status) — and we fix those
as they surface.

## Wishlist ({{OPERATOR}}, verbatim intent)
- [x] Single interactive flowchart (replaces the static hiérarchie / 4-moments / rails sections)
- [x] Principal → Management (Tony) → Ops (Ben, Maya, CGP…), concierges beside, Mac-local tier
- [x] Directive (down) / KPI (up) edges between Principal↔Tony↔depts
- [x] Per-dept layer 1-2-3-4 status, red when stale
- [ ] **Live pulse must be correct** — "boucle silencieuse 2h/3j" looked wrong to {{OPERATOR}}; verify the heartbeat reading reflects reality, not a reader bug. Display must be live on each open.
- [ ] **Tiered status** ok / warn / alert (not everything-red). never-run ≠ stale.
- [ ] **Clickable edges** (Tony↔dept): clicking an arrow shows the relation — which file it writes, is it bidirectional, and **when it last happened** ("log visuel sur le flowchart").
- [ ] **Clickable per-dept layer arrows** 1-2-3-4: click → see what's inside (the layer output), last run time; red if never/over-due.
- [ ] **Two big rails AROUND the flowchart**: Sécurité + Wiki-compile, drawn as enclosing arrows/rails (belt + suspenders).
- [ ] **Concierges (Morty, Claudette)** with input/output arrows: which authorisations, last-used, yes/no/why.
- [ ] Mac-local agents (Rick, Tony-local, Miranda) — currently static nodes; wire heartbeats later (issue #15).

## Backend bugs surfaced by the flowchart (fix log)
- Layer 3 (Execution) has NEVER produced an output for ANY dept (no `outputs/*/3/.last-run` anywhere). Truthful in Phase 1 (human-approved: L3 only runs on an approved gate) — so "never" must render as idle/neutral, not red alarm. [under fix: tiered status + idle state]

## Backend bugs surfaced by the flowchart — full diagnosis (2026-06-04)

### BUG 1 — Maya: frozen date + parser blind spot (CONSOLE FIX SHIPPED + agent-config follow-up)
Root cause is threefold:
1. **Prompt freeze**: `<today>` in the loop heartbeat path is substituted by the AGENT from context, not computed. Maya's long-lived session froze the date at 2026-06-02 (re-derived only the clock). Files: `agents/bubble-ops-maya/CLAUDE.md` /loop protocol.
2. **Wrong harness**: Maya's `.claude/hooks/session-start.sh` is still the ONBOARDING hook (no "check your layers under outputs/" nudge), so unlike Ben she never re-observes the current-date folder. Fix: swap Maya onto Ben's operating hook.
3. **Console parser blind spot (FIXED here)**: `loop_backup.py::_ISO_RE` required a literal `Z`, so Maya's `...SS.ffffff+00:00` lines never matched → `latest_heartbeat_epoch` fell back to file mtime → FALSE-FRESH on the cockpit (masked the staleness). Fixed: widened regex + `fromisoformat`. The cockpit will now correctly show stale instead of hiding it.

### BUG 2 — Ben/Tony: loops DEAD, watchdog blind (INFRA FIX NEEDED — touches live agents)
- Ben & Tony are **dead, not wedged**: the `/loop` cadence needs ralph-loop's Stop hook + a session-scoped `.claude/ralph-loop.local.md`; that file doesn't exist for either → every turn the Stop hook exits without re-injecting a tick. Service stays `active` (process alive, idle).
- **fork-session disarm**: telegram-watchdog restarts with `--continue --fork-session` → new session id → ralph-loop's session-id guard disarms the loop. Nothing re-issues `/loop`.
- **Freshness watchdog is blind**: `scripts/loop-watchdog.sh` is HARDCODED to the prototype `fixture` dept (`HEARTBEAT=.../agents/fixture/...`) — never checks real depts. The watchdog that should've caught this is structurally blind. → make it per-dept.
- Recovery: re-issue `/loop 20m` in each live session (recreates the state file); fix the fork-session/loop interaction; render per-dept freshness watchdogs.
- The flowchart is currently the ONLY surface showing Ben/Tony loops are dead.
