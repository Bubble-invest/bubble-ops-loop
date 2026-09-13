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

# Mode is passed as $1: compile (nightly, default) | synthesis (weekly) |
# pruning (weekly) | skillsmith (weekly). One script, four systemd units, four
# SKILL entrypoints — keeps the launcher DRY while honouring Joris's "one compile
# job" for the nightly mining (synthesis + pruning are maintenance; skillsmith is
# the #1222 KNOW-HOW authoring/pruning pass — a DIFFERENT skill, see below).
MODE="${1:-compile}"

# Per-mode SKILL to load. The wiki modes load cloud-wiki-compile (KNOWLEDGE);
# skillsmith loads the separate skill-authoring skill (KNOW-HOW) — the doctrine
# boundary Joris drew (#1222): wiki=knowledge is cloud-wiki-compile's job,
# skills=know-how is skill-authoring's job, complementary NOT overlapping. We
# reuse this launcher/service/timer machinery (the "reuse the weekly slot" ask),
# not the wiki-compile skill's steps.
case "$MODE" in
    skillsmith) SKILL_NAME=skill-authoring ;;
    *)          SKILL_NAME=cloud-wiki-compile ;;
esac

# Skill location (synced from Mac source of truth — Lab owns it).
SKILL_DIR=/home/claude/.claude/skills/${SKILL_NAME}
if [ ! -f "${SKILL_DIR}/SKILL.md" ]; then
    log "FATAL: ${SKILL_DIR}/SKILL.md missing."
    exit 1
fi

# Wiki must be a git clone (cloud-wiki-sync keeps it synced) — required only for
# the wiki modes. skillsmith does not touch the shared wiki (it works the skill
# registry + the centralized transcript corpus), so its git presence is optional.
WIKI_DIR=/home/claude/.claude/agent-memory/shared-wiki
if [ "$MODE" != "skillsmith" ] && [ ! -d "${WIKI_DIR}/.git" ]; then
    log "FATAL: ${WIKI_DIR} is not a git repo — cannot compile."
    exit 1
fi

# Board #1247: collect structural intent-frontmatter evidence before the model
# runs. The report never assigns intent or declares a semantic leak; the COMPILE
# skill reads candidates and makes that judgment. Missing tooling is fatal so an
# incomplete deploy cannot silently disable the audit.
INTENT_AUDIT_SCRIPT=/home/claude/scripts/wiki-intent-audit.py
INTENT_AUDIT_REPORT=/home/claude/monitoring/wiki-intent-audit/latest.json
DELTA_SCRIPT=/home/claude/scripts/wiki-delta.py
DELTA_DIR=/home/claude/monitoring/wiki-compile-delta
DELTA_STATE=$DELTA_DIR/state.json
DELTA_PLAN=$DELTA_DIR/current-plan.json
DELTA_MARKER=$DELTA_DIR/completed.json
if [ "$MODE" = "compile" ]; then
    if [ ! -f "$INTENT_AUDIT_SCRIPT" ]; then
        log "FATAL: ${INTENT_AUDIT_SCRIPT} missing (incomplete #1247 install)."
        exit 1
    fi
    mkdir -p "$(dirname "$INTENT_AUDIT_REPORT")"
    if ! python3 "$INTENT_AUDIT_SCRIPT" \
        --wiki "$WIKI_DIR" --output "$INTENT_AUDIT_REPORT"; then
        log "FATAL: intent audit failed; compile not started."
        exit 1
    fi
    if [ ! -f "$DELTA_SCRIPT" ]; then
        log "FATAL: ${DELTA_SCRIPT} missing (incomplete delta-planner install)."
        exit 1
    fi
    mkdir -p "$DELTA_DIR"
    # systemd should serialize this unit, but the explicit lock also protects
    # manual starts from racing the same plan/state files.
    exec 9>"$DELTA_DIR/compile.lock"
    if ! flock -n 9; then
        log "FATAL: another cloud wiki compile owns $DELTA_DIR/compile.lock."
        exit 1
    fi
    rm -f "$DELTA_MARKER"
    DELTA_ARGS=(
        plan --state "$DELTA_STATE" --plan "$DELTA_PLAN"
        --run-root "$DELTA_DIR/runs"
        --max-folders "${WIKI_COMPILE_MAX_FOLDERS:-4}"
        --max-batches "${WIKI_COMPILE_MAX_BATCHES:-3}"
        --max-reduced-chars "${WIKI_COMPILE_MAX_REDUCED_CHARS:-240000}"
        --context-rows "${WIKI_COMPILE_CONTEXT_ROWS:-8}"
        --sla-target-runs "${WIKI_COMPILE_SLA_RUNS:-3}"
    )
    [ "$(date -u +%u)" = "7" ] && DELTA_ARGS+=(--weekly)
    # Operator/on-demand escape hatch. Full creates a persistent frozen
    # generation which subsequent bounded runs continue alongside live delta.
    [ "${WIKI_COMPILE_FULL:-0}" = "1" ] && DELTA_ARGS+=(--full)
    if ! python3 "$DELTA_SCRIPT" "${DELTA_ARGS[@]}"; then
        log "FATAL: delta planning failed; compile not started."
        exit 1
    fi
fi

case "$MODE" in
    compile)    TASK="Run the cloud-wiki-compile skill in COMPILE mode (nightly). The deterministic work plan is ${DELTA_PLAN}; use ONLY its exact reduced feeds and batches for transcript extraction, including every selected folder and all piggyback passes. Do not find or parse transcript files yourself." ;;
    synthesis)  TASK="Run the cloud-wiki-compile skill in SYNTHESIS mode (weekly): read the week's wiki git diffs and write the weekly synthesis meta-document." ;;
    pruning)    TASK="Run the cloud-wiki-compile skill in PRUNING mode (weekly): TTL-based staleness review, archive what's stale, enforce per-agent page caps." ;;
    skillsmith) TASK="Run the skill-authoring skill (weekly, #1222): ARM A CREATE — consume THIS week's STEP 4.6 skill-gap candidates.md (do NOT re-mine transcripts), dedupe against existing skills, draft survivors, eval WITH vs WITHOUT, and only author what demonstrably helps; ARM B PRUNE — count actual Skill usage across the centralized transcript corpus and judge genuinely-dead vs rare-but-critical skills, flagging (never auto-removing) dead ones. File via emit-kanban-task, nudge owning agents, gate critical changes needs:human." ;;
    *) log "FATAL: unknown mode '$MODE' (expected compile|synthesis|pruning|skillsmith)"; exit 1 ;;
esac

PROMPT="${TASK} Follow the skill step-by-step, end-to-end. Today is ${DATE_STAMP} (UTC). At the end, post the Telegram report ONLY if the SKILL's reporting rule says to (silent on quiet runs)."

cd /home/claude || exit 1

# Per-mode model (Joris 2026-06-19): the nightly compile is mechanical
# orchestration (spawn extraction/write-back subagents, regenerate index) — Sonnet
# is the right tier, and the subagents it spawns are themselves Sonnet. The WEEKLY
# synthesis is the deep, judgment-forming "what did the system learn" thesis over
# the fleet's shared memory — that runs ONCE A WEEK, so it gets Opus (worth the
# strongest model; cost is bounded by frequency). Pruning is maintenance → Sonnet.
# skillsmith (#1222) must stay CHEAP — it's a weekly anti-bloat pass whose
# deterministic pre-passes (candidate parse, embed-dedupe, usage counting) burn
# ~0 model tokens and kill most candidates before any reasoning runs; only the
# handful of survivors reach the model. Haiku is the right tier (the eval-gate
# subagents it spawns are Haiku too). Sonnet for wiki compile/pruning; Opus only
# for the once-weekly synthesis thesis.
case "$MODE" in
    synthesis)  RUN_MODEL="opus";   RUN_THINKING=20000 ;;
    skillsmith) RUN_MODEL="haiku";  RUN_THINKING=6000  ;;
    *)          RUN_MODEL="sonnet"; RUN_THINKING=8000  ;;
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

# Two-phase watermark: claude's zero exit is necessary but not sufficient. The
# launcher parses the JSON result and creates a plan-bound completion marker
# only for is_error=false plus the exact WIKI_COMPILE_RECEIPT:<run_id>. Budget
# exhaustion, malformed output, or tool failure leaves the semantic queue
# untouched, so the same immutable chunks are retried.
if [ "$MODE" = "compile" ] && [ "$EXIT" -eq 0 ]; then
    if ! python3 "$DELTA_SCRIPT" accept-result \
        --plan "$DELTA_PLAN" --marker "$DELTA_MARKER" --result "$RUN_LOG"; then
        log "FATAL: compile output was not valid JSON success; watermark not advanced."
        EXIT=1
    elif ! python3 "$DELTA_SCRIPT" commit --plan "$DELTA_PLAN" --marker "$DELTA_MARKER"; then
        log "FATAL: success marker could not be committed; watermark not advanced."
        EXIT=1
    fi
fi

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
