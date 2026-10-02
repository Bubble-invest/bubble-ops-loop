#!/bin/bash
# bubble-session-rotate-mac.sh — daily fresh-session rotation for a MAC dept (#1195).
#
# Mac twin of scripts/bubble-session-rotate.sh (VPS/systemd). Rotates a launchd+tmux
# dept to a FRESH claude session so its on-disk transcript can't grow until the
# reconstructed context overflows the window and wedges the agent ("Prompt is too
# long"). Auto-compaction bounds only the in-memory context per turn, NOT the .jsonl;
# --continue re-parses the whole file (content/miranda hit 108MB, 2026-09-22).
#
# Mechanism (matches the VPS design + its hard-won ordering):
#   1. bootout the launchd job so KeepAlive won't relaunch mid-rotation.
#   2. SIGTERM the claude process (tmux pane pid) so it flushes a COMPLETE transcript
#      on graceful shutdown, then wait for the tmux session to end.
#   3. THEN archive the .jsonl out of the projects dir (STOP-BEFORE-ARCHIVE — else the
#      shutdown flush rewrites a partial transcript that crash-loops the next start;
#      see the VPS bug bubble-ops-loop #467).
#   4. assert no *.jsonl remain, then bootstrap the job. The wrapper's #1195 gate
#      (--continue only if a transcript exists) then starts a FRESH session, which
#      reads HANDOFF.md at STEP 0 (written by the L4 session_handoff mission).
#
# FAIL-SAFE: only rotate if a FRESH HANDOFF.md exists (< HANDOFF_MAX_AGE_H) — else a
# fresh session would start context-blind, so SKIP and keep the current session.
#
# #1469: tightened from ~24h to 12h (override via HANDOFF_MAX_AGE_H) — a stale-day
# handoff must SKIP, not rotate into a context-thin session. Every SKIP now alerts
# (see _alert_skip below) instead of failing silently.
#
# Optional: --ask-handoff [--inject-file F] [--ask-wait S] (see ASK-for-handoff below).
# Runs AS the dept's Mac user (its launchd gui domain). Usage:
#   bubble-session-rotate-mac.sh <slug> [--workdir DIR] [--force] [--dry-run]
set -euo pipefail

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

slug="${1:?usage: bubble-session-rotate-mac.sh <slug> [--workdir DIR] [--force] [--dry-run]}"; shift || true
workdir=""; force=0; dry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2 ;;
    --force)   force=1; shift ;;
    --dry-run) dry=1; shift ;;
    --ask-handoff) ASK_HANDOFF=1; shift ;;
    --inject-file) INJECT_FILE="$2"; shift 2 ;;
    --ask-wait) ASK_WAIT_S="$2"; shift 2 ;;
    *) shift ;;
  esac
done

[[ -n "$workdir" ]] || workdir="$HOME/claude-workspaces/bubble-ops-${slug}"
label="com.bubble.ops-loop-${slug}"
session="ops-loop-${slug}"
plist="$HOME/Library/LaunchAgents/${label}.plist"
handoff="${workdir}/HANDOFF.md"
uid="$(id -u)"
# Claude names the project dir after the physical cwd with every non-alnum -> '-'.
proj="$HOME/.claude/projects/$(printf '%s' "$workdir" | LC_ALL=C tr -c 'A-Za-z0-9' '-')"
TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"

log() { printf '[session-rotate-mac] %s\n' "$*"; }

# ── #1469: SKIP alert (Mac twin of scripts/bubble-session-rotate.sh) ───────
# Reuse the fleet's existing kanban emitter rather than invent a new channel:
# it already dedupes on task+title (repeat SKIPs today collapse to one card)
# and already falls back to a best-effort Telegram ping when the board is
# unreachable. Best-effort only — never turns a SKIP into a crash.
_resolve_emit_kanban() {
  local here cand
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for cand in \
    "${EMIT_KANBAN_ITEM:-}" \
    "${workdir}/tools/kanban/emit_kanban_item.sh" \
    "${here}/../../tools/kanban/emit_kanban_item.sh" \
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

[[ -d "$workdir" ]] || { log "FATAL: $slug workdir $workdir missing"; exit 2; }
[[ -f "$plist" ]]   || { log "FATAL: $slug plist $plist missing"; exit 2; }


_mtime() { stat -f %m "$1"; }
_stamp_epoch() { date -j -u -f '%Y-%m-%d %H:%M' "$1" +%s 2>/dev/null; }
_newest_jsonl_mtime() { ls -t "$proj"/*.jsonl >/dev/null 2>&1 && stat -f %m "$(ls -t "$proj"/*.jsonl | head -1)" || true; }

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

# FAIL-SAFE handoff gate.
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

count_jsonl() { ls "$proj"/*.jsonl >/dev/null 2>&1 && ls "$proj"/*.jsonl | wc -l | tr -d ' ' || echo 0; }

if (( dry )); then
  log "DRY-RUN $slug: would bootout $label, SIGTERM the $session pane, archive $(count_jsonl) transcript(s) from $proj, bootstrap"
  exit 0
fi

# 1. Stop the launchd job so KeepAlive can't relaunch mid-rotation.
log "$slug: booting out $label (stops KeepAlive) ..."
launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
sleep 2

# 2. Gracefully stop claude so it flushes a COMPLETE transcript (SIGTERM the pane
#    pid — the tmux server is independent of the launchd job, so bootout alone does
#    not signal claude). Then wait for the session to end.
if "$TMUX_BIN" has-session -t "$session" 2>/dev/null; then
  pane_pid="$("$TMUX_BIN" list-panes -t "$session" -F '#{pane_pid}' 2>/dev/null | head -1 || true)"
  if [[ -n "$pane_pid" ]]; then
    log "$slug: SIGTERM claude (pane pid $pane_pid) to flush transcript ..."
    kill -TERM "$pane_pid" 2>/dev/null || true
  fi
  for _i in $(seq 1 20); do
    "$TMUX_BIN" has-session -t "$session" 2>/dev/null || break
    sleep 1
  done
  "$TMUX_BIN" kill-session -t "$session" 2>/dev/null || true
fi
sleep 2

# 3. Archive transcripts OUT of the projects tree (now the flush is complete and
#    nothing holds them open / will recreate them).
arch="$HOME/.claude/_session-archive/${slug}-$(date -u +%Y-%m-%dT%H%M%SZ)"
if ls "$proj"/*.jsonl >/dev/null 2>&1; then
  mkdir -p "$arch"
  moved=0
  for f in "$proj"/*.jsonl; do mv -- "$f" "$arch/" 2>/dev/null && moved=$((moved+1)) || true; done
  log "$slug: archived ${moved} transcript(s) -> $arch"
else
  log "$slug: no transcripts to archive (already fresh?)"
fi

# 4. Assert clean, then restart. The wrapper's #1195 gate -> fresh session.
if (( $(count_jsonl) != 0 )); then
  log "FATAL $slug: transcript(s) still present after archive — NOT restarting (avoids a bad --continue). Investigate."
  # best-effort: bring the dept back up on its (bloated) session rather than leave it down
  launchctl bootstrap "gui/${uid}" "$plist" 2>/dev/null || true
  exit 3
fi
log "$slug: bootstrapping $label (fresh session; first tick reads HANDOFF.md) ..."
launchctl bootstrap "gui/${uid}" "$plist"
log "$slug: rotated to a fresh session"
