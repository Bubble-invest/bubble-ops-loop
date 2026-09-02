// bubble-inject.block.ts — CANONICAL source-of-truth for the bubble-inject
// telegram-plugin patch (board #956, folding into scripts/install-channel-patches.sh).
//
// This file is NOT executable on its own — it is the literal TypeScript block
// that gets inserted into the telegram channel plugin's server.ts, right after
// the `await mcp.connect(new StdioServerTransport())` anchor line. Keeping it
// as its own versioned file (instead of duplicated inline heredocs) means both
// installers below insert byte-identical code and can never drift apart:
//   - scripts/install-channel-patches.sh   (NEW, #956 — the durable re-applier;
//     re-applies both bubble-inject + boot_rearm on every dept (re)start and
//     validates the result with `bun build`)
//   - scripts/apply-inject-patch.sh        (the original VPS ExecStartPre hook;
//     kept for back-compat, still reads its own embedded copy today — retiring
//     that duplicate copy in favor of this file is a follow-up, not required
//     for #956 to land)
//
// WHY the patch exists: the bubble-inject feature (a local file-watcher that
// delivers a message INTO a running --channels session as if from {{OPERATOR}} —
// closing the upstream no-external-injection gap #24947/#27441/#53049) lives as
// a patch to the OFFICIAL telegram plugin's server.ts, which sits in a non-git
// plugin CACHE dir that a plugin UPDATE overwrites (see
// claude-plugin-update-mechanism). See also deploy/telegram-plugin/boot_rearm.ts
// (the sibling patch) and Rick_RnD/skills/telegram-inject/references/mechanism.md
// (the full mechanism writeup + the audio-listener caller).
//
// Everything between the BEGIN/END markers below is inserted VERBATIM into
// server.ts by the installer — nothing outside that span (including this
// docstring) is copied.
// === BUBBLE-INJECT PATCH BEGIN ===
// ─── Local inject channel (Bubble, {{OPERATOR}} msg 4036, 2026-06-07) ──────────────
// Deliver a message INTO this running --channels session AS IF from {{OPERATOR}}, via a
// local file watcher — closing the upstream no-external-injection gap
// (#24947/#27441/#53049). Fires the SAME notifications/claude/channel event the
// telegram getUpdates path uses, meta forged to {{OPERATOR}}'s chat_id. On-box only;
// every inject logged (meta.source='bubble-inject'). Off unless BUBBLE_INJECT_FILE
// or TELEGRAM_STATE_DIR is set.
try {
  const injectFile =
    process.env.BUBBLE_INJECT_FILE ||
    (process.env.TELEGRAM_STATE_DIR ? `${process.env.TELEGRAM_STATE_DIR}/inject` : '')
  if (injectFile) {
    const fs = await import('node:fs')
    const injectAs = process.env.BUBBLE_INJECT_AS || process.env.BUBBLE_OPERATOR_CHAT_ID || ''
    try { fs.closeSync(fs.openSync(injectFile, 'a')) } catch {}
    const drain = () => {
      let raw = ''
      try { raw = fs.readFileSync(injectFile, 'utf8') } catch { return }
      if (!raw.trim()) return
      try { fs.truncateSync(injectFile, 0) } catch {}
      for (const line of raw.split('\n')) {
        const text = line.trim()
        if (!text) continue
        // Drop stray bare shell-path lines (e.g. "/usr/bin/bash") — a session
        // STARTUP-RACE artifact written into the inject file at restart, never a
        // legitimate agent turn. Was delivered as a forged-Joris no-op turn that
        // churned the agent. (Rick 2026-06-27, board #336.)
        if (/^\/(usr\/)?bin\/(ba|z|fi|a|da)?sh$/.test(text)) {
          process.stderr.write(`telegram inject: dropped stray shell-path line: ${text}\n`)
          continue
        }
        process.stderr.write(`telegram inject: delivering as ${injectAs}: ${text.slice(0, 80)}\n`)
        mcp.notification({
          method: 'notifications/claude/channel',
          params: { content: text, meta: { chat_id: injectAs, user: 'operator', user_id: injectAs, ts: new Date().toISOString(), source: 'bubble-inject' } },
        }).catch((err: unknown) => { process.stderr.write(`telegram inject: delivery failed: ${String(err)}\n`) })
      }
    }
    try { fs.watch(injectFile, { persistent: false }, () => drain()) } catch {}
    setInterval(drain, 2000).unref?.()
    process.stderr.write(`telegram inject: watching ${injectFile} (as ${injectAs})\n`)
  }
} catch (e) {
  process.stderr.write(`telegram inject: setup failed (non-fatal): ${String(e)}\n`)
}
// === BUBBLE-INJECT PATCH END ===
