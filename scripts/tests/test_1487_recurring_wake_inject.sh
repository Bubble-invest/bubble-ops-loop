#!/usr/bin/env bash
# Board #1487: the floor's idle-nudge inject (loop-backup.sh::inject_live_loop)
# must try the machine-generated DUE_MISSIONS wake-prompt FIRST (the same
# generator #1484 built for Mac, now extended to the VPS/content/accountant
# `recurring_missions` schema) and fall back to the historical free-text
# nudge, UNCHANGED, whenever the generator refuses or errors.
#
# Modeled on scripts/tests/test_606_primary_wake_only.sh's "Successful
# classic primary wakes" fixture (same systemctl/inject/watcher harness),
# narrowed to just the inject CONTENT this card changes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASH_BIN="${BASH_BIN:-$(command -v bash)}"
PY_BIN="${PY_BIN:-$(command -v python3)}"
fail() { echo "FAIL: $*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'find "$TMP" -depth -delete 2>/dev/null || true' EXIT
mkdir -p "$TMP/framework/venv/bin" "$TMP/agents" "$TMP/locks" "$TMP/home" "$TMP/selectors"
# A raw `ln -s $PY_BIN .../python` trips macOS's xcode-select CLT shim in some
# sandboxes ("Failed to locate 'python'") purely because of the symlink's
# argv[0] name — nothing to do with this card. A tiny exec wrapper sidesteps
# it and behaves identically everywhere else (same technique confirmed against
# the pre-existing scripts/tests/test_606_primary_wake_only.sh fixture, which
# hits the identical sandbox artifact with its own `ln -s "$PY_BIN" ... python`).
# It also pins PYTHONPATH to $PY_BIN's OWN pyyaml site dir: loop-backup.sh
# deliberately overrides HOME per dept (isolation), and this sandbox's
# `python3` resolves pyyaml from a HOME-relative user site-packages dir — a
# real venv (production's `${REPO_ROOT}/venv/bin/python`) is not affected by
# HOME, so this only compensates for the fixture using the bare interpreter.
PY_SITE_DIR="$("$PY_BIN" -c 'import yaml, os; print(os.path.dirname(os.path.dirname(yaml.__file__)))' 2>/dev/null || true)"
cat >"$TMP/framework/venv/bin/python" <<EOF_PYWRAP
#!/usr/bin/env bash
export PYTHONPATH="${PY_SITE_DIR}\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY_BIN" "\$@"
EOF_PYWRAP
chmod +x "$TMP/framework/venv/bin/python"
ln -s "$ROOT/scripts" "$TMP/framework/scripts"

cat >"$TMP/systemctl" <<'EOF_SYSTEMCTL'
#!/usr/bin/env bash
case "$1" in
  is-enabled) exit 0 ;;
  show) printf '%s\n' "${STUB_MAINPID:-123}"; exit 0 ;;
  restart) exit 91 ;;
  *) exit 1 ;;
esac
EOF_SYSTEMCTL
chmod +x "$TMP/systemctl"
printf 'CLAUDE_CODE_OAUTH_TOKEN=synthetic\n' >"$TMP/env"

FALLBACK_NEEDLE="Resume your OODA loop (self-paced). Run your full tick now: STEP A (safe_pull)"

run_floor() {
  local slug="$1" log="$2"
  HOME="$TMP/home/$slug" \
  BUBBLE_BACKUP_TEST_UID_OK=1 \
  BUBBLE_BACKUP_TEST_LIVE_POLLER_OK=1 \
  BUBBLE_BACKUP_PRIMARY_WAKE_ONLY=1 \
  BUBBLE_BACKUP_HARNESS_SELECTOR_DIR="$TMP/selectors" \
  BUBBLE_OPS_LOOP_ROOT="$TMP/framework" \
  BUBBLE_BACKUP_SRV_AGENTS_ROOT="$TMP/agents" \
  BUBBLE_BACKUP_AGENTS_ROOT="$TMP/legacy" \
  BUBBLE_BACKUP_LOCK_DIR="$TMP/locks" \
  BUBBLE_BACKUP_LOG="$log" \
  BUBBLE_BACKUP_SHARED_ENV="$TMP/env" \
  BUBBLE_BACKUP_SYSTEMCTL="$TMP/systemctl" \
  BUBBLE_BACKUP_LAYER_OFFSET_H=-24 \
  BUBBLE_BACKUP_WAKE_WAIT_ITERATIONS=1 \
  BUBBLE_BACKUP_WAKE_WAIT_SECONDS=0 \
  BUBBLE_DISPATCH_DIRECTIVES=0 \
  BUBBLE_AUTORESTART=0 \
  "$BASH_BIN" "$ROOT/scripts/loop-backup.sh" --dept "$slug"
}

printf 'claude\n' >"$TMP/selectors/ben"
printf 'claude\n' >"$TMP/selectors/tony"

# ── Case 1: recurring_missions schema with a live-due Layer-1 mission ──────
# Mirrors Ben's real dept.yaml shape (layer as a plain int, no loop: block).
# `every_1h` with no prior .last-run is due unconditionally; Layer 1's own
# floor-time gate (07:00 Paris) still applies, so pin now to a safe daytime
# UTC instant well past it and with no other layer's queues populated (so L1
# is the only eligible branch, C.0: "morning floor not yet run today").
mkdir -p "$TMP/agents/ben/missions/data_update" "$TMP/agents/ben/layers/1"
cat >"$TMP/agents/ben/dept.yaml" <<'EOF_DEPT'
department:
  slug: ben
  display_name: Ben
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: data_update
  layer: 1
  cadence: every_1h
  description: test fixture mission
EOF_DEPT
printf 'test mission prompt\n' >"$TMP/agents/ben/missions/data_update/PROMPT.md"
printf 'layer one\n' >"$TMP/agents/ben/layers/1/PROMPT.md"
mkdir -p "$TMP/home/ben/.claude/channels/telegram-ben"
inject_ben="$TMP/home/ben/.claude/channels/telegram-ben/inject"
: >"$inject_ben"

export STUB_MAINPID=123
export BUBBLE_BACKUP_TEST_NOW_UTC="2026-09-24T09:00:00+00:00"   # 11:00 Paris (CEST)
run_floor ben "$TMP/ben.jsonl" >"$TMP/ben-run.log" 2>&1 || true

[[ -s "$inject_ben" ]] || fail "ben: nothing was injected"
grep -q "DUE_MISSIONS=\[data_update{cadence=every_1h,layer=1,file=" "$inject_ben" \
  || fail "ben: generated DUE_MISSIONS envelope missing/malformed: $(cat "$inject_ben")"
grep -q "COMPLETE data_update =>" "$inject_ben" \
  || fail "ben: COMPLETE line for data_update missing"
grep -q "commit_dispatch" "$inject_ben" \
  || fail "ben: COMPLETE line does not reference commit_dispatch (VPS completion path)"
grep -q "STALENESS:" "$inject_ben" \
  || fail "ben: staleness re-check clause missing"
grep -qF "$FALLBACK_NEEDLE" "$inject_ben" \
  && fail "ben: fallback free text used even though the generator had real due work"
echo "case 1 (recurring_missions, live due mission) PASS"

# ── Case 2: unrecognized/empty schema — must fall back UNCHANGED ───────────
mkdir -p "$TMP/agents/tony/layers/1"
printf '{}\n' >"$TMP/agents/tony/dept.yaml"
printf 'layer one\n' >"$TMP/agents/tony/layers/1/PROMPT.md"
mkdir -p "$TMP/home/tony/.claude/channels/telegram-tony"
inject_tony="$TMP/home/tony/.claude/channels/telegram-tony/inject"
: >"$inject_tony"

run_floor tony "$TMP/tony.jsonl" >"$TMP/tony-run.log" 2>&1 || true

[[ -s "$inject_tony" ]] || fail "tony: nothing was injected"
grep -qF "$FALLBACK_NEEDLE" "$inject_tony" \
  || fail "tony: fallback text missing/changed for an unrecognized manifest: $(cat "$inject_tony")"
grep -q "DUE_MISSIONS=" "$inject_tony" \
  && fail "tony: a DUE_MISSIONS envelope leaked in for a manifest with no due work"
echo "case 2 (unrecognized manifest, fallback unchanged) PASS"

printf '%s\n' "#1487 recurring-missions wake-inject tests: PASS"
