# `deploy/local/` — Mac-side (launchd) runtime for a `host: local` dept

A bubble-ops-loop dept normally runs on the VPS under systemd: a per-dept unit
(`deploy/templates/ops-loop-dept.service.template`) runs its `/loop`, and a
4-cron loop-backup floor (`scripts/install-loop-backup.sh`) force-ticks any dept
whose live loop went stale.

A dept can instead declare `host: local` in `onboarding/STATE.yaml` to run its
`/loop` on an operator's **Mac** (e.g. for real Chrome / local tools). The VPS
cannot reach that Mac, so:

- the VPS **loop-backup floor SKIPS** `host: local` depts (B1), and
- the Mac gets its **own** launchd analogues of the systemd unit + backup floor.

These scripts are the Mac twins. They are **generic** (any local dept, ours or a
client's — parameterized by `--dept-dir` / `--slug`), and they ship in the
open-source repo's `deploy/local/`. They drop all the VPS-only plumbing
(systemd, SOPS env pre-decrypt, the token-broker, tmpfs): on the Mac the dept
pushes via the operator's own **`gh`/git credential**.

## Files

| File | Role |
|------|------|
| `install-local-loop.sh` | Install the **main `/loop` runner** as a **KeepAlive** launchd agent (`com.bubble.ops-loop-<slug>`) supervising a generic wrapper. The systemd-unit twin. |
| `install-local-loop-backup.sh` | Install the **backup floor** as a **StartInterval** launchd agent (`com.bubble.ops-loop-backup-<slug>`). The VPS loop-backup twin, for one local dept. |
| `local-loop-backup-runner.sh` | The per-tick body the backup agent runs: heartbeat-staleness check → force-tick the `/loop` only if stale. |
| `lib/local_loop_lib.sh` | Shared helpers: `is_heartbeat_stale` (the testable core) + `render_loop_wrapper` / `render_loop_plist` / `render_backup_plist`. |

### Main runner shape — persistent `--channels` session (KeepAlive), NOT a per-tick job

The main runner is the **exact Mac twin of the VPS systemd dept unit**: a
**persistent interactive** `claude --dangerously-skip-permissions --channels
plugin:telegram@claude-plugins-official` session, run inside a **tmux** session
(`ops-loop-<slug>` — a human can `tmux attach -t ops-loop-<slug>` to watch it
live) by a generic **wrapper** that launchd **`KeepAlive`** supervises (restarts
on crash). The dept's Telegram bot — its only channel to its owners — needs the
interactive `--channels` binary; `claude -p` would lose the channel + hooks (VPS
Ban #2). Loop **cadence** comes from the dept arming its **own `/loop` cron**
inside the session (boot-rearm), exactly like the VPS depts — not from an
external timer. The **backup floor** below remains a periodic `StartInterval`
job (it's a stale-heartbeat backstop, not a session).

Tests (run from repo root, no launchctl, no live machine):

```sh
bash tests/test_local_loop_staleness.sh   deploy/local/lib/local_loop_lib.sh deploy/local/local-loop-backup-runner.sh
bash tests/test_local_loop_plist_render.sh deploy/local/install-local-loop.sh deploy/local/install-local-loop-backup.sh
```

## Test-safe by default (no `--activate`)

Both installers **only render** the plist (write the file + print what they would
do) unless `--activate` is passed. `launchctl load` happens **only** with
`--activate`. The backup runner likewise only **decides + prints** unless
`--activate-tick` is passed — it never launches `claude` in a dry run. This is
how the test suites exercise the full render + decision paths with zero side
effects.

## Install (on the Mac, only after re-audit PASS + Joris go — see MIRANDA-BUILD-SPEC P4)

```sh
# Dry render first (writes wrapper + plist, NO launchctl) — inspect them:
deploy/local/install-local-loop.sh \
    --dept-dir ~/claude-workspaces/bubble-ops-content --slug content \
    --claude-bin ~/.npm-global/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir ~/.claude/channels/telegram-socials

deploy/local/install-local-loop-backup.sh \
    --dept-dir ~/claude-workspaces/bubble-ops-content \
    --slug content --interval 10800 --stale-sec 5400

# When ready, activate (loads the launchd agents):
deploy/local/install-local-loop.sh        --dept-dir ... --slug content ... --activate
deploy/local/install-local-loop-backup.sh --dept-dir ... --slug content --activate
```

Uninstall: `install-local-loop.sh --uninstall --slug content` (removes the plist
+ wrapper; add `--activate` to also `launchctl unload`).

## Mac-asleep catch-up — no special code

When the Mac is closed/asleep through one or more scheduled windows and is then
reopened:

- the **main runner** (KeepAlive) is relaunched on wake (RunAtLoad + KeepAlive),
  so the persistent `/loop` session comes back up;
- the **backup floor** uses **`StartInterval`** (NOT `StartCalendarInterval`), and
  launchd **coalesces the missed `StartInterval` and fires it on wake**:

- `StartInterval` = "run every N seconds; if a fire was missed while asleep, run
  once on wake." → the backstop always gets a tick shortly after the Mac reopens.
- `StartCalendarInterval` = "run at this wall-clock time." → a window that passed
  while asleep is **silently missed**. We deliberately avoid it.

On wake the dept's existing `/loop` protocol does the catch-up itself —
**no catch-up code is needed here**:

1. **STEP A `safe_pull`** pulls anything merged while the Mac was asleep
   (e.g. approvals the operator committed in the cockpit → the dept's GitHub
   repo). A merged change auto-lands; a dirty tree never blocks it.
2. **`decide_dispatch`'s morning-floor** picks up the layers that should have
   run since the last tick — the dept's work is "since last run", so a missed
   morning is caught up on the first wake tick.
3. **The backup floor is the backstop.** If the main `/loop` session is wedged
   (not merely asleep), the backup agent — also `StartInterval`, so it too fires
   on wake — finds a **stale heartbeat** (`outputs/<today>/heartbeat.log` last
   tick older than `--stale-sec`, default 90 min, mirroring the VPS
   `BUBBLE_BACKUP_STALE_SEC`) and force-ticks the loop once.

### Fail-safe staleness

`is_heartbeat_stale` reuses the canonical `scripts/lib/loop_backup.py`
(`latest_heartbeat_epoch` + `backup_decision`) so the Mac floor and the VPS floor
share **one** staleness definition. A **missing** heartbeat, an unreadable file,
or any error in the check is treated as **stale → tick** (never skip when blind),
and the runner never crashes the launchd agent (a failed force-tick logs but
exits 0, so launchd doesn't fast-respawn).
