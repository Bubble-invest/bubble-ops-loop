#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
mkdir -p "$TMP/agents/alpha/layers" "$TMP/agents/concierge"
printf '{}\n' >"$TMP/agents/alpha/dept.yaml"
for n in 1 2 3 4; do mkdir -p "$TMP/agents/alpha/layers/$n"; : >"$TMP/agents/alpha/layers/$n/PROMPT.md"; done
cat >"$TMP/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  list-units) printf 'bubble-agent@alpha.service loaded active running\nbubble-agent@concierge.service loaded active running\n' ;;
  is-enabled) exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$TMP/systemctl"
out="$(BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/systemd" BUBBLE_FLOOR_SYSTEMCTL="$TMP/systemctl" BUBBLE_FLOOR_TEST_UIDS=1 "$ROOT/scripts/install-loop-backup.sh" --dry-run --activate)"
[[ "$out" == *"skip concierge: no dept.yaml"* ]] || fail "concierge not skipped"
[[ "$(grep -c 'enable --now loop-layer' <<<"$out")" -eq 4 ]] || fail "wrong replacement count"
[[ "$(grep -c 'disable --now loop-layer' <<<"$out")" -eq 4 ]] || fail "wrong legacy retirement count"
[[ "$out" == *"installed 4 per-department layer timers"* ]] || fail "summary wrong"
[[ ! -e "$TMP/systemd" ]] || fail "dry-run wrote systemd dir"

out="$(BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/systemd" BUBBLE_FLOOR_DEPTS=alpha BUBBLE_FLOOR_TEST_UIDS=1 "$ROOT/scripts/install-loop-backup.sh" --dry-run)"
[[ "$out" == *"timers unchanged"* ]] || fail "install-only result missing"
[[ "$out" != *"enable --now"* ]] || fail "install-only enabled timer"
[[ "$out" != *"disable --now"* ]] || fail "install-only disabled timer"

# A scoped activation must fail before installing/disabling anything: global
# timers still cover omitted departments, so retiring them for alpha alone
# would silently drop the rest of the fleet.
set +e
out="$(BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/scoped-systemd" BUBBLE_FLOOR_TEST_UIDS=1 "$ROOT/scripts/install-loop-backup.sh" --dry-run --activate --dept alpha 2>&1)"
rc=$?
set -e
[[ "$rc" -eq 64 ]] || fail "scoped activation rc=$rc: $out"
[[ "$out" == *"scoped activation would drop floor coverage"* ]] || fail "scoped refusal missing"
[[ ! -e "$TMP/scoped-systemd" ]] || fail "scoped activation mutated templates"

# A global disable may fail after systemd has already changed that timer's
# state. The installer must restore all four exact enabled/active snapshots.
STATE="$TMP/timer-state"
mkdir -p "$STATE" "$TMP/fake-bin" "$TMP/rollback-systemd"
for n in 1 2 3 4; do printf enabled >"$STATE/loop-layer$n.timer.enabled"; printf active >"$STATE/loop-layer$n.timer.active"; done
cat >"$TMP/fake-bin/sudo" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == -- ]] && shift
exec "$@"
EOF
cat >"$TMP/fake-bin/systemd-analyze" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$TMP/fake-bin/install" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
EOF
cat >"$TMP/stateful-systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cmd="$1"; shift
case "$cmd" in
  list-units) printf 'bubble-agent@alpha.service loaded active running\n' ;;
  is-enabled)
    [[ "$1" == bubble-agent@*.service ]] && { printf enabled; exit 0; }
    cat "$STATE_DIR/$1.enabled" 2>/dev/null || { printf disabled; exit 1; }
    ;;
  is-active) cat "$STATE_DIR/$1.active" 2>/dev/null || { printf inactive; exit 1; } ;;
  daemon-reload) ;;
  disable)
    [[ "${1:-}" == --now ]] && shift
    timer="$1"; printf disabled >"$STATE_DIR/$timer.enabled"; printf inactive >"$STATE_DIR/$timer.active"
    [[ "$timer" == "${FAIL_DISABLE:-}" ]] && exit 42
    ;;
  enable)
    now=0; runtime=0
    while [[ "${1:-}" == --* ]]; do
      [[ "$1" == --now ]] && now=1
      [[ "$1" == --runtime ]] && runtime=1
      shift
    done
    timer="$1"; [[ "$runtime" == 1 ]] && printf enabled-runtime >"$STATE_DIR/$timer.enabled" || printf enabled >"$STATE_DIR/$timer.enabled"
    [[ "$now" == 1 ]] && printf active >"$STATE_DIR/$timer.active"
    [[ "$timer" == "${FAIL_ENABLE:-}" ]] && exit 43
    ;;
  mask)
    runtime=0; [[ "${1:-}" == --runtime ]] && { runtime=1; shift; }
    [[ "$runtime" == 1 ]] && printf masked-runtime >"$STATE_DIR/$1.enabled" || printf masked >"$STATE_DIR/$1.enabled"
    ;;
  unmask) printf disabled >"$STATE_DIR/$1.enabled" ;;
  start) printf active >"$STATE_DIR/$1.active" ;;
  stop) printf inactive >"$STATE_DIR/$1.active" ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$TMP/fake-bin/sudo" "$TMP/fake-bin/systemd-analyze" "$TMP/fake-bin/install" "$TMP/stateful-systemctl"
set +e
out="$(PATH="$TMP/fake-bin:$PATH" STATE_DIR="$STATE" FAIL_DISABLE=loop-layer2.timer \
  BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/rollback-systemd" \
  BUBBLE_FLOOR_SYSTEMCTL="$TMP/stateful-systemctl" BUBBLE_FLOOR_TEST_UIDS=1 \
  "$ROOT/scripts/install-loop-backup.sh" --activate 2>&1)"
rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail "partial old disable returned green"
[[ "$out" == *"restoring exact prior timer states"* ]] || fail "rollback message missing"
for n in 1 2 3 4; do
  [[ "$(cat "$STATE/loop-layer$n.timer.enabled")" == enabled ]] || fail "global L$n not re-enabled"
  [[ "$(cat "$STATE/loop-layer$n.timer.active")" == active ]] || fail "global L$n not restarted"
done

# A replacement enable may likewise mutate before returning failure. Restore
# its target, replacements enabled before this rerun, and all global timers to
# their exact prior states (including runtime-only vs persistent enablement).
for n in 1 2 3 4; do
  printf enabled >"$STATE/loop-layer$n.timer.enabled"
  printf active >"$STATE/loop-layer$n.timer.active"
  printf disabled >"$STATE/loop-layer$n@alpha.timer.enabled"
  printf inactive >"$STATE/loop-layer$n@alpha.timer.active"
done
printf enabled-runtime >"$STATE/loop-layer1@alpha.timer.enabled"
printf active >"$STATE/loop-layer1@alpha.timer.active"
set +e
out="$(PATH="$TMP/fake-bin:$PATH" STATE_DIR="$STATE" FAIL_ENABLE=loop-layer3@alpha.timer \
  BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/rollback-systemd" \
  BUBBLE_FLOOR_SYSTEMCTL="$TMP/stateful-systemctl" BUBBLE_FLOOR_TEST_UIDS=1 \
  "$ROOT/scripts/install-loop-backup.sh" --activate 2>&1)"
rc=$?
set -e
[[ "$rc" -ne 0 ]] || fail "replacement mutate-then-fail returned green"
[[ "$out" == *"restoring exact prior timer states"* ]] || fail "replacement rollback message missing"
for n in 1 2 3 4; do
  [[ "$(cat "$STATE/loop-layer$n.timer.enabled")" == enabled ]] || fail "global L$n enablement changed"
  [[ "$(cat "$STATE/loop-layer$n.timer.active")" == active ]] || fail "global L$n activity changed"
done
[[ "$(cat "$STATE/loop-layer1@alpha.timer.enabled")" == enabled-runtime ]] || fail "runtime enablement widened"
[[ "$(cat "$STATE/loop-layer1@alpha.timer.active")" == active ]] || fail "pre-existing replacement stopped"
for n in 2 3 4; do
  [[ "$(cat "$STATE/loop-layer$n@alpha.timer.enabled")" == disabled ]] || fail "replacement L$n left enabled"
  [[ "$(cat "$STATE/loop-layer$n@alpha.timer.active")" == inactive ]] || fail "replacement L$n left active"
done

echo "isolated floor installer tests: PASS"
find "$TMP" -depth -delete
