# Phase 3 — Self-drive onboarding · Phase 4 — Activation · Phase 5 — Domain

## Phase 3 — Prime + the agent self-drives its 7-step éclosure

**3a. Prime the first turn.** The boot-rearm mechanism (`OPS_LOOP_BOOT_REARM=1` in the unit)
injects a synthetic first turn on service start. On older boxes that wasn't wired → the human
sends `/start` to the new bot once to trigger it.

**3b. The agent drives itself** (you do NOT do this work — you watch for it to progress). Its
onboarding `CLAUDE.md` walks it through 7 steps, communicating only via its dedicated bot:

| Step | Artifact | STATE transition |
|---|---|---|
| 1. Mandate | `MANDATE.md` + `dept.yaml.draft::mandate` | Idea → Configuring |
| 1b. Working memory + mission lock | `WORKING_MEMORY.md` | (part of 1) |
| 2. Recurring missions | `missions/<slug>.yaml` | Configuring → Drafting |
| 3. Layers | `layers/N/PROMPT.md` (customized) | Drafting |
| 4. Skills & tools | `skills/`, `tools/` | Drafting → Needs validation |
| 5. Gates & KPIs | gate_policies in draft, gate schema | Needs validation |
| 6. Dry-run | smoke tests, STATE updated | Dry run |
| 7. Activation | activation PR opened | Ready to activate → Live |

At each step the agent: proposes 3 options via Telegram → **human picks/edits** → commits the
artifact → updates `onboarding/STATE.yaml` → sends "✓ Step N validated". The operator's only
input here is the pre-drafted `ONBOARDING-ANSWERS.md` to speed the human's choices. **The agent
proposes; the human chooses. You never hand-write the mandate.**

## Phase 4 — Activation PR + live promotion

Script: `./scripts/activate-dept.sh --slug=<slug>`

`can_activate()` preconditions: all 7 steps validated in STATE.yaml · `dept.yaml.draft::status ==
Ready to activate` · `dry_run_status == PASS`.

It promotes `dept.yaml.draft → dept.yaml` (status=live) and opens a PR `onboarding/<slug> → main`.
**The human merges it** (never you — spawning a live world-acting agent is the irreversible step).

Post-merge operator checklist (manual):
- Floor-backup cron auto-discovered the dept: `BUBBLE_BACKUP_DRY_RUN=1 scripts/loop-backup.sh --layer 1`
- `OPS_LOOP_BOOT_REARM=1` present in the unit env
- `validate_gate_card` wired in the gate-writing layer PROMPT(s)
- Watchdog active + enabled
- Prime with a real first task (human sends it)
- Smoke: dispatch-drift (2 L1 keys), gate-card YAML validity, layer-floor coverage

## Phase 5 — Domain-specific wiring (post-live, unique per dept)
- Fund dept (Ben): trading skills, SQLite schema, vault, policy-engine stub; secrets in a separate pass.
- Accounting dept (Geraldine): wire `dougs-devis` skill, ship Dougs session cookie SOPS → VPS
  `/var/lib/bubble-<slug>/`, supervised headful calibration on TEST data.
