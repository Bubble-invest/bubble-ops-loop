#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASH_BIN="${BASH_BIN:-$(command -v bash)}"
PY_BIN="${PY_BIN:-$(command -v python3)}"
fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'find "$TMP" -depth -delete 2>/dev/null || true' EXIT
mkdir -p "$TMP/framework/venv/bin" "$TMP/agents" "$TMP/locks" "$TMP/home" "$TMP/bin"
ln -s "$PY_BIN" "$TMP/framework/venv/bin/python"
ln -s "$ROOT/scripts" "$TMP/framework/scripts"

for slug in ben tony maya; do
  mkdir -p "$TMP/agents/$slug/layers/1" "$TMP/agents/$slug/onboarding"
  printf 'layer one\n' >"$TMP/agents/$slug/layers/1/PROMPT.md"
  printf 'host: vps\n' >"$TMP/agents/$slug/onboarding/STATE.yaml"
  printf '{}\n' >"$TMP/agents/$slug/dept.yaml"
done

cat >"$TMP/systemctl" <<'EOF_SYSTEMCTL'
#!/usr/bin/env bash
case "$1" in
  is-enabled) exit 0 ;;
  show) printf '%s\n' "${STUB_MAINPID:-0}"; exit 0 ;;
  restart) exit 91 ;;
  *) exit 1 ;;
esac
EOF_SYSTEMCTL
cat >"$TMP/deny-model" <<'EOF_MODEL'
#!/usr/bin/env bash
: >"${MODEL_CALLED:?}"
exit 97
EOF_MODEL
chmod +x "$TMP/systemctl" "$TMP/deny-model"
printf 'CLAUDE_CODE_OAUTH_TOKEN=synthetic\n' >"$TMP/env"

run_floor() {
  local slug="$1" log="$2"
  HOME="$TMP/home/$slug" \
  BUBBLE_BACKUP_TEST_UID_OK=1 \
  BUBBLE_BACKUP_PRIMARY_WAKE_ONLY=1 \
  BUBBLE_BACKUP_HERMES_DEPTS=maya \
  BUBBLE_OPS_LOOP_ROOT="$TMP/framework" \
  BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" \
  BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" \
  BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" \
  BUBBLE_BACKUP_LOG="$log" \
  BUBBLE_BACKUP_SHARED_ENV="$TMP/env" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" \
  BUBBLE_BACKUP_CLAUDE_BIN="$TMP/deny-model" \
  BUBBLE_BACKUP_LAYER_OFFSET_H=-24 \
  BUBBLE_BACKUP_WAKE_WAIT_ITERATIONS=2 \
  BUBBLE_BACKUP_WAKE_WAIT_SECONDS=0 \
  BUBBLE_DISPATCH_DIRECTIVES=0 \
  BUBBLE_AUTORESTART=0 \
  MODEL_CALLED="$TMP/model-called" \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept "$slug"
}

assert_no_completion_evidence() {
  local slug="$1" out="$TMP/agents/$slug/outputs"
  [[ ! -e "$out/$(date -u +%Y-%m-%d)/dispatch.json" ]] || fail "$slug dispatch ledger was forged"
  if [[ -d "$out" ]] && find "$out" -type f \( -name .last-run -o -name .last-materialized \) -print -quit | grep -q .; then
    fail "$slug handled marker was forged"
  fi
}

# Missing Ben primary: visible nonzero defer, with no model and no fake heartbeat.
rm -f "$TMP/model-called"
export STUB_MAINPID=0
set +e
ben_out="$(run_floor ben "$TMP/ben-failed.jsonl" 2>&1)"
ben_rc=$?
set -e
[[ "$ben_rc" -ne 0 ]] || fail "missing Ben primary returned green"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called for missing Ben primary"
[[ ! -e "$TMP/agents/ben/outputs/$(date -u +%Y-%m-%d)/heartbeat.log" ]] || fail "fake Ben heartbeat written"
assert_no_completion_evidence ben
[[ "$ben_out" == *"primary runtime wake unavailable; headless fallback disabled"* ]] || fail "Ben defer invisible"
grep -Eq '"action"[[:space:]]*:[[:space:]]*"deferred"' "$TMP/ben-failed.jsonl" || fail "Ben deferred event missing"

# Busy Tony primary: inject is accepted but no heartbeat advances. Still defer,
# never declare handled, and never fall through to the deny-model binary.
rm -f "$TMP/model-called"
export STUB_MAINPID=123
export BUBBLE_BACKUP_TEST_LIVE_POLLER_OK=1
mkdir -p "$TMP/home/tony/.claude/channels/telegram-tony"
set +e
tony_out="$(run_floor tony "$TMP/tony-busy.jsonl" 2>&1)"
tony_rc=$?
set -e
[[ "$tony_rc" -ne 0 ]] || fail "busy Tony primary returned green"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called after busy Tony inject"
[[ ! -e "$TMP/agents/tony/outputs/$(date -u +%Y-%m-%d)/heartbeat.log" ]] || fail "fake Tony heartbeat written"
assert_no_completion_evidence tony
[[ "$tony_out" == *"inject sent but no tick within window"* ]] || fail "Tony timeout invisible"
[[ "$tony_out" == *"headless fallback disabled"* ]] || fail "Tony defer invisible"
grep -Eq '"action"[[:space:]]*:[[:space:]]*"deferred"' "$TMP/tony-busy.jsonl" || fail "Tony deferred event missing"

# Successful classic primary wakes: the existing Ben/Tony primary writes its
# own heartbeat; the floor records success and the deny-model stays untouched.
for slug in ben tony; do
  rm -f "$TMP/model-called"
  rm -rf "$TMP/agents/$slug/outputs"
  state_dir="$TMP/home/$slug/.claude/channels/telegram-$slug"
  mkdir -p "$state_dir"
  inject="$state_dir/inject"
  : >"$inject"
  hb="$TMP/agents/$slug/outputs/$(date -u +%Y-%m-%d)/heartbeat.log"
  (
    for _ in $(seq 1 200); do
      if [[ -s "$inject" ]]; then
        mkdir -p "$(dirname "$hb")"
        printf 'synthetic primary tick\n' >"$hb"
        exit 0
      fi
      sleep 0.01
    done
    exit 1
  ) &
  watcher=$!
  ok_out="$(run_floor "$slug" "$TMP/$slug-ok.jsonl" 2>&1)"
  wait "$watcher"
  [[ ! -e "$TMP/model-called" ]] || fail "headless model called after successful $slug wake"
  [[ -s "$hb" ]] || fail "$slug primary heartbeat missing"
  [[ "$ok_out" == *"live session ticked from inject"* ]] || fail "$slug success invisible"
  grep -Eq '"action"[[:space:]]*:[[:space:]]*"run"' "$TMP/$slug-ok.jsonl" || fail "$slug run event missing"
done

# Observer boundary: 23:30 UTC is already the next Europe/Paris day on both
# sides of DST. The floor must watch the canonical primary path, not UTC's.
rm -f "$TMP/model-called"
rm -rf "$TMP/agents/ben/outputs"
: >"$TMP/home/ben/.claude/channels/telegram-ben/inject"
export BUBBLE_BACKUP_TEST_NOW_UTC="2026-03-28T23:30:00+00:00"
paris_hb="$TMP/agents/ben/outputs/2026-03-29/heartbeat.log"
(
  for _ in $(seq 1 200); do
    if [[ -s "$TMP/home/ben/.claude/channels/telegram-ben/inject" ]]; then
      mkdir -p "$(dirname "$paris_hb")"
      printf 'synthetic Paris-day primary tick\n' >"$paris_hb"
      exit 0
    fi
    sleep 0.01
  done
  exit 1
) &
watcher=$!
boundary_out="$(run_floor ben "$TMP/ben-paris-boundary.jsonl" 2>&1)"
wait "$watcher"
unset BUBBLE_BACKUP_TEST_NOW_UTC
[[ "$boundary_out" == *"live session ticked from inject"* ]] || fail "Paris-day heartbeat was not observed"
[[ -s "$paris_hb" ]] || fail "Paris-day primary heartbeat missing"
[[ ! -e "$TMP/agents/ben/outputs/2026-03-28/heartbeat.log" ]] || fail "UTC-day heartbeat was synthesized"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called at Paris-day boundary"

# Successful Maya wake uses Hermes control, writes only the simulated primary
# heartbeat, and cannot reach the headless binary.
rm -f "$TMP/model-called"
cat >"$TMP/hermes-wake" <<EOF_HERMES
#!/usr/bin/env bash
mkdir -p "$TMP/agents/maya/outputs/\$(date -u +%Y-%m-%d)"
printf 'synthetic Hermes primary tick\n' >"$TMP/agents/maya/outputs/\$(date -u +%Y-%m-%d)/heartbeat.log"
EOF_HERMES
chmod +x "$TMP/hermes-wake"
export BUBBLE_BACKUP_HERMES_PY="$BASH_BIN"
export BUBBLE_BACKUP_HERMES_WAKE_HELPER="$TMP/hermes-wake"
export BUBBLE_BACKUP_HERMES_PROFILE_HOME="$TMP/home/maya"
maya_out="$(run_floor maya "$TMP/maya-ok.jsonl" 2>&1)"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called after successful Maya wake"
[[ "$maya_out" == *"live Hermes gateway ticked"* ]] || fail "Maya success invisible"
grep -Eq '"action"[[:space:]]*:[[:space:]]*"run"' "$TMP/maya-ok.jsonl" || fail "Maya run event missing"

printf '%s\n' "primary wake-only floor tests: PASS"
