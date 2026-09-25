#!/usr/bin/env bash
# Board #1503: bubble-deploy-infra syncs /opt/bubble-ops-loop's code every 15
# minutes but never installed console/requirements.txt into that checkout's
# venv — a merged dependency bump (e.g. #492's `cryptography`) only surfaced
# as a crash on the console's NEXT restart, sometimes hours/days later.
#
# This hermetically tests scripts/bubble-deploy.sh's sync_console_requirements
# step: it installs console/requirements.txt into $SOURCE_INFRA_DIR/venv
# (the venv the console's ExecStart actually runs) using
# `venv/bin/python -m pip install` (never `venv/bin/pip`, whose shebang can
# point at a different, copied venv — the exact gotcha logged on #1503), only
# when the requirements file's hash changed since the last successful
# install, and only records success after a post-install
# `import console.main` smoke test also passes. A fake `venv/bin/python`
# stub stands in for pip/python so this never touches real Python or network.
#
# No real git checkout is needed to exercise this: leaving $SOURCE_INFRA_DIR
# and $CONSOLE_INFRA_DIR without a `.git` directory makes sync_repo_safe_ff
# SKIP the code-sync half cleanly (rc 0), so each case below isolates the
# console-deps step through the real script entry point.
#
# T1 requirements.txt present, no prior state → installs, smoke-tests,
#    records the hash, logs UPDATED, exits 0.
# T2 unchanged requirements.txt on a second run → pip/import are NOT
#    invoked again, logs CURRENT, exits 0.
# T3 requirements.txt content changes → reinstalls, hash file updates.
# T4 pip install fails → logs FAIL, state file left unwritten, script
#    exits 1 (so systemd's OnFailure alarm fires) — and the smoke test
#    (import) is never reached.
# T5 pip install succeeds but the import smoke test fails → logs FAIL,
#    state file left unwritten, script exits 1.
# T6 venv python missing/non-executable → logs FAIL, script exits 1.
# T7 --dry-run with a changed requirements.txt → logs DRY_RUN, pip/import
#    are never invoked, state file is never written.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$ROOT/scripts/bubble-deploy.sh}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SOURCE_DIR="$WORK/opt/bubble-ops-loop"
CONSOLE_DIR="$WORK/home-claude/bubble-ops-loop"
mkdir -p "$SOURCE_DIR/console" "$SOURCE_DIR/venv/bin" "$CONSOLE_DIR" "$WORK/agents" "$WORK/legacy"

PY_CALL_LOG="$WORK/py-calls.log"
cat >"$SOURCE_DIR/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$PY_CALL_LOG"
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
    [[ "${PIP_SHOULD_FAIL:-0}" == "1" ]] && exit 1
    exit 0
fi
if [[ "$1" == "-c" ]]; then
    [[ "${IMPORT_SHOULD_FAIL:-0}" == "1" ]] && exit 1
    exit 0
fi
echo "unexpected python invocation: $*" >&2
exit 99
EOF
chmod +x "$SOURCE_DIR/venv/bin/python"

STATE_FILE="$SOURCE_DIR/venv/.requirements.sha256"

run() {
    : >"$PY_CALL_LOG"
    set +e
    PATH="$WORK/emptybin:$PATH" \
    PY_CALL_LOG="$PY_CALL_LOG" \
    PIP_SHOULD_FAIL="${PIP_SHOULD_FAIL:-0}" \
    IMPORT_SHOULD_FAIL="${IMPORT_SHOULD_FAIL:-0}" \
    BUBBLE_DEPLOY_SOURCE_INFRA_DIR="$SOURCE_DIR" \
    BUBBLE_DEPLOY_CONSOLE_INFRA_DIR="$CONSOLE_DIR" \
    BUBBLE_DEPLOY_AGENTS_ROOT="$WORK/agents" \
    BUBBLE_DEPLOY_LEGACY_AGENTS_ROOT="$WORK/legacy" \
    BUBBLE_DEPLOY_LOCK_FILE="$WORK/deploy.lock" \
        bash "$SCRIPT" --infra-only "$@" >"$WORK/out.log" 2>"$WORK/err.log"
    RC=$?
    set -e
}
mkdir -p "$WORK/emptybin"
# The script (VPS/Linux-only, same as its existing sha256sum-using sibling
# scripts/morty-security-audit.sh) shells out to GNU `sha256sum`. This macOS
# dev sandbox doesn't ship it, so shim it via BSD `shasum -a 256` in the same
# `<hash>  <path>` output format — test portability only, not a script change.
if ! command -v sha256sum >/dev/null 2>&1; then
    cat >"$WORK/emptybin/sha256sum" <<'EOF'
#!/usr/bin/env bash
exec shasum -a 256 "$@"
EOF
    chmod +x "$WORK/emptybin/sha256sum"
fi
export PATH="$WORK/emptybin:$PATH"

fail() { echo "FAIL: $1" >&2; echo "--- out ---"; cat "$WORK/out.log" >&2 || true; echo "--- err ---"; cat "$WORK/err.log" >&2 || true; exit 1; }

echo "T1 first run installs + smoke-tests + records hash"
echo "fastapi==0.115.2" >"$SOURCE_DIR/console/requirements.txt"
run
[[ "$RC" == "0" ]] || fail "T1: expected exit 0, got $RC"
grep -q "UPDATED framework-source-console-deps" "$WORK/out.log" || fail "T1: missing UPDATED log line"
[[ -f "$STATE_FILE" ]] || fail "T1: state file not written"
grep -q -- "-m pip install --quiet -r" "$PY_CALL_LOG" || fail "T1: pip was not invoked"
grep -q -- "-c import console.main" "$PY_CALL_LOG" || fail "T1: smoke test was not invoked"
want_hash="$(sha256sum -- "$SOURCE_DIR/console/requirements.txt" | awk '{print $1}')"
[[ "$(cat "$STATE_FILE")" == "$want_hash" ]] || fail "T1: state file hash mismatch"
echo "  ok"

echo "T2 unchanged requirements.txt: no reinstall"
run
[[ "$RC" == "0" ]] || fail "T2: expected exit 0, got $RC"
grep -q "CURRENT framework-source-console-deps" "$WORK/out.log" || fail "T2: missing CURRENT log line"
[[ -s "$PY_CALL_LOG" ]] && fail "T2: pip/python was invoked on an unchanged requirements.txt"
echo "  ok"

echo "T3 changed requirements.txt: reinstalls, hash updates"
prev_hash="$(cat "$STATE_FILE")"
echo "fastapi==0.115.3" >"$SOURCE_DIR/console/requirements.txt"
run
[[ "$RC" == "0" ]] || fail "T3: expected exit 0, got $RC"
grep -q "UPDATED framework-source-console-deps" "$WORK/out.log" || fail "T3: missing UPDATED log line"
new_hash="$(cat "$STATE_FILE")"
[[ "$new_hash" != "$prev_hash" ]] || fail "T3: state file hash did not change"
echo "  ok"

echo "T4 pip install fails: FAIL logged, state file untouched, exit 1"
before_hash="$(cat "$STATE_FILE")"
echo "fastapi==0.115.4" >"$SOURCE_DIR/console/requirements.txt"
PIP_SHOULD_FAIL=1 run
[[ "$RC" == "1" ]] || fail "T4: expected exit 1, got $RC"
grep -q "FAIL framework-source-console-deps: pip install" "$WORK/out.log" || fail "T4: missing pip FAIL log line"
! grep -q -- "-c import console.main" "$PY_CALL_LOG" || fail "T4: smoke test was invoked despite pip failure"
[[ "$(cat "$STATE_FILE")" == "$before_hash" ]] || fail "T4: state file changed despite pip failure"
echo "  ok"

echo "T5 pip succeeds but import smoke test fails: FAIL logged, state file untouched, exit 1"
before_hash="$(cat "$STATE_FILE")"
IMPORT_SHOULD_FAIL=1 run
[[ "$RC" == "1" ]] || fail "T5: expected exit 1, got $RC"
grep -q "FAIL framework-source-console-deps: post-install smoke test failed" "$WORK/out.log" || fail "T5: missing smoke-test FAIL log line"
[[ "$(cat "$STATE_FILE")" == "$before_hash" ]] || fail "T5: state file changed despite smoke-test failure"
echo "  ok"

echo "T6 missing venv python: FAIL logged, exit 1"
mv "$SOURCE_DIR/venv/bin/python" "$SOURCE_DIR/venv/bin/python.bak"
run
[[ "$RC" == "1" ]] || fail "T6: expected exit 1, got $RC"
grep -q "FAIL framework-source-console-deps: venv python not found" "$WORK/out.log" || fail "T6: missing venv-python FAIL log line"
mv "$SOURCE_DIR/venv/bin/python.bak" "$SOURCE_DIR/venv/bin/python"
echo "  ok"

echo "T7 --dry-run never installs, never writes state"
before_hash="$(cat "$STATE_FILE")"
echo "fastapi==0.115.5" >"$SOURCE_DIR/console/requirements.txt"
run --dry-run
[[ "$RC" == "0" ]] || fail "T7: expected exit 0, got $RC"
grep -q "DRY_RUN framework-source-console-deps" "$WORK/out.log" || fail "T7: missing DRY_RUN log line"
[[ -s "$PY_CALL_LOG" ]] && fail "T7: pip/python was invoked during --dry-run"
[[ "$(cat "$STATE_FILE")" == "$before_hash" ]] || fail "T7: state file changed during --dry-run"
echo "  ok"

echo "ALL PASS"
