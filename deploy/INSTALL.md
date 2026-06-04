# bubble-ops-loop — box install manifest

Canonical list of the box-level (Morty/VPS) install steps for the
bubble-ops-loop platform. Run these on a fresh box bring-up, and re-run
the idempotent ones on every deploy. Each step points at the script that
performs it. This file exists so no step gets forgotten when the box is
rebuilt or a new tenant box is provisioned.

> Provisioning note: the VPS itself is provisioned by the separate
> **bubble-vps-platform** (pyinfra) repo. The steps below run *inside*
> the cloned `bubble-ops-loop` repo on the box. The platform repo should
> invoke each `scripts/install-*.sh` after the repo is cloned — see the
> "wire into pyinfra" backlog item.

## Steps

| # | What | Script | Idempotent | Notes |
|---|------|--------|------------|-------|
| 1 | Per-dept agent units (`ops-loop-<slug>`) | `deploy/templates/ops-loop-dept.service.template` via `scripts/bootstrap-dept.sh` / `activate-dept.sh` | yes | One per live dept. Decrypts per-dept SOPS → `/run/claude-agent-<slug>/env`. |
| 2 | Console (cockpit) | `console/deploy/bubble-ops-console.service.template` + `scripts/deploy-console-to-morty.sh` | yes | Tailscale-served `:8443`. |
| 3 | Loop liveness watchdog (alerts) | `scripts/ops-loop-watchdog.{service,timer}` + `scripts/loop-watchdog.sh` | yes | Telegram alert on stale heartbeat. |
| 4 | **Loop layer FLOOR (4 crons)** | **`scripts/install-loop-backup.sh`** | **yes** | **EXACTLY 4 cron units (`loop-layer1..4`), one per OODA layer (L1 07:00 / L2 12:00 / L3 16:00 / L4 19:00 Paris). Each fires its layer for every eligible dept, auto-discovered at runtime. The daily floor + safety net. New depts inherit it with ZERO config.** |
| 5 | Restic backups | `scripts/morty-restic-setup.sh` | yes | 6h backup + retention timers. |
| 6 | **OS sandbox (Layer B)** | **`scripts/install-sandbox.sh`** | **yes** | **bwrap+socat+sandbox-runtime+AppArmor + merges the sandbox block into managed-settings. Jails the Bash tool fleet-wide (anti prompt-injection). Restart agents after, verify via userns check. See `deploy/sandbox-tests/` + wiki `vps-agent-sandbox`.** |
| 7 | Age-key offline backup | `scripts/backup-age-key.sh` | operator | Needs Keychain passphrase (operator). |

## Loop layer FLOOR (step 4) — what it is

Each dept runs a persistent `/loop` session. If that session dies for any
reason — auth lapse, crash, OOM, or "parked" after a restart — the dept
silently stops working while systemd still reports `active`. The layer floor
is an independent safety net AND a daily cadence guarantee.

- **EXACTLY 4 cron units, forever** — one per OODA layer, sharing one template
  service `loop-layer@.service` (`ExecStart=…/loop-backup.sh --layer %i`):
  - `loop-layer1.timer` — L1 (Observe) 07:00 Europe/Paris
  - `loop-layer2.timer` — L2 (Orient)  12:00 Europe/Paris
  - `loop-layer3.timer` — L3 (Decide)  16:00 Europe/Paris
  - `loop-layer4.timer` — L4 (Act)     19:00 Europe/Paris
- Each cron fires ITS layer for EVERY eligible dept, **discovered at runtime**
  by globbing `/home/claude/agents/bubble-ops-*` — **no hardcoded dept list**.
  A NEW dept being born adds **ZERO** new units; it is picked up automatically
  once its `ops-loop-<slug>.service` is enabled and it has `layers/N/PROMPT.md`.
- A dept is SKIPPED if its loop service is disabled/absent (paused depts,
  concierges) or — in layer-floor mode — it lacks `layers/N/PROMPT.md` for that
  layer.
- For each in-scope dept the cron checks heartbeat freshness
  (`scripts/lib/loop_backup.py`): fresh (loop alive) → **skip** (no
  double-processing); stale > 90 min, or no heartbeat → run **ONE** forced
  Layer-N tick via `claude -p` in the dept's authed workspace, then stop. The
  tick's work summary is relayed to Joris on Telegram.
- A `flock` mutex guarantees a floor tick never overlaps a live tick.

The floor SUPERSEDES the old twice-daily generic `loop-backup.timer` (which
fired only ~L1 via `decide_dispatch`); the installer retires that legacy timer.
The script's generic mode (`scripts/loop-backup.sh` with no `--layer`) is kept
for manual / emergency "loop fully dead" use.

Install / re-install:
```bash
bash scripts/install-loop-backup.sh            # idempotent: installs the 4 floor crons, retires the legacy timer
bash scripts/install-loop-backup.sh --dry-run  # preview
```

Manual smoke test (no side effects):
```bash
BUBBLE_BACKUP_DRY_RUN=1 scripts/loop-backup.sh --layer 1   # floor L1, dry
BUBBLE_BACKUP_DRY_RUN=1 scripts/loop-backup.sh             # generic, dry
```

Tune the global threshold / model / budget via the template service
`Environment=` lines (`BUBBLE_BACKUP_STALE_SEC`, `BUBBLE_BACKUP_MODEL`,
`BUBBLE_BACKUP_BUDGET_USD`) — no script edit, no per-dept config needed. The
dept set is auto-discovered, not configured; `BUBBLE_BACKUP_DEPTS` remains a
test/pin override only.
