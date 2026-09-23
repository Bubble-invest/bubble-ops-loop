#!/usr/bin/env bash
# test_1463_console_agents_acl.sh — board #1463 follow-up: scripts/deploy-console-to-vps.sh
# must apply the scoped `setfacl -R -m g:bubble-console:rwX` (+ recursive default ACL)
# grant on the dept-state dir, idempotently, before any restart — reproducing the live
# fix applied by hand during the #489 rollout (group membership alone 500'd on
# 0700/0755 dept dirs).
#
# Hermetic: stubs ssh/sudo/setfacl/systemctl/hostname on PATH, forces the "on the box"
# local-exec branch (no real ssh), and never touches the real /etc/systemd/system or
# /home/claude/agents — CONSOLE_AGENTS_DIR points the ACL step at a tmp dir instead.
#
# Run: bash tests/test_1463_console_agents_acl.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/deploy-console-to-vps.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

BIN="$WORK/bin"
mkdir -p "$BIN"

# --- fixture: a fake bubble-ops-loop clone with the checked-in template ---
WORKDIR="$WORK/opt-bubble-ops-loop"
mkdir -p "$WORKDIR/console/deploy"
cat > "$WORKDIR/console/deploy/bubble-ops-console.service.template" <<'UNIT'
[Unit]
Description=fixture unit for test_1463_console_agents_acl
[Service]
User=bubble-console
UNIT

AGENTS_DIR="$WORK/home-claude-agents"
mkdir -p "$AGENTS_DIR/ben"

SETFACL_LOG="$WORK/setfacl.log"
CALL_LOG="$WORK/calls.log"
: > "$SETFACL_LOG"
: > "$CALL_LOG"

# --- stub: hostname — force the ON_MORTY local-exec branch ---
cat > "$BIN/hostname" <<'EOF'
#!/usr/bin/env bash
echo "test-morty-fixture"
EOF

# --- stub: sudo -n <cmd> ... — dispatch by subcommand, log, never touch real fs ---
FAKE_UNIT_STORE="$WORK/fake-installed-unit"
cat > "$BIN/sudo" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "-n" ]]; then shift; fi
cmd="\$1"; shift
echo "sudo \$cmd \$*" >> "$CALL_LOG"
case "\$cmd" in
  cat)
    [[ -f "$FAKE_UNIT_STORE" ]] && cat "$FAKE_UNIT_STORE" || exit 1
    ;;
  install)
    # args: -o root -g root -m 0644 SRC DEST — SRC is second-to-last.
    args=("\$@")
    src="\${args[\${#args[@]}-2]}"
    cp "\$src" "$FAKE_UNIT_STORE"
    ;;
  systemctl)
    exit 0
    ;;
  setfacl)
    echo "setfacl \$*" >> "$SETFACL_LOG"
    exit "\${TEST_SETFACL_EXIT:-0}"
    ;;
  *)
    exit 98
    ;;
esac
EOF

# --- stub: systemctl (called WITHOUT sudo for the final is-active check) ---
cat > "$BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "systemctl \$*" >> "$CALL_LOG"
case "\$1" in
  is-active) echo active ;;
  *) exit 0 ;;
esac
EOF

chmod +x "$BIN"/*

run_script() {
  PATH="$BIN:$PATH" \
  BUBBLE_VPS_HOST=test-morty-fixture \
  SERVICE="bubble-ops-console-test-1463-$$" \
  CONSOLE_WORKDIR="$WORKDIR" \
  CONSOLE_AGENTS_DIR="$AGENTS_DIR" \
  bash "$SCRIPT" "$@"
}

# === Case 1: fresh box, unit differs — ACL must be applied before the restart ===
: > "$SETFACL_LOG"; : > "$CALL_LOG"
out1=$(run_script 2>&1) || fail "first (fresh-install) run exited nonzero:\n$out1"
echo "$out1" | grep -q "ACL applied" || fail "case 1: no 'ACL applied' confirmation in output:\n$out1"

grep -q -- "-R -m g:bubble-console:rwX $AGENTS_DIR" "$SETFACL_LOG" \
  || fail "case 1: recursive access-ACL setfacl call not logged (got: $(cat "$SETFACL_LOG"))"
grep -q -- "-R -d -m g:bubble-console:rwX $AGENTS_DIR" "$SETFACL_LOG" \
  || fail "case 1: recursive DEFAULT-ACL setfacl call not logged (got: $(cat "$SETFACL_LOG"))"

# Ordering: the setfacl call(s) must appear before the restart in the call log.
setfacl_line=$(grep -n "sudo setfacl" "$CALL_LOG" | head -1 | cut -d: -f1)
restart_line=$(grep -n "systemctl restart" "$CALL_LOG" | head -1 | cut -d: -f1)
[[ -n "$setfacl_line" && -n "$restart_line" ]] || fail "case 1: could not locate both setfacl and restart in call log:\n$(cat "$CALL_LOG")"
[[ "$setfacl_line" -lt "$restart_line" ]] || fail "case 1: setfacl ran AFTER the restart (line $setfacl_line vs $restart_line) — must run before"
pass "case 1: fresh install applies the scoped ACL (access + recursive default) before the restart"

# === Case 2: re-run against an unchanged unit (already-provisioned box) — ACL is
#     re-applied anyway (idempotent self-heal), and no restart happens. ===
: > "$SETFACL_LOG"; : > "$CALL_LOG"
out2=$(run_script 2>&1) || fail "second (already-matches) run exited nonzero:\n$out2"
echo "$out2" | grep -q "already matches" || fail "case 2: expected the 'already matches — nothing to install' path:\n$out2"
grep -q -- "-R -m g:bubble-console:rwX $AGENTS_DIR" "$SETFACL_LOG" \
  || fail "case 2: ACL not re-applied on an unchanged-unit rerun (got: $(cat "$SETFACL_LOG"))"
grep -q "systemctl restart" "$CALL_LOG" && fail "case 2: unexpected restart on an unchanged-unit rerun:\n$(cat "$CALL_LOG")"
pass "case 2: unchanged-unit rerun still (idempotently) re-applies the ACL, without restarting"

# === Case 3: --dry-run makes no writes at all (no setfacl call) ===
: > "$SETFACL_LOG"; : > "$CALL_LOG"
out3=$(run_script --dry-run 2>&1) || fail "dry-run exited nonzero:\n$out3"
[[ -s "$SETFACL_LOG" ]] && fail "case 3: --dry-run must not invoke setfacl, but it did:\n$(cat "$SETFACL_LOG")"
echo "$out3" | grep -qi "DRY RUN.*setfacl" || fail "case 3: expected a DRY RUN setfacl preview line:\n$out3"
pass "case 3: --dry-run previews the ACL step without calling setfacl"

echo "ALL PASS"
exit 0
