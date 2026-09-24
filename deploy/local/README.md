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
| `install-local-loop-backup.sh` | Install the **backup floor** as a **StartInterval** launchd agent (`com.bubble.ops-loop-backup-<slug>`, default 3h). The VPS loop-backup twin, for one local dept. With `--wake-catch` it renders the **wake-catch** agent (`com.bubble.ops-loop-wake-<slug>`, default 5m) instead — same runner, shorter interval, so a stale loop is caught promptly after the Mac wakes. |
| `local-loop-backup-runner.sh` | The per-tick body: optional due-mission plan from `dept.yaml` (`loop.due_dispatch` lease/claims schema, or — board #1491 — a `due_missions.py wake-prompt` envelope for the `recurring_missions` schema, falling back to the historical generic wake text unchanged on any refusal/error) → heartbeat-staleness check (overridden once by due periodic work) → harness-aware wake of the existing tmux session (Claude secure inject file, or Hermes gateway helper). It never launches a model. |
| `lib/local_loop_lib.sh` | Shared helpers: `is_heartbeat_stale` (the testable core) + `render_loop_wrapper` / `render_loop_plist` / `render_backup_plist`. |
| `bubble-deploy-mac.sh` | Board #1477: keeps the Mac's `bubble-ops-loop` **framework checkout itself** (`~/claude-workspaces/bubble-ops-loop`) current — `git fetch` + `merge --ff-only origin/main`, only on a clean `main` checkout. Mac twin of `scripts/bubble-deploy.sh --infra-only`. Refuses (ALERT + exit 1, worktree untouched) on a dirty tree, a non-`main` branch, or a diverged (ahead) checkout. Board #1488 widened its "never execute from the pulled tree" contract for one explicit, hard-coded, allow-listed installer — see below. |
| `install-mac-loop-deploy.sh` | Installs `bubble-deploy-mac.sh` as a **StartInterval** launchd agent (`com.bubble.mac-loop-deploy`, default 15 min — mirrors `bubble-deploy-infra.timer`'s `*:0/15`). One instance per Mac (not per-slug): it updates the shared framework checkout every local dept's installers run out of. |

## `bubble-deploy-mac.sh`'s widened contract (#1488)

Board #1488: all 3 Macs were found running a telegram-plugin `boot_rearm.ts`
from June (missed board #850), because `bubble-deploy-mac.sh` kept the
**framework checkout** current but never re-ran the installers that actually
apply that framework to the live Mac state — `git log` moving forward said
nothing about whether the Mac's plugin cache or vendored hooks had. Joris
approved (Telegram: "ok, not manual") widening the script's "never execute
anything from the pulled tree" rule, but **only** for one explicit,
hard-coded, allow-listed installer — this is a deliberate, narrow carve-out,
not a blanket allow to run arbitrary vendored scripts. After a successful
fast-forward, or when the checkout is already current (both cases already
passed the same clean-`main`-at-`origin/main` verification), the script now
also:

1. **Re-runs `scripts/install-boot-rearm.sh`** (the sole allow-listed
   executable) — idempotent, vetted, and already the canonical installer for
   this exact patch (see `deploy/telegram-plugin/`). On a Mac it's invoked
   with `BOOT_REARM_PLUGIN_GLOB="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/"`
   and `BOOT_REARM_BUN` set to `~/.bun/bin/bun` (falling back to `command -v
   bun` when that isn't present) — the same env-var contract
   `scripts/install-channel-patches.sh` already uses, just resolved for a
   Mac's `$HOME` instead of the VPS's `/home/claude`. A cheap pre-check
   (`cmp` the vendored source against the installed plugin copy + `grep` for
   `bootRearmNotification` in `server.ts`) means the installer — and its
   `bun build` validation — only actually runs when there's real drift to
   fix, not on every 15-minute tick forever. It never restarts anything;
   Rick controls restarts.
2. **Compare-and-installs `deploy/hooks/rearm-loop-on-compact.py`** into
   `$SUPPORT_DIR/hooks/rearm-loop-on-compact.py` — copy-only, exactly like
   the existing rotate-script vendoring — but **only when that path already
   exists** there, i.e. the Mac already opted in to the compact re-arm hook
   by having it vendored once. It never creates the hook from scratch. Every
   overwrite keeps a timestamped `.bak-<UTC-timestamp>` of the previous
   content.

Every action is logged (`BOOT-REARM: ...` / `COMPACT-HOOK: ...` lines). An
installer failure raises an ALERT and the script exits 1 (so the launchd
job's own exit status surfaces it) — but a fast-forward that already
succeeded is **never** rolled back because a later step failed; only that
step's own failure is reported. Everything else this script touches stays
copy-only, as before.

Covered by `tests/test_1477_bubble_deploy_mac_safe_ff.sh` (T10-T15), which
stubs both the installer and `bun` via env overrides
(`BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER`, `BOOT_REARM_PLUGIN_GLOB`,
`BOOT_REARM_BUN`) so the suite never touches a real bun or a real telegram
plugin cache.

**Deploy step after this PR merges:** re-run
`install-mac-loop-deploy.sh --activate` on each Mac (jade-m1, jade-m5,
Joris's Mac) so the vendored `bubble-deploy-mac.sh` copy under
`~/Library/Application Support/bubble-ops-loop/` picks up the widened
contract — the launchd job runs the vendored copy, not the live checkout.

## Operator-intents: read from the wiki, not an isolated host mirror (#1333)

Board #1333 (Option C, Joris-approved 2026-09-14) retired the isolated,
root-owned, filesystem-immutable operator-intents mirror this section used to
describe (`install-operator-intents-mirror.sh`, its sync/plist templates, and
`verify-operator-intents-isolation.sh` — never actually deployed on any host,
Mac or VPS — are removed; git history preserves them). Agents now read
operator intents straight from the wiki's own `shared/operator-intents/` (see
`skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh` and
`wiki_intent_audit.py`). The tamper guarantee is no longer filesystem
ownership/mode/symlink/manifest immutability; it is the git-level branch-hook
(#12): agents can't commit straight to a protected `main`, only open a PR a
human merges. The private vault
(`Bubble-invest/bubble-operator-intents`) stays the authoritative,
human-only record — Joris alone changes/merges it and the wiki copy is
git-PR-gated from there; agents never write/push the vault or receive vault
credentials. Proposals still go only to `shared/operator-intents-proposals/`
through a Joris-reviewed shared-wiki PR.

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
bash tests/test_local_loop_injection_floor.sh deploy/local/local-loop-backup-runner.sh
bash tests/test_rnd_due_mission_floor.sh deploy/local/local-loop-backup-runner.sh
bash tests/test_1491_mac_recurring_wake_inject.sh deploy/local/local-loop-backup-runner.sh
python3 -m pytest -q scripts/lib/tests/test_due_missions.py
```

## Test-safe by default (no `--activate`)

Both installers **only render** the plist unless `--activate` is passed.
`launchctl load` happens only with `--activate`. The backup plist always selects
`--activate-inject`: a stale heartbeat appends one fixed wake to the explicitly
configured existing channel/session, subject to a cooldown. The runner contains
no model-launch path. Calling it without `--activate-inject` reports a non-green
deferred result, and legacy `--activate-tick` is rejected. The runner re-reads
the main wrapper's security-checked harness selector each time. `claude` uses
the private channel inject path and its shared cooldown marker. `hermes` pipes
the same fixed resume prompt to `scripts/wake_hermes_gateway.py`, using the
existing tmux session and the local install defaults
`~/.hermes/hermes-agent/venv/bin/python`, `~/.hermes/hermes-agent`, and
`~/.hermes/profiles/<slug>`. The helper validates and re-arms an active loop only
when it already targets the exact selected Telegram session; otherwise it
creates an exact-route one-shot rescue and verifies the persisted row. Helper
acceptance is explicitly logged as unconfirmed; it is not proof that the turn
executed or heartbeat advanced.

## Install (on the Mac, only after re-audit PASS + {{OPERATOR}} go — see MIRANDA-BUILD-SPEC P4)

```sh
# Dry render first (writes wrapper + plist, NO launchctl) — inspect them:
deploy/local/install-local-loop.sh \
    --dept-dir ~/claude-workspaces/bubble-ops-content --slug content \
    --claude-bin ~/.npm-global/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir ~/.claude/channels/telegram-socials

deploy/local/install-local-loop-backup.sh \
    --dept-dir ~/claude-workspaces/bubble-ops-content --slug content \
    --telegram-state-dir ~/.claude/channels/telegram-socials \
    --session-name ops-loop-content --tmux-bin /opt/homebrew/bin/tmux \
    --harness-selector "$HOME/Library/Application Support/bubble-ops-loop/harness-content" \
    --interval 10800 --stale-sec 5400 --cooldown-sec 900

# When ready, activate (loads the launchd agents):
deploy/local/install-local-loop.sh        --dept-dir ... --slug content ... --activate
deploy/local/install-local-loop-backup.sh --dept-dir ... --slug content --telegram-state-dir ... --session-name ... --tmux-bin ... --activate

# ALSO install the wake-catch agent (same args + --wake-catch): catches a stale
# loop promptly after the Mac wakes, not only at the 3h floor window. Default
# interval is 5m; it shares the runner + cooldown marker with the floor, so it
# can never double-tick a healthy loop.
deploy/local/install-local-loop-backup.sh --dept-dir ... --slug content --telegram-state-dir ... --session-name ... --tmux-bin ... --wake-catch --activate
```

The backup installer renders and lints a private candidate before atomically
publishing the plist. Without `--activate`, the loaded job is untouched. With
`--activate`, a load failure restores both the previous plist bytes and prior
registration state.

Uninstall: `install-local-loop.sh --uninstall --slug content` (removes the plist
+ wrapper; add `--activate` to also `launchctl unload`).

## Rick #1269 post-merge render and activation (not performed by the PR)

Merge/update the R&D shape **before** activating the dispatcher: a runner that
sees `loop.due_dispatch` validates all scoped rules and fails closed rather than
falling back to the old generic wake. These commands do not edit
`~/.claude/agents/rnd.md` or Rick's live mandate.

**Interpreter pin (board #1330):** `due_missions.py` (and other scripts/lib/*.py
dept.yaml readers) import PyYAML at module scope. A bare `python3` on a Mac can
resolve NON-DETERMINISTICALLY between an interpreter that has pyyaml and one
that doesn't (e.g. a homebrew python@3.x upgrade vs. the CommandLineTools
python), silently crashing a mission's `complete` step — the completion looks
identical to one that never ran (#1235/#1316's class). `install-local-loop-backup.sh`
now runs `deploy/local/ensure-loop-venv.sh` automatically, which builds a
dedicated `<repo-root>/.venv` (from `scripts/requirements.txt`) and verifies it
can `import yaml`; `_lll_py()` in `lib/local_loop_lib.sh` then prefers that
pinned venv (by absolute path — never a fresh PATH lookup) over bare
`python3`/`python`, and every candidate is verified to import yaml before being
returned rather than trusted blindly. `due_missions.py`'s generated `COMPLETE
... =>` command uses `sys.executable` (the interpreter that is actually running
it, i.e. the same pinned venv) instead of a hardcoded `"python3"`, so the
completion command Rick's session runs at the end of a tick is pinned too. To
(re)build the venv manually: `deploy/local/ensure-loop-venv.sh [--force]`.

The live owner checkouts are not interchangeable with clean deploy clones. At
the time this note was written, `/Users/joris/claude-workspaces/Rick_RnD` was
dirty and diverged (ahead 1, behind 29); preserve and reconcile that owner work
first without stash, reset, checkout-overwrite, or destructive cleanup. The
framework checkout was clean but behind. Only a clean checkout on the exact
`main` branch whose HEAD is an ancestor of `origin/main` may fast-forward:

```sh
# One fail-stop preflight + dry-render transaction. Any failed branch, dirty-
# tree, ancestry, fetch, merge, plan, render, or lint check stops this subshell
# before the later steps run. It never activates a LaunchAgent.
(
set -eu
for repo in "$HOME/claude-workspaces/Rick_RnD" "$HOME/claude-workspaces/bubble-ops-loop"; do
  git -C "$repo" status --short --branch
  git -C "$repo" fetch origin main
  test "$(git -C "$repo" branch --show-current)" = main
  test -z "$(git -C "$repo" status --porcelain)"
  git -C "$repo" merge-base --is-ancestor HEAD origin/main
  git -C "$repo" merge --ff-only origin/main
done

# Read-only proof of the currently due set before touching launchd:
cd ~/claude-workspaces/bubble-ops-loop
python3 scripts/due_missions.py plan \
  --dept-dir ~/claude-workspaces/Rick_RnD --format json

# Render both plist candidates only; loaded jobs remain unchanged:
deploy/local/install-local-loop-backup.sh \
  --dept-dir "$HOME/claude-workspaces/Rick_RnD" --slug rnd \
  --telegram-state-dir "$HOME/.claude/channels/telegram-rnd" \
  --session-name ops-loop-rnd --tmux-bin "$HOME/.local/bin/tmux" \
  --harness-selector "$HOME/Library/Application Support/bubble-ops-loop/harness-rnd" \
  --interval 10800 --stale-sec 5400 --cooldown-sec 900
deploy/local/install-local-loop-backup.sh \
  --dept-dir "$HOME/claude-workspaces/Rick_RnD" --slug rnd \
  --telegram-state-dir "$HOME/.claude/channels/telegram-rnd" \
  --session-name ops-loop-rnd --tmux-bin "$HOME/.local/bin/tmux" \
  --harness-selector "$HOME/Library/Application Support/bubble-ops-loop/harness-rnd" \
  --wake-catch --stale-sec 5400 --cooldown-sec 900

plutil -lint "$HOME/Library/LaunchAgents/com.bubble.ops-loop-backup-rnd.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.bubble.ops-loop-wake-rnd.plist"
)

# Only after Joris approves activation, rerun those same two renderer commands
# with --activate appended, then inspect both jobs and their logs:
launchctl print "gui/$(id -u)/com.bubble.ops-loop-backup-rnd"
launchctl print "gui/$(id -u)/com.bubble.ops-loop-wake-rnd"
tail -n 50 "$HOME/Library/Logs/bubble-ops-loop/com.bubble.ops-loop-"{backup,wake}"-rnd.out.log"
```

Do not pre-populate the watermark. Its absence is the intended first-run state:
M2-M7 catch up once, while continuous M1/M8 run on every actual tick. The
watermark advances only when Rick executes a prompt-supplied `COMPLETE` command
after that mission succeeds. Accepted periodic work carries a six-hour pending
delivery lease so the backup and wake-catch agents cannot duplicate it after
the shorter inbox cooldown. Pending is not success: an uncompleted mission is
eligible again when the lease expires, and a failed injection releases its own
claims immediately.

## Framework-clone auto-update (board #1477)

The scripts above assume `~/claude-workspaces/bubble-ops-loop` on the Mac is
itself kept current — that's where every installer, wrapper, and the rotate
script are read from. On the VPS that's `bubble-deploy-infra.timer` (every 15
min, ff-only, `/opt/bubble-ops-loop`). The Mac clones had **no equivalent**
until #1477: jade-m1 drifted ~100 commits behind main with no upstream
tracking at all, and jade-m5's clone was ~100 merges behind with a missing
`.venv`, so its backup floor likely couldn't even run.

`install-mac-loop-deploy.sh` closes that gap — the Mac twin of
`bubble-deploy-infra.timer` + `bubble-deploy.sh --infra-only`, simplified for
a single non-systemd checkout (no per-dept owner switching, no primary-unit
defer logic):

```sh
# Dry render first (writes the plist, vendors the runner, NO launchctl):
deploy/local/install-mac-loop-deploy.sh

# Inspect, then activate:
deploy/local/install-mac-loop-deploy.sh --activate

# Uninstall:
deploy/local/install-mac-loop-deploy.sh --uninstall --activate
```

Every 15 minutes (default; `--interval SECONDS` to change), `bubble-deploy-mac.sh`:
`git fetch origin main`, then `merge --ff-only` **only** when the checkout is
on `main` with a clean working tree. A dirty tree, a non-`main` branch, or a
checkout that has diverged (local commits ahead of `origin/main`) is left
completely untouched — the run logs an `ALERT:` line and exits 1, which shows
up in `~/Library/Logs/bubble-ops-loop/com.bubble.mac-loop-deploy.err.log` and
as the launchd job's `LastExitStatus` (`launchctl print
gui/$(id -u)/com.bubble.mac-loop-deploy`) for the floor/watchdog to catch. It
never executes anything from the pulled tree — the one exception is a plain
`install` (copy, not execute) of the vendored `bubble-session-rotate-mac.sh`
into Application Support when a merge changed its content, so a merged rotate
fix lands automatically instead of needing a manual re-run of
`install-session-rotate-mac.sh` after every framework update.

Tests: `bash tests/test_1477_bubble_deploy_mac_safe_ff.sh`.

## No-sudo tmux (M5 hosts without Homebrew)

The main runner wants tmux so a human can `tmux attach -t ops-loop-<slug>` to
**watch and type to** the agent live. On a Mac with no admin rights ("M5": no
sudo → no Homebrew → no tmux) that used to force a `/usr/bin/script` fallback
wrapper — viewable via `tail -f` of the transcript but **not** attachable.

`install-tmux-nosudo.sh` closes the gap: it builds tmux (+ static libevent) from
upstream source into `~/.local`, needing only the Xcode CLT (`cc`/`make`). Then
render the wrapper with that tmux — no `script` fallback:

```sh
deploy/local/install-tmux-nosudo.sh                       # → ~/.local/bin/tmux

deploy/local/install-local-loop.sh \
    --dept-dir ~/claude-workspaces/bubble-ops-accountant --slug accountant \
    --claude-bin ~/.local/bin/claude --tmux-bin ~/.local/bin/tmux \
    --telegram-state-dir ~/.claude/channels/telegram-accountant --activate
```

Note: the accountant wrapper also exports `PYTHONPATH` for the
`department-onboarding-guide` skill — preserve it when re-rendering.

## Mac-asleep catch-up — StartInterval + wake-catch

The Macs are laptops that spend long stretches asleep (M5 especially), so the
floor must survive sleep. When the Mac is closed/asleep through one or more
scheduled windows and is then reopened:

- the **main runner** (KeepAlive) is relaunched on wake (RunAtLoad + KeepAlive),
  so the persistent `/loop` session comes back up;
- the **backup floor** uses **`StartInterval`** (NOT `StartCalendarInterval`), and
  launchd **coalesces the missed `StartInterval` and fires it on wake**:

- `StartInterval` = "run every N seconds; if a fire was missed while asleep, run
  once on wake." → the backstop always gets a tick shortly after the Mac reopens.
- `StartCalendarInterval` = "run at this wall-clock time." → a window that passed
  while asleep is **silently missed**. We deliberately avoid it.

For a dept with `loop.due_dispatch`, the wake-time planner then compares each
allow-listed mission's current local calendar token (day, ISO week, or month)
with its last-success watermark. A missed period therefore becomes due on the
first floor invocation after wake (even if M1's heartbeat is fresh); there is
no fixed clock window to miss. A mission already successful in the current
period is omitted. Continuous
missions remain due on every actual tick. The wake/inbox append writes no
success watermark: the injected turn receives one explicit completion command
per due mission and advances only the missions that actually succeeded. Before
append, the floor atomically claims each periodic mission-period for the
manifest's bounded `pending_lease_seconds`; competing floor agents suppress an
unexpired claim, and any failed append releases only the caller's claim.

For a dept with `recurring_missions` but no `loop.due_dispatch` block —
Miranda/content (jade-m1) and Géraldine/accountant (jade-m5), the same schema
ben/tony/maya use on the VPS — board #1491 wires this same runner to
`due_missions.py wake-prompt --dept-dir <dept>` (the generator board #1487
built for that schema, already trusted by the VPS floor's own idle-nudge,
`scripts/loop-backup.sh::inject_live_loop`). It has no lease/claims concept to
reuse (VPS-style completion is a timestamp ledger, not a calendar-period
watermark), so on any refusal or error (unrecognized schema, nothing due this
instant, hard error) the runner falls back to the historical generic wake
text, byte-for-byte unchanged — never fatal.

### Prompt wake-catch (`com.bubble.ops-loop-wake-<slug>`)

launchd's coalesce-on-wake fires the missed `StartInterval` only **once** on wake
and its timing can **lag**, so on the 3h floor a loop that is stale at wake could
wait a long time for its catch-up tick. The **wake-catch** agent
(`install-local-loop-backup.sh --wake-catch`, default 5m interval) closes that
gap: it runs the **same** `local-loop-backup-runner.sh` (same staleness guard,
same shared cooldown marker), so after wake a stale loop gets a real `/loop`
inject within one short interval, and a still-wedged loop keeps getting re-caught
each interval while awake — while a fresh or recently-injected loop is an instant
no-op. This is a **native launchd** mechanism (no `sleepwatcher` / third-party
dep; the unified-log `Wake reason` predicate is version-fragile and was empty in
testing, so it is deliberately not used). The `pmset -g log` "Wake" line is the
authoritative record if you need to correlate a wake with a wake-catch fire.

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
