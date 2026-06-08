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
| 8 | **`/loop` boot re-arm (telegram plugin)** | **`scripts/install-boot-rearm.sh`** | **yes** | **Patches the telegram channel plugin so a dept's `/loop` re-arms on poller startup after ANY restart (synthetic boot turn via MCP channel notification — supersedes `bubble-loop-reinit.sh`). Re-run after every deploy / plugin update (the plugin cache is volatile). Source-of-truth in `deploy/telegram-plugin/`. Restart depts after to load the patched plugin. Requires `OPS_LOOP_BOOT_REARM=1` + `OPS_LOOP_DEPT=<slug>` in the unit (in the template for new depts; drop-in for existing ones — see below). See `tests/test_boot_rearm_install.sh`.** |

## /loop boot re-arm (step 8) — env for existing depts

NEW depts inherit the boot-rearm env automatically: it is baked into
`deploy/templates/ops-loop-dept.service.template`
(`Environment=OPS_LOOP_BOOT_REARM=1` + `Environment=OPS_LOOP_DEPT=<slug>`),
substituted per-dept at scaffold/deploy time
(`scripts/deploy-to-morty.sh`, `console/services/eclosure_launcher.py`).

EXISTING live depts (tony, maya, cgp, claudette, …) were provisioned before
this env existed, so their installed units lack it. Two ways to add it (Rick
applies to live units; this installer never touches live units):

- **Re-render + reinstall the unit** (preferred, keeps the unit in sync with
  the template): `scripts/deploy-to-morty.sh --slug=<dept>` re-renders from the
  template (now including the env) and reinstalls the unit, then
  `daemon-reload` + restart.
- **systemd drop-in** (surgical, no full re-render):

      sudo systemctl edit ops-loop-<dept>.service
      # add under [Service]:
      #   Environment=OPS_LOOP_BOOT_REARM=1
      #   Environment=OPS_LOOP_DEPT=<dept>
      sudo systemctl daemon-reload
      sudo systemctl restart ops-loop-<dept>.service

The env alone does nothing until the plugin is patched
(`scripts/install-boot-rearm.sh`) AND the dept is restarted so the patched
plugin code loads.

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
  tick's work summary is relayed to {{OPERATOR}} on Telegram.
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

| 9 | Cache sync (every 10min) |  | yes | Keeps /srv/bubble-ops/repos/ synced with GitHub. |
| 10 | Secrets tmp sweep (every 30min) |  | yes | Scans /tmp for leaked plaintext secrets. |
| 11 | Transcript leak scan (daily 06:30) |  | yes | Scans JSONL transcripts for credential leaks. |
