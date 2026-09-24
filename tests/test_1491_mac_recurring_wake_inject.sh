#!/usr/bin/env bash
# Board #1491 (follow-up to #1487/#501): the Mac floor's own idle-nudge —
# deploy/local/local-loop-backup-runner.sh — injects into an EXISTING
# persistent session for the two Mac `recurring_missions`-schema depts,
# Miranda/content (jade-m1) and Géraldine/accountant (jade-m5). Before this
# card it always used the generic hardcoded WAKE_MESSAGE for these two depts
# (its due_dispatch grep gate only recognizes the OTHER Mac schema, Rick's
# loop.due_dispatch — see tests/test_rnd_due_mission_floor.sh), even after
# due_missions.py grew a `wake-prompt` generator for recurring_missions
# (#1487). ops-loop#501 already wired the VPS twin of this same idle-nudge
# (loop-backup.sh::inject_live_loop) to try that generator first and fall
# back to the historical free text, UNCHANGED, on any refusal/error. This
# test mirrors scripts/tests/test_1487_recurring_wake_inject.sh's two cases
# against the MAC runner instead, using the same fixture shape (mirrors
# Miranda/Géraldine's real dept.yaml: recurring_missions, no loop: block).
set -uo pipefail

RUNNER="${1:-$(cd "$(dirname "$0")/.." && pwd)/deploy/local/local-loop-backup-runner.sh}"
ROOT="$(cd "$(dirname "$RUNNER")/../.." && pwd)"
WORK="$(mktemp -d "$HOME/.rnd-1491-mac-wake-test.XXXXXX")"
cleanup() { find "$WORK" -depth -delete; }
trap cleanup EXIT

PASS=0
FAIL=0
ok() { echo "PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }
run() { /bin/bash "$RUNNER" "$@"; }

FALLBACK_NEEDLE='Resume your OODA loop (self-paced). Run one normal full tick now: STEP A safe pull'

TMUX="$WORK/tmux"
cat >"$TMUX" <<'TMUX'
#!/bin/sh
[ "$1" = has-session ] && exit 0
exit 1
TMUX
chmod 700 "$TMUX"
SELECTOR="$WORK/harness-selector"
printf 'claude\n' >"$SELECTOR"
chmod 600 "$SELECTOR"

# ── Case 1: recurring_missions schema with a live-due Layer-1 mission ──────
# Mirrors Miranda/content's and Géraldine/accountant's real dept.yaml shape
# (recurring_missions list, layer as a plain int, no loop: block at all —
# verified read-only against both live hosts, board #1491). No heartbeat
# file exists, so the floor's own staleness gate also requires a wake here —
# unchanged behaviour, this card only changes WHAT TEXT gets injected.
DUE="$WORK/content"
DUE_STATE="$WORK/content-state"
mkdir -p "$DUE/missions/gather_internal_work" "$DUE/layers/1" "$DUE/outputs" "$DUE_STATE"
chmod 700 "$DUE_STATE"
cat >"$DUE/dept.yaml" <<'YAML'
department:
  slug: content
  display_name: Miranda
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: gather_internal_work
  layer: 1
  cadence: every_1h
  description: test fixture mission (mirrors content's real gather_* missions)
  output_queue: queues/research/
  creates: [context_pool_item]
YAML
printf 'test mission prompt\n' >"$DUE/missions/gather_internal_work/PROMPT.md"
printf 'layer one\n' >"$DUE/layers/1/PROMPT.md"

due_args=(--dept-dir "$DUE" --slug content --telegram-state-dir "$DUE_STATE" --session-name ops-loop-content --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject)
LOCAL_LOOP_NOW_EPOCH=1789300800 run "${due_args[@]}" >"$WORK/content.log" 2>&1
rc=$?
prompt="$(tail -n 1 "$DUE_STATE/inject" 2>/dev/null)"
if [[ "$rc" -eq 0 ]] && [[ -n "$prompt" ]]; then
    ok "content: a wake was injected"
else
    bad "content: no wake was injected (rc=$rc): $(cat "$WORK/content.log")"
fi
[[ "$prompt" == *'DUE_MISSIONS=[gather_internal_work{cadence=every_1h,layer=1,file='* ]] \
    && ok "content: generated DUE_MISSIONS envelope present, correct mission/cadence/layer" \
    || bad "content: generated envelope missing/malformed: $prompt"
[[ "$prompt" == *'commit_dispatch'* ]] \
    && ok "content: COMPLETE line references commit_dispatch (VPS/Mac-recurring completion path, not the lease/claims path)" \
    || bad "content: COMPLETE/commit_dispatch line missing"
[[ "$prompt" == *'STALENESS:'* ]] \
    && ok "content: staleness re-check clause present (shared with the self-arm wake-prompt path)" \
    || bad "content: staleness clause missing"
[[ "$prompt" != *"$FALLBACK_NEEDLE"* ]] \
    && ok "content: fallback free text NOT used even though the generator had real due work" \
    || bad "content: fallback free text leaked in despite live due work"
grep -q 'inject using generated DUE_MISSIONS wake-prompt' "$WORK/content.log" \
    && ok "content: log records the generator path was used" \
    || bad "content: log did not record generator use: $(cat "$WORK/content.log")"

# ── Case 2: recurring_missions configured but NOTHING due right now ────────
# select_due_missions has no eligible mission this instant (every mission's
# own cadence/time gate is unmet) — wake-prompt's fail-closed contract (#1484
# PR review: never emit an empty DUE_MISSIONS=[]) refuses, and the runner
# MUST fall back to the historical generic WAKE_MESSAGE, byte-for-byte
# unchanged, exactly like #501 did for loop-backup.sh's inject_live_loop.
NOTDUE="$WORK/accountant"
NOTDUE_STATE="$WORK/accountant-state"
mkdir -p "$NOTDUE/missions/weekly_cfo_report" "$NOTDUE/layers/4" "$NOTDUE/outputs" "$NOTDUE_STATE"
chmod 700 "$NOTDUE_STATE"
cat >"$NOTDUE/dept.yaml" <<'YAML'
department:
  slug: accountant
  display_name: Géraldine
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: weekly_cfo_report
  layer: 4
  cadence: weekly
  day: wednesday
  time: '08:00'
  description: test fixture mission (mirrors accountant's real weekly_cfo_report)
  output_queue: queues/gates/
  creates: [cfo_report]
YAML
printf 'test mission prompt\n' >"$NOTDUE/missions/weekly_cfo_report/PROMPT.md"
printf 'layer four\n' >"$NOTDUE/layers/4/PROMPT.md"

# 2026-09-20T12:00:00Z is a Sunday — the only configured mission (Wednesday
# 08:00 Paris) is not due, so select_due_missions has nothing this instant.
notdue_args=(--dept-dir "$NOTDUE" --slug accountant --telegram-state-dir "$NOTDUE_STATE" --session-name ops-loop-accountant --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject)
LOCAL_LOOP_NOW_EPOCH=1789905600 run "${notdue_args[@]}" >"$WORK/accountant.log" 2>&1
rc=$?
prompt2="$(tail -n 1 "$NOTDUE_STATE/inject" 2>/dev/null)"
[[ "$rc" -eq 0 && -n "$prompt2" ]] \
    && ok "accountant: a wake was still injected (fallback path, not a hard failure)" \
    || bad "accountant: wake injection failed outright (rc=$rc): $(cat "$WORK/accountant.log")"
[[ "$prompt2" == *"$FALLBACK_NEEDLE"* ]] \
    && ok "accountant: fallback free text used, unchanged, when nothing is due" \
    || bad "accountant: fallback text missing/changed: $prompt2"
[[ "$prompt2" != *'DUE_MISSIONS='* ]] \
    && ok "accountant: no DUE_MISSIONS envelope leaked in when nothing is due" \
    || bad "accountant: a DUE_MISSIONS envelope leaked in despite nothing due"
grep -q 'wake-prompt generator refused/errored' "$WORK/accountant.log" \
    && ok "accountant: log records the generator refusal, non-fatally" \
    || bad "accountant: log did not record the generator refusal: $(cat "$WORK/accountant.log")"

echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
