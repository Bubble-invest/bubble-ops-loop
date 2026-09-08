#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASH_BIN="${BASH_BIN:-/opt/homebrew/bin/bash}"
[[ -x "$BASH_BIN" ]] || BASH_BIN="$(command -v bash)"
fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
mkdir -p "$TMP/framework/venv/bin" "$TMP/agents/demo/layers/1" "$TMP/agents/demo/onboarding" "$TMP/locks" "$TMP/bin"
PY_BIN="${PY_BIN:-/opt/homebrew/bin/python3.12}"
[[ -x "$PY_BIN" ]] || PY_BIN="$(command -v python3)"
ln -s "$PY_BIN" "$TMP/framework/venv/bin/python"
ln -s "$ROOT/scripts" "$TMP/framework/scripts"
printf 'host: vps\n' >"$TMP/agents/demo/onboarding/STATE.yaml"
printf 'layer one\n' >"$TMP/agents/demo/layers/1/PROMPT.md"
printf '{}\n' >"$TMP/agents/demo/dept.yaml"

cat >"$TMP/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  is-enabled) exit 0 ;;
  show) printf '0\n'; exit 0 ;;
  restart) echo restart-forbidden >&2; exit 91 ;;
  *) exit 1 ;;
esac
EOF
cat >"$TMP/fake-model" <<EOF
#!/usr/bin/env bash
[[ "\${CLAUDE_CODE_OAUTH_TOKEN:-}" == 'literal-\$6-\$(touch never-executed)' ]] || exit 92
printf 'called\n' >"$TMP/model-called"
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"synthetic floor ok"}'
EOF
cat >"$TMP/notify" <<EOF
#!/usr/bin/env bash
printf 'notified\n' >>"$TMP/notified"
EOF
chmod +x "$TMP/systemctl" "$TMP/fake-model" "$TMP/notify"
cat >"$TMP/untrusted.env" <<EOF
CLAUDE_CODE_OAUTH_TOKEN=literal-\$6-\$(touch "$TMP/ENV_EXECUTED")
EOF

set +e
out="$(
  HOME="$TMP/home" \
  CLAUDE_CODE_OAUTH_TOKEN='literal-$6-$(touch never-executed)' \
  BUBBLE_BACKUP_TEST_UID_OK=1 \
  BUBBLE_OPS_LOOP_ROOT="$TMP/framework" \
  BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" \
  BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" \
  BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" \
  BUBBLE_BACKUP_LOG="$TMP/agents/demo/state/loop-backup.jsonl" \
  BUBBLE_BACKUP_SHARED_ENV="$TMP/untrusted.env" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" \
  BUBBLE_BACKUP_CLAUDE_BIN="$TMP/fake-model" \
  BUBBLE_BACKUP_NOTIFY_CMD="$TMP/notify" \
  BUBBLE_BACKUP_BRIEF_NOTIFY_CMD="$TMP/notify" \
  BUBBLE_BACKUP_LAYER_OFFSET_H=-24 \
  BUBBLE_DISPATCH_DIRECTIVES=remote \
  BUBBLE_AUTORESTART=0 \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept demo 2>&1
)"
rc=$?
set -e
[[ "$rc" -eq 0 ]] || fail "fixture rc=$rc: $out"
[[ -f "$TMP/model-called" ]] || fail "fake model not called"
[[ ! -e "$TMP/ENV_EXECUTED" ]] || fail "dotenv content executed"
[[ "$out" == *"dispatch: delegated to Tony"* ]] || fail "relay delegation invisible"
[[ "$out" != *'literal-$6'* ]] || fail "literal env leaked"
[[ "$out" != *'never-executed'* ]] || fail "literal command text leaked"
[[ -s "$TMP/agents/demo/state/loop-backup.jsonl" ]] || fail "event state missing"
[[ -s "$TMP/agents/demo/outputs/$(date -u +%Y-%m-%d)/heartbeat.log" ]] || fail "heartbeat missing"

# A Hermes harness wakes its existing gateway through the supported /loop
# control state and can never fall through to the fake headless model.
python3 - "$TMP/model-called" "$TMP/agents/demo/outputs" <<'PY'
from pathlib import Path
import shutil, sys
Path(sys.argv[1]).unlink()
shutil.rmtree(sys.argv[2], ignore_errors=True)
PY
cat >"$TMP/hermes-wake" <<EOF
#!/usr/bin/env bash
mkdir -p "$TMP/agents/demo/outputs/\$(date -u +%Y-%m-%d)"
touch "$TMP/agents/demo/outputs/\$(date -u +%Y-%m-%d)/heartbeat.log"
printf 'hermes-wake-called\n' >"$TMP/hermes-wake-called"
EOF
chmod +x "$TMP/hermes-wake"
out="$(
  HOME="$TMP/home" CLAUDE_CODE_OAUTH_TOKEN=synthetic BUBBLE_BACKUP_TEST_UID_OK=1 \
  BUBBLE_OPS_LOOP_ROOT="$TMP/framework" BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" \
  BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" \
  BUBBLE_BACKUP_LOG="$TMP/agents/demo/state/inject-only.jsonl" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" BUBBLE_BACKUP_CLAUDE_BIN="$TMP/fake-model" \
  BUBBLE_BACKUP_HERMES_PY="$BASH_BIN" BUBBLE_BACKUP_HERMES_WAKE_HELPER="$TMP/hermes-wake" \
  BUBBLE_BACKUP_HERMES_PROFILE_HOME="$TMP/home" BUBBLE_BACKUP_WAKE_WAIT_ITERATIONS=1 \
  BUBBLE_BACKUP_WAKE_WAIT_SECONDS=0 \
  BUBBLE_BACKUP_LAYER_OFFSET_H=-24 BUBBLE_BACKUP_INJECT_ONLY_DEPTS=demo \
  BUBBLE_DISPATCH_DIRECTIVES=remote BUBBLE_AUTORESTART=0 \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept demo 2>&1
)"
rc=$?
[[ "$rc" -eq 0 ]] || fail "Hermes gateway wake failed: $out"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called for inject-only harness"
[[ -e "$TMP/hermes-wake-called" ]] || fail "Hermes wake helper not called"
[[ "$out" == *"live Hermes gateway ticked"* ]] || fail "Hermes wake result invisible"

# A failed Hermes control wake is explicit and still never falls through.
python3 - "$TMP/agents/demo/outputs" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
cat >"$TMP/hermes-wake" <<'EOF'
#!/usr/bin/env bash
exit 71
EOF
set +e
out="$(
  HOME="$TMP/home" CLAUDE_CODE_OAUTH_TOKEN=synthetic BUBBLE_BACKUP_TEST_UID_OK=1 \
  BUBBLE_OPS_LOOP_ROOT="$TMP/framework" BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" \
  BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" \
  BUBBLE_BACKUP_LOG="$TMP/agents/demo/state/hermes-failed.jsonl" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" BUBBLE_BACKUP_CLAUDE_BIN="$TMP/fake-model" \
  BUBBLE_BACKUP_HERMES_PY="$BASH_BIN" BUBBLE_BACKUP_HERMES_WAKE_HELPER="$TMP/hermes-wake" \
  BUBBLE_BACKUP_HERMES_PROFILE_HOME="$TMP/home" BUBBLE_BACKUP_LAYER_OFFSET_H=-24 \
  BUBBLE_BACKUP_INJECT_ONLY_DEPTS=demo BUBBLE_DISPATCH_DIRECTIVES=remote BUBBLE_AUTORESTART=0 \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept demo 2>&1
)"
rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail "failed Hermes wake returned green"
[[ ! -e "$TMP/model-called" ]] || fail "headless model called after failed Hermes wake"
[[ "$out" == *"Hermes gateway wake unavailable"* ]] || fail "Hermes failure invisible"

# UID mismatch fails before any model execution.
python3 - "$TMP/model-called" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
if p.exists(): p.unlink()
PY
set +e
out="$(BUBBLE_OPS_LOOP_ROOT="$TMP/framework" "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept demo 2>&1)"
rc=$?
set -e
[[ "$rc" -eq 77 ]] || fail "UID mismatch rc=$rc"
[[ ! -e "$TMP/model-called" ]] || fail "model called on UID mismatch"
[[ "$out" == *"must run as agent-demo"* ]] || fail "UID error missing"

# Even under the test UID seam, missing ambient auth fails before fake model.
python3 - "$TMP/agents/demo/outputs" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
set +e
out="$(
  env -u CLAUDE_CODE_OAUTH_TOKEN \
  HOME="$TMP/home" BUBBLE_BACKUP_TEST_UID_OK=1 BUBBLE_OPS_LOOP_ROOT="$TMP/framework" \
  BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" \
  BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" BUBBLE_BACKUP_LOG="$TMP/agents/demo/state/missing-auth.jsonl" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" BUBBLE_BACKUP_CLAUDE_BIN="$TMP/fake-model" \
  BUBBLE_BACKUP_LAYER_OFFSET_H=-24 BUBBLE_DISPATCH_DIRECTIVES=0 BUBBLE_AUTORESTART=0 \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --layer 1 --dept demo 2>&1
)"
rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail "missing auth returned green"
[[ ! -e "$TMP/model-called" ]] || fail "model called without auth"
[[ "$out" == *"per-dept auth environment missing; model was not invoked"* ]] || fail "missing auth failure invisible: $out"

echo "isolated floor runner tests: PASS"
find "$TMP" -depth -delete
