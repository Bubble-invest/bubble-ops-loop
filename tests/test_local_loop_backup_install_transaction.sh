#!/usr/bin/env bash
set -uo pipefail
INSTALLER="${1:?installer required}"
RUNNER="${2:?runner required}"
WORK="$(mktemp -d "$HOME/.local-floor-install-test.XXXXXX")"
trap 'find "$WORK" -depth -delete' EXIT
LA="$WORK/LaunchAgents"; LOGS="$WORK/logs"; DEPT="$WORK/dept"; STATE="$WORK/state"
mkdir -p "$LA" "$LOGS" "$DEPT/outputs" "$STATE" "$WORK/bin"; chmod 700 "$STATE"
TMUX="$WORK/tmux"; printf '#!/bin/sh\nexit 0\n' >"$TMUX"; chmod 700 "$TMUX"
SELECTOR="$WORK/harness-fixture"; printf 'claude\n' >"$SELECTOR"
LABEL=com.bubble.ops-loop-backup-fixture
PLIST="$LA/$LABEL.plist"
CALLS="$WORK/launchctl.calls"; LOADED="$WORK/loaded"; : >"$LOADED"
REAL_PLUTIL=/usr/bin/plutil
cat >"$WORK/bin/launchctl" <<'SH'
#!/bin/sh
echo "$*" >>"$CALLS"
case "$1" in
 print) [ -f "$LOADED" ] ;;
 bootout) rm -f "$LOADED"; exit 0 ;;
 bootstrap)
   if [ "${FAIL_BOOTSTRAP_ONCE:-0}" = 1 ] && [ ! -f "$BOOTSTRAP_FAILED" ]; then
     : >"$BOOTSTRAP_FAILED"; exit 1
   fi
   : >"$LOADED"; exit 0 ;;
 *) exit 1 ;;
esac
SH
cat >"$WORK/bin/plutil" <<SH
#!/bin/sh
[ "\${FAIL_LINT:-0}" = 1 ] && exit 1
exec "$REAL_PLUTIL" "\$@"
SH
cat >"$WORK/bin/mktemp" <<'SH'
#!/bin/sh
if [ "${FAIL_RENDER:-0}" = 1 ]; then echo /dev/full; exit 0; fi
exec /usr/bin/mktemp "$@"
SH
chmod 700 "$WORK/bin/"*
export CALLS LOADED BOOTSTRAP_FAILED="$WORK/bootstrap.failed" PATH="$WORK/bin:$PATH"
args=(--dept-dir "$DEPT" --slug fixture --telegram-state-dir "$STATE" --session-name ops-loop-fixture --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --launch-agents-dir "$LA" --log-dir "$LOGS" --runner "$RUNNER")
fail(){ echo "FAIL: $*" >&2; exit 1; }
old='OLD-PLIST-BYTES'
printf '%s' "$old" >"$PLIST"; chmod 600 "$PLIST"

FAIL_RENDER=1 "$INSTALLER" "${args[@]}" >"$WORK/render.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && "$(cat "$PLIST")" = "$old" && -f "$LOADED" ]] || fail "render failure changed prior state"
FAIL_LINT=1 "$INSTALLER" "${args[@]}" >"$WORK/lint.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && "$(cat "$PLIST")" = "$old" && -f "$LOADED" ]] || fail "lint failure changed prior state"

: >"$CALLS"
"$INSTALLER" "${args[@]}" >"$WORK/render-ok.log" 2>&1; rc=$?
[[ "$rc" -eq 0 && "$(cat "$PLIST")" != "$old" && -f "$LOADED" ]] || fail "validated render publish"
! grep -q '^bootout\|^bootstrap' "$CALLS" || fail "render-only changed registration"
/usr/bin/plutil -lint "$PLIST" >/dev/null || fail "published plist invalid"

printf '%s' "$old" >"$PLIST"; : >"$LOADED"; : >"$CALLS"
FAIL_BOOTSTRAP_ONCE=1 "$INSTALLER" "${args[@]}" --activate >"$WORK/load-fail.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && "$(cat "$PLIST")" = "$old" && -f "$LOADED" ]] || fail "load failure did not restore file/registration"

printf '%s' "$old" >"$PLIST"; : >"$LOADED"; : >"$CALLS"; rm -f "$BOOTSTRAP_FAILED"
"$INSTALLER" "${args[@]}" --activate >"$WORK/load-ok.log" 2>&1; rc=$?
[[ "$rc" -eq 0 && "$(cat "$PLIST")" != "$old" && -f "$LOADED" ]] || fail "activation success"
grep -q '^bootout ' "$CALLS" && grep -q '^bootstrap ' "$CALLS" || fail "activation lifecycle missing"

echo "RESULT: 4 transaction cases passed"
