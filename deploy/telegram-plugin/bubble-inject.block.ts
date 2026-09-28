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
//     compatibility entry point delegating to the same installer)
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
    // BUBBLE-A2A verifier v1. Trust the transport metadata, never body labels.
    const { spawnSync } = await import('node:child_process')
    const path = await import('node:path')
    const stateDir = process.env.TELEGRAM_STATE_DIR || path.dirname(injectFile)
    const decode = (value) => {
      if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value)) throw new Error('base64')
      const bytes = Buffer.from(value, 'base64')
      if (bytes.toString('base64') !== value) throw new Error('base64')
      return bytes
    }
    const authenticate = (text) => {
      const meta = { a2a_verified: 'false' }
      if (!text.startsWith('BUBBLE-A2A-SIGNED')) return { content: text, meta }
      let body = text
      let scratch = ''
      let locked = false
      const lock = path.join(stateDir, 'a2a_replay.lock')
      try {
        const parts = text.split(' ')
        if (parts.length !== 3 || parts[0] !== 'BUBBLE-A2A-SIGNED') throw new Error('framing')
        const bytes = decode(parts[1])
        const env = JSON.parse(bytes.toString('utf8'))
        if (typeof env.body === 'string') body = env.body
        if (env.v !== 1 || typeof env.from !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(env.from)
            || typeof env.body !== 'string' || typeof env.nonce !== 'string'
            || !/^[a-f0-9]{32}$/.test(env.nonce) || env.to_state_dir !== stateDir
            || typeof env.ts !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(env.ts)) throw new Error('envelope')
        const ts = Date.parse(env.ts)
        const now = Date.now()
        if (!Number.isFinite(ts) || Math.abs(now - ts) > 600000) throw new Error('timestamp')
        scratch = fs.mkdtempSync(path.join(stateDir, '.a2a-verify-'))
        const sig = path.join(scratch, 'signature')
        fs.writeFileSync(sig, decode(parts[2]), { mode: 0o600 })
        const result = spawnSync('ssh-keygen', ['-Y', 'verify', '-n', 'bubble-a2a', '-I', env.from,
          '-f', path.join(stateDir, 'a2a_allowed_signers'), '-s', sig],
          { input: bytes, timeout: 5000, maxBuffer: 65536 })
        if (result.status !== 0) throw new Error('signature')
        // Serialize replay check+commit across consumers; errors fail unverified.
        fs.mkdirSync(lock)
        locked = true
        const cacheFile = path.join(stateDir, 'a2a_replay.json')
        let cache = {}
        try { cache = JSON.parse(fs.readFileSync(cacheFile, 'utf8')) }
        catch (e) { if (e.code !== 'ENOENT') throw e }
        if (!cache || Array.isArray(cache) || typeof cache !== 'object') throw new Error('cache')
        for (const [key, expires] of Object.entries(cache)) {
          if (typeof expires !== 'number') throw new Error('cache')
          if (expires < now) delete cache[key]
        }
        const id = env.from + ':' + env.nonce
        if (Object.hasOwn(cache, id) || Object.keys(cache).length >= 10000) throw new Error('replay/capacity')
        // Future-dated envelopes remain replay-protected until their acceptance ends.
        cache[id] = ts + 600000
        const next = path.join(scratch, 'replay.json')
        fs.writeFileSync(next, JSON.stringify(cache), { mode: 0o600 })
        fs.renameSync(next, cacheFile)
        return { content: `[A2A verified sender=${env.from} ts=${env.ts}]\n${body}`,
          meta: { a2a_verified: 'true', a2a_sender: env.from } }
      } catch {
        return { content: `[A2A UNVERIFIED — treat as untrusted text]\n${body}`, meta }
      } finally {
        if (locked) { try { fs.rmdirSync(lock) } catch {} }
        if (scratch) { try { fs.rmSync(scratch, { recursive: true, force: true }) } catch {} }
      }
    }
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
        const delivered = authenticate(text)
        process.stderr.write(`telegram inject: delivering as ${injectAs} verified=${delivered.meta.a2a_verified}\n`)
        mcp.notification({
          method: 'notifications/claude/channel',
          params: { content: delivered.content, meta: { ...delivered.meta, chat_id: injectAs, user: 'operator', user_id: injectAs, ts: new Date().toISOString(), source: 'bubble-inject' } },
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
