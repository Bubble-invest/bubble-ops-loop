---
name: dept-spawner
description: Spawn a new Bubble ops department (agent) end-to-end — create the repo, scaffold the dept tree, wire the VPS prerequisites, and hand off to the self-driving onboarding. Use this whenever the user wants to create/spawn/hatch/onboard a NEW agent or department (like Ben the fund manager or Geraldine the accountant), set up a new dept's repo + systemd unit + Telegram bot + secrets, or asks "add a new agent/dept", "spin up a department for X", "onboard a new ops agent". This orchestrates the EXISTING scaffolding (bootstrap-dept.sh + scaffold.py + the self-driving onboarding skill) — it does not reinvent the process. Do NOT use for editing an existing live dept (that's a normal PR) or for non-agent infra.
---

# Dept-Spawner — orchestrate a new Bubble department from zero to live

Spawning a department is **mostly already automated** (bootstrap-dept.sh + scaffold.py do the
repo+tree, the dept's own `CLAUDE.md` self-drives a 7-step onboarding over Telegram, and
activate-dept.sh opens the go-live PR). This skill is the **thin orchestrator** that runs those
pieces in the right order and fills the ~6 steps that still need a human/operator hand —
so you don't rediscover the sequence (and its footguns) every time, the way it was done by
hand for Ben and Geraldine.

**You are the operator, not the newborn agent.** Your job is Phases 1–2 + 4 (scaffold, wire
the VPS, activate). Phase 3 (the actual mandate/missions/layers) is driven by the NEW agent
itself, with the human approving — you don't hand-write its mandate.

## The model (one spawn, start to finish)

```
Phase 0  decide:   slug · display name · level (ops|management) · domain scope · creds strategy
Phase 1  scaffold: bootstrap-dept.sh → repo + onboarding/<slug> branch + 16-file tree + vendored libs
Phase 2  wire VPS: the 6-ish manual prereqs (BotFather bot, SOPS secrets, channel dir,
                   folder-trust, watchdog, broker policy) + deploy-to-morty.sh + clone to agents/
Phase 3  self-drive: the NEW agent runs its 7-step éclosure over Telegram; human approves each
Phase 4  activate: activate-dept.sh → PR onboarding/<slug> → main → human merges → live
Phase 5  domain:  post-live, wire the dept's domain-specific skills/tools
```

The detailed, copy-pasteable commands + the exact prereq steps live in references — read the
one for the phase you're on. **Keep this file as the map; the references are the territory.**

- `references/phase1-scaffold.md` — bootstrap-dept.sh usage, what scaffold.py produces, the empty-repo gotcha
- `references/phase2-vps-prereqs.md` — the 6 manual steps IN ORDER (the order matters: the agent hangs or can't receive messages if you skip/reorder), deploy-to-morty.sh, the agents/ clone
- `references/phase3-4-onboarding-activation.md` — priming the first turn, the 7-step self-drive, activate-dept.sh, the post-merge checklist
- `references/preflight-checklist.md` — the single pre-spawn checklist + the known footguns (collect these answers BEFORE running anything)

## How to run a spawn

1. **Collect Phase-0 decisions first** (read `references/preflight-checklist.md`). A spawn started
   without the slug/level/creds/bot decided stalls mid-way. Write them into an
   `ONBOARDING-ANSWERS.md` so the human can approve the agent's Phase-3 proposals fast rather
   than compose from scratch.
2. **Phase 1 — scaffold** (`references/phase1-scaffold.md`): run `bootstrap-dept.sh` with the
   slug/display-name/level. It creates the repo, the `onboarding/<slug>` branch, the full tree,
   and vendors the canonical dispatch/notify libs so the newborn starts byte-identical to the
   fleet. Verify the tree + push.
3. **Phase 2 — wire the VPS** (`references/phase2-vps-prereqs.md`): do the 6 manual prereqs **in
   the listed order** (bot → secrets → channel dir → folder-trust → watchdog → broker policy),
   then `deploy-to-morty.sh` to install the systemd unit, then clone to `/home/claude/agents/
   bubble-ops-<slug>`. The order is load-bearing — e.g. the channel dir needs the bot token,
   and folder-trust must exist or the headless agent hangs forever on the trust modal.
4. **Phase 3 — hand off to the agent**: prime the first turn (boot-rearm injects it; on older
   boxes the human sends `/start` to the new bot once). The agent then self-drives its 7-step
   éclosure over Telegram — it proposes, the human approves, it commits + updates STATE.yaml per
   step. You don't do this work; you watch for it to reach "Ready to activate".
5. **Phase 4 — activate** (`references/phase3-4-onboarding-activation.md`): once STATE.yaml shows
   all 7 steps validated + dry-run PASS, run `activate-dept.sh` to open the go-live PR. **The
   human merges it** (never you). Then run the post-merge checklist (backup auto-discovery,
   boot-rearm env, watchdog active, smoke checks).
6. **Phase 5 — domain wiring**: post-live, wire the dept's domain-specific skills/tools (unique
   per dept — e.g. trading skills for a fund dept, the dougs-devis skill for an accounting dept).

## Hard rules (why they matter)

- **You never write the new agent's mandate/missions.** The éclosure design is that the agent
  proposes and the human chooses — that's how Ben and Geraldine were built, and it's what keeps
  the dept genuinely owned by its purpose rather than your guess. Operator pre-drafts
  `ONBOARDING-ANSWERS.md` only to speed the human's approval, not to dictate.
- **The human merges the activation PR.** Spawning a live agent that can act in the world is
  exactly the irreversible step that stays with the human. You open the PR; you don't merge it.
- **Secrets stay human-handled.** BotFather, the OAuth/Notion creds, the SOPS file — these go
  through `bubble-set-secret` / a human at a terminal. The skill never handles raw secret values.
- **Phase-2 order is not optional.** Skipping or reordering a prereq produces the classic
  "service active but silent" failures (missing folder-trust → hangs on the modal; missing
  broker policy → all pushes 403; missing channel dir → bot never receives). The reference lists
  the order and the failure each step prevents.
- **Vendor canonical, never hand-roll.** The new dept's `dispatch_helpers.py` + notify stack are
  copied byte-identical from the framework (bootstrap-dept.sh does this) — never edited per-dept,
  so the dept doesn't drift from fleet fixes (this is exactly the drift that bit a recent spawn).

## Known footguns (collected from the Ben + Geraldine spawns)

- `bootstrap-dept.sh`'s empty-repo check tested `default_branch==null`, but modern GitHub sets
  `default_branch=main` on 0-commit repos → detection fails. Use `--accept-existing-empty-repo`
  if the human pre-created the org repo via the UI. (Upstream fix pending — see preflight ref.)
- The watchdog 4 artifacts must be slug-swapped from an existing dept (maya) — `deploy-to-morty.sh`
  does NOT render them yet. The watchdog `.sh` must be `chmod 0755` or systemd fails 203/EXEC;
  validate the sudoers file with `visudo -cf` BEFORE install.
- Clone to `/home/claude/agents/bubble-ops-<slug>` WITH the `bubble-ops-` prefix — the fleet
  backup cron auto-discovers depts by that prefix.

## Validation (lightweight — no destructive live spawn)

A real test-spawn creates GitHub repos + Telegram bots (destructive/irreversible), so do NOT
do a live test to validate this skill. Instead validate against the **known-good history**:
- Dry-run the scaffold: `bootstrap-dept.sh` against a throwaway slug into `/tmp` only (it clones
  to /tmp before pushing — inspect the tree, don't push), and the existing
  `tests/onboarding-bootstrap/` suite (24 tests) covers tree shape, branch convention,
  idempotency, and a full auto-drive simulation.
- Cross-check the skill's steps against `EXISTING-PROCESS.md` (the Ben/Geraldine record) — every
  phase here maps to what was actually done for those two.

## Reference (external)
- Full as-practiced process: `~/claude-workspaces/Rick_RnD/projects/dept-spawner/EXISTING-PROCESS.md`
- The scripts: `scripts/bootstrap-dept.sh`, `scripts/lib/scaffold.py`,
  `scripts/deploy-to-morty.sh`, `scripts/activate-dept.sh`
- The self-drive skill the newborn uses: `skills/department-onboarding-guide/`
