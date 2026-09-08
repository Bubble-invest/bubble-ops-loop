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
| 8 | **`/loop` boot re-arm** | **Claude:** `scripts/install-boot-rearm.sh`; **Hermes:** `scripts/wake_hermes_gateway.py` called by `bubble-vps-platform`'s lifecycle helper | **yes** | **Claude keeps the Telegram-plugin/inject path. A Hermes-selected unit has no Claude/Bun poller, so its `ExecStartPost` drops to the department UID and arms one one-shot LoopManager row through the live Hermes control socket. Install source without restarting; prove the path on one separately approved service lifecycle. Claude requires `OPS_LOOP_BOOT_REARM=1` + `OPS_LOOP_DEPT=<slug>`; Hermes requires the root-owned `hermes` selector plus the rendered `BUBBLE_AGENT_BOOT_MESSAGE`. See `tests/test_boot_rearm_install.sh` and `scripts/tests/test_606_hermes_wake.py`.** |
| 9 | **On-box helper scripts** (`/usr/local/bin/`) | **`deploy/bin/*`** (see `deploy/bin/README.md`) — **except `guard-stale-credentials.sh`, which lives at `scripts/guard-stale-credentials.sh`** (board #1150; same `scripts/`-tree convention as `install-boot-rearm.sh`/`install-channel-patches.sh`, so a root-owned `/opt/bubble-ops-loop` checkout + `bubble-safe-install` can source it) | operator | **Token minters (`bubble-board-token{,-refresh}.sh`), pre-auth git/gh wrappers (`bubble-git`, `bubble-gh`), the structural-push guard (`bubble-is-structural-push.py`), the L4 canary (`bubble-layer4-canary.sh`), the watchdog resume drop-in installer (`bubble-watchdog-resume-dropin`), the stale-credentials shadow guard (`guard-stale-credentials.sh`, board #294 / #1150), and the secrets lifecycle tools (`bubble-rotate-dept-secret`, `bubble-secrets` add/rotate/apply, board #676). Copy each into `/usr/local/bin/`; per-script env / sudoers wiring documented in `deploy/bin/README.md`. Operator-specific values are env-driven — never hardcoded.** |

## /loop boot re-arm (step 8) — existing departments

New departments inherit the boot message and Claude-plugin env automatically;
they are rendered into the
canonical `bubble-agent@<slug>.service.d/<slug>.conf` drop-in by
`bubble-vps-platform/scripts/render-agent-units.py`
(`Environment=OPS_LOOP_BOOT_REARM=1` + `Environment=OPS_LOOP_DEPT=<slug>`),
consumed by `scripts/deploy-to-morty.sh` and
`console/services/eclosure_launcher.py`.

Existing live departments may predate those fields. Prefer re-rendering and
reinstalling the canonical unit. A surgical systemd drop-in is appropriate for
the Claude-plugin env only; a Hermes department also needs its root-owned
selector and rendered boot message. Installing source or unit text does not
authorize a restart.

- **Re-render + reinstall the canonical unit** (preferred):
  `scripts/deploy-to-morty.sh --slug=<dept> --tenant-yaml=<path>` renders the
  platform template + instance drop-in, then performs the ordered cutover.
- **systemd drop-in** (surgical, no full re-render):

      sudo systemctl edit bubble-agent@<dept>.service
      # add under [Service]:
      #   Environment=OPS_LOOP_BOOT_REARM=1
      #   Environment=OPS_LOOP_DEPT=<dept>
      sudo systemctl daemon-reload
      # restart bubble-agent@<dept>.service only during a separately approved lifecycle

For a Claude-selected department, the env alone does nothing until the plugin
is patched (`scripts/install-boot-rearm.sh`) and the department later starts
with that patch. Those two variables do not drive the Hermes branch. For a
Hermes-selected department, the canonical `bubble-agent-prepare boot` branch
is selected by `/etc/bubble-harness/<slug>` and passes the rendered
`BUBBLE_AGENT_BOOT_MESSAGE` to
`scripts/wake_hermes_gateway.py` as the department UID. The helper verifies the
live control socket and exact Telegram home session, then arms one persisted
LoopManager row; it never starts a second gateway or model process.

Install both helpers from reviewed source without restarting a department.
Live acceptance is a separate, one-service lifecycle action: preserve the
session and queues, require one consumed loop row and one fresh normal
heartbeat, and stop after the first unexplained failure.

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
agent UID. The runner never shell-sources dotenv text. The isolated production
floor is primary-wake-only: Ben and Tony receive their normal tick through the
existing channel injection path, while Maya's Hermes floor verifies the live
profile through `gateway.sock` and arms a one-shot persisted `/loop` row. No
second model process starts. A missing, busy, failed, or ambiguous primary wake
returns nonzero, records a visible deferred event, and leaves heartbeat and
mission completion evidence untouched. Independent headless failover is
disabled until the primary/floor mutex can be enforced outside the primary's
instruction-following loop. Tony relays operator-approved directives and publishes manager status
through private remote clones, so a manager-push failure leaves the live source
approved and retryable. A normalized SHA-256 snapshot binds the child commit to
the manager acknowledgement; a same-ID child payload or remotely edited source
with different directive semantics fails visibly instead of being acknowledged.

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
