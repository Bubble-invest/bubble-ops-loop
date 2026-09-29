"""cockpit_health — evidence collector + first-pass judge for cockpit/dashboard
bugs (board card origin: Joris tg 10023-10029, "is there a mission to detect
those bugs (cockpit /cost page broken, Ben exposure page stale again)? how do
we make it so you find and fix them on your own?").

Design: **tools as evidence, agent as judgment**
(~/.claude/agent-memory/shared-wiki/shared/systems/cron-judgment-vs-tools.md,
Joris msg 1794; reaffirmed board #1222 "we are agentic not deterministic").

  collect.py — deterministic evidence collector. Renders each cockpit page
    in-process (the console's own TestClient auth path, no new bypass) and
    runs independent source-of-truth comparisons. NEVER emits a verdict.
  checks.py  — the pluggable per-page evidence checks collect.py runs.
  judge.py   — reads the evidence, asks hosted Jev (OpenRouter typesafe/
    jev-1.13, via system-one-decisions' jev.py client) a bounded first-pass
    question, and combines that with the collector's own hard-inconsistency
    flags under an ASYMMETRIC rule: Jev may only escalate ok -> suspicious,
    never downgrade a collector-flagged hard inconsistency back to ok.
    Shadow mode (default ON) logs verdicts without touching the real board,
    per system-one-decisions/SKILL.md's "Mandatory rollout discipline".

Rick's own per-tick review of what this framework surfaces, and the weekly
deep walk, live in bubble-rnd-workspace/missions/cockpit-health.md — this
package is the framework only, not the mission doctrine.
"""
