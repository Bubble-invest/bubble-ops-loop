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
| 4 | **Loop BACKUP execution** | **`scripts/install-loop-backup.sh`** | **yes** | **Twice-daily (08:00+14:00 Paris) backup tick for any dead/parked dept loop. The safety net.** |
| 5 | Restic backups | `scripts/morty-restic-setup.sh` | yes | 6h backup + retention timers. |
| 6 | **OS sandbox (Layer B)** | **`scripts/install-sandbox.sh`** | **yes** | **bwrap+socat+sandbox-runtime+AppArmor + merges the sandbox block into managed-settings. Jails the Bash tool fleet-wide (anti prompt-injection). Restart agents after, verify via userns check. See `deploy/sandbox-tests/` + wiki `vps-agent-sandbox`.** |
| 7 | Age-key offline backup | `scripts/backup-age-key.sh` | operator | Needs Keychain passphrase (operator). |

## Loop backup (step 4) — what it is

Each dept runs a persistent `/loop` session. If that session dies for any
reason — auth lapse, crash, OOM, or "parked" after a restart — the dept
silently stops working while systemd still reports `active`. The loop
backup is an independent safety net:

- Fires twice a day via `loop-backup.timer` (08:00 + 14:00 Europe/Paris).
- For each dept, checks heartbeat freshness (`scripts/lib/loop_backup.py`):
  - fresh (loop alive) → **skip** (no double-processing),
  - stale > 90 min, or no heartbeat → run **ONE** dispatch tick via
    `claude -p` in the dept's authed workspace, then stop.
- A `flock` mutex guarantees the backup tick never overlaps a live tick;
  `decide_dispatch` is deterministic, so a backup tick is idempotent.

Install / re-install:
```bash
bash scripts/install-loop-backup.sh            # idempotent
bash scripts/install-loop-backup.sh --dry-run  # preview
```

Manual smoke test (no side effects):
```bash
BUBBLE_BACKUP_DRY_RUN=1 scripts/loop-backup.sh
```

Tune per-dept set / threshold via the service `Environment=` lines
(`BUBBLE_BACKUP_DEPTS`, `BUBBLE_BACKUP_STALE_SEC`, `BUBBLE_BACKUP_MODEL`,
`BUBBLE_BACKUP_BUDGET_USD`) — no script edit needed.
