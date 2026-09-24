#!/usr/bin/env bash
# test_1477_bubble_deploy_mac_safe_ff.sh — hermetic contract tests for the Mac
# framework-clone auto-updater (deploy/local/bubble-deploy-mac.sh) and its
# launchd installer (deploy/local/install-mac-loop-deploy.sh). Board #1477:
# jade-m1/jade-m5/Joris's Mac clones of bubble-ops-loop had no updater and
# drifted ~100 commits behind main.
#
# Covers, against throwaway synthetic git repos (never the real checkout):
#   T1  clean tree, behind origin/main → ff-only merge, exit 0, log UPDATED.
#   T2  already current → no-op merge, exit 0, log CURRENT.
#   T3  dirty working tree → refuses to touch the worktree, ALERT + exit 1.
#   T4  wrong branch (not main) → refuses to touch the worktree, ALERT + exit 1.
#   T5  diverged (local commit ahead of origin/main) → refuses, ALERT + exit 1.
#   T6  never executes anything from the pulled tree (only `install`/copy of
#       the vendored session-rotate script, never invokes it).
#   T7  vendored rotate script is reinstalled (compare-and-install) only when
#       its content changed, idempotently.
#   T8  the installer (no --activate) renders a valid plist, uses StartInterval
#       (not StartCalendarInterval / KeepAlive), and never calls launchctl.
#   T9  --uninstall (no --activate) removes the plist without touching launchctl.
#
# Board #1488 — widened contract (allow-listed installer + compare-and-install
# hook), stubbed via env overrides (BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER,
# BOOT_REARM_PLUGIN_GLOB, BOOT_REARM_BUN) so these never touch a real bun or a
# real telegram plugin cache:
#   T10 boot-rearm source unchanged + server.ts already wired → installer is
#       never invoked (cheap pre-check skip).
#   T11 boot-rearm source differs from the installed plugin copy → installer
#       IS invoked, receiving the Mac plugin glob + bun path via env.
#   T12 the ff/state check fails (dirty tree) → installer is never invoked,
#       same ALERT + exit 1 as T3.
#   T13 the installer itself fails (simulated) → exit 1, but the fast-forward
#       that already happened is NOT rolled back (HEAD stays at origin/main).
#   T14 deploy/hooks/rearm-loop-on-compact.py is never created in the support
#       dir when the Mac never opted in (no pre-existing vendored copy).
#   T15 a pre-existing vendored compact hook IS updated (compare-and-install),
#       keeping exactly one timestamped .bak of the previous content, and a
#       second no-op run creates no additional .bak.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
RUNNER="${RUNNER:-$ROOT/deploy/local/bubble-deploy-mac.sh}"
INSTALLER="${INSTALLER:-$ROOT/deploy/local/install-mac-loop-deploy.sh}"
[[ -f "$RUNNER" ]]    || { echo "FATAL: not found: $RUNNER"; exit 2; }
[[ -f "$INSTALLER" ]] || { echo "FATAL: not found: $INSTALLER"; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
chk() { if [[ "$2" == "$3" ]]; then ok "$1 (rc=$3)"; else bad "$1 (expected rc=$2, got rc=$3)"; fi; }
want() { if grep -q "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (no match '$2' in $3)"; fi; }

new_pair() {
    local base="$1"
    local origin="$base-origin.git" seed="$base-seed"
    git init --bare -q "$origin"
    git clone -q "$origin" "$seed"
    git -C "$seed" checkout -qb main
    git -C "$seed" config user.email synthetic@example.invalid
    git -C "$seed" config user.name synthetic
    mkdir -p "$seed/deploy/local"
    echo '#!/bin/bash' >"$seed/deploy/local/bubble-session-rotate-mac.sh"
    echo 'echo original-rotate' >>"$seed/deploy/local/bubble-session-rotate-mac.sh"
    echo initial >"$seed/file.txt"
    git -C "$seed" add file.txt deploy/local/bubble-session-rotate-mac.sh
    git -C "$seed" commit -qm initial
    git -C "$seed" push -q origin main
    git clone -q --branch main "$origin" "$base"
}

push_upstream() {
    local base="$1"
    echo "upstream-$(date +%s%N)" >>"$base-seed/file.txt"
    git -C "$base-seed" commit -qam upstream
    git -C "$base-seed" push -q origin main
}

# Board #1488 fixtures ------------------------------------------------------

new_pair_br() {
    # Like new_pair, but also seeds deploy/telegram-plugin/boot_rearm.ts so
    # the boot-rearm pre-check has a real source file to diff against.
    local base="$1"
    new_pair "$base"
    mkdir -p "$base-seed/deploy/telegram-plugin"
    echo "boot-rearm-src-v1" >"$base-seed/deploy/telegram-plugin/boot_rearm.ts"
    git -C "$base-seed" add deploy/telegram-plugin/boot_rearm.ts
    git -C "$base-seed" commit -qm "seed boot_rearm.ts"
    git -C "$base-seed" push -q origin main
    git -C "$base" fetch -q origin main
    git -C "$base" merge -q --ff-only origin/main
}

new_pair_hook() {
    # Like new_pair, but also seeds deploy/hooks/rearm-loop-on-compact.py.
    local base="$1" content="${2:-compact-hook-src-v1}"
    new_pair "$base"
    mkdir -p "$base-seed/deploy/hooks"
    echo "$content" >"$base-seed/deploy/hooks/rearm-loop-on-compact.py"
    git -C "$base-seed" add deploy/hooks/rearm-loop-on-compact.py
    git -C "$base-seed" commit -qm "seed compact hook ($content)"
    git -C "$base-seed" push -q origin main
    git -C "$base" fetch -q origin main
    git -C "$base" merge -q --ff-only origin/main
}

# Writes a stub install-boot-rearm.sh replacement to $1 that logs each
# invocation (including the env vars it received) to $STUB_CALL_LOG and
# exits with $STUB_EXIT_CODE (default 0). Never touches bun or a real
# plugin cache — this IS the "stub the installer... via env overrides" the
# card asks for.
write_stub_installer() {
    local path="$1"
    cat >"$path" <<'EOF'
#!/usr/bin/env bash
{
  echo "CALLED"
  echo "glob=$BOOT_REARM_PLUGIN_GLOB"
  echo "bun=$BOOT_REARM_BUN"
} >>"$STUB_CALL_LOG"
echo "stub-installer-output-line"
exit "${STUB_EXIT_CODE:-0}"
EOF
    chmod +x "$path"
}

# A harmless, executable stand-in for the real `bun` binary. Board #1488's
# stub installer above never actually invokes bun (it only records the path
# it was handed), but bubble-deploy-mac.sh's own BOOT_REARM_BUN resolution
# falls back to `command -v bun` for any override that isn't executable —
# this file must exist and be +x so the T10-T13 overrides below survive
# that check and prove the passthrough end-to-end.
FAKE_BUN="$WORK/fake-bun"
printf '#!/usr/bin/env bash\nexit 0\n' >"$FAKE_BUN"
chmod +x "$FAKE_BUN"

echo "T1 clean tree, behind origin/main: fast-forwards, exit 0, logs UPDATED"
new_pair "$WORK/t1"
push_upstream "$WORK/t1"
before_target="$(git -C "$WORK/t1-seed" rev-parse origin/main 2>/dev/null || git -C "$WORK/t1-seed" rev-parse HEAD)"
out="$WORK/t1.out"
bash "$RUNNER" --repo-dir "$WORK/t1" --support-dir "$WORK/t1-support" >"$out" 2>&1
rc=$?
chk "T1 exit code" 0 "$rc"
head="$(git -C "$WORK/t1" rev-parse HEAD)"
origin_head="$(git -C "$WORK/t1" rev-parse origin/main)"
[[ "$head" == "$origin_head" ]] && ok "T1 HEAD == origin/main" || bad "T1 HEAD != origin/main ($head vs $origin_head)"
want "T1 logs UPDATED" 'UPDATED' "$out"

echo "T2 already current: no-op, exit 0, logs CURRENT"
out="$WORK/t2.out"
bash "$RUNNER" --repo-dir "$WORK/t1" --support-dir "$WORK/t1-support" >"$out" 2>&1
rc=$?
chk "T2 exit code" 0 "$rc"
want "T2 logs CURRENT" 'CURRENT' "$out"

echo "T3 dirty working tree: refuses to touch worktree, ALERT + exit 1"
new_pair "$WORK/t3"
push_upstream "$WORK/t3"
echo dirty >>"$WORK/t3/file.txt"
head_before="$(git -C "$WORK/t3" rev-parse HEAD)"
out="$WORK/t3.out"
bash "$RUNNER" --repo-dir "$WORK/t3" --support-dir "$WORK/t3-support" >"$out" 2>&1
rc=$?
chk "T3 exit code" 1 "$rc"
head_after="$(git -C "$WORK/t3" rev-parse HEAD)"
[[ "$head_before" == "$head_after" ]] && ok "T3 HEAD unchanged" || bad "T3 HEAD moved despite dirty tree"
[[ "$(cat "$WORK/t3/file.txt" | tail -1)" == "dirty" ]] && ok "T3 dirty edit preserved" || bad "T3 dirty edit lost"
want "T3 logs ALERT" 'ALERT' "$out"
want "T3 mentions dirty" 'dirty' "$out"

echo "T4 wrong branch (not main): refuses to touch worktree, ALERT + exit 1"
new_pair "$WORK/t4"
push_upstream "$WORK/t4"
git -C "$WORK/t4" checkout -qb some-other-branch
head_before="$(git -C "$WORK/t4" rev-parse HEAD)"
out="$WORK/t4.out"
bash "$RUNNER" --repo-dir "$WORK/t4" --support-dir "$WORK/t4-support" >"$out" 2>&1
rc=$?
chk "T4 exit code" 1 "$rc"
head_after="$(git -C "$WORK/t4" rev-parse HEAD)"
[[ "$head_before" == "$head_after" ]] && ok "T4 HEAD unchanged" || bad "T4 HEAD moved on wrong branch"
branch_after="$(git -C "$WORK/t4" symbolic-ref --quiet --short HEAD)"
[[ "$branch_after" == "some-other-branch" ]] && ok "T4 branch unchanged" || bad "T4 branch was switched"
want "T4 logs ALERT" 'ALERT' "$out"
want "T4 mentions branch" 'branch' "$out"

echo "T5 diverged (local commit ahead of origin/main): refuses, ALERT + exit 1"
new_pair "$WORK/t5"
git -C "$WORK/t5" config user.email synthetic@example.invalid
git -C "$WORK/t5" config user.name synthetic
echo local-work >>"$WORK/t5/file.txt"
git -C "$WORK/t5" commit -qam "local work not yet pushed"
push_upstream "$WORK/t5"
head_before="$(git -C "$WORK/t5" rev-parse HEAD)"
out="$WORK/t5.out"
bash "$RUNNER" --repo-dir "$WORK/t5" --support-dir "$WORK/t5-support" >"$out" 2>&1
rc=$?
chk "T5 exit code" 1 "$rc"
head_after="$(git -C "$WORK/t5" rev-parse HEAD)"
[[ "$head_before" == "$head_after" ]] && ok "T5 HEAD unchanged" || bad "T5 HEAD moved despite divergence"
want "T5 logs ALERT" 'ALERT' "$out"
want "T5 mentions ahead/diverged" 'ahead' "$out"

echo "T6 never executes anything from the pulled tree"
new_pair "$WORK/t6"
# A rotate script that would prove execution if ever invoked.
cat >"$WORK/t6-seed/deploy/local/bubble-session-rotate-mac.sh" <<'EOF'
#!/bin/bash
echo "EXECUTED" >"__CANARY__"
EOF
sed -i.bak "s#__CANARY__#$WORK/t6-executed.canary#" "$WORK/t6-seed/deploy/local/bubble-session-rotate-mac.sh"
rm -f "$WORK/t6-seed/deploy/local/bubble-session-rotate-mac.sh.bak"
git -C "$WORK/t6-seed" commit -qam "rotate script would leave a canary if executed"
git -C "$WORK/t6-seed" push -q origin main
out="$WORK/t6.out"
bash "$RUNNER" --repo-dir "$WORK/t6" --support-dir "$WORK/t6-support" >"$out" 2>&1
rc=$?
chk "T6 exit code" 0 "$rc"
[[ ! -f "$WORK/t6-executed.canary" ]] && ok "T6 rotate script was never executed" || bad "T6 rotate script WAS executed"

echo "T7 vendored rotate script reinstalled only when content changed (compare-and-install)"
[[ -f "$WORK/t6-support/bubble-session-rotate-mac.sh" ]] && ok "T7 rotate script vendored into support dir" || bad "T7 rotate script not vendored"
cmp -s "$WORK/t6/deploy/local/bubble-session-rotate-mac.sh" "$WORK/t6-support/bubble-session-rotate-mac.sh" \
    && ok "T7 vendored copy matches source" || bad "T7 vendored copy differs from source"
before_mtime="$(stat -f '%m' "$WORK/t6-support/bubble-session-rotate-mac.sh" 2>/dev/null || stat -c '%Y' "$WORK/t6-support/bubble-session-rotate-mac.sh")"
sleep 1
out="$WORK/t7-noop.out"
bash "$RUNNER" --repo-dir "$WORK/t6" --support-dir "$WORK/t6-support" >"$out" 2>&1
after_mtime="$(stat -f '%m' "$WORK/t6-support/bubble-session-rotate-mac.sh" 2>/dev/null || stat -c '%Y' "$WORK/t6-support/bubble-session-rotate-mac.sh")"
[[ "$before_mtime" == "$after_mtime" ]] && ok "T7 unchanged rotate script is not reinstalled" || bad "T7 rotate script reinstalled with no content change"
! grep -q 'VENDORED' "$out" && ok "T7 no VENDORED log line on no-op" || bad "T7 unexpected VENDORED log line"
echo "echo changed" >>"$WORK/t6-seed/deploy/local/bubble-session-rotate-mac.sh"
git -C "$WORK/t6-seed" commit -qam "change rotate script"
git -C "$WORK/t6-seed" push -q origin main
out="$WORK/t7-change.out"
bash "$RUNNER" --repo-dir "$WORK/t6" --support-dir "$WORK/t6-support" >"$out" 2>&1
cmp -s "$WORK/t6/deploy/local/bubble-session-rotate-mac.sh" "$WORK/t6-support/bubble-session-rotate-mac.sh" \
    && ok "T7 changed rotate script reinstalled" || bad "T7 changed rotate script not reinstalled"
want "T7 logs VENDORED on change" 'VENDORED' "$out"

echo "T8 installer (no --activate) renders a valid StartInterval plist, never calls launchctl"
FAKE_BIN="$WORK/fakebin"; mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/launchctl" <<'EOF'
#!/bin/bash
echo "LAUNCHCTL CALLED: $*" >>"$TEST_LAUNCHCTL_LOG"
exit 0
EOF
chmod +x "$FAKE_BIN/launchctl"
FAKE_HOME="$WORK/fakehome"; mkdir -p "$FAKE_HOME"
: >"$WORK/t8-launchctl.log"
PATH="$FAKE_BIN:$PATH" HOME="$FAKE_HOME" TEST_LAUNCHCTL_LOG="$WORK/t8-launchctl.log" \
    bash "$INSTALLER" --repo-dir "$WORK/t1" --support-dir "$FAKE_HOME/support" --interval 1234 \
    >"$WORK/t8.out" 2>&1
rc=$?
chk "T8 install exit code" 0 "$rc"
PLIST="$FAKE_HOME/Library/LaunchAgents/com.bubble.mac-loop-deploy.plist"
[[ -f "$PLIST" ]] && ok "T8 plist rendered" || bad "T8 plist not rendered at $PLIST"
want "T8 uses StartInterval" 'StartInterval' "$PLIST"
! grep -q 'StartCalendarInterval' "$PLIST" && ok "T8 does not use StartCalendarInterval" || bad "T8 unexpectedly uses StartCalendarInterval"
! grep -q 'KeepAlive' "$PLIST" && ok "T8 does not use KeepAlive (periodic job, not a persistent session)" || bad "T8 unexpectedly uses KeepAlive"
want "T8 interval honored" '<integer>1234</integer>' "$PLIST"
[[ -f "$FAKE_HOME/support/bubble-deploy-mac.sh" ]] && ok "T8 runner vendored into support dir" || bad "T8 runner not vendored"
[[ ! -s "$WORK/t8-launchctl.log" ]] && ok "T8 launchctl was never called" || bad "T8 launchctl was called: $(cat "$WORK/t8-launchctl.log")"
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null 2>&1 && ok "T8 plutil -lint OK" || bad "T8 plutil -lint failed"
fi

echo "T9 --uninstall (no --activate) removes the plist without touching launchctl"
: >"$WORK/t9-launchctl.log"
PATH="$FAKE_BIN:$PATH" HOME="$FAKE_HOME" TEST_LAUNCHCTL_LOG="$WORK/t9-launchctl.log" \
    bash "$INSTALLER" --uninstall >"$WORK/t9.out" 2>&1
rc=$?
chk "T9 uninstall exit code" 0 "$rc"
[[ ! -f "$PLIST" ]] && ok "T9 plist removed" || bad "T9 plist still present"
[[ ! -s "$WORK/t9-launchctl.log" ]] && ok "T9 launchctl was never called" || bad "T9 launchctl was called: $(cat "$WORK/t9-launchctl.log")"

echo "T10 boot-rearm: unchanged + already-wired source is skipped (installer never invoked)"
new_pair_br "$WORK/t10"
PLUGIN_ROOT="$WORK/t10-plugin"; PLUGIN_DIR="$PLUGIN_ROOT/telegram/9.9.9"; mkdir -p "$PLUGIN_DIR"
cp "$WORK/t10/deploy/telegram-plugin/boot_rearm.ts" "$PLUGIN_DIR/boot_rearm.ts"
echo "// bootRearmNotification already wired" >"$PLUGIN_DIR/server.ts"
STUB="$WORK/t10-stub-installer.sh"; write_stub_installer "$STUB"
STUB_CALL_LOG="$WORK/t10-stub.log"; : >"$STUB_CALL_LOG"
out="$WORK/t10.out"
BOOT_REARM_PLUGIN_GLOB="$PLUGIN_ROOT/telegram/*/" BOOT_REARM_BUN="$FAKE_BUN" \
    BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER="$STUB" STUB_CALL_LOG="$STUB_CALL_LOG" \
    bash "$RUNNER" --repo-dir "$WORK/t10" --support-dir "$WORK/t10-support" >"$out" 2>&1
rc=$?
chk "T10 exit code" 0 "$rc"
[[ ! -s "$STUB_CALL_LOG" ]] && ok "T10 installer never invoked (unchanged + wired)" || bad "T10 installer was invoked: $(cat "$STUB_CALL_LOG")"
want "T10 logs skip" 'BOOT-REARM: source unchanged and already wired' "$out"

echo "T11 boot-rearm: source differs from installed copy -> installer invoked with correct env"
new_pair_br "$WORK/t11"
PLUGIN_ROOT="$WORK/t11-plugin"; PLUGIN_DIR="$PLUGIN_ROOT/telegram/9.9.9"; mkdir -p "$PLUGIN_DIR"
echo "installed-OLD" >"$PLUGIN_DIR/boot_rearm.ts"
echo "// not wired yet" >"$PLUGIN_DIR/server.ts"
STUB="$WORK/t11-stub-installer.sh"; write_stub_installer "$STUB"
STUB_CALL_LOG="$WORK/t11-stub.log"; : >"$STUB_CALL_LOG"
out="$WORK/t11.out"
BOOT_REARM_PLUGIN_GLOB="$PLUGIN_ROOT/telegram/*/" BOOT_REARM_BUN="$FAKE_BUN" \
    BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER="$STUB" STUB_CALL_LOG="$STUB_CALL_LOG" \
    bash "$RUNNER" --repo-dir "$WORK/t11" --support-dir "$WORK/t11-support" >"$out" 2>&1
rc=$?
chk "T11 exit code" 0 "$rc"
[[ -s "$STUB_CALL_LOG" ]] && ok "T11 installer was invoked" || bad "T11 installer never invoked"
grep -qF "glob=$PLUGIN_ROOT/telegram/*/" "$STUB_CALL_LOG" \
    && ok "T11 installer received the Mac plugin glob" || bad "T11 wrong/missing glob passed to installer ($(cat "$STUB_CALL_LOG" 2>/dev/null))"
grep -qF "bun=$FAKE_BUN" "$STUB_CALL_LOG" \
    && ok "T11 installer received the bun path" || bad "T11 wrong/missing bun path passed to installer ($(cat "$STUB_CALL_LOG" 2>/dev/null))"
want "T11 logs change detected" 'BOOT-REARM: change detected' "$out"
want "T11 logs installer OK" 'BOOT-REARM: installer OK' "$out"

echo "T12 boot-rearm: installer never runs when the ff/state check fails (dirty tree)"
new_pair_br "$WORK/t12"
echo dirty >>"$WORK/t12/file.txt"
PLUGIN_ROOT="$WORK/t12-plugin"; PLUGIN_DIR="$PLUGIN_ROOT/telegram/9.9.9"; mkdir -p "$PLUGIN_DIR"
echo "installed-OLD" >"$PLUGIN_DIR/boot_rearm.ts"
STUB="$WORK/t12-stub-installer.sh"; write_stub_installer "$STUB"
STUB_CALL_LOG="$WORK/t12-stub.log"; : >"$STUB_CALL_LOG"
out="$WORK/t12.out"
BOOT_REARM_PLUGIN_GLOB="$PLUGIN_ROOT/telegram/*/" BOOT_REARM_BUN="$FAKE_BUN" \
    BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER="$STUB" STUB_CALL_LOG="$STUB_CALL_LOG" \
    bash "$RUNNER" --repo-dir "$WORK/t12" --support-dir "$WORK/t12-support" >"$out" 2>&1
rc=$?
chk "T12 exit code" 1 "$rc"
[[ ! -s "$STUB_CALL_LOG" ]] && ok "T12 installer never invoked when ff check fails" || bad "T12 installer WAS invoked despite dirty tree"
want "T12 logs ALERT" 'ALERT' "$out"

echo "T13 boot-rearm: installer failure -> exit 1, but the successful ff is kept (not rolled back)"
new_pair_br "$WORK/t13"
push_upstream "$WORK/t13"
target="$(git -C "$WORK/t13-seed" rev-parse origin/main)"
PLUGIN_ROOT="$WORK/t13-plugin"; PLUGIN_DIR="$PLUGIN_ROOT/telegram/9.9.9"; mkdir -p "$PLUGIN_DIR"
echo "installed-OLD" >"$PLUGIN_DIR/boot_rearm.ts"
STUB="$WORK/t13-stub-installer.sh"; write_stub_installer "$STUB"
STUB_CALL_LOG="$WORK/t13-stub.log"; : >"$STUB_CALL_LOG"
out="$WORK/t13.out"
BOOT_REARM_PLUGIN_GLOB="$PLUGIN_ROOT/telegram/*/" BOOT_REARM_BUN="$FAKE_BUN" \
    BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER="$STUB" STUB_CALL_LOG="$STUB_CALL_LOG" STUB_EXIT_CODE=4 \
    bash "$RUNNER" --repo-dir "$WORK/t13" --support-dir "$WORK/t13-support" >"$out" 2>&1
rc=$?
chk "T13 exit code" 1 "$rc"
[[ -s "$STUB_CALL_LOG" ]] && ok "T13 installer was invoked" || bad "T13 installer never invoked"
head="$(git -C "$WORK/t13" rev-parse HEAD)"
[[ "$head" == "$target" ]] && ok "T13 ff was kept despite installer failure (HEAD == origin/main)" || bad "T13 ff was rolled back or not applied ($head vs $target)"
want "T13 logs UPDATED (ff happened)" 'UPDATED' "$out"
want "T13 logs installer failure ALERT" 'boot-rearm installer failed' "$out"

echo "T14 compact-hook: never created when the Mac never opted in (no pre-existing vendored copy)"
new_pair_hook "$WORK/t14"
out="$WORK/t14.out"
bash "$RUNNER" --repo-dir "$WORK/t14" --support-dir "$WORK/t14-support" >"$out" 2>&1
rc=$?
chk "T14 exit code" 0 "$rc"
[[ ! -f "$WORK/t14-support/hooks/rearm-loop-on-compact.py" ]] \
    && ok "T14 compact hook was not created" || bad "T14 compact hook was created despite no opt-in"
want "T14 logs opted-out skip" 'COMPACT-HOOK: not vendored on this Mac' "$out"

echo "T15 compact-hook: pre-existing vendored copy is updated with a timestamped .bak; re-run is a no-op"
new_pair_hook "$WORK/t15" "compact-hook-src-NEW"
mkdir -p "$WORK/t15-support/hooks"
echo "compact-hook-OLD" >"$WORK/t15-support/hooks/rearm-loop-on-compact.py"
out="$WORK/t15.out"
bash "$RUNNER" --repo-dir "$WORK/t15" --support-dir "$WORK/t15-support" >"$out" 2>&1
rc=$?
chk "T15 exit code" 0 "$rc"
dst="$WORK/t15-support/hooks/rearm-loop-on-compact.py"
[[ "$(cat "$dst")" == "compact-hook-src-NEW" ]] && ok "T15 compact hook reinstalled with new content" || bad "T15 compact hook not updated"
bak_count=$(find "$WORK/t15-support/hooks" -name 'rearm-loop-on-compact.py.bak-*' | wc -l | tr -d ' ')
[[ "$bak_count" == "1" ]] && ok "T15 exactly one .bak created" || bad "T15 expected exactly one .bak, found $bak_count"
bak_file=$(find "$WORK/t15-support/hooks" -name 'rearm-loop-on-compact.py.bak-*' | head -1)
[[ -n "$bak_file" && "$(cat "$bak_file" 2>/dev/null)" == "compact-hook-OLD" ]] && ok "T15 .bak preserves old content" || bad "T15 .bak does not preserve old content"
want "T15 logs reinstalled" 'COMPACT-HOOK: reinstalled' "$out"
out2="$WORK/t15-rerun.out"
bash "$RUNNER" --repo-dir "$WORK/t15" --support-dir "$WORK/t15-support" >"$out2" 2>&1
rc2=$?
chk "T15 re-run exit code" 0 "$rc2"
bak_count2=$(find "$WORK/t15-support/hooks" -name 'rearm-loop-on-compact.py.bak-*' | wc -l | tr -d ' ')
[[ "$bak_count2" == "1" ]] && ok "T15 no additional .bak on unchanged re-run" || bad "T15 unexpected extra .bak on unchanged re-run (found $bak_count2)"
want "T15 re-run logs unchanged skip" 'COMPACT-HOOK: unchanged' "$out2"

echo
echo "SUMMARY: pass=$PASS fail=$FAIL"
((FAIL == 0)) || exit 1
echo "PASS: all bubble-deploy-mac.sh / install-mac-loop-deploy.sh contract cases"
