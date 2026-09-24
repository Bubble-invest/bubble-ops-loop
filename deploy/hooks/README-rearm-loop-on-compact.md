# rearm-loop-on-compact hook (#754)

Re-arms an ops-loop agent's `/loop` after a `/compact` (manual or auto), which
otherwise silently kills the in-memory `/loop` cron and leaves the agent dormant.

## What it does
On SessionStart with source `compact` or `resume`, and ONLY when
`OPS_LOOP_BOOT_REARM=1`, it appends a re-arm turn to the agent's telegram inject
file (resolved by glob `$HOME/.claude/channels/telegram-*/inject`, exactly one).
The telegram plugin delivers it as a session turn → the agent runs a tick +
re-arms its self-paced cron. No-op (exit 0) in any other case, including a human
interactive `/compact`.

**Self-wake prompt is generated, not authored (#1483/#1484).** `REARM_TURN` (and
`boot_rearm.ts`'s `content`) instruct the agent to arm its NEXT CronCreate wake
by running `scripts/due_missions.py wake-prompt --dept-dir <dept repo>`. That
command reuses the exact deterministic envelope the floor/backup tick already
renders (`due_missions.py`'s `_prompt()` — DUE_MISSIONS + per-mission COMPLETE
commands), plus a staleness re-check clause and a fixed footer pointing at
WORKING_MEMORY/HANDOFF.md and requiring a citation on any operator-intent claim.
**If it exits 0**, the agent passes its stdout to CronCreate verbatim — never
composes or appends its own tick-protocol prose. **It is FAIL-CLOSED by
design**: it refuses (non-zero exit, empty stdout) whenever it cannot positively
confirm real, live, due work — e.g. a dept.yaml that doesn't (yet) use the
`loop.due_dispatch` schema this generator understands (this is the case for
every VPS dept today, which use the `recurring_missions:{layer,cadence,time}`
schema instead — see board card #1487 for the VPS follow-up and why VPS reuse
isn't in this PR),
or a `loop.due_dispatch`-configured dept with nothing currently due. On that
refusal the agent falls back to composing its OWN full tick protocol text for
that one arm (the pre-#1484 behavior) and records the refusal in its HEARTBEAT
ONLY, **never Telegram** — this is a KNOWN gap on VPS until the follow-up
lands (board #1487), and firing on Telegram on every re-arm/compaction (daily
rotation + every compaction, for ben/tony/maya) would spam the operator. It
never treats empty/failed output as "nothing to do," it just doesn't page
anyone about an already-known, already-tracked gap.
This closes the channel through which Ben's uncited "operator flagged spend —
be cost-conscious" note self-reinforced across ~36 wake prompts and silently
dropped a mission deliverable for 12 days (see board #1483's audit), without
ever letting the generator itself arm a wake with an empty/wrong mission list
(the earlier draft of this PR did exactly that against Ben's real dept.yaml —
see the PR review). The agent still chooses WHEN (the cron time/cadence) —
never WHAT the prompt says.

### Reliability (#754 durable fix, 2026-09)
- **Dedupe on the re-arm sentinel, not on "inject non-empty".** The first version
  skipped whenever ANY content was pending in the inject file. But the inject is a
  shared delivery channel: if a cross-agent inject or a not-yet-drained operator
  message happened to sit there at compact time, the re-arm was **silently
  swallowed** and the loop stayed dormant — the very failure this card chases. Now
  it skips only when a re-arm (phrase `re-arm your /loop, SELF-PACED`) is already
  queued (never stack two); an unrelated pending turn is preserved and the re-arm
  is appended on its own line.
- **Audit log.** Every fire/skip appends one line to
  `<channel_dir>/rearm-on-compact.log` (`<ts>  source=…  decision=fired|skip:already-queued`),
  so a REAL `/compact` leaves greppable proof the hook ran — the acceptance
  blocker this card was stuck on ("no clear auto-fire evidence"). Best-effort;
  never changes the exit code. A human `/compact` (no `OPS_LOOP_BOOT_REARM`) writes
  nothing at all.
- **CI-gated.** `deploy/hooks/` now runs in `.github/workflows/tests.yml`, so a
  regression in the re-arm decision fails the build instead of shipping silently.

## Settings snippet (Miranda / M1 — `~/.claude/settings.json`, merge into `hooks`)
```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact",
        "hooks": [
          { "type": "command",
            "command": "python3 /Users/jadethi-viet-lanhoang/Library/Application Support/bubble-ops-loop/hooks/rearm-loop-on-compact.py",
            "timeout": 10 }
        ]
      },
      {
        "matcher": "resume",
        "hooks": [
          { "type": "command",
            "command": "python3 /Users/jadethi-viet-lanhoang/Library/Application Support/bubble-ops-loop/hooks/rearm-loop-on-compact.py",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```
NOTE: if Miranda's settings.json already has a `SessionStart` array, APPEND
these two entries to it — do not overwrite. The hook resolves the inject file by
GLOB (`telegram-*/inject`), so it needs NO dept env for the path — only
`OPS_LOOP_BOOT_REARM=1` (already exported by the wrapper) to arm. On M1 there is
exactly one channel dir (`telegram-socials`), so the glob is unambiguous.

## Apply (Rick, on M1, after PR merge)
1. `scp` / place `rearm-loop-on-compact.py` into `~/Library/Application Support/bubble-ops-loop/hooks/` on M1.
2. Back up `~/.claude/settings.json` → `.bak-754-<ts>`.
3. Merge the two SessionStart entries (append if the key exists).
4. Validate: `python3 -c "import json;json.load(open('~/.claude/settings.json'))"`.

## Live proof (the #754 acceptance test)
1. Confirm Miranda has an armed `/loop` cron (CronList in her session, or observe a recent auto-tick).
2. With Jade aware, run `/compact` in Miranda's session.
3. Verify the hook FIRED — now directly observable via the audit log:
   `tail ~/.claude/channels/telegram-socials/rearm-on-compact.log` should show a
   fresh `decision=fired  source=compact` line at the compact time. Then confirm
   the inject drained within seconds; Miranda runs a tick; `CronList` shows exactly
   ONE self-paced loop wake (no double).
4. Safety check: confirm the hook does NOT fire in a session lacking
   `OPS_LOOP_BOOT_REARM=1` (e.g. a plain `claude` session) — no audit line, inject
   untouched.

## Follow-up (option B — fleet rollout, separate card)
This hook is still installed by HAND (steps under "Apply" above). Nothing in the
automated install path (`install-local-loop.sh` / `scaffold.py`) wires the two
SessionStart entries into a dept's `settings.json`, so a settings reset or a new
dept does not inherit it. Folding this into the local-loop installer + the factory
scaffold (so every host:local Mac dept + future dept gets it) is the deliberate
next card, gated on this proving out live.

## Rollback
Remove the two SessionStart entries from settings.json (restore the `.bak`) and
delete the hook file. Fully reversible; no other component depends on it.
