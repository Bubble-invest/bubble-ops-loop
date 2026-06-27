#!/usr/bin/env bash
# apply-inject-patch.sh — idempotently (re)apply the bubble-inject patch to the
# telegram channel plugin's server.ts ({{OPERATOR}} msg 4038, 2026-06-07).
#
# WHY: the bubble-inject feature (a local file-watcher that delivers a message
# INTO a running --channels session as if from {{OPERATOR}} — closing the upstream
# no-external-injection gap #24947/#27441/#53049) lives as a patch to the OFFICIAL
# telegram plugin's server.ts, which sits in a non-git plugin CACHE dir that a
# plugin UPDATE overwrites (see claude-plugin-update-mechanism). Run this at every
# service start (ExecStartPre) so the patch self-heals after any plugin bump —
# same durability pattern as vendor-dept-libs.sh.
#
# Idempotent + fail-OPEN: if already patched, or anything errors, exit 0 (never
# block the loop from starting). Finds the plugin server.ts under the cache glob.
set -uo pipefail

log() { logger -t apply-inject-patch "$*" 2>/dev/null; echo "[apply-inject-patch] $*" >&2; }

# Newest installed telegram plugin server.ts (handles version bumps).
SRV="$(ls -t /home/claude/.claude/plugins/cache/*/telegram/*/server.ts 2>/dev/null | head -1)"
[[ -n "${SRV:-}" && -f "$SRV" ]] || { log "no telegram server.ts found — skip (fail-open)"; exit 0; }

if grep -q 'bubble-inject' "$SRV" 2>/dev/null; then
  exit 0   # already patched, nothing to do (quiet — runs every boot)
fi

ANCHOR='await mcp.connect(new StdioServerTransport())'
grep -qF "$ANCHOR" "$SRV" 2>/dev/null || { log "anchor not found in $SRV — skip (fail-open)"; exit 0; }

# The patch block (kept in sync with the live edit). Inserted right after connect.
read -r -d '' PATCH <<'PATCH_EOF' || true

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
PATCH_EOF

cp -f "$SRV" "${SRV}.bak-preinject" 2>/dev/null || true
# Insert PATCH immediately after the anchor line, via python (robust on the cache file).
SRV="$SRV" PATCH="$PATCH" ANCHOR="$ANCHOR" python3 - <<'PY'
import os
p=os.environ["SRV"]; anchor=os.environ["ANCHOR"]; patch=os.environ["PATCH"]
s=open(p).read()
line=anchor+"\n"
if line not in s:
    # anchor may lack trailing newline variance
    import re
    m=re.search(re.escape(anchor), s)
    if not m: raise SystemExit("anchor vanished")
    idx=s.index("\n", m.end())+1
    s=s[:idx]+patch+"\n"+s[idx:]
else:
    s=s.replace(line, line+patch+"\n", 1)
open(p,"w").write(s)
print("inserted")
PY
log "re-applied bubble-inject patch to $SRV"
exit 0
