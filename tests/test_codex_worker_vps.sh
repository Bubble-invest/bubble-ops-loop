#!/usr/bin/env bash
# Regression coverage for board #1139 private disposable Codex clones.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
WRAPPER="$REPO_ROOT/scripts/codex-worker-vps.sh"
SWEEP="$REPO_ROOT/deploy/templates/secrets-tmp-sweep.sh"

PASS=0
FAIL=0
ok() { echo "PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

TEST_ROOT="$(mktemp -d)"
cleanup_test() { rm -rf -- "$TEST_ROOT"; }
trap cleanup_test EXIT
chmod 700 "$TEST_ROOT"

SOURCE="$TEST_ROOT/source"
SCRATCH="$TEST_ROOT/scratch"
mkdir -p "$SOURCE" "$SCRATCH"
git -C "$SOURCE" init -q
git -C "$SOURCE" config user.email test@example.invalid
git -C "$SOURCE" config user.name test
printf '%s\n' 'tracked source fixture' > "$SOURCE/secrets_cli.py"
git -C "$SOURCE" add secrets_cli.py
git -C "$SOURCE" commit -qm fixture

PROBE="$TEST_ROOT/probe"
CODEX_STUB="$TEST_ROOT/codex-stub"
cat > "$CODEX_STUB" <<'STUB'
#!/usr/bin/env bash
set -e
printf '%s\n' "$CODEX_WORK_ROOT" > "$PROBE.root"
stat -c '%a' "$CODEX_WORK_ROOT" > "$PROBE.root_mode"
stat -c '%a' "$CODEX_WORK_REPO" > "$PROBE.repo_mode"
stat -c '%a' "$CODEX_WORK_REPO/secrets_cli.py" > "$PROBE.file_mode"
stat -c '%a' "$CODEX_WORKER_TASK_FILE" > "$PROBE.task_mode"
pwd > "$PROBE.pwd"
printf '%s\n' "$@" > "$PROBE.args"
cat > "$PROBE.stdin"
exit "${STUB_RC:-0}"
STUB
chmod 700 "$CODEX_STUB"

TASK_TEXT='bounded private task'
if printf '%s\n' "$TASK_TEXT" | \
  CODEX_WORKER_TMPDIR="$SCRATCH" CODEX_WORKER_CODEX_BIN="$CODEX_STUB" \
  PROBE="$PROBE" "$WRAPPER" --repo "$SOURCE" --sandbox workspace-write; then
  ok "wrapper command succeeds"
else
  bad "wrapper command succeeds"
fi

WORK_ROOT="$(cat "$PROBE.root" 2>/dev/null || true)"
[[ "$(cat "$PROBE.root_mode" 2>/dev/null)" == "700" ]] \
  && ok "scratch root is mode 0700" || bad "scratch root is mode 0700"
[[ "$(cat "$PROBE.repo_mode" 2>/dev/null)" == "700" ]] \
  && ok "clone root is mode 0700" || bad "clone root is mode 0700"

FILE_MODE="$(cat "$PROBE.file_mode" 2>/dev/null || true)"
if [[ "$FILE_MODE" =~ ^[0-7][0-7][0-3]$ ]]; then
  ok "tracked source is not world-readable"
else
  bad "tracked source is not world-readable (mode=${FILE_MODE:-missing})"
fi
[[ "$(cat "$PROBE.task_mode" 2>/dev/null)" == "600" ]] \
  && ok "stdin task brief is mode 0600" || bad "stdin task brief is mode 0600"
[[ "$(cat "$PROBE.stdin" 2>/dev/null)" == "$TASK_TEXT" ]] \
  && ok "stdin task is replayed to Codex" || bad "stdin task is replayed to Codex"
[[ "$(cat "$PROBE.pwd" 2>/dev/null)" == "$WORK_ROOT/repo" ]] \
  && ok "Codex runs from fresh clone" || bad "Codex runs from fresh clone"
[[ -n "$WORK_ROOT" && ! -e "$WORK_ROOT" ]] \
  && ok "successful worker clone is removed" || bad "successful worker clone is removed"

ARGS_FILE="$PROBE.args"
grep -qx 'gpt-5.6-sol' "$ARGS_FILE" \
  && ok "model is pinned to gpt-5.6-sol" || bad "model is pinned to gpt-5.6-sol"
grep -qx 'model_reasoning_effort=high' "$ARGS_FILE" \
  && ok "reasoning is pinned high" || bad "reasoning is pinned high"
grep -qx 'workspace-write' "$ARGS_FILE" \
  && ok "requested sandbox reaches Codex" || bad "requested sandbox reaches Codex"

set +e
printf '%s\n' "$TASK_TEXT" | CODEX_WORKER_TMPDIR="$SCRATCH" \
  CODEX_WORKER_CODEX_BIN="$CODEX_STUB" PROBE="$PROBE-fail" STUB_RC=23 \
  "$WRAPPER" --repo "$SOURCE"
FAIL_RC=$?
set -e
[[ "$FAIL_RC" -eq 23 ]] \
  && ok "Codex failure status is preserved" || bad "Codex failure status is preserved"
if ! find "$SCRATCH" -mindepth 1 -maxdepth 1 -name 'codex-worker.*' | grep -q .; then
  ok "failed worker clone is removed"
else
  bad "failed worker clone is removed"
fi

# The scanner itself remains strict. A real leftover outside the wrapper is
# still found; #1139 must not be fixed by weakening detection.
LEAK_ROOT="$TEST_ROOT/leak-fixture"
LOG_ROOT="$TEST_ROOT/logs"
mkdir -p "$LEAK_ROOT" "$LOG_ROOT"
printf '%s\n' 'fixture only' > "$LEAK_ROOT/secrets-real-leftover"
chmod 644 "$LEAK_ROOT/secrets-real-leftover"
SCAN_DIRS="$LEAK_ROOT" LOG_DIR="$LOG_ROOT" \
  ENV_FILE="$TEST_ROOT/no-env" BUBBLE_OPERATOR_CHAT_ID='' \
  bash "$SWEEP"
SWEEP_LOG="$LOG_ROOT/secrets-tmp-sweep-$(date -u +%Y-%m-%d).log"
if grep -q 'secrets-real-leftover' "$SWEEP_LOG"; then
  ok "real matching leftover is still detected"
else
  bad "real matching leftover is still detected"
fi

echo "$PASS passed, $FAIL failed"
((FAIL == 0))
