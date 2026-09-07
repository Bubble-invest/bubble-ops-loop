# RUNBOOK — #1133 Mac harness switcher (Claude Code ⇄ Hermes)

The Mac analog of the VPS `bubble-switch-harness` (bubble-vps-platform#38). Flips
one agent between the Claude Code and Hermes harnesses **on its own Mac**, keeping
the same launchd job / tmux session / Telegram bot, with continuity + auto-rollback.

## How it fits together
- **Selector**: `~/Library/Application Support/bubble-ops-loop/harness-<slug>` —
  contains `claude` (default) or `hermes`. The aligned wrapper (#748, rendered by
  `install-local-loop.sh`) reads it at launch and runs `start_claude` or
  `start_hermes` (`hermes -p <slug> gateway run --replace`).
- **Controller**: `deploy/local/bubble-switch-harness-mac` — flips the selector
  then `launchctl kickstart -k gui/<uid>/com.bubble.ops-loop-<slug>`; the wrapper
  re-reads the selector on relaunch. Fence-then-start = one harness, one poller, no
  409. Deploy it to each Mac at `~/.local/bin/bubble-switch-harness-mac`.
- **Convention**: the Hermes profile is named by **slug** (`main`, `accountant`,
  `ellie`, `content`, `rnd`), so `hermes -p <slug>` resolves fleet-wide.

## Usage
```
bubble-switch-harness-mac <agent> <claude|hermes> [<request-id>]
# agents: tonio | geraldine | ellie | content | rick(guarded)
```
Runs ON the agent's own Mac (the launchd-label preflight fails cleanly if you
invoke an agent that doesn't live on this Mac — that's how the future Mac commander
targets M5/M1: it SSHes to that Mac and runs the controller there; **no VPS→Mac
SSH**). Rick (`rnd`) is refused unless `BUBBLE_ALLOW_RICK_SWITCH=1` — it is the
session that runs the controller; switching it would kill the operating session.

## What the controller does (in order)
1. Validate target + resolve the agent registry entry (slug/label/profile/workdir/session).
2. Idempotent: if already on the target, exit ready.
3. Preflight: for `hermes`, require `~/.hermes/profiles/<slug>`.
4. **Completed-turn gate**: newest `~/.claude/projects/<workdir-mangled>/*.jsonl`
   must be mtime-stable (not mid-turn) before switching away from claude.
5. **Continuity**: claude→hermes imports the newest jsonl (`hermes -p <slug>
   sessions import --from claude <jsonl>`); hermes→claude writes `HARNESS_HANDOFF.md`.
6. Save prev selector, flip selector, `launchctl kickstart -k`.
7. **Readiness** (poll ≤75s): the tmux pane's process must be the target harness
   (pane-scoped, so it never matches another agent's claude) + a `getMe` bot check.
8. **Auto-rollback** on any failure: restore the previous selector + kickstart.
Emits `SWITCH_RESULT=ready|failed|rolled_back`.

## Validation (2026-09-07, Tonio pilot)
Round-trip PASSED: claude→hermes (jsonl-import continuity, pane=hermes, no 409,
getMe ok) → hermes→claude (handoff note, pane=claude, resumed). Rollback also
proved itself: a first attempt failed because the wrapper runs `hermes -p main`
but the profile was named `tonio` → gateway didn't come up → clean auto-rollback,
Tonio never stranded. Fixed by naming the profile by slug (`main`).

## Deploy to a Mac
```
install -m 755 deploy/local/bubble-switch-harness-mac ~/.local/bin/bubble-switch-harness-mac
```
Requires on that Mac: `~/.local/bin/hermes` (or on PATH), `~/.local/bin/tmux`, the
agent's aligned wrapper (#748) already installed, and (for hermes targets) a
`~/.hermes/profiles/<slug>` profile that is gateway-onboarded (telegram token +
TELEGRAM_ALLOWED_USERS + home_chat_id + model provider + cwd).

## Still to come (tracked)
- The Mac **commander** bot (`@Unclestockbubblebot`, Mac-hosted): buttons to
  switch/status any Mac agent — local via this controller, remote (M5/M1) via
  Mac→Mac SSH. No VPS→Mac SSH.
- Hermes profiles (by slug) for geraldine / ellie / content; real hermes install on M1.
