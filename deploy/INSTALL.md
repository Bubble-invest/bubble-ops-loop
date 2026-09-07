# bubble-ops-loop — box install manifest

Canonical list of the box-level (VPS) install steps for the
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
| 1 | Per-dept agent units (`bubble-agent@<slug>`) | `bubble-vps-platform/scripts/render-agent-units.py` from tenant.yaml + dept.yaml | yes | One per live dept. Decrypts per-dept SOPS → `/run/bubble-agent-<slug>/env`. |
| 2 | Console (cockpit) | `console/deploy/bubble-ops-console.service.template` + `scripts/deploy-console-to-vps.sh` | yes | Tailscale-served `:8443`. Fresh box: run `deploy-console-to-vps.sh` directly (materializes the unit, no pre-existing service needed). Ongoing deploys: `scripts/deploy-console-to-morty.sh` (git-pull + restart) calls `deploy-console-to-vps.sh --no-restart` after every pull, so a template edit merged to `main` reaches the box on the next deploy — see board #1081. |
| 3 | Loop liveness watchdog (alerts) | `scripts/ops-loop-watchdog.{service,timer}` + `scripts/loop-watchdog.sh` | yes | Telegram alert on stale heartbeat. |
| 4 | **Isolated loop layer floor** | **`scripts/install-loop-backup.sh`** | **yes** | **One timer per enabled department and supported layer, running as `agent-<slug>` from `/srv/agents/<slug>`. Existing 09:00 / 14:00 / 18:00 / 21:00 Paris cadence and 120-second jitter are preserved.** |
| 5 | Restic backups | `scripts/morty-restic-setup.sh` | yes | 6h backup + retention timers. (Script filename kept as-is; see script for VPS-specific config.) |
| 6 | **OS sandbox (Layer B)** | **`scripts/install-sandbox.sh`** | **yes** | **bwrap+socat+sandbox-runtime+AppArmor + merges the sandbox block into managed-settings. Jails the Bash tool fleet-wide (anti prompt-injection). Restart agents after, verify via userns check. See `deploy/sandbox-tests/` + wiki `vps-agent-sandbox`.** |
| 7 | Age-key offline backup | `scripts/backup-age-key.sh` | operator | Needs Keychain passphrase (operator). |
| 8 | **`/loop` boot re-arm (telegram plugin)** | **`scripts/install-boot-rearm.sh`** | **yes** | **Patches the telegram channel plugin so a dept's `/loop` re-arms on poller startup after ANY restart (synthetic boot turn via MCP channel notification — supersedes `bubble-loop-reinit.sh`). Re-run after every deploy / plugin update (the plugin cache is volatile). Source-of-truth in `deploy/telegram-plugin/`. Restart depts after to load the patched plugin. Requires `OPS_LOOP_BOOT_REARM=1` + `OPS_LOOP_DEPT=<slug>` in the unit (in the template for new depts; drop-in for existing ones — see below). See `tests/test_boot_rearm_install.sh`. The LIVE boot-rearm path in production is actually the template's second `ExecStartPost` (inject-file write, not the MCP notification) — board #461 generalized that SAME turn to also re-arm any dept-declared `config/crons.yaml` durable cron (mail-brief, etc.), not just `/loop`. See `docs/durable-cron-manifest.md`.** |
| 9 | **On-box helper scripts** (`/usr/local/bin/`) | **`deploy/bin/*`** (see `deploy/bin/README.md`) — **except `guard-stale-credentials.sh`, which lives at `scripts/guard-stale-credentials.sh`** (board #1150; same `scripts/`-tree convention as `install-boot-rearm.sh`/`install-channel-patches.sh`, so a root-owned `/opt/bubble-ops-loop` checkout + `bubble-safe-install` can source it) | operator | **Token minters (`bubble-board-token{,-refresh}.sh`), pre-auth git/gh wrappers (`bubble-git`, `bubble-gh`), the structural-push guard (`bubble-is-structural-push.py`), the L4 canary (`bubble-layer4-canary.sh`), the watchdog resume drop-in installer (`bubble-watchdog-resume-dropin`), the stale-credentials shadow guard (`guard-stale-credentials.sh`, board #294 / #1150), and the secrets lifecycle tools (`bubble-rotate-dept-secret`, `bubble-secrets` add/rotate/apply, board #676). Copy each into `/usr/local/bin/`; per-script env / sudoers wiring documented in `deploy/bin/README.md`. Operator-specific values are env-driven — never hardcoded.** |

## /loop boot re-arm (step 8) — env for existing depts

NEW depts inherit the boot-rearm env automatically: it is rendered into the
canonical `bubble-agent@<slug>.service.d/<slug>.conf` drop-in by
`bubble-vps-platform/scripts/render-agent-units.py`
(`Environment=OPS_LOOP_BOOT_REARM=1` + `Environment=OPS_LOOP_DEPT=<slug>`),
consumed by `scripts/deploy-to-morty.sh` and
`console/services/eclosure_launcher.py`.

EXISTING live depts (tony, maya, cgp, claudette, …) were provisioned before
this env existed, so their installed units lack it. Two ways to add it (Rick
applies to live units; this installer never touches live units):

- **Re-render + reinstall the canonical unit** (preferred):
  `scripts/deploy-to-morty.sh --slug=<dept> --tenant-yaml=<path>` renders the
  platform template + instance drop-in, then performs the ordered cutover.
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

Each VPS department has one isolated timer instance per supported OODA layer.
The service runs as that department's `agent-<slug>` UID from
`/srv/agents/<slug>` and reads the root-owned framework at
`/opt/bubble-ops-loop`; it never uses the retired shared `claude` identity.

- `loop-layer1@<slug>.timer` — 09:00 Europe/Paris
- `loop-layer2@<slug>.timer` — 14:00 Europe/Paris
- `loop-layer3@<slug>.timer` — 18:00 Europe/Paris
- `loop-layer4@<slug>.timer` — 21:00 Europe/Paris

The existing 120-second deterministic jitter, persistence, heartbeat freshness,
layer offset/prerequisite, mission due-set, and approval gates remain. The
installer discovers enabled `bubble-agent@<slug>.service` instances and creates
timers only for workspaces with `dept.yaml` and the matching layer prompt.
Concierges are skipped.

Systemd loads `/run/bubble-agent-<slug>/env` literally before dropping to the
agent UID. The runner never shell-sources dotenv text. Live and floor ticks use
the same department-private runtime lock. Maya's Hermes floor first verifies the
live profile through `gateway.sock`, then arms a one-shot persisted `/loop` row;
the existing gateway idle watcher injects it into the home Telegram session. No
second model process starts. Paused, busy, missing, or ambiguous sessions defer
visibly. Tony relays operator-approved directives and publishes manager status
through private remote clones, so a manager-push failure leaves the live source
approved and retryable.

Install or stage for review:
```bash
bash scripts/install-loop-backup.sh --dry-run       # preview, no writes/timer changes
bash scripts/install-loop-backup.sh                 # install unit files, leave timers unchanged
bash scripts/install-loop-backup.sh --activate      # reviewed cutover only; may catch up persistent timers
```

`--activate` is fleet-wide. Combining it with `--dept` or
`BUBBLE_FLOOR_DEPTS` is rejected before any write because retiring the four
global timers for a partial replacement would drop omitted departments. The
cutover snapshots every global timer's enabled/active state and restores those
exact states if either global retirement or replacement activation fails.

The old global templates remain for rollback, but the installer does not enable
them. Manual fixture runs can pin one department explicitly:
```bash
BUBBLE_BACKUP_DRY_RUN=1 scripts/loop-backup.sh --layer 1 --dept maya
```

| 9 | Cache sync (every 10min) |  | yes | Keeps /srv/bubble-ops/repos/ synced with GitHub. |
| 10 | Secrets tmp sweep (every 30min) |  | yes | Scans /tmp for leaked plaintext secrets. |
| 11 | Transcript leak scan (daily 06:30) |  | yes | Scans JSONL transcripts for credential leaks. |
