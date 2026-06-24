# `bubble-ops-loop` — Strategic Roadmap

**Owner:** {{OPERATOR}} (founder) + Tony (CEO agent)
**Builder:** Rick (R&D, Lab)
**Repo target:** `git@github.com:Bubble-invest/bubble-ops-loop.git` (TBD)
**Status:** Draft v1 — pending {{OPERATOR}} green-light (open questions §5)
**Created:** 2026-05-20
**Target ship date:** 2026-06-10 (3 weeks) — Maya operational on framework end-of-week-2

---

## 0. Executive Summary

`bubble-ops-loop` is the **cloud-first agentic operating framework** for every department at Bubble Invest. It collapses the 9-iteration design into a deliberately minimal stack: **4 globally-shared Cloud Routines** (one per OODA layer: Data / Research / Execution / Risk) act as a daily safety net; each routine fans out subagents (one per dept subscribed) with **isolated permissions** defined by per-dept subagent files. The main engine is **`/loop`** running in a long-lived **tmux session per dept on the VPS**, cadence tuned per dept. Inter-layer communication is **filesystem-only** (`queues/`, `outputs/`, `inbox/` folders in a per-dept GitHub repo forked from this template) — no DB, no orchestrator, no bus. A new department is **auto-detected by routines** scanning the parent org folder for any directory containing a `dept.yaml` manifest; user supplies the per-layer goals, then the loop progresses with **human-in-the-loop gates** between Research and Execution. Templates carry the prompts, tools, scoped permissions, and skill bindings — deployment is `register 4 routines + spawn N tmux sessions`. Reuses the production-grade `bubble-vps-platform` (209/209 tests passing) for VPS provisioning, SOPS secrets, and systemd units. **No backwards compat** — Ben & Maya migrate clean, legacy crons run in parallel only until parity is proven, then are retired.

---

## 1. Architecture

### 1.1 ASCII diagram — the runtime

```
                 ┌─────────────────────────────────────────────────────────────────┐
                 │              ANTHROPIC CLOUD (Routines)                         │
                 │  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐               │
                 │  │ R1 Data │ │ R2 Plan │ │ R3 Exec  │ │ R4 Risk │ (daily cron)  │
                 │  └────┬────┘ └────┬────┘ └────┬─────┘ └────┬────┘               │
                 └───────┼──────────┼───────────┼─────────────┼────────────────────┘
                         │ git pull│ git pull  │ git pull    │ git pull
                         ▼          ▼           ▼              ▼
                 ┌────────────────────────────────────────────────────────────────┐
                 │   GitHub org `Bubble-invest` — one repo per dept                       │
                 │   bubble-ops-loop          (template)                           │
                 │   bubble-ops-ben           ─┐                                   │
                 │   bubble-ops-maya          ─┼─ each contains:                   │
                 │   bubble-ops-tony          ─┤   dept.yaml + layers/{1,2,3,4}/   │
                 │   bubble-ops-miranda       ─┤   queues/  outputs/  inbox/      │
                 │   bubble-ops-eliot         ─┘   subagents/ wiki-link            │
                 └────────────────────────┬───────────────────────────────────────┘
                                          │ git push (commits = audit trail)
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │                       VPS  (Hetzner CX33, hardened)                          │
   │   ┌────────────────────────────────────────────────────────────────────┐    │
   │   │ systemd: claude-agent-<dept>@.service  (per dept)                  │    │
   │   │   └─ tmux session: ops-loop-<dept>                                 │    │
   │   │       └─ claude --resume + /loop 20m  (MAIN ENGINE)                │    │
   │   │           └─ spawns subagents with ISOLATED tools/perms/MCPs       │    │
   │   └────────────────────────────────────────────────────────────────────┘    │
   │   secrets: SOPS+age in tmpfs   |  Tailscale: dept-private mesh              │
   │   phone-home + security-audit + telegram-watchdog (from vps-platform)       │
   └─────────────────────────────────────────────────────────────────────────────┘
                                          ▲
                                          │ HTTPS (Tailscale-fronted) + CLI
   ┌──────────────────────────────────────┴──────────────────────────────────────┐
   │   FRONTEND  `bubble-ops-console`  (FastAPI + HTMX, single binary)           │
   │   /            → cross-dept board (kanban of pending decisions per dept)    │
   │   /dept/<x>    → per-dept view: layer state, queues, gates, settings        │
   │   /gate/<id>   → decision card: approve / reject / modify (writes inbox/)   │
   │   /settings/<x>→ per-dept knobs (cadence, gate thresholds, goals)           │
   └─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 The 4 layers — fractal OODA

| # | Layer | Cadence (default) | Reads from | Writes to | Subagent persona |
|---|---|---|---|---|---|
| **1** | **Data Update** | 06:00 UTC daily | `outputs/<yesterday>/{2,3,4}/` + external feeds | `layers/1/<date>/plan.md` with strategic options + gate markers | `data-curator` |
| **2** | **Research / Plan Execution** | every 20–60 min (per dept via /loop) | `layers/1/<date>/plan.md` + `queues/research/` | `outputs/<date>/2/research/*.md` and `queues/gates/<id>.yaml` | `task-orchestrator` (fans out to N task-specific subagents) |
| **3** | **Execution** | every 10–20 min | `inbox/decisions/<id>.yaml` (user-validated) | broker orders / git commits / emails sent + `outputs/<date>/3/exec-log.jsonl` | `executor` |
| **4** | **Risk Control** | 22:00 UTC daily | full day's `outputs/<date>/{1,2,3}/` + dept mandate | `outputs/<date>/4/risk-brief.md` + issues into `queues/improvements/` | `mandate-guardian` |

Communication = **filesystem only**. Git is the bus, log, and audit trail. No SQL, no Redis, no Kafka.

### 1.3 Per-dept repo skeleton (forked from `bubble-ops-loop`)

```
bubble-ops-<dept>/
├── dept.yaml                  # name, mandate, owner, cadences, subscribed layers, gate policy
├── README.md                  # auto-generated from dept.yaml
├── layers/
│   ├── 1/PROMPT.md           # layer-1 prompt template (inherited from bubble-ops-loop, overridable)
│   ├── 2/PROMPT.md
│   ├── 3/PROMPT.md
│   └── 4/PROMPT.md
├── subagents/                 # per-dept .claude/agents/*.md (isolated tools/perms)
│   ├── data-curator.md
│   ├── task-orchestrator.md
│   ├── executor.md
│   └── mandate-guardian.md
├── skills/                    # dept-specific skills (or symlinks to shared)
├── queues/
│   ├── research/             # tasks waiting for Layer 2
│   ├── gates/                # decisions waiting for human
│   └── improvements/         # Layer-4 outputs → Layer-1 inputs (next day)
├── inbox/
│   └── decisions/            # user-validated decisions → consumed by Layer 3
├── outputs/
│   └── <YYYY-MM-DD>/{1,2,3,4}/
└── .claude/
    ├── settings.json         # tools / permissionMode / MCP servers per dept
    └── CLAUDE.md             # dept operating doc (mission, cadence, gates)
```

---

## 2. Component Inventory

### 2.1 Templates (in `bubble-ops-loop` repo)

| Artifact | Path | Purpose |
|---|---|---|
| Layer-1 prompt | `templates/layers/1/PROMPT.md` | Data-curator OODA-Observe instructions |
| Layer-2 prompt | `templates/layers/2/PROMPT.md` | Plan-orchestrator instructions + gate-generation rules |
| Layer-3 prompt | `templates/layers/3/PROMPT.md` | Executor instructions + irrevocable-action guard |
| Layer-4 prompt | `templates/layers/4/PROMPT.md` | Mandate-guardian audit framework |
| `dept.yaml` schema | `templates/dept.schema.yaml` | JSON-schema validation of per-dept manifest |
| Subagent: data-curator | `templates/subagents/data-curator.md` | tools: Read, WebFetch, Bash(read-only); no Write outside `layers/1/` |
| Subagent: task-orchestrator | `templates/subagents/task-orchestrator.md` | tools: Read, Write, Agent (can spawn task-subagents); permissionMode: ask for new MCPs |
| Subagent: executor | `templates/subagents/executor.md` | tools: Read, Write, Bash(scoped allowlist), MCP scoped to dept secrets; `allow_live=True`-style poka-yoke gates |
| Subagent: mandate-guardian | `templates/subagents/mandate-guardian.md` | tools: Read, Grep, Glob, WebSearch; **no Write to anything except `outputs/4/`**. Pure auditor. |
| Per-dept CLAUDE.md | `templates/CLAUDE.md.j2` | jinja template, rendered at fork time from `dept.yaml` |
| Per-dept `.claude/settings.json` | `templates/claude-settings.json.j2` | inherits sane defaults; per-dept override file |
| `bootstrap-dept.sh` | `templates/scripts/bootstrap-dept.sh` | one-shot: fork → fill dept.yaml → render templates → first commit |

### 2.2 VPS infrastructure (additions to `bubble-vps-platform`)

| Artifact | Path (target) | Purpose |
|---|---|---|
| pyinfra task: ops-loop session | `pyinfra/tasks/ops_loop/_tmux_session.py` | idempotent: ensure tmux session `ops-loop-<dept>` exists, attach `claude --resume` + `/loop <cadence>` |
| systemd unit template | `pyinfra/templates/ops-loop-session.service.j2` | wraps tmux session so systemd restarts on crash; one unit per dept (`ops-loop-<dept>.service`) |
| pyinfra task: dept repo clone | `pyinfra/tasks/ops_loop/_repo_clone.py` | clones `bubble-ops-<dept>` into `/home/claude/depts/<dept>` with deploy-key from SOPS |
| /loop autostart wrapper | `pyinfra/templates/loop-autostart.sh.j2` | called inside tmux: `cd /home/claude/depts/<dept> && claude --resume -p "/loop {{cadence}} read layers/2/PROMPT.md and run the next iteration"` |
| Cadence config | `dept.yaml::ops_loop.cadence` | minutes between loop iterations |
| Tailscale ACL | (in `bubble-vps-data`) | restrict per-dept session port to operator + console |
| Dashboard upgrade | `pyinfra/templates/dashboard-app.py.j2` → migrate to `bubble-ops-console` (see §2.4) | unified front-end |

### 2.3 Cloud Routines (Anthropic-managed)

Four YAML routines registered via `mcp__scheduled-tasks__create_scheduled_task`. Each scans the org folder for `bubble-ops-*` repos, reads `dept.yaml`, and fans out one subagent invocation per subscribed dept.

| Routine | Schedule (UTC) | Script |
|---|---|---|
| `bubble-ops-layer-1-data` | `0 6 * * *` | for each dept: spawn `data-curator` subagent, write `layers/1/<date>/plan.md`, commit |
| `bubble-ops-layer-2-research` | `*/30 8-20 * * *` | for each dept where `queues/research/` non-empty: spawn `task-orchestrator`, write gates |
| `bubble-ops-layer-3-exec` | `*/15 8-20 * * *` | for each dept where `inbox/decisions/` non-empty: spawn `executor` |
| `bubble-ops-layer-4-risk` | `0 22 * * *` | for each dept: spawn `mandate-guardian`, write risk-brief, file improvements |

**Cloud Routines are the safety net. The /loop sessions on VPS are the main engine.** If VPS dies, routines still tick (with degraded latency).

### 2.4 Frontend — `bubble-ops-console` (replaces per-dept dashboards)

| Page | Endpoint | Renders |
|---|---|---|
| Cross-dept board | `GET /` | kanban: per dept × per layer × {pending gates, in-flight tasks, last run} |
| Per-dept detail | `GET /dept/<slug>` | live layer state + recent outputs + queue depths |
| Gate decision | `GET /gate/<dept>/<id>` | decision card; `POST` writes `inbox/decisions/<id>.yaml` + commits |
| Per-dept settings | `GET /settings/<slug>` | edit `dept.yaml` knobs (cadence, gate policy, goals per layer) |
| Audit trail | `GET /audit/<dept>` | git-log over `outputs/` rendered as timeline |
| Routine health | `GET /health` | last successful run per (layer × dept), red if stale > 2× cadence |

**Stack:** FastAPI + HTMX + Jinja, same pattern as `Ben_Fund/dashboard/` (HTMX kanban is proven). Authenticated via Tailscale-only port + bearer token. Single binary, lives in `bubble-ops-loop/console/`, deployed via new pyinfra task `pyinfra/tasks/console/`.

### 2.5 Auto-detection mechanism

Each Cloud Routine runs this prelude:

```bash
# pseudo-code; real impl in templates/routines/_dept_scan.py
gh repo list Bubble-invest --json name --jq '.[].name' \
  | grep '^bubble-ops-' \
  | grep -v '^bubble-ops-loop$' \
  | while read repo; do
      git clone --depth 1 "git@github.com:Bubble-invest/$repo" /tmp/$repo
      yq '.subscribed_layers[]' /tmp/$repo/dept.yaml \
        | grep -q "^$LAYER_NUM$" && echo "$repo"
    done
```

A new dept is live the moment its repo is pushed + `dept.yaml` lists the layer. No code change in routines.

### 2.6 Human-in-the-loop gates

| Trigger | Mechanism | UX |
|---|---|---|
| Layer-2 emits a decision | `queues/gates/<id>.yaml` written; commit triggers GitHub webhook → console refresh; Telegram ping via existing `telegram-reporter` skill | {{OPERATOR}} sees gate card in `/`; clicks approve/reject/modify; console writes `inbox/decisions/<id>.yaml` + commits |
| Layer-3 fails mid-execution | writes `queues/gates/exec-<id>.yaml` with `kind: exec_retry`; same flow | {{OPERATOR}} decides retry / abort / hand-off to persistent user agent |
| Layer-4 flags mandate breach | writes `outputs/<date>/4/alert.md` + Telegram urgent | {{OPERATOR}} reviews; can override or accept |
| Modify decision | console writes amended `inbox/decisions/<id>.yaml`; Layer 3 re-reads on next loop tick | one round-trip; if still wrong, escalates to persistent user agent (out of scope v1) |

### 2.7 Migration paths

| Dept | From | To | Strategy |
|---|---|---|---|
| **Maya** | `Maya_Sales/` + 5 crons (morning-sync, draft-batch, linkedin-presence, validated-draft-sender, weekly-sales-analyst) | `bubble-ops-maya/` with 4 layers; existing crons mapped to layer-1 (morning-sync) + layer-2 (draft-batch, linkedin-presence) + layer-3 (validated-draft-sender) + layer-4 (weekly-sales-analyst) | Phase 3: shadow-mode (new framework writes, legacy crons still send); Phase 4 parity check; Phase 5 retire legacy |
| **Ben** | `Ben_Fund/` + Notion-driven flows + dashboard | `bubble-ops-ben/` ; layer-1 = data refresh + Notion sync, layer-2 = idea research, layer-3 = order placement (with `allow_live=True`), layer-4 = drawdown/risk audit | Phase 4 (after Maya proven) |
| **Tony** | `claude-workspaces/main/` + main-strategist agent | `bubble-ops-tony/` ; layer-1 = read all STATUS.md + CEO_INBOX, layer-2 = decide cross-dept priorities, layer-3 = write CEO_INBOX entries + reply Telegram, layer-4 = weekly meta-audit | Phase 4 in parallel with Ben |
| **Miranda / Eliot** | existing weekly skills | Phase 5 — opt-in once Tony + Ben stable |

---

## 3. Phased Roadmap

### Phase 0 — Spec & repo skeleton (Days 1–2)

- **Goal:** `bubble-ops-loop` repo exists with template skeleton, schema, and one fixture dept that runs locally.
- **Deliverables:**
  - `bubble-ops-loop` repo created on GitHub + cloned locally
  - `templates/` tree (layers, subagents, dept.yaml schema, bootstrap script)
  - `examples/bubble-ops-fixture/` — a "hello world" dept with stub prompts that round-trip through all 4 layers on Mac
  - `README.md` + `CONTRACT.md`
- **Acceptance:**
  - `./templates/scripts/bootstrap-dept.sh --name=fixture --mandate="say hi"` produces a valid repo
  - Manually invoking `claude -p "$(cat templates/layers/1/PROMPT.md)"` on fixture writes `layers/1/<date>/plan.md`
  - JSON-schema validates 3 hand-written dept.yaml samples
- **Effort:** 2 days (Rick solo)
- **Dependencies:** none
- **Risks:** ⚠️ Layer prompts under-specified → spend time on round-trip with fixture before locking. *Mitigation:* fixture forces concreteness early.

### Phase 1 — Cloud Routines + auto-detection (Days 3–5)

- **Goal:** 4 routines registered, scanning the org, running against fixture dept end-to-end with no human involved (gates auto-approved in fixture).
- **Deliverables:**
  - `templates/routines/{layer-1,layer-2,layer-3,layer-4}.yaml` (Anthropic Cloud Routine specs)
  - `templates/routines/_dept_scan.py` (gh-cli-based detector)
  - 4 routines created via `mcp__scheduled-tasks__create_scheduled_task`
  - Heartbeat output for each routine, written to `monitoring/heartbeats.jsonl` in `Rick_RnD/`
- **Acceptance:**
  - Each routine runs on schedule for 48 h without manual intervention
  - Fixture dept's `outputs/<date>/{1,2,3,4}/` is populated daily
  - Adding a 2nd fixture dept (commit new `dept.yaml`) auto-picks up on next routine tick (no routine edit)
- **Effort:** 3 days
- **Dependencies:** Phase 0
- **Risks:** ⚠️ Cloud Routine 7-day session expiry — *Mitigation:* routines spawn fresh subagents per run (no session reuse). ⚠️ GitHub rate limits if 20+ depts — *Mitigation:* shallow clones + `gh` API caching; not a v1 concern with <10 depts.

### Phase 2 — VPS /loop engine (Days 6–9)

- **Goal:** the VPS hosts one tmux+systemd session running `/loop 20m` against the fixture dept; main engine takes over from cloud safety-net.
- **Deliverables:**
  - `pyinfra/tasks/ops_loop/*` modules in `bubble-vps-platform` (new SPEC-021)
  - `ops-loop-fixture.service` systemd unit on the VPS
  - `loop-autostart.sh` invokes `claude --resume + /loop` inside tmux
  - Console smoke page at `/health` shows last-run timestamp per dept × layer
  - Tests: 15+ new pyinfra tests, all passing (push the suite from 209 → 225+)
- **Acceptance:**
  - `./scripts/deploy.sh --tenant={{VPS_HOST}}` provisions the new units idempotently
  - tmux session survives `systemctl restart ops-loop-fixture` (state rebuilt from git)
  - /loop produces new outputs every 20 min in fixture's repo on GitHub (visible via console /health)
  - Cloud routines + /loop coexist (no double-execution; routines detect "fresh enough" output and skip)
- **Effort:** 4 days
- **Dependencies:** Phase 1
- **Risks:** ⚠️ tmux + systemd race conditions on boot — *Mitigation:* `Type=forking` + ExecStartPre that waits for tmux server. ⚠️ `claude --resume` semantics on long-lived sessions — *Mitigation:* per-loop-iteration `--resume` against same session id stored in `/run/ops-loop-<dept>/session_id`. ⚠️ "Fresh enough" guard for routine/loop coexistence — *Mitigation:* routine reads `outputs/<date>/<layer>/.last-run` and skips if newer than cadence.

### Phase 3 — Maya migration (Days 10–14)

- **Goal:** `bubble-ops-maya` repo runs on the VPS in shadow mode alongside legacy crons; outputs match within ±10%.
- **Deliverables:**
  - `bubble-ops-maya` repo forked from template + filled `dept.yaml`
  - All 4 layer prompts customized for Maya's mandate (prospection)
  - Maya's existing skills (notion-reader, telegram-reporter, etc.) bound in `.claude/settings.json` mcpServers
  - Subagent definitions with scoped perms (e.g., `executor` can call Gmail draft API but NOT send; only `validated-draft-sender` equivalent gate-approved decisions)
  - **Shadow mode**: layers 1–2–4 write but layer 3 logs `would-have-done` instead of executing for 3 days
  - Parity report comparing shadow output vs legacy cron output
- **Acceptance:**
  - 3 consecutive days where shadow Layer-2 generates Tier-1 drafts within ±2 of legacy
  - At least 1 gate decision approved by {{OPERATOR}} via console → consumed correctly by Layer 3 (still shadow)
  - Maya's wiki page reflects the migration; STATUS.md updated
- **Effort:** 5 days
- **Dependencies:** Phases 0–2
- **Risks:** ⚠️ Skill-binding mismatch (mcpServers per-subagent vs per-workspace) — *Mitigation:* verify against https://code.claude.com/docs/en/sub-agents §"tools and permissions". ⚠️ Prompt drift from current Maya tone — *Mitigation:* lift existing SKILL.md prose into layer prompts verbatim where possible.

### Phase 4 — Tony + Ben in parallel, console v1, cutover (Days 15–19)

- **Goal:** 3 depts live on framework (Maya in prod, Tony + Ben in shadow); console v1 ships; Maya legacy crons retired.
- **Deliverables:**
  - `bubble-ops-tony` repo + 4 layer prompts (CEO mandate, weekly cadence)
  - `bubble-ops-ben` repo + layer prompts (fund mandate, daily cadence, `allow_live=True` poka-yoke)
  - Console v1 with `/`, `/dept/<x>`, `/gate/<id>`, `/settings/<x>`, `/health` pages
  - Maya legacy crons disabled (kept on disk, paused via `systemctl disable`, easy revert for 30 days)
  - Telegram notifications wired for every gate via existing `telegram-reporter` skill
- **Acceptance:**
  - {{OPERATOR}} approves 3+ gates per day on Maya from console for 3 consecutive days
  - Tony shadow Layer 4 catches a real cross-dept issue
  - Ben shadow Layer 2 produces a research brief that {{OPERATOR}} judges "would have shipped"
  - Console health page shows all-green for 48 h
- **Effort:** 5 days (parallel: Rick on console, Tony+Ben in parallel by reusing Maya template)
- **Dependencies:** Phase 3
- **Risks:** ⚠️ Console scope creep — *Mitigation:* hard cap at 5 pages above; defer audit timeline + per-task views to v2. ⚠️ Ben's `allow_live=True` order placement is high-blast-radius — *Mitigation:* Ben Layer-3 stays in shadow for v1; live execution behind explicit {{OPERATOR}}-on-CLI OOB email auth (use existing `auth` skill flow). ⚠️ Telegram gate spam — *Mitigation:* digest mode (one message per dept per hour) + `react` for low-stakes.

### Phase 5 — Polish, retire legacy, document, opt-in for Miranda/Eliot (Days 20–22)

- **Goal:** Stable v1, full docs, sustainable cadence; Miranda + Eliot have a paved path to opt in.
- **Deliverables:**
  - `docs/ONBOARDING.md` — "how to add a new dept" in <30 min
  - `docs/LAYERS.md` — each layer's contract documented with examples
  - `docs/RUNBOOK.md` — day-2 ops (rotate secrets, restart loop, debug stuck gate)
  - `docs/MIGRATION.md` — Maya cutover post-mortem
  - Legacy Maya crons archived to `_archive/` after 14-day parallel run with zero divergence
  - Miranda + Eliot dept.yaml stubs committed (not yet active) with documented opt-in steps
  - Strategy log updated, BACKLOG.md cleaned of superseded items
- **Acceptance:**
  - A new agent (or Rick acting fresh) can bootstrap a 6th dept in <30 min following ONBOARDING.md
  - 7 consecutive days of all-green console with no manual intervention
  - {{OPERATOR}} signs off "this is leaner than what we had"
- **Effort:** 3 days
- **Dependencies:** Phase 4
- **Risks:** ⚠️ Docs drift immediately — *Mitigation:* docs include "last verified" frontmatter; weekly wiki-compile cron picks up changes.

### Effort totals

| Phase | Days | Cumulative |
|---|---|---|
| 0 — Spec & skeleton | 2 | 2 |
| 1 — Cloud routines | 3 | 5 |
| 2 — VPS /loop engine | 4 | 9 |
| 3 — Maya migration | 5 | 14 |
| 4 — Tony+Ben+console+cutover | 5 | 19 |
| 5 — Polish & retire legacy | 3 | 22 |

**Total: 22 working days ≈ 3 weeks of focused build, 4 weeks calendar with slack.**

---

## 4. Risks & Mitigations (cross-phase)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| `/loop` undocumented edge cases (7-day expiry, resume semantics) | Med | High | Cloud routines are the safety net; if /loop hiccups, routines still tick |
| Cloud Routine quota / cost surprise | Low | Med | 4 routines × ~10 dept-runs/day = ~40 runs/day; well within reasonable cost. Monitor via Anthropic console |
| Subagent perm-scoping not as isolated as docs claim | Med | High | Phase-0 fixture proves isolation BEFORE binding any real secret. Hard gate. |
| Filesystem-as-bus race conditions (two layers writing same path) | Med | Med | Layer writes are per-`<date>/<layer>/` sharded; only one writer per path. Use `flock` on shared dirs like `queues/` |
| Telegram gate spam → {{OPERATOR}} ignores | High | High | Phase-4 digest mode + severity tiers; only HIGH-blast-radius pings immediately |
| Maya migration drift breaks live prospection | Med | High | 3-day shadow + parity report + 14-day parallel run = 17 days of safety net before retire |
| Ben live trading from new framework misfires | Low | Critical | Ben Layer-3 stays shadow in v1; live execution requires OOB email auth per order |
| Scope creep: "let's also build X" | High | Med | Non-goals §6 is the canonical list; deviations need written exception |
| Frontend becomes its own project | High | Med | 5-page hard cap; HTMX, no React, no new framework |
| Wiki/CLAUDE.md docs go stale | Med | Low | Nightly wiki-compile cron + frontmatter `last_verified` |

---

## 5. Open Questions (numbered, blocking-marked)

1. **[Phase 0, BLOCKING]** Repo name: `bubble-ops-loop` confirmed? Or `bubble-loop`, `bubble-ops-fabric`?
2. **[Phase 0, BLOCKING]** GitHub org: stay on `Bubble-invest` or move to `bubble-invest` org now to avoid rename later?
3. **[Phase 1]** Cloud Routines cost ceiling: what's the monthly Anthropic-bill budget for routines so Rick can size cadences? *Default if no answer:* assume 4 routines × 10 depts × daily layer-1/layer-4 + half-hourly layer-2/3 = budget-friendly.
4. **[Phase 1]** Cloud Routines: register under {{OPERATOR}}'s personal Anthropic account or a Bubble Invest team account? Affects which token to use in `gh secret set`.
5. **[Phase 2]** /loop cadence default: 20 min reasonable starting point for layer-2/3, or do specific depts need faster (Ben in trading hours)? *Default:* 20 min, per-dept override via `dept.yaml`.
6. **[Phase 2]** Single VPS for v1 or sharded already? *Default:* single VPS, plan shard once we hit 5+ depts.
7. **[Phase 3, BLOCKING]** Maya migration strategy: shadow + parity (proposed) or hard cutover after 1 day? *Default:* shadow, lower risk.
8. **[Phase 3]** Existing Maya skills (`maya-warming-batch`, `maya-draft-batch`, etc.) — re-package as Layer-2 subagent calls, or invoke as-is via `Skill` tool? *Default:* invoke as-is in v1, refactor in v2.
9. **[Phase 4, BLOCKING]** Ben live execution: gate via OOB email auth per order, or batch-approve via console? *Default:* OOB per order for v1 (safer); batch later.
10. **[Phase 4]** Tony's cadence: daily layer-1 like ops depts, or weekly? *Default:* daily layer-1 (lightweight CEO_INBOX sweep), weekly layer-4.
11. **[Phase 4]** Console auth: bearer token only, or also SSO via {{OPERATOR}}'s Gmail OAuth? *Default:* bearer + Tailscale-only port for v1.
12. **[Phase 5]** Miranda + Eliot opt-in trigger: time-based (week 4) or capability-based (after 3 weeks zero-incident on Maya/Ben)? *Default:* capability-based.
13. **[Phase 5]** Legacy crons: archive in-place under `_archive_<date>/` or delete entirely after 14-day parallel? *Default:* archive, never delete in v1.
14. **[Cross-phase]** Saxo/Bourso auth on VPS — block Phase 4 Ben? Or can Ben Layer-3 stay shadow until Saxo OAuth refresh is live? *Default:* Ben stays shadow until Saxo OAuth verified on the VPS.
15. **[Cross-phase]** Wiki integration: each dept repo symlinks to `bubble-shared-wiki`, or wiki stays in `Rick_RnD/` and depts read via HTTP? *Default:* symlink at clone time (already validated).

---

## 6. Explicit Non-Goals (v1)

We are **NOT** doing the following in v1. Anything below requires written exception in this file.

1. **No backwards compatibility.** Legacy crons run in parallel only until parity; then archived. No effort spent on "shim" layers.
2. **No new Python framework.** No SQLAlchemy models, no Pydantic-everywhere, no event bus, no Celery. Stdlib + PyYAML + FastAPI/HTMX (proven stack).
3. **No DB.** Filesystem + git is the bus, log, and storage.
4. **No real-time orchestrator daemon.** /loop is the engine; routines are the safety net. No always-on supervisor process beyond systemd.
5. **No multi-VPS sharding in v1.** Single VPS box. Re-evaluate at 5+ depts.
6. **No web UI for editing prompts.** Prompts edited via `git` + PR. Console only edits `dept.yaml` knobs.
7. **No auto-merge of Layer-4 improvements into Layer-1 next-day prompts.** Improvements are queued; {{OPERATOR}}/Tony approve via gate before next-day cycle picks them up.
8. **No live trading from Ben Layer-3 until Saxo OAuth + OOB email auth proven on the VPS.** Ben Layer-3 starts shadow.
9. **No Miranda or Eliot in v1.** They opt in Phase 5 only.
10. **No persistent user agent (the "modification retry handoff" mentioned in Layer 3 spec).** v1 sends modifications back to Layer 2 next loop tick; persistent agent is v2.
11. **No multi-tenant frontend.** Console serves {{OPERATOR}}'s single org; per-client multi-tenancy is a future product (lives in `bubble-vps-platform`, not here).
12. **No SDK / public API.** Internal only. Versioning is "main branch + tags as needed".
13. **No replacement of `bubble-vps-platform`.** This builds **on top** of it; vps-platform stays the canonical infra layer.

---

## 7. First Commit Checklist (`bubble-ops-loop` repo)

Exact first 10 commits, in order, that should land:

1. **`chore: bootstrap repo with README, LICENSE-TBD, .gitignore, .editorconfig`**
   - skeleton repo, no logic
2. **`docs: ROADMAP.md (this file) + ARCHITECTURE.md stub`**
   - lock the plan in-repo
3. **`spec: templates/dept.schema.yaml + 3 sample dept.yaml fixtures`**
   - schema-first, validate-as-you-go
4. **`feat: templates/layers/{1,2,3,4}/PROMPT.md (v0 prose, untested)`**
   - first drafts of the 4 layer prompts
5. **`feat: templates/subagents/{data-curator,task-orchestrator,executor,mandate-guardian}.md`**
   - scoped permissions baked in from line 1
6. **`feat: templates/scripts/bootstrap-dept.sh (fixture dept generation)`**
   - reproducible "new dept in 30 min" mechanism
7. **`test: examples/bubble-ops-fixture/ — round-trips locally on Mac with stub prompts`**
   - first proof the architecture compiles end-to-end
8. **`feat: templates/routines/{layer-1..4}.yaml + _dept_scan.py`**
   - Cloud Routine specs ready to register
9. **`feat: console/ — FastAPI + HTMX skeleton with /, /health, /dept/<slug>`**
   - 3 pages, no live data, just shell
10. **`docs: ONBOARDING.md v0 — adding a new dept in 30 minutes`**
    - close the loop: the next dept-add should follow this doc, not tribal knowledge

After commit 10: open PR for Phase-1 routines (Cloud Routine registration is by-hand via Claude Desktop, but the YAMLs + scan script land in this commit).

---

## 8. Reference Assets (existing, to be reused)

| Asset | Path | Use |
|---|---|---|
| VPS infra (pyinfra modules, systemd, SOPS, Tailscale) | `/Users/{{OPERATOR_USER}}/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/` | Phase 2 builds on this; new task module `ops_loop/` added |
| `auth` skill (OOB email + SOPS secret setting) | `/Users/{{OPERATOR_USER}}/.claude/skills/auth/SKILL.md` | Phase 4 Ben live execution + per-dept secret rotation |
| Ben dashboard pattern | `/Users/{{OPERATOR_USER}}/claude-workspaces/Ben_Fund/dashboard/` | Phase 4 console reuses kanban + HTMX patterns |
| Maya webapp pattern | `/Users/{{OPERATOR_USER}}/claude-workspaces/Maya_Sales/webapp/` | Phase 4 console gate-card UX |
| Shared wiki (github-first) | `git@github.com:Bubble-invest/bubble-shared-wiki.git` | each dept repo symlinks; nightly wiki-compile already works |
| Heartbeats infra | `/Users/{{OPERATOR_USER}}/claude-workspaces/Rick_RnD/monitoring/heartbeats.jsonl` | Cloud Routines emit heartbeats here |
| Notion logbook (reader skill) | `~/.claude/skills/notion-reader/SKILL.md` | Maya/Ben Layer-1 data sources |
| Subagent isolation spec | https://code.claude.com/docs/en/sub-agents | canonical reference; verify in Phase 0 fixture |
| Scheduled tasks MCP | `mcp__scheduled-tasks__*` tools | Phase 1 routine registration |
| Telegram reporter | `~/.claude/skills/telegram-reporter/SKILL.md` | gate notifications in Phase 4 |
| Existing strategy log | `.claude/strategy-log.md` (new) | persist decisions session-to-session |

---

## 9. Success Criteria (whole project)

The project is **done** when ALL of:

- [ ] 3 depts (Maya, Tony, Ben) live on the framework, console all-green for 7 days
- [ ] Adding dept #4 takes <30 min following `docs/ONBOARDING.md`
- [ ] Legacy Maya crons archived (not running)
- [ ] {{OPERATOR}} approves the statement "this is leaner than what we had before"
- [ ] Zero broker incidents (Ben Layer-3 may still be shadow — that's fine for v1)
- [ ] All 4 layers' gate-decision UX tested end-to-end by {{OPERATOR}} from his phone (console mobile-friendly)
- [ ] `bubble-ops-loop` + `bubble-vps-platform` documented as a coherent stack in shared wiki

If any criterion slips: write an exception in this ROADMAP.md, push to main, ping Tony.

---

*Strategic planning by Rick (R&D). Architecture co-designed with {{OPERATOR}} over 9 iterations on Telegram, 2026-05-20. Next review: end of Phase 2 (Day 9), or any blocking open question above.*
