#!/usr/bin/env bash
# bubble-session-rotate.sh — daily fresh-session rotation for a dept (board #1195).
#
# Rotates a dept to a FRESH Claude session once per day so its on-disk transcript
# .jsonl can't grow until the reconstructed context overflows the window and wedges
# the agent ("Prompt is too long"). Auto-compaction bounds only the in-memory
# context per turn, NOT the .jsonl; --continue re-parses the whole file, so a
# forever-session inevitably overflows (maya @12MB, ben @16.7MB, 2026-09-21).
#
# Mechanism (NO edit to the fund-critical bubble-agent-prepare): archive the dept's
# session transcripts out of the projects dir, then restart the unit. The EXISTING
# fresh-fallback gate (bubble-agent-prepare: --continue only if a *.jsonl exists in
# the cwd's project dir) then starts a FRESH session. Context is preserved because
# the L4 `session_handoff` mission wrote HANDOFF.md, which the layer prompts read at
# STEP 0 on the fresh session's first tick.
#
# FAIL-SAFE: only rotate if a FRESH HANDOFF.md exists (< HANDOFF_MAX_AGE_H). Without
# it the fresh session would start context-blind, so we SKIP the rotation and keep
# the current session (a bloated-but-working session beats a fresh-but-blind one;
# the #1436 wedge check + this next run will catch/retry it). Run as root (needs
# systemctl restart + to move the agent-uid-owned transcripts).
#
# Usage: bubble-session-rotate.sh <slug> [--force] [--dry-run]
set -euo pipefail

HANDOFF_MAX_AGE_H="${HANDOFF_MAX_AGE_H:-20}"   # handoff must be newer than this

slug="${1:?usage: bubble-session-rotate.sh <slug> [--force] [--dry-run]}"; shift || true
force=0; dry=0
for a in "$@"; do
  case "$a" in --force) force=1;; --dry-run) dry=1;; esac
done

workdir="/srv/agents/${slug}"
home="/home/agent-${slug}"
proj="${home}/.claude/projects"
handoff="${workdir}/HANDOFF.md"
svc="bubble-agent@${slug}.service"

log() { printf '[session-rotate] %s\n' "$*"; }

[[ -d "$workdir" && -d "$proj" ]] || { log "FATAL: $slug workdir/proj missing"; exit 2; }

# FAIL-SAFE handoff gate (skippable only with --force).
if (( ! force )); then
  if [[ ! -f "$handoff" ]]; then
    log "SKIP $slug: no HANDOFF.md — refusing to rotate into a context-blind session"
    exit 0
  fi
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$handoff") ) / 3600 ))
  if (( age_h > HANDOFF_MAX_AGE_H )); then
    log "SKIP $slug: HANDOFF.md is ${age_h}h stale (> ${HANDOFF_MAX_AGE_H}h) — refusing to rotate"
    exit 0
  fi
  log "$slug: HANDOFF.md present + fresh (${age_h}h) — proceeding"
fi

# Archive all of the dept's transcripts OUT of the projects tree so the
# fresh-fallback gate finds none -> starts fresh. (_session-archive is a sibling
# of projects/, never matched by the gate's projects/<dir>/*.jsonl glob.)
arch="${home}/.claude/_session-archive/$(date -u +%Y-%m-%dT%H%M%SZ)"
shopt -s nullglob
mapfile -t jsonls < <(find "$proj" -maxdepth 2 -name '*.jsonl' -type f 2>/dev/null)
if (( ${#jsonls[@]} == 0 )); then
  log "$slug: no live transcripts to archive (already fresh?) — restart only"
else
  if (( dry )); then
    log "DRY-RUN $slug: would archive ${#jsonls[@]} transcript(s) -> $arch and restart $svc"
    exit 0
  fi
  mkdir -p "$arch"
  moved=0
  for f in "${jsonls[@]}"; do mv -- "$f" "$arch/" 2>/dev/null && moved=$((moved+1)) || true; done
  log "$slug: archived ${moved}/${#jsonls[@]} transcript(s) -> $arch"
fi

if (( dry )); then log "DRY-RUN $slug: would restart $svc"; exit 0; fi

# DEEP restart (stop+start, not restart) — the reliable teardown for the session
# (see wiki vps-dept-silence-session-wedge-fix). boot_rearm re-arms the loop; the
# first tick reads HANDOFF.md.
systemctl stop "$svc" || true
sleep 5
systemctl start "$svc"
log "$slug: rotated to a fresh session (restarted $svc)"
