#!/usr/bin/env bash
# =============================================================================
# test_boot_rearm_manifest.sh — the manifest-driven half of board card #461
# (child of #456): the boot-rearm inject text in
# deploy/templates/ops-loop-dept.service.template must now ALSO instruct the
# agent to self-heal its declarative config/crons.yaml, not just re-arm /loop.
#
# This is a bash-level SMOKE test of the actual ExecStartPost line shipped in
# the template (extracted + executed against a fixture TELEGRAM_STATE_DIR),
# complementing scripts/lib/tests/test_crons_manifest.py (the pure Python
# load/diff core) and tests/test_boot_rearm_install.sh (the pre-existing
# /loop-only boot-rearm plugin installer, untouched by this card).
#
# Covers:
#   A. The template's inject ExecStartPost line is present exactly once and
#      is syntactically valid `sh -c '...'` (executes cleanly, writes to the
#      inject file, does not error).
#   B. The written message mentions config/crons.yaml, CronList, CronCreate,
#      the file: prompt_ref convention, and the critical-alert safety net —
#      i.e. the (b)+(c) instructions from #456 are actually IN the boot turn,
#      not just planned.
#   C. The pre-existing /loop self-pacing instructions are UNCHANGED
#      (STEP A-F, "never hardcode an hourly cron", the deaf-watchdog note) —
#      this card generalizes boot-rearm, it does not replace the /loop arm.
#
# Run:  bash tests/test_boot_rearm_manifest.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
TEMPLATE="$REPO_ROOT/deploy/templates/ops-loop-dept.service.template"

[[ -f "$TEMPLATE" ]] || { echo "FATAL: template not found: $TEMPLATE"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== test_boot_rearm_manifest.sh =="
echo "   template: $TEMPLATE"
echo ""

# ── A. exactly one manifest-aware inject ExecStartPost line ─────────────────
echo "A. template has exactly one manifest-aware boot-inject ExecStartPost line"
MATCHING_LINES="$(grep -c 'ExecStartPost=/bin/sh -c "sleep 8 && printf' "$TEMPLATE" || true)"
[[ "$MATCHING_LINES" == "1" ]] && ok "exactly one boot-inject ExecStartPost line" || bad "expected 1 matching line, found $MATCHING_LINES"

INJECT_LINE="$(grep 'ExecStartPost=/bin/sh -c "sleep 8 && printf' "$TEMPLATE")"
[[ -n "$INJECT_LINE" ]] || { echo "FATAL: could not extract inject line — aborting"; exit 2; }

# Strip the `ExecStartPost=` prefix so we get a bare `/bin/sh -c "..."` we can
# execute directly with a fixture TELEGRAM_STATE_DIR and a fast sleep.
CMD="${INJECT_LINE#ExecStartPost=}"
CMD="${CMD/sleep 8/sleep 0}"   # don't actually wait 8s in the test

TELEGRAM_STATE_DIR="$WORK/tg-state"
mkdir -p "$TELEGRAM_STATE_DIR"
export TELEGRAM_STATE_DIR

eval "$CMD"
RC=$?
[[ "$RC" == "0" ]] && ok "extracted inject command executes cleanly (rc=0)" || bad "inject command exited $RC"
[[ -f "$TELEGRAM_STATE_DIR/inject" ]] && ok "inject file was written" || bad "inject file missing after run"

MSG="$(cat "$TELEGRAM_STATE_DIR/inject" 2>/dev/null || true)"

# ── B. manifest re-arm instructions present in the boot turn ────────────────
echo "B. boot turn contains the #456 (b)+(c) manifest instructions"
check_contains() {
  local needle="$1" label="$2"
  if echo "$MSG" | grep -qF "$needle"; then
    ok "$label"
  else
    bad "$label (missing: \"$needle\")"
  fi
}
check_contains "config/crons.yaml"            "mentions the manifest path"
check_contains "CronList"                     "instructs a CronList read"
check_contains "CronCreate it"                 "instructs re-creating missing entries via CronCreate"
check_contains "prompt_ref starting with file:" "explains the file: prompt_ref convention"
check_contains "critical: true"                "names the critical flag"
check_contains "loud Telegram alert"           "instructs the (c) safety-net alert on a still-missing critical entry"
check_contains "idempotent"                    "states the re-arm is idempotent (no dupes)"
check_contains "absent file = nothing else to do here" "documents the no-manifest no-op case"

# ── C. pre-existing /loop self-pacing instructions are unchanged ────────────
echo "C. pre-existing /loop self-arm instructions are preserved"
check_contains "STEP A-F"                      "still references STEP A-F tick protocol"
check_contains "Never hardcode an hourly cron" "still bans hardcoded hourly crons"
check_contains "deaf-watchdog"                 "still warns against bare slash-command CronCreate prompts"
check_contains "arm your OWN next wake"        "still arms the agent's own /loop wake"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
