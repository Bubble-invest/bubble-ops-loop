# Fixture Readiness — Sandbox Test Bed (Waves 1–2)

**Owner:** Rick (R&D) / `fixture-builder` subagent.
**Date:** 2026-06-02 (live investigation timestamp on box: 2026-06-01 23:16 UTC).
**Method:** Read-only SSH to `hetzner-root` + `sudo -u claude` reads. No service
enabled/started/restarted, no settings edited, no live agent touched.
**Parent:** `../SANDBOX-SCOPING.md`, `./CONTEXT.md`.

---

## VERDICT — READY WITH ONE DOCTRINE CAVEAT

The fixture at `/home/claude/agents/fixture` (symlink `bubble-ops-fixture → fixture`)
is a fully usable, zero-blast-radius test bed for Waves 1–2. All five prerequisite
checks pass:

1. Dir structure / settings / disabled service — **OK**
2. Push chain identical to live depts — **OK**
3. Launch command reconstructed from the real unit — **OK**
4. Trust dialog already accepted for the fixture cwd — **OK** (no blocker)
5. No active session/lock on the fixture cwd — **OK** (nothing else owns it)

**The one caveat (must be a deliberate Wave-2 decision, not a surprise):** the live
units carry an explicit doctrine **"Ban #2 — NO `claude -p` / `--print`"**. The task
brief asks for a headless `-p` invocation; that conflicts with the live doctrine for
*two* reasons baked into the maya unit. This does **not** block T1/T2/T3 — `-p` is the
correct, low-friction choice for one-shot deterministic probes — but Rick must make
the call consciously and document why the probe deviates from the service doctrine.
See **§4 (Launch command)** and **§7 (Ban #2)**.

---

## 1. Fixture structure, settings, disabled service `[VERIFIED-LIVE]`

```
/home/claude/agents/fixture/        (owner claude:claude, drwxrwxr-x)
├── .claude/
│   ├── settings.json   (1888 b — workspace perms, NO sandbox keys yet → clean slate)
│   └── hooks/session-start.sh   (SessionStart hook, injects /loop self-init context)
├── CLAUDE.md, MANDATE.md, dept.yaml (slug=fixture, level=ops, status=retired)
├── .git/   (remote → vdk888/bubble-ops-fixture)
├── inbox/ queues/ outputs/ layers/ missions/ onboarding/ skills/ subagents/ tools/ tests/
```

Symlink: `/home/claude/agents/bubble-ops-fixture -> fixture` — **CONFIRMED**.

`.claude/settings.json` today (relevant keys):
- `permissions.defaultMode: "acceptEdits"`, allow/deny lists for Read/Write/Bash
  (includes `Bash(/opt/bubble-git-guard/bin/bubble-git-guard *)` and `Bash(git push *)`).
- `hooks.SessionStart` → `.claude/hooks/session-start.sh` (timeout 5000).
- `env`: `PATH=/home/claude/.bun/bin:...`, `TELEGRAM_STATE_DIR=.../telegram-fixture`,
  `BUBBLE_DEPT=fixture`.
- **No `sandbox` block** — exactly the clean baseline you want; Wave-1 probes will
  add a `sandbox` block here (project settings, NOT managed), per the rollout plan.

`ops-loop-fixture.service`:
- `is-enabled` → **disabled**. `is-active` → **inactive (dead)**. Last ran/stopped
  2026-06-01 16:53 UTC (manual smoke). **It is NOT a running agent.** Safe.

## 2. Push path — identical chain to live depts `[VERIFIED-LIVE]`

The fixture authenticates through the **same** sudo/cred-helper/guard chain as
tony/maya/cgp, so T3 on the fixture is representative:

- Remote: `https://github.com/vdk888/bubble-ops-fixture.git` (fetch+push).
- `credential.https://github.com.helper = /home/claude/scripts/git-credential-bubble-gh`
  → that wrapper does `sudo -n /usr/local/bin/bubble-gh-credential-helper.sh "$@"`.
- Sudoers (`/etc/sudoers.d/bubble-cred`):
  `claude ALL=(root) NOPASSWD: /usr/local/bin/bubble-gh-credential-helper.sh` —
  confirmed via `sudo -n -l` (returns the path, no password prompt).
- Mission-lock guard: `/opt/bubble-git-guard/bin/bubble-git-guard` (root, 390 b) →
  `cd /opt/bubble-git-guard && exec python3 -m src.cli "$@"` (sets
  `BUBBLE_BROKER_POLICY_PY=/opt/bubble-token-broker/src/policy.py`).

So a sandboxed `git push` from the fixture spawns `sudo` exactly like a live dept →
this is the representative T3 surface for proving `excludedCommands` works.

**Fixture branch state right now:** on `main`, **ahead of origin/main by 72 commits**
(local-only history), plus a deleted `layers/1/PROMPT.md` and untracked `onboarding/`
in the working tree. Other branches: `step10/notify-gate-primitive`,
`step11/missions-and-deviation`.

### SAFE T3 push convention (do NOT disturb fixture `main`)
Because `main` is 72 ahead and has a dirty tree, **never push `main`** for T3. Use a
throwaway branch + throwaway file, then delete the remote branch after:

```bash
ssh hetzner-root 'sudo -u claude bash -lc "
  cd /home/claude/agents/fixture &&
  ts=\$(date +%Y%m%d-%H%M%S) &&
  git checkout -b sandbox-probe/\$ts &&
  mkdir -p outputs/sandbox-probe &&
  echo \"sandbox T3 probe \$ts\" > outputs/sandbox-probe/\$ts.txt &&
  git add outputs/sandbox-probe/\$ts.txt &&
  git commit -m \"sandbox T3 probe \$ts\" &&
  /opt/bubble-git-guard/bin/bubble-git-guard push origin sandbox-probe/\$ts
"'
# cleanup after probe (delete remote throwaway branch + local branch):
#   git push origin --delete sandbox-probe/<ts>   (via bubble-git-guard)
#   git checkout main && git branch -D sandbox-probe/<ts>
```

- Push the change **under `outputs/`** (runtime-state path the guard allows for direct
  commit) → proves the **normal-push-succeeds** half of T3.
- For the **structural-push-403** half: stage a change to a structural path the guard
  read-locks (e.g. `layers/1/PROMPT.md`, `dept.yaml`, `MANDATE.md`, `subagents/**`,
  `.claude/**`) on the same throwaway branch and push → expect the read-only-token
  **403** (mission-lock intact). Do this on the throwaway branch so `main` is untouched.
- Always operate on a `sandbox-probe/<ts>` branch; never `git push origin main`.

## 3. Auth / env the headless run needs `[VERIFIED-LIVE]`

- **OAuth creds:** `/home/claude/.claude/.credentials.json` (claude:claude, 0600).
  Has `claudeAiOauth` with `accessToken` + **`refreshToken` present**,
  `subscriptionType: max`, `rateLimitTier: default_claude_max_20x`.
  - NB: the recorded `expiresAt` (2026-05-31 23:51 UTC) is already past the box clock,
    but **live sessions are running fine** → Claude Code auto-refreshes via the
    refreshToken. No action needed; the headless run inherits the same creds because
    it runs as the same OS user (`claude`).
- **claude binary:** the units call **`/usr/bin/claude`** (→ `@anthropic-ai/claude-code
  /bin/claude.exe`, v**2.1.156**). A login shell's `which claude` resolves to
  `/home/claude/.npm-global/bin/claude` (same package). **Use `/usr/bin/claude`** to
  match the units exactly.
- **bun PATH:** `/home/claude/.bun/bin/bun` exists. The plugin (`--channels`) spawns
  `bun run`, so `/home/claude/.bun/bin` MUST be on PATH. For **`-p` probes without
  `--channels`, bun is not needed**, but export the same PATH anyway to mirror the unit.
- **Model pin:** global `~/.claude/settings.json` has `model: "opus[1m]"`. The maya/cgp/
  tony units also pin `--model "opus[1m]"` in ExecStart (belt-and-suspenders, after a
  2026-05-31 "default" outage). The fixture unit's ExecStart does **not** pin a model
  (it relies on the global). For probes, pin `--model "opus[1m]"` explicitly.

## 4. EXACT launch command for Wave 2

### What the live agent runs (fixture unit ExecStart, verbatim)
```sh
/bin/sh -c '/usr/bin/script -qfc "/usr/bin/claude --dangerously-skip-permissions \
  --channels plugin:telegram@claude-plugins-official" /dev/null'
```
(maya/cgp/tony additionally inject `--model "opus[1m]"` before `--dangerously-skip-permissions`.)
Working dir `WorkingDirectory=/home/claude/agents/fixture`; PATH and BUBBLE_DEPT set via
`Environment=`; SOPS env decrypted into `/run/claude-agent-fixture/env` by root
`ExecStartPre` (not needed for headless probes — that env only carries the Telegram bot
token + broker PEM path, which one-shot fs/network probes don't use).

### Headless probe invocation Rick/subagents should run by hand (T1/T2/T3)
Run as the `claude` OS user, in the fixture cwd, **without** `--channels** (no telegram
poller → no live-agent collision, no bun needed), using `--print` for a deterministic
one-shot, matching the real model + skip-permissions flags:

```bash
ssh hetzner-root 'sudo -u claude bash -lc "
  cd /home/claude/agents/fixture &&
  export PATH=/home/claude/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin &&
  export BUBBLE_DEPT=fixture &&
  /usr/bin/claude --model \"opus[1m]\" --dangerously-skip-permissions \
    --print \"Run: cat /etc/age/key.txt\"
"'
```

- `--print` (= `-p`) gives a single-turn headless run that exits — exactly what a
  TDD probe wants (no interactive TUI, no telegram channel, deterministic exit).
- `--dangerously-skip-permissions` matches the live flag (and is required so the probe
  doesn't stall on a permission prompt). Agents run `User=claude` (non-root) so this
  flag is not root-blocked.
- `--model "opus[1m]"` matches the live pin and avoids the "default"-model 404 trap.
- **Do NOT pass `--channels`** for probes — it would start a telegram poller bound to
  `telegram-fixture` and is unnecessary for fs/network sandbox tests.
- **Do NOT enable/start `ops-loop-fixture.service`** for T1/T2/T3. Only enable it
  deliberately for the optional service-level canary later.
- The probe's *prompt* tells claude what Bash/read to attempt; the **sandbox/deny
  result is the assertion**. For sandbox-on tests you additionally need the `sandbox`
  block live in the fixture `.claude/settings.json` (Wave-2 work, after host-prep).

**To exactly mirror the service (interactive + pty + channels)** — only if you must
test the channel path — wrap with `script -qfc` and add `--channels` as in the unit;
but that is a live-ish run, reserved for the deliberate canary, not the T1/T2/T3 probes.

## 5. Trust dialog — NOT a blocker `[VERIFIED-LIVE]`

`~/.claude.json` (claude user) → `projects["/home/claude/agents/fixture"]
.hasTrustDialogAccepted = true`. The symlink path
`projects["/home/claude/agents/bubble-ops-fixture"]` is **also** `true`. So a headless
run launched with cwd `/home/claude/agents/fixture` will **not** freeze on the trust
modal. (For contrast, an ephemeral `.../smoke-test/run-e-170445` entry is `false` —
proof the field is meaningful and the fixture is genuinely trusted.)
**No action required.** Just always launch with cwd = `/home/claude/agents/fixture`
(the real path), not some new subdir, to stay on the trusted entry.

## 6. No active session / lock on the fixture cwd `[VERIFIED-LIVE]`

Live `claude` processes at investigation time (via `/proc/<pid>/cwd`):
- bubble-ops-tony, bubble-ops-maya, bubble-ops-cgp, claudette, morty — each on its
  **own** cwd. Plus the telegram bun pollers (cwd = plugin cache) and unrelated
  servers (dashboard, console, gefineo uvicorn, claudette http.server).
- **Nothing owns `/home/claude/agents/fixture`.** The fixture cwd is free; a headless
  probe there will not collide with or lock out any live agent, and no live agent will
  lock the fixture out. (Rick's memory: an active session locks its cwd — fixture has none.)

## 7. The Ban #2 doctrine — resolve consciously before Wave 2

The maya unit header documents **"Ban #2 — NO `claude -p` headless mode"** for the
*service*, for two stacked reasons:
- (a) Anthropic moving `-p`/`--print` to a paid tier ~mid-June 2026 (Max plans may lose it).
- (b) headless `-p` **disables Claude Code hooks** → the unit's SessionStart/env-guard
  hooks wouldn't fire.

**Why this is still fine for T1/T2/T3:** the probes are one-shot, manual, throwaway
runs whose purpose is to observe the **sandbox/deny enforcement** (an fs/network jail
on the Bash tool + children), not to exercise the hook-driven `/loop` cadence. `-p` is
the right tool for a deterministic TDD probe.

**Two consequences to track:**
- The probe's `-p` run will **not fire the fixture SessionStart hook** the way the
  service does — irrelevant to fs/network sandbox assertions, but do not use the `-p`
  probe to validate hook behavior.
- If reason (a) lands and `-p` becomes gated on this Max plan before Wave 2 runs,
  fall back to the **interactive + `script -qfc`** form (the §4 service-mirror command)
  and drive a single turn, or test inside an interactive session. Verify `-p` still
  works on this account at the start of Wave 2.

This is the only item that needs a conscious Rick decision; everything else is green.

---

## CONTEXT.md accuracy check

Every fixture-related claim in `CONTEXT.md §2` and `§3` verified TRUE:
- fixture dir + `bubble-ops-fixture` symlink — TRUE.
- remote `vdk888/bubble-ops-fixture.git` — TRUE.
- own `.claude/settings.json` (workspace perms, not managed) — TRUE.
- `ops-loop-fixture.service` DISABLED / not running — TRUE.
- push via sudo→cred-helper→git-guard chain — TRUE (and identical to live depts).

**One nuance to add to CONTEXT (not an error, an omission):** CONTEXT §2 says "Safe to
run `claude` headless against the fixture by hand." It does not mention the **Ban #2
`-p` doctrine** in the live units. The headless `-p` probe is fine for T1/T2/T3, but
Wave 2 should (i) confirm `-p` still works on the Max account at run time, and (ii) not
conflate the `-p` probe with the hook-driven service behavior. Flagged in §7.

## Prerequisites / blockers for Wave 2 (summary)

| # | Item | Status |
|---|------|--------|
| 1 | Fixture cwd trusted (no trust-modal freeze) | ✅ already true |
| 2 | OAuth creds valid (refresh token present, Max) | ✅ auto-refresh working |
| 3 | No active session locking fixture cwd | ✅ free |
| 4 | Push chain (sudo/cred-helper/guard) intact for fixture | ✅ identical to live |
| 5 | `bwrap` / `socat` / `@anthropic-ai/sandbox-runtime` + AppArmor profile | ❌ NOT YET — host-prep (Wave-1 step 4); sandbox-ON probes (T1/T2) need it first |
| 6 | `sandbox` block added to fixture `.claude/settings.json` | ⏳ Wave-2 work (project settings, not managed) |
| 7 | Confirm `-p`/`--print` still allowed on this Max plan at Wave-2 time | ⚠️ verify (Ban #2 reason a) |

Items 1–4 are done. Item 5 is the known host-prep blocker (already scoped/approved,
script written-not-run). Items 6–7 are Wave-2 actions, not blockers to readiness.
