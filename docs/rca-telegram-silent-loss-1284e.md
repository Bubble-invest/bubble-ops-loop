# RCA — Claudette silent Telegram message loss (board #1284 pt E)

**Incident:** 2026-09-10 16:30 → 2026-09-11 11:23. 19 of Jade's Telegram
messages (update-bearing message_ids 7527–7545) never reached Claudette's
session — absent from her session log, with **no loss signal anywhere**. The
core defect is the ABSENCE of a loss signal (silent drop), not merely the drop.

## How the 19 were dropped

Claudette ran the **hermes** harness during the incident (journal:
`hermes_plugins.telegram_platform.adapter`, `gateway.run`). She was switched to
the **claude/bun** harness on 2026-09-13 17:48 (current). The loss mechanism is
the same class on both harnesses:

1. The Telegram poller (grammy on the claude harness; python-telegram-bot on
   hermes) **advances the getUpdates offset the moment it fetches a batch** — it
   ACKs the batch to Telegram *before* the handler enqueues the turn into the
   agent session. Once acked, **Telegram will never redeliver** those updates.
2. At the incident boundaries the `bubble-agent@claudette` service **crashed
   hard** — `Main process exited, code=exited, status=1/FAILURE` at 16:36:01 on
   09-10, and again 06:22:54 / 06:23:22 on 09-11 — around an auxiliary
   "payment / credit error" cascade (`agent.auxiliary_client: marking nous
   unhealthy`).
3. hermes's in-process guard against offset-ahead loss (`_held_inbound_events`,
   redispatch-on-reconnect) is an **in-memory list that a process crash
   destroys** — it survives an in-process *disconnect*, not a restart. So on each
   hard restart, updates the poller had fetched-and-acked but not yet
   enqueued/persisted were **permanently lost**, with no record of what was
   dropped.

## Why nothing signalled it

The existing watchdogs only check **liveness**, never **completeness**:

- `telegram-kick-watchdog` (Mac) and `telegram-watchdog-<dept>` +
  `loop-tick-watchdog` (VPS) answer *"is the poller up / is the tick
  progressing?"* — never *"did every update Telegram accepted reach the
  session?"*.
- Worse, `telegram-watchdog-claudette.timer` was found **disabled** during this
  investigation (only `telegram-watchdog-morty.timer` was enabled; ben/maya/tony
  also disabled) — so even Claudette's poller *liveness* was not being
  auto-checked or recovered.

A silent inbound drop is therefore invisible to the entire watchdog fleet.

## The fix (this PR)

A **delivery-gap detector** — completeness, not liveness — extending the
existing channel-patch + watchdog machinery:

1. **delivery-ledger plugin patch** (`deploy/telegram-plugin/delivery-ledger.block.ts`,
   installed by `scripts/install-channel-patches.sh` as a third idempotent patch
   alongside boot_rearm + bubble-inject). A grammy `bot.use` middleware appends
   one line per received update to `<state>/delivery-ledger.jsonl` in the
   strictly-increasing `update_id` order grammy delivers them. A numeric **gap**
   between consecutive lines (…7526 then 7546) is the crash-loss fingerprint —
   the lost 19 become **visible after the fact**. Append-only, best-effort,
   cannot throw into the handler chain; uses only already-imported symbols so
   `bun build` stays green (validated on the live VPS plugin).

2. **telegram-gap-detector** (`scripts/telegram-gap-detector.py` + pure,
   unit-tested `scripts/lib/telegram_gap_detector.py`; run every 5 min by
   `deploy/templates/telegram-gap-detector.{service,timer}`). Each tick it reads
   the ledger and on a **GAP** / **DEAD poller** (bot.pid) / **WEDGE** (optional
   read-only `getWebhookInfo` pending-count probe — does not consume updates)
   **signals LOUDLY out-of-band**: operator Telegram ping via the MAIN bot (so
   the alert lands even when the dept's own poller is dead) + a kanban card, and
   — when `BUBBLE_GAP_RESTART=1` — kicks the poller to recover. State is
   persisted so the same gap is not re-alerted every tick.

This turns a silent drop into a loud one, which was the point.

## Deploy (Rick-controlled, not in this PR)

- Install the `telegram-gap-detector` timer fleet-wide; wire `BUBBLE_MAIN_BOT_TOKEN`
  into `/run/telegram-gap-detector/env` from the commander secret scope.
- **Re-enable `telegram-watchdog-claudette.timer`** (and review the other
  disabled dept timers) so poller liveness is auto-recovered again.
- The delivery-ledger patch is picked up automatically on the next dept restart
  (install-channel-patches runs at every `bubble-agent@<slug>` ExecStartPre).

## Verification in this PR

- `scripts/lib/tests/test_telegram_gap_detector.py` — 10 tests incl. the exact
  incident (ledger …7526→7546 ⇒ detects a 19-update gap and alerts).
- `tests/test_install_channel_patches.sh` — 64 tests (4 new) incl. the ledger
  patch landing once, correctly placed, with a backup.
- Patched `server.ts` `bun build` validated against the live VPS plugin (real
  deps): OK.
