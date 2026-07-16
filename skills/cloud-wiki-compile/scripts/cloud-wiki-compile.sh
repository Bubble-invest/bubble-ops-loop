#!/usr/bin/env bash
# /home/claude/scripts/cloud-wiki-compile.sh
# Managed by bubble-vps-platform / Lab. Source of truth:
#   ~/claude-workspaces/Rick_RnD/projects/cloud-wiki-compile/vps/cloud-wiki-compile.sh
#
# The single, always-on compiler for the shared wiki. The VPS is always on, so
# it owns ALL wiki compilation now — the three former Mac crons
# (shared-wiki-compile, wiki-weekly-synthesis, wiki-pruning) are retired.
#
# Wakes a one-shot `claude -p` session that loads the cloud-wiki-compile skill
# and mines today's transcripts from THREE sources:
#   1. The 6 VPS-native agents (tony, cgp, maya, claudette, morty, ricky)
#      at /home/claude/.claude/projects/-home-claude-agents-*/
#   2. Joris's Mac    -> /home/claude/.claude/projects/_mac-joris/  (rsync'd in)
#   3. Jade's Mac     -> /home/claude/.claude/projects/_mac-jade/   (rsync'd in)
# ...and writes/updates pages in the shared wiki (a git clone kept in lockstep
# with GitHub by cloud-wiki-sync.timer).
#
# Same headless pattern as morty-agentic-audit.sh: --print, per-mode model
# (sonnet for nightly compile/pruning, opus for the weekly synthesis thesis),
# --max-budget-usd, --no-session-persistence, --dangerously-skip-permissions.
#
# The actual instructions live in the SKILL; this script is just the launcher.
set -uo pipefail

DATE_STAMP=$(date -u +"%Y-%m-%d")
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
RUN_LOG=/tmp/cloud-wiki-compile-${DATE_STAMP}.log
LOG_TAG=cloud-wiki-compile
log() { logger -t "$LOG_TAG" "$*"; echo "[$TS] $*"; }

# Env (TELEGRAM_BOT_TOKEN for the optional report) — already decrypted into
# /run/claude-agent/env at agent boot. Optional: the SKILL handles absence.
ENV_FILE=/run/claude-agent/env

# AUTH FIX (Rick 2026-06-06): the headless `claude -p` below was authenticating
# off the claude user's ~/.claude/.credentials.json, which EXPIRED 2026-06-03
# (refresh not auto-applied headless) -> every compile failed 401. The dept
# agents stay alive because systemd injects a fresh CLAUDE_CODE_OAUTH_TOKEN from
# SOPS into /run/claude-agent*/env. Source that SAME maintained token here so the
# compile uses it instead of the stale on-disk credential. Non-fatal if absent
# (claude falls back to the credentials file, same as before).
if [ -r "${ENV_FILE}" ]; then
    _TOK=$(awk -F= '/^CLAUDE_CODE_OAUTH_TOKEN=/{print $2; exit}' "${ENV_FILE}" 2>/dev/null)
    if [ -n "${_TOK}" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="${_TOK}"
    fi
fi

# Skill location (synced from Mac source of truth — Lab owns it).
SKILL_DIR=/home/claude/.claude/skills/cloud-wiki-compile
if [ ! -f "${SKILL_DIR}/SKILL.md" ]; then
    log "FATAL: ${SKILL_DIR}/SKILL.md missing."
    exit 1
fi

# Wiki must be a git clone (cloud-wiki-sync keeps it synced).
WIKI_DIR=/home/claude/.claude/agent-memory/shared-wiki
if [ ! -d "${WIKI_DIR}/.git" ]; then
    log "FATAL: ${WIKI_DIR} is not a git repo — cannot compile."
    exit 1
fi

# Mode is passed as $1: compile (nightly, default) | synthesis (weekly) |
# pruning (weekly). One script, three systemd units, three SKILL entrypoints —
# keeps the launcher DRY while honouring Joris's "one compile job" for the
# nightly mining (synthesis + pruning are maintenance, not compilation).
MODE="${1:-compile}"
case "$MODE" in
    compile)   TASK="Run the cloud-wiki-compile skill in COMPILE mode (nightly): mine today's transcripts from the 6 VPS agents plus both Mac caches (_mac-joris, _mac-jade) and update the shared wiki." ;;
    synthesis) TASK="Run the cloud-wiki-compile skill in SYNTHESIS mode (weekly): read the week's wiki git diffs and write the weekly synthesis meta-document." ;;
    pruning)   TASK="Run the cloud-wiki-compile skill in PRUNING mode (weekly): TTL-based staleness review, archive what's stale, enforce per-agent page caps." ;;
    *) log "FATAL: unknown mode '$MODE' (expected compile|synthesis|pruning)"; exit 1 ;;
esac

PROMPT="${TASK} Follow the skill step-by-step, end-to-end. Today is ${DATE_STAMP} (UTC). At the end, post the Telegram report ONLY if real knowledge was written (silent on quiet runs)."

cd /home/claude || exit 1

# Per-mode model (Joris 2026-06-19): the nightly compile is mechanical
# orchestration (spawn extraction/write-back subagents, regenerate index) — Sonnet
# is the right tier, and the subagents it spawns are themselves Sonnet. The WEEKLY
# synthesis is the deep, judgment-forming "what did the system learn" thesis over
# the fleet's shared memory — that runs ONCE A WEEK, so it gets Opus (worth the
# strongest model; cost is bounded by frequency). Pruning is maintenance → Sonnet.
case "$MODE" in
    synthesis) RUN_MODEL="opus";   RUN_THINKING=20000 ;;
    *)         RUN_MODEL="sonnet"; RUN_THINKING=8000  ;;
esac

log "starting mode=${MODE} model=${RUN_MODEL}"
# Thinking tokens bill as output: 8000 is ample for mechanical dispatch; the
# weekly Opus synthesis gets more headroom for the thesis.
MAX_THINKING_TOKENS=${RUN_THINKING} \
/usr/bin/claude \
    --print \
    --no-session-persistence \
    --setting-sources user \
    --model "${RUN_MODEL}" \
    --max-budget-usd 12.00 \
    --output-format json \
    --dangerously-skip-permissions \
    "$PROMPT" \
    > "$RUN_LOG" 2>&1
EXIT=$?

log "mode=${MODE} exit=${EXIT} (output: $RUN_LOG)"

# Keep the run log for debugging (root-owned if possible, else leave in /tmp).
if [ -f "$RUN_LOG" ]; then
    # Write the run log to the claude-owned dir the freshness watchdog reads
    # (was /var/log/bubble-wiki — root-owned, sudo install denied, marker lost; Rick 2026-06-20).
    LOG_DIR=/home/claude/logs/bubble-wiki
    mkdir -p "$LOG_DIR"
    cp "$RUN_LOG" "${LOG_DIR}/compile-${MODE}-${DATE_STAMP}.log" 2>/dev/null || true
    rm -f "$RUN_LOG"
fi

exit $EXIT
