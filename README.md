# 🫧 Bubble Ops Loop — Agentic 4-Layer OODA Framework

**The open-source engine that powers autonomous AI departments with mandatory human gates.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Built on Claude Code](https://img.shields.io/badge/built%20on-Claude%20Code-orange)](https://claude.ai)
[![Tests](https://github.com/Bubble-invest/bubble-ops-loop/actions/workflows/tests.yml/badge.svg)](https://github.com/Bubble-invest/bubble-ops-loop/actions)

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

The four layers are **phases of the OODA cycle**, not single tasks:

**Layer 1 — Observe:** Snapshot the world. What changed since the last run.
**Layer 2 — Research:** Deep-dive. Score leads, analyze positions, surface risks.
**Layer 3 — Execute:** Gated. Draft proposals, route to human approval. Never auto-executes.
**Layer 4 — Debrief:** Audit. What happened, what needs attention next.

Each layer runs as a stateless subagent spawned by the main loop; they share nothing except what's written to disk.

### Missions: many per layer

A layer is **not** a single job — it's a phase that runs one or more **missions**. You declare your department's missions in `dept.yaml` under `recurring_missions[]`, each pinned to a `layer` and a `cadence`:

```yaml
layers:
  subscribed: [1, 2, 3, 4]

recurring_missions:
  - id: data_update          # a Layer-1 mission
    layer: 1
    cadence: daily
    description: Sync data, compute the situation brief...
    output_queue: queues/research/
  - id: news_scan            # ANOTHER Layer-1 mission, different cadence
    layer: 1
    cadence: hourly
    description: Watch the newswire, flag material events...
  - id: research             # a Layer-2 mission
    layer: 2
    cadence: daily
    description: For each research item, write an investment case...
```

When a layer fires, the dispatcher (`select_due_missions`) materializes **every mission pinned to that layer whose cadence is due right now** and runs them in parallel within the layer — so Layer 1 can carry a daily `data_update` *and* an hourly `news_scan` without either orphaning the other. Add a mission by adding a `recurring_missions[]` entry; no code changes. See the worked example in [`agents/ben/dept.yaml`](agents/ben/dept.yaml).

## Quickstart

```bash
git clone https://github.com/Bubble-invest/bubble-ops-loop.git
cd bubble-ops-loop
# Create a department:
./scripts/bootstrap-dept.sh --slug=my-dept --display-name="My Department"
# Follow the 7-step onboarding — the agent drives itself
```

## Key features

- **4-layer OODA loop** with mission-centric dispatch — declare many missions per layer (each with its own cadence); the loop materializes whichever are due
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
