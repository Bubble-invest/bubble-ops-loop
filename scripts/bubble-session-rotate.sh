#!/usr/bin/env bash
# bubble-session-rotate.sh — daily fresh-session rotation for a dept (board #1195).
#
# Rotates a dept to a FRESH Claude session once per day so its on-disk transcript
# .jsonl can't grow until the reconstructed context overflows the window and wedges
# the agent ("Prompt is too long"). Auto-compaction bounds only the in-memory
# context per turn, NOT the .jsonl; --continue re-parses the whole file, so a
# forever-session inevitably overflows (maya @12MB, ben @16.7MB, 2026-09-21).
#
# Mechanism (NO edit to the fund-critical bubble-agent-prepare): STOP the unit
# (so the agent flushes its complete transcript on graceful shutdown), THEN archive
# the dept's session transcripts out of the projects dir, THEN start the unit. The
# EXISTING fresh-fallback gate (bubble-agent-prepare: --continue only if a *.jsonl
# exists in the cwd's project dir) then starts a FRESH session. Context is preserved
# because the L4 `session_handoff` mission wrote HANDOFF.md, which the layer prompts
# read at STEP 0 on the fresh session's first tick.
#
# STOP-BEFORE-ARCHIVE is load-bearing (see the action section) — archiving while the
# agent runs lets its shutdown flush rewrite a PARTIAL transcript that crash-loops
# the next start (found on the tony prototype, 2026-09-22).
#
# WHY archive-and-restart rather than the existing tony.once marker: bubble-agent-
# prepare ALREADY has a forced-fresh primitive (BUBBLE_AGENT_FORCE_FRESH_ONCE + the
# root-owned `tony.once` marker), but it is hardcoded `slug == tony` and lives INSIDE
# the fund-critical prepare path. Generalizing it fleet-wide would mean editing that
# path; this script reaches the identical outcome (gate sees zero *.jsonl -> no
# --continue -> fresh) from OUTSIDE it, for every dept, without that edit.
#
# FAIL-SAFE: only rotate if a FRESH HANDOFF.md exists (< HANDOFF_MAX_AGE_H). Without
# it the fresh session would start context-blind, so we SKIP the rotation and keep
# the current session (a bloated-but-working session beats a fresh-but-blind one;
# the #1436 wedge check + this next run will catch/retry it). Run as root (needs
# systemctl restart + to move the agent-uid-owned transcripts).
#
# #1469: the 24h-ish window let a SKIP slide (Rick rotated on an 18h-old HANDOFF
# the same day his own late wake didn't run) and let a real SKIP go unnoticed for
# a full day (ben, no late wake -> no HANDOFF.md -> silent SKIP). Tightened to
# 12h (override via HANDOFF_MAX_AGE_H) and every SKIP now alerts (see below).
#
# Usage: bubble-session-rotate.sh <slug> [--force] [--dry-run]
set -euo pipefail
# Imports must never leave bytecode owned by this maintenance UID in dept trees.
export PYTHONDONTWRITEBYTECODE=1

HANDOFF_MAX_AGE_H="${HANDOFF_MAX_AGE_H:-12}"   # handoff must be newer than this (#1469: 20->12)

# ── #1195 rollout: ASK-for-handoff (agents with no L4 session_handoff mission) ──
# Concierge / manager-loop agents (Tonio, Ellie, Claudette) have no L4, so nothing
# writes HANDOFF.md for them. With --ask-handoff (or ROTATE_ASK_HANDOFF=1) the rotation
# itself asks the live agent to write it: wait for idle, append ONE honest line to the
# agent's bubble-inject file (same-host local process, tagged source=bubble-inject),
# then poll until HANDOFF.md is rewritten. Everything after that is the unchanged gate.
# Also: the freshness gate now trusts the header stamp "(updated YYYY-MM-DD HH:MM UTC)"
# over mtime when present (something on M5 touched a 3-day-old handoff's mtime).
ASK_HANDOFF="${ROTATE_ASK_HANDOFF:-0}"
ASK_WAIT_S="${ROTATE_ASK_WAIT_S:-600}"     # max wait for idle, and again for the file
IDLE_S="${ROTATE_IDLE_S:-300}"             # transcript quiet this long = idle
INJECT_FILE="${ROTATE_INJECT_FILE:-}"

slug="${1:?usage: bubble-session-rotate.sh <slug> [--force] [--dry-run]}"; shift || true
force=0; dry=0
for a in "$@"; do
  case "$a" in --force) force=1;; --dry-run) dry=1;; --ask-handoff) ASK_HANDOFF=1;; esac
done

workdir="/srv/agents/${slug}"
home="/home/agent-${slug}"
proj="${home}/.claude/projects"
handoff="${workdir}/HANDOFF.md"
svc="bubble-agent@${slug}.service"
[[ -n "$INJECT_FILE" ]] || INJECT_FILE="${home}/.claude/channels/telegram-${slug}/inject"

log() { printf '[session-rotate] %s\n' "$*"; }

# ── #1469: SKIP alert ─────────────────────────────────────────────────────
# A silent SKIP is exactly what let ben's 2026-09-23 05:34Z no-HANDOFF skip
# go unnoticed for a full day. Reuse the fleet's existing kanban emitter
# (tools/kanban/emit_kanban_item.sh — same tool the emit-kanban-task skill
# wraps) instead of inventing a new alert channel: it already dedupes on
# task+title (an open issue for the same key is not re-created) and already
# falls back to a best-effort Telegram ping when the board is unreachable.
# Best-effort only — an alert failure must never turn a SKIP into a crash.
_resolve_emit_kanban() {
  local here cand
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for cand in \
    "${EMIT_KANBAN_ITEM:-}" \
    "${workdir}/tools/kanban/emit_kanban_item.sh" \
    "${here}/../tools/kanban/emit_kanban_item.sh" \
    "/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh" \
    "/home/claude/scripts/emit_kanban_item.sh" \
    "$HOME/claude-workspaces/Rick_RnD/tools/kanban/emit_kanban_item.sh" \
  ; do
    [[ -n "$cand" && -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  done
  return 1
}

# _alert_skip REASON — emit a board card for a SKIP. Title is per-slug-per-
# day so repeat SKIPs today collapse to one card (dedup key = task+title).
_alert_skip() {
  local reason="$1" emitter
  if emitter="$(_resolve_emit_kanban)"; then
    log "$slug: alerting SKIP via $emitter"
    BUBBLE_AGENT_WORKDIR="$workdir" "$emitter" \
      task="session-rotate" \
      title="session-rotate SKIP: ${slug} ($(date -u +%Y-%m-%d))" \
      body="$reason" \
      type=incident \
      priority=normal \
      owner="$slug" \
      budget=10 \
      actions="investigate,retry" \
    || log "$slug: WARN — SKIP alert emit did not reach the board (see stderr above); SKIP still stands"
  else
    log "$slug: WARN — no emit_kanban_item.sh found (checked workdir/framework/Rick-dev paths) — SKIP alert NOT sent"
  fi
}

[[ -d "$workdir" && -d "$proj" ]] || { log "FATAL: $slug workdir/proj missing"; exit 2; }


_mtime() { stat -c %Y "$1"; }
_stamp_epoch() { date -u -d "$1" +%s 2>/dev/null; }
_newest_jsonl_mtime() { find "$proj" -maxdepth 2 -name '*.jsonl' -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1; }

# _handoff_age_h FILE — age in whole hours; the OLDER of mtime and the header stamp.
_handoff_age_h() {
  local f="$1" now mt st se age_m age_s
  now="$(date +%s)"; mt="$(_mtime "$f")"; age_m=$(( (now - mt) / 3600 )); age_s=$age_m
  st="$(grep -o -m1 'updated [0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\} [0-9]\{2\}:[0-9]\{2\} UTC' "$f" 2>/dev/null | head -1 | sed 's/^updated //; s/ UTC$//' || true)"
  if [[ -n "$st" ]] && se="$(_stamp_epoch "$st")" && [[ -n "$se" ]]; then age_s=$(( (now - se) / 3600 )); fi
  (( age_s > age_m )) && echo "$age_s" || echo "$age_m"
}

# _newest_jsonl_age_s — seconds since the live transcript last changed (idle probe).
_newest_jsonl_age_s() {
  local newest now; now="$(date +%s)"
  newest="$(_newest_jsonl_mtime)"; [[ -n "$newest" ]] || { echo 999999; return; }
  echo $(( now - newest ))
}

# _ask_handoff — idle-wait, inject the request, wait for HANDOFF.md to be rewritten.
# Returns 0 only if a fresh handoff now exists. Never kills anything.
_ask_handoff() {
  local t0 deadline
  [[ -n "$INJECT_FILE" && -e "$INJECT_FILE" ]] || { log "$slug: ask-handoff: inject file '${INJECT_FILE}' missing"; return 1; }
  deadline=$(( $(date +%s) + ASK_WAIT_S ))
  while (( $(_newest_jsonl_age_s) < IDLE_S )); do
    (( $(date +%s) < deadline )) || { log "$slug: ask-handoff: session busy (transcript changed < ${IDLE_S}s ago) for ${ASK_WAIT_S}s"; return 2; }
    sleep 15
  done
  t0="$(date +%s)"
  log "$slug: idle — asking the agent for a handoff via ${INJECT_FILE}"
  printf '%s\n' "[session-rotate, automated maintenance - not a human message] A daily fresh-session rotation runs in a few minutes. Write ./HANDOFF.md NOW in your workdir (overwrite, <= 2 KB): first line '# ${slug} - session handoff (updated $(date -u +%Y-%m-%d) HH:MM UTC)' with the real UTC time, then sections: Current goals, In-flight work (resume these), Recent key decisions (+why), Next steps, Blockers. Write for a successor who has your durable memory but NOT this transcript. Do not message anyone on Telegram; just write the file." >> "$INJECT_FILE"
  deadline=$(( $(date +%s) + ASK_WAIT_S ))
  while (( $(date +%s) < deadline )); do
    if [[ -f "$handoff" ]] && (( $(_mtime "$handoff") >= t0 )) && (( $(_handoff_age_h "$handoff") <= HANDOFF_MAX_AGE_H )); then
      sleep 20   # let the agent finish its turn so the transcript flush is quiet
      log "$slug: ask-handoff: HANDOFF.md rewritten"; return 0
    fi
    sleep 10
  done
  log "$slug: ask-handoff: no fresh HANDOFF.md within ${ASK_WAIT_S}s"; return 1
}

# FAIL-SAFE handoff gate (skippable only with --force).
if (( ! force )); then
  stale_reason=""
  if [[ ! -f "$handoff" ]]; then stale_reason="no HANDOFF.md at ${handoff}"
  else
    age_h="$(_handoff_age_h "$handoff")"
    (( age_h > HANDOFF_MAX_AGE_H )) && stale_reason="HANDOFF.md is ${age_h}h stale (> ${HANDOFF_MAX_AGE_H}h threshold) at ${handoff}"
  fi
  if [[ -n "$stale_reason" && "$ASK_HANDOFF" == "1" ]]; then
    if (( dry )); then log "DRY-RUN $slug: would ask the agent for a handoff via ${INJECT_FILE:-<unset>} (${stale_reason})"
    elif _ask_handoff; then stale_reason=""; fi
  fi
  if [[ -n "$stale_reason" ]]; then
    if (( dry )); then
      log "DRY-RUN $slug: would skip rotation and alert: ${stale_reason} — refusing to rotate into a context-thin session"
    else
      log "SKIP $slug: ${stale_reason} — refusing to rotate into a context-thin session"
      _alert_skip "${stale_reason} — refusing to rotate into a context-thin session."
    fi
    exit 0
  fi
  log "$slug: HANDOFF.md present + fresh — proceeding"
fi

arch="${home}/.claude/_session-archive/$(date -u +%Y-%m-%dT%H%M%SZ)"
shopt -s nullglob

if (( dry )); then
  mapfile -t jsonls < <(find "$proj" -maxdepth 2 -name '*.jsonl' -type f 2>/dev/null)
  log "DRY-RUN $slug: would stop $svc, archive ${#jsonls[@]} transcript(s) -> $arch, restart"
  exit 0
fi

# ORDER IS LOAD-BEARING — STOP FIRST, THEN ARCHIVE (bug found on the tony prototype
# 2026-09-22). Claude flushes its session transcript on graceful shutdown (SIGTERM).
# If we archive (mv) the .jsonl while the agent is still running, the shutdown then
# REWRITES a PARTIAL transcript (same session UUID) back into the projects dir; the
# next start's fresh-fallback gate sees that truncated file, adds --continue, and
# claude chokes on the incomplete transcript -> exit 1 -> crash loop. Stopping first
# lets the agent write its COMPLETE transcript, which we then archive with nothing
# holding it open or able to recreate it.
log "$slug: stopping $svc (lets the agent flush its transcript) ..."
systemctl stop "$svc" || true
sleep 5

# Now archive every transcript OUT of the projects tree so the gate finds none ->
# fresh start. (_session-archive is a sibling of projects/, never matched by the
# gate's projects/<dir>/*.jsonl glob.) Re-scan AFTER the stop so the shutdown flush
# is included.
mapfile -t jsonls < <(find "$proj" -maxdepth 2 -name '*.jsonl' -type f 2>/dev/null)
if (( ${#jsonls[@]} == 0 )); then
  log "$slug: no transcripts to archive (already fresh?)"
else
  mkdir -p "$arch"
  moved=0
  for f in "${jsonls[@]}"; do mv -- "$f" "$arch/" 2>/dev/null && moved=$((moved+1)) || true; done
  log "$slug: archived ${moved}/${#jsonls[@]} transcript(s) -> $arch"
fi

# Safety assert: the projects dir must hold no *.jsonl before we start, else the
# start would --continue a leftover and defeat the rotation.
leftover=$(find "$proj" -maxdepth 2 -name '*.jsonl' -type f 2>/dev/null | wc -l)
if (( leftover != 0 )); then
  log "FATAL $slug: ${leftover} transcript(s) still present after archive — NOT starting (avoids a bad --continue). Investigate."
  exit 3
fi

# Start -> prepare's fresh-fallback gate finds no transcript -> fresh session.
# boot_rearm re-arms the loop; the first tick reads HANDOFF.md.
systemctl start "$svc"
log "$slug: rotated to a fresh session (restarted $svc)"
