# 🫧 Bubble Ops Loop — Agentic 4-Layer OODA Framework

**The open-source engine that powers autonomous AI departments with mandatory human gates.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Built on Claude Code](https://img.shields.io/badge/built%20on-Claude%20Code-orange)](https://claude.ai)
[![Tests](https://img.shields.io/badge/tests-270%20passed-green)]()

---

## What is bubble-ops-loop?

A framework for creating and running **autonomous AI departments** — each with its own mandate, missions, skills, and 4-layer OODA loop. Every execute-level action goes through a human approval gate. The agent proposes, you decide, it executes.

**Live in production** since May 2026, managing a real family office across 4 brokers, real LinkedIn prospecting, real risk monitoring.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                 /loop (every 20 min)              │
│                                                   │
│  STEP A: git pull                                 │
│  STEP B: read queues                              │
│  STEP C: decide_dispatch (L1/L2/L3/L4/heartbeat)  │
│  STEP D: spawn layer subagent                     │
│  STEP E: commit + push                            │
│  STEP F: Telegram notify                          │
└──────────────────────────────────────────────────┘
         │
    ┌────┼────┬────┐
    ▼    ▼    ▼    ▼
  L1    L2   L3   L4
Observe Research Execute Debrief
(07:00)(12:00)(16:00)(19:00)
```

**Layer 1 — Observe:** Morning brief. Snapshot of the world. What changed overnight.
**Layer 2 — Research:** Deep-dive. Score leads, analyze positions, surface risks.
**Layer 3 — Execute:** Gated. Draft proposals, route to human approval. Never auto-executes.
**Layer 4 — Debrief:** Evening audit. What happened today. What needs attention tomorrow.

Every layer is a stateless subagent spawned by the main loop. They share nothing except what's written to disk.

## Quickstart

```bash
git clone https://github.com/Bubble-invest/bubble-ops-loop.git
cd bubble-ops-loop
# Create a department:
./scripts/bootstrap-dept.sh --slug=my-dept --display-name="My Department"
# Follow the 7-step onboarding — the agent drives itself
```

## Key features

- **4-layer OODA loop** with deterministic dispatch (L1→L2→L3→L4)
- **Mandatory human gates** — L3 never auto-executes
- **Department scaffolding** — bootstrap a new AI dept in 30 minutes
- **Loop-backup safety net** — 4x daily floor timers guarantee layers fire even if /loop dies
- **Boot-rearm** — agents self-recover after restart without operator intervention
- **Notion optional** — uses Notion internally but framework works without it
- **Local-git mode** — zero external dependencies (no GitHub needed)
- **270 tests** — TDD from day one

## Deployment

| Mode | Description |
|---|---|
| **VPS** | Deploy on Hetzner/DigitalOcean via `bubble-vps-platform` |
| **On-Prem** | Docker via `bubble-cabinet` |
| **Local** | Run directly on Mac/Linux for development |

## Repos

- [bubble-ops-loop](https://github.com/Bubble-invest/bubble-ops-loop) — this repo, the framework
- [bubble-vps-platform](https://github.com/Bubble-invest/bubble-vps-platform) — VPS provisioning
- [bubble-cabinet](https://github.com/Bubble-invest/bubble-cabinet) — Docker on-prem deployment

## License

MIT © 2026 Bubble Invest
