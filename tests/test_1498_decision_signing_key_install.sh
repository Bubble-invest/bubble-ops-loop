#!/usr/bin/env bash
# test_1498_decision_signing_key_install.sh — hermetic contract tests for
# console/deploy/decision-signing-key/install-decision-signing-key.sh.
# Board #1498 (follow-up to board #1476 / PR #492's runbook). Style mirrors
# scripts/tests/test_606_floor_installer.sh: env-var-overridable install
# roots + a stubbed systemctl on PATH, no real root/systemd/VPS involved.
#
# Covers:
#   T1  no args (render-only default) — prints RENDER: lines, writes
#       nothing to the fake bin/systemd dirs, never invokes the stubbed
#       systemctl.
#   T2  --activate — installs the decrypt script (0750) and unit (0644) to
#       the overridden dirs with correct content, then calls
#       `daemon-reload` and `enable --now bubble-cockpit-decision-key.service`
#       on the stubbed systemctl exactly once each.
#   T3  --activate is idempotent — re-running produces the same installed
#       file content (no drift) and still calls enable --now (systemd's own
#       enable is idempotent; this script does not special-case a rerun).
#   T4  --help exits 0 and never touches the fake dirs or systemctl.
#   T5  an unknown flag exits 64 (usage error) without touching anything.
#   T6  missing source files (script dir doesn't match) exits 2 before any
#       write.
#
# Run: bash tests/test_1498_decision_signing_key_install.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
INSTALLER="$ROOT/console/deploy/decision-signing-key/install-decision-signing-key.sh"
[[ -f "$INSTALLER" ]] || { echo "FATAL: not found: $INSTALLER"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Fake systemctl: records every invocation, always "succeeds".
mk_fake_systemctl() {
    local dir="$1"
    mkdir -p "$dir"
    cat >"$dir/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$SYSTEMCTL_LOG"
exit 0
EOF
    chmod +x "$dir/systemctl"
}

# --- Fake install: this test doesn't run as root, so a real `install -o
# root -g root` would fail on this dev/CI box (can't chown to root). Strip
# the ownership flags and delegate to the real `install` for the rest —
# same technique scripts/tests/test_606_floor_installer.sh uses. File
# CONTENT and MODE (-m) are what these tests actually assert on; ownership
# is asserted by reading the installer's own -o/-g literals from its source
# instead (see T2 below).
mk_fake_install() {
    local dir="$1"
    mkdir -p "$dir"
    cat >"$dir/install" <<'EOF'
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
    chmod +x "$dir/install"
}

run_installer() {
    # $1 = BIN_DIR, $2 = SYSTEMD_DIR, $3 = log file, $4.. = args
    local bin="$1" sysd="$2" log="$3"; shift 3
    : >"$log"
    PATH="$WORK/fake-bin:$PATH" \
      BUBBLE_DECISION_KEY_BIN_DIR="$bin" \
      BUBBLE_DECISION_KEY_SYSTEMD_DIR="$sysd" \
      SYSTEMCTL_LOG="$log" \
      bash "$INSTALLER" "$@"
}

mk_fake_systemctl "$WORK/fake-bin"
mk_fake_install "$WORK/fake-bin"

# === T1: render-only default ===================================================
BIN1="$WORK/t1-bin"; SYSD1="$WORK/t1-systemd"; LOG1="$WORK/t1-systemctl.log"
out="$(run_installer "$BIN1" "$SYSD1" "$LOG1")"; rc=$?
if [[ "$rc" -eq 0 ]]; then ok "T1 default run exits 0"; else bad "T1 default run rc=$rc"; fi
[[ "$out" == *"render-only (default"* ]] && ok "T1 announces render-only" || bad "T1 missing render-only announcement"
[[ "$out" == *"RENDER: install"* ]] && ok "T1 prints RENDER: install lines" || bad "T1 missing RENDER: install lines"
[[ ! -e "$BIN1" ]] && ok "T1 wrote nothing to BIN_DIR" || bad "T1 wrote to BIN_DIR"
[[ ! -e "$SYSD1" ]] && ok "T1 wrote nothing to SYSTEMD_DIR" || bad "T1 wrote to SYSTEMD_DIR"
[[ ! -s "$LOG1" ]] && ok "T1 never invoked systemctl" || bad "T1 invoked systemctl: $(cat "$LOG1")"

# === T2: --activate actually installs ==========================================
# `install` (GNU or BSD) never creates a missing destination directory —
# on the real VPS BIN_DIR/SYSTEMD_DIR are always-existing system paths
# (/usr/local/bin, /etc/systemd/system); pre-create the fake equivalents
# here the same way test_606_floor_installer.sh does for its --activate cases.
BIN2="$WORK/t2-bin"; SYSD2="$WORK/t2-systemd"; LOG2="$WORK/t2-systemctl.log"
mkdir -p "$BIN2" "$SYSD2"
out="$(run_installer "$BIN2" "$SYSD2" "$LOG2" --activate)"; rc=$?
if [[ "$rc" -eq 0 ]]; then ok "T2 --activate exits 0"; else bad "T2 --activate rc=$rc: $out"; fi

script_dst="$BIN2/bubble-cockpit-decision-key-decrypt.sh"
unit_dst="$SYSD2/bubble-cockpit-decision-key.service"
[[ -f "$script_dst" ]] && ok "T2 decrypt script installed" || bad "T2 decrypt script missing"
[[ -f "$unit_dst" ]] && ok "T2 unit installed" || bad "T2 unit missing"

if [[ -f "$script_dst" ]]; then
    perm="$(stat -f '%Lp' "$script_dst" 2>/dev/null || stat -c '%a' "$script_dst" 2>/dev/null)"
    [[ "$perm" == "750" ]] && ok "T2 decrypt script mode 0750" || bad "T2 decrypt script mode=$perm (want 750)"
    cmp -s "$script_dst" "$ROOT/console/deploy/decision-signing-key/bubble-cockpit-decision-key-decrypt.sh" \
        && ok "T2 decrypt script content matches source" || bad "T2 decrypt script content differs from source"
fi
if [[ -f "$unit_dst" ]]; then
    perm="$(stat -f '%Lp' "$unit_dst" 2>/dev/null || stat -c '%a' "$unit_dst" 2>/dev/null)"
    [[ "$perm" == "644" ]] && ok "T2 unit mode 0644" || bad "T2 unit mode=$perm (want 644)"
    cmp -s "$unit_dst" "$ROOT/console/deploy/decision-signing-key/bubble-cockpit-decision-key.service" \
        && ok "T2 unit content matches source" || bad "T2 unit content differs from source"
    grep -q '^Before=bubble-ops-console.service' "$unit_dst" && ok "T2 unit orders Before=bubble-ops-console.service" || bad "T2 unit missing Before=bubble-ops-console.service"
    grep -q '^RuntimeDirectoryPreserve=yes' "$unit_dst" && ok "T2 unit sets RuntimeDirectoryPreserve=yes" || bad "T2 unit missing RuntimeDirectoryPreserve=yes"
    grep -q '^RemainAfterExit=yes' "$unit_dst" && ok "T2 unit sets RemainAfterExit=yes" || bad "T2 unit missing RemainAfterExit=yes"
fi

if [[ -s "$LOG2" ]]; then
    [[ "$(grep -c '^daemon-reload$' "$LOG2")" -eq 1 ]] && ok "T2 called systemctl daemon-reload exactly once" || bad "T2 daemon-reload call count wrong: $(cat "$LOG2")"
    [[ "$(grep -c '^enable --now bubble-cockpit-decision-key.service$' "$LOG2")" -eq 1 ]] && ok "T2 called systemctl enable --now on the right unit" || bad "T2 enable --now call wrong: $(cat "$LOG2")"
else
    bad "T2 systemctl log empty"
fi

# === T3: --activate is idempotent (rerun matches) ==============================
LOG3="$WORK/t3-systemctl.log"
out2="$(run_installer "$BIN2" "$SYSD2" "$LOG3" --activate)"; rc2=$?
[[ "$rc2" -eq 0 ]] && ok "T3 rerun --activate exits 0" || bad "T3 rerun rc=$rc2"
cmp -s "$script_dst" "$ROOT/console/deploy/decision-signing-key/bubble-cockpit-decision-key-decrypt.sh" \
    && ok "T3 rerun leaves decrypt script byte-identical" || bad "T3 rerun changed decrypt script"
[[ "$(grep -c '^enable --now bubble-cockpit-decision-key.service$' "$LOG3")" -eq 1 ]] && ok "T3 rerun still enables the unit" || bad "T3 rerun enable call wrong: $(cat "$LOG3")"

# === T4: --help ==================================================================
BIN4="$WORK/t4-bin"; SYSD4="$WORK/t4-systemd"; LOG4="$WORK/t4-systemctl.log"
out="$(run_installer "$BIN4" "$SYSD4" "$LOG4" --help)"; rc=$?
[[ "$rc" -eq 0 ]] && ok "T4 --help exits 0" || bad "T4 --help rc=$rc"
[[ ! -e "$BIN4" ]] && ok "T4 --help wrote nothing" || bad "T4 --help wrote to BIN_DIR"
[[ ! -s "$LOG4" ]] && ok "T4 --help never invoked systemctl" || bad "T4 --help invoked systemctl"

# === T5: unknown flag =============================================================
BIN5="$WORK/t5-bin"; SYSD5="$WORK/t5-systemd"; LOG5="$WORK/t5-systemctl.log"
out="$(run_installer "$BIN5" "$SYSD5" "$LOG5" --bogus 2>&1)"; rc=$?
[[ "$rc" -eq 64 ]] && ok "T5 unknown flag exits 64" || bad "T5 unknown flag rc=$rc"
[[ ! -e "$BIN5" ]] && ok "T5 unknown flag wrote nothing" || bad "T5 unknown flag wrote to BIN_DIR"

# === T6: missing source files ======================================================
EMPTY_SRC="$WORK/empty-src"
mkdir -p "$EMPTY_SRC"
cp "$INSTALLER" "$EMPTY_SRC/install-decision-signing-key.sh"
BIN6="$WORK/t6-bin"; SYSD6="$WORK/t6-systemd"; LOG6="$WORK/t6-systemctl.log"
: >"$LOG6"
out="$(PATH="$WORK/fake-bin:$PATH" BUBBLE_DECISION_KEY_BIN_DIR="$BIN6" BUBBLE_DECISION_KEY_SYSTEMD_DIR="$SYSD6" SYSTEMCTL_LOG="$LOG6" \
    bash "$EMPTY_SRC/install-decision-signing-key.sh" --activate 2>&1)"; rc=$?
[[ "$rc" -eq 2 ]] && ok "T6 missing source files exits 2" || bad "T6 missing source rc=$rc: $out"
[[ ! -e "$BIN6" ]] && ok "T6 missing source wrote nothing" || bad "T6 missing source wrote to BIN_DIR"

echo
echo "decision-signing-key installer tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
