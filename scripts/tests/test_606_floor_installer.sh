#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
mkdir -p "$TMP/agents/alpha/layers" "$TMP/agents/concierge"
printf '{}\n' >"$TMP/agents/alpha/dept.yaml"
for n in 1 2 3 4; do mkdir -p "$TMP/agents/alpha/layers/$n"; : >"$TMP/agents/alpha/layers/$n/PROMPT.md"; done
out="$(BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/systemd" BUBBLE_FLOOR_DEPTS='alpha concierge' BUBBLE_FLOOR_TEST_UIDS=1 "$ROOT/scripts/install-loop-backup.sh" --dry-run --activate)"
[[ "$out" == *"skip concierge: no dept.yaml"* ]] || fail "concierge not skipped"
[[ "$(grep -c 'systemctl enable --now loop-layer' <<<"$out")" -eq 4 ]] || fail "wrong replacement count"
[[ "$(grep -c 'systemctl disable --now loop-layer' <<<"$out")" -eq 4 ]] || fail "wrong legacy retirement count"
[[ "$out" == *"installed 4 per-department layer timers"* ]] || fail "summary wrong"
[[ ! -e "$TMP/systemd" ]] || fail "dry-run wrote systemd dir"

out="$(BUBBLE_FLOOR_AGENTS_ROOT="$TMP/agents" BUBBLE_FLOOR_SYSTEMD_DIR="$TMP/systemd" BUBBLE_FLOOR_DEPTS=alpha BUBBLE_FLOOR_TEST_UIDS=1 "$ROOT/scripts/install-loop-backup.sh" --dry-run)"
[[ "$out" == *"timers unchanged"* ]] || fail "install-only result missing"
[[ "$out" != *"enable --now"* ]] || fail "install-only enabled timer"
[[ "$out" != *"disable --now"* ]] || fail "install-only disabled timer"

echo "isolated floor installer tests: PASS"
find "$TMP" -depth -delete
