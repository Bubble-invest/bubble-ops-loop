# Durable-cron manifest (`config/crons.yaml`)

Board card #461 (child of #456). Fixes: a dept's `CronCreate durable: true`
wakes (a fixed-time daily brief, etc.) vanish silently on a systemd/launchd
restart, because `durable: true` is honored only within a session /
`--resume` chain, not across a cold process restart — a platform limitation
of the headless CLI, not a Bubble bug (see #456's investigation comment for
the full evidence chain, including Claudette's missed 08:32 mail-brief on
session `daf5bf7b`).

## What this is NOT

`dept.yaml::recurring_missions` already exists and is a **different**
concept: it declares the Layer 1-4 OODA pipeline work a dept does (what
materializes into `queues/`, consumed by `scripts/lib/dispatch_helpers.py`).
It has nothing to do with session-level `CronCreate` wakes — a dept's
`/loop` self-pacing cadence and a fixed-time daily brief are not "missions",
they're just "wake the agent at time X with prompt Y", and today that state
lives ONLY in the running process's memory.

`config/crons.yaml` is the new, separate, minimal manifest for exactly that:
durable session-level wakes. It intentionally does NOT reuse or extend
`dept.yaml`'s schema — see `schemas-draft/crons-manifest.schema.yaml`'s
description for the full rationale.

## Where it lives

`<dept-dir>/config/crons.yaml`, dept-owned and git-tracked, sitting next to
the dept's existing `dept.yaml` and `config.yaml`. A dept with no such file
is a no-op — the boot-rearm turn already re-arms `/loop` unconditionally
regardless of this manifest's presence.

Schema: `schemas-draft/crons-manifest.schema.yaml`.
Examples: `schemas-draft/crons-manifest-examples/` (Claudette's mail-brief —
the pilot entry — and Miranda's M1 pattern, per #461's job description).
Negative fixtures + validator: `schemas-draft/crons-manifest-negative/` +
`schemas-draft/tests/validate_crons_manifest.py`.

## Shape

```yaml
version: 1
crons:
  - name: mail_brief_0832          # stable id — the diff key against CronList
    schedule: "32 6 * * *"          # 5-field cron, UTC (matches CronCreate)
    description: Jade's morning mail brief.
    prompt_ref: "file:config/crons/mail_brief.md"   # or an inline literal prompt
    critical: true                  # missing + still-missing-after-rearm -> loud alert
```

`prompt_ref` starting with `file:` is read relative to the dept dir (keeps
long prompts out of the manifest); anything else is the literal prompt text.

## How it gets re-armed

The mechanism is the SAME boot-rearm turn that already re-arms `/loop` on
every service (re)start — generalized, not replaced. It lives in
`deploy/templates/ops-loop-dept.service.template`'s second `ExecStartPost`
(the one that sleeps 8s then appends a synthetic turn to
`${TELEGRAM_STATE_DIR}/inject`). That turn now ALSO instructs the agent to:

1. Check for `config/crons.yaml` in its own dept dir (absent → nothing else
   to do — no-op).
2. Run `CronList`.
3. For every manifest entry whose `name` is missing from the live list,
   resolve its `prompt_ref` and call `CronCreate` with that entry's
   `schedule` + resolved prompt. Already-live entries are left untouched —
   this makes re-runs idempotent (no duplicate crons stack up on repeated
   restarts).
4. If a `critical: true` entry is STILL missing after the re-arm attempt
   (e.g. the `CronCreate` call itself failed), send ONE loud Telegram alert
   naming it — the #456 option-(c) safety net, layered on top of the
   self-heal rather than substituting for it.

This is the SAME proven pattern already shipped for `/loop`'s own re-arm
(the earlier boot-rearm work); this card generalizes it to any dept-declared
durable cron instead of only `/loop`.

## Testing

- `scripts/lib/tests/test_crons_manifest.py` — the pure load/diff core
  (pytest): a manifest with 2 crons both missing from `CronList` → both
  flagged for re-arm; once live, a second diff reports nothing missing
  (idempotent — no dupes); a dept with no manifest file → no-op.
- `tests/test_boot_rearm_manifest.sh` — extracts the actual
  `ExecStartPost` inject line from the service template and asserts the
  boot turn text contains the manifest-aware instructions (CronList read,
  CronCreate re-arm, the `file:` convention, the critical-alert safety net)
  WITHOUT having dropped any of the pre-existing `/loop` self-arm
  instructions (STEP A-F, no hardcoded hourly cron, no bare slash-command).
- `python3 schemas-draft/tests/validate_crons_manifest.py` — schema
  validation of the example + negative fixtures.

## Scope notes (per #461)

- Out of scope / not touched: mission-dispatch logic, any live VPS unit
  (this ships as a template + framework change only — Rick re-renders +
  restarts live depts separately), the stale-heartbeat backup floor in
  `scripts/loop-backup.sh` (a different mechanism — force-ticks a live-but-
  quiet session; it is not a restart/boot path, so it is untouched here).
- The Mac (`launchd`/`deploy/local/`) twin has no equivalent boot-inject
  today at all (host:local depts rely on the plugin's own `onStart`
  mechanism inside a persistent tmux session). Extending manifest re-arm to
  that path is a natural follow-up but is a separate change — flagged, not
  bundled into this card.
