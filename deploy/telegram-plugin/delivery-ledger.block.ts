// delivery-ledger.block.ts — CANONICAL source-of-truth for the bubble
// delivery-ledger telegram-plugin patch (board #1284 pt E — Claudette silent
// Telegram message loss).
//
// This file is NOT executable on its own — it is the literal TypeScript block
// inserted into the telegram channel plugin's server.ts, right after the
// `const bot = new Bot(TOKEN)` anchor line, by scripts/install-channel-patches.sh
// (mirrors the bubble-inject / boot_rearm patch machinery — same volatile-cache
// re-apply story: a plugin version bump re-extracts server.ts from scratch and
// wipes any hand patch, so the source-of-truth lives HERE and is re-applied on
// every dept (re)start).
//
// ── WHY THIS EXISTS (the incident) ──────────────────────────────────────────
// grammy (the plugin's bot framework) long-polls Telegram getUpdates and
// advances the update offset the MOMENT it fetches a batch — i.e. it ACKs the
// batch to Telegram *before* our handler enqueues the turn into Claude. Once
// acked, Telegram will NEVER redeliver those updates. So if the harness process
// crashes / restarts (or a different harness takes over the token) in the window
// between fetch-ack and enqueue, the acked-but-not-yet-delivered updates are
// gone forever — a SILENT, permanent loss with NO signal anywhere.
//
// That is exactly the 2026-09-10→11 Claudette incident: 19 of Jade's messages
// (update-bearing message_ids 7527–7545) vanished — absent from her session log,
// with no loss trace at all. The service had crashed (status=1/FAILURE) at the
// window boundaries. The existing watchdogs only check *liveness* ("is the
// poller up?"), never *completeness* ("did every update Telegram accepted reach
// the session?"), so a silent drop is invisible to them.
//
// ── WHAT THIS PATCH DOES ────────────────────────────────────────────────────
// A grammy middleware (runs for EVERY update, before routing/gating) appends one
// JSONL line per received update to  <TELEGRAM_STATE_DIR>/delivery-ledger.jsonl :
//     {"update_id":N,"chat_id":"...","message_id":M,"kind":"text","ts":"..."}
// grammy hands updates to middleware in STRICTLY INCREASING update_id order, so
// a numeric GAP between consecutive ledger entries (…7526 then 7546) is the
// crash-loss fingerprint — the 19 missing updates become VISIBLE after the fact.
// The out-of-band telegram-gap-detector (scripts/telegram-gap-detector.py) reads
// this ledger, detects the gap, and alarms LOUDLY (operator Telegram ping +
// kanban card) — turning a silent drop into a loud one. That is the fix: the
// ABSENCE of a loss signal was the core problem, not just the drop.
//
// It also touches <TELEGRAM_STATE_DIR>/poll-heartbeat (mtime = last update seen)
// as extra context for the detector.
//
// Constraints honoured: append-only, best-effort, wrapped so it can NEVER throw
// into the handler chain (a ledger failure must not stop message delivery); uses
// only `join` + `writeFileSync`, both already imported by server.ts (no new
// imports, so `bun build` stays green across plugin versions); no secrets, no
// message *content* is written (only ids + kind + timestamp).
//
// Everything between the BEGIN/END markers is inserted VERBATIM. Nothing outside
// that span (including this docstring) is copied.
// === BUBBLE-DELIVERY-LEDGER PATCH BEGIN ===
// bubble delivery-ledger (board #1284 pt E) — make silent inbound loss VISIBLE.
try {
  const _ledgerDir = process.env.TELEGRAM_STATE_DIR
  if (_ledgerDir) {
    const _ledgerFile = join(_ledgerDir, 'delivery-ledger.jsonl')
    const _ledgerHeartbeat = join(_ledgerDir, 'poll-heartbeat')
    bot.use(async (ctx, next) => {
      try {
        const uid = ctx.update?.update_id
        if (typeof uid === 'number') {
          const kind =
            ctx.message?.text != null ? 'text'
            : ctx.message?.caption != null ? 'caption'
            : ctx.message != null ? 'message'
            : ctx.callbackQuery != null ? 'callback'
            : ctx.editedMessage != null ? 'edited'
            : 'other'
          const rec: Record<string, unknown> = { update_id: uid, kind, ts: new Date().toISOString() }
          if (ctx.chat?.id != null) rec.chat_id = String(ctx.chat.id)
          if (ctx.message?.message_id != null) rec.message_id = ctx.message.message_id
          if (ctx.from?.id != null) rec.user_id = String(ctx.from.id)
          writeFileSync(_ledgerFile, JSON.stringify(rec) + '\n', { flag: 'a', mode: 0o600 })
          try { writeFileSync(_ledgerHeartbeat, String(Date.now()), { mode: 0o600 }) } catch {}
        }
      } catch (e) {
        // Never let a ledger failure interrupt message delivery.
        process.stderr.write(`telegram delivery-ledger: append failed (non-fatal): ${String(e)}\n`)
      }
      await next()
    })
    process.stderr.write(`telegram delivery-ledger: recording update_ids to ${_ledgerFile}\n`)
  }
} catch (e) {
  process.stderr.write(`telegram delivery-ledger: setup failed (non-fatal): ${String(e)}\n`)
}
// === BUBBLE-DELIVERY-LEDGER PATCH END ===
