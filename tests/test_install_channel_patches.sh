#!/usr/bin/env bash
# =============================================================================
# test_install_channel_patches.sh — bash harness for scripts/install-channel-patches.sh
# (board #956 — the durable fold of boot_rearm + bubble-inject re-apply).
#
# Covers, in order:
#
#   A. FRESH / un-patched plugin dir (simulating a version-bump re-extract that
#      wiped both patches) -> running the installer re-applies BOTH: boot_rearm
#      markers present, bubble-inject markers present, `bun build` passes, exit 0.
#
#   B. Idempotency — re-running against the now-patched dir is a clean no-op:
#      no new backups, both patches still present exactly once, exit 0.
#
#   C. Partial state — a dir with boot_rearm ALREADY applied but bubble-inject
#      missing -> the installer leaves boot_rearm untouched and applies ONLY
#      bubble-inject (proves the two patches are handled independently, per
#      the mechanism.md doctrine: "a plugin can have neither, one, or both").
#
#   D. --dry-run footprint — against a fresh dir, --dry-run touches NOTHING:
#      no boot_rearm.ts copy, no server.ts edit, no backups, exit 0.
#
#   E. --strict exit code — against a dir where the bubble-inject anchor is
#      missing (drift), --strict surfaces a nonzero exit; default (non-strict)
#      mode still exits 0 (the fail-open pre-launch-hook contract).
#
#   F. Host-agnostic glob — the SAME script, pointed via
#      CHANNEL_PATCHES_PLUGIN_GLOB at a Mac-shaped path (no /home/claude
#      anywhere in it), still finds and patches the plugin. This is the actual
#      #956 regression: bubble-inject's old hook (apply-inject-patch.sh) was
#      VPS-only (/home/claude hardcoded), so a Mac plugin bump had nothing
#      re-applying it.
#
#   G/H. Concurrency (independent-review finding) — two invocations racing the
#      SAME server.ts (all VPS depts share one $HOME, hence one plugin cache)
#      must serialize via the lock, never corrupt: exactly one backup per
#      patch type, both patches present exactly once, still bun-builds, and
#      the pair's wall-clock proves real serialization (not scheduling luck).
#      Run against the box's native lock (G) and, when this box has no real
#      flock(1) (true on stock macOS), again forcing the flock(1) code path
#      via a Python-fcntl shim (H) so both lock backends are exercised.
#
#   I. GNU-mktemp regression (live-VPS dry-run finding) — the boot_rearm
#      step's `mktemp -t install-channel-patches-boot-rearm` (no XXXXXX) is a
#      silent no-op difference between BSD mktemp (macOS: `-t PREFIX` just
#      works) and GNU mktemp (the VPS: errors "too few X's in template"),
#      which is exactly why this bug survived 44 passing tests run only on a
#      Mac. A Python-based GNU-mktemp work-alike shim is used to reproduce
#      that stricter behavior on THIS (Mac) box, first proving the shim is
#      faithful (I1: it fails on the exact original buggy invocation), then
#      proving the FIXED script produces a true clean no-op (rc=0 even under
#      --strict — not fail-open masking a real failure) against an
#      already-fully-patched plugin under that same strict mktemp.
#
# Hermetic: builds throw-away fake plugin dirs under a tmpdir, each seeded from
# a genuinely PRISTINE server.ts (neither patch) + the real package.json + a
# SYMLINK to the real plugin node_modules, so `bun build` resolves grammy /
# @modelcontextprotocol without a network install. The live plugin cache is
# NEVER modified.
#
# Requirements on the box: bun, and a PRISTINE (unpatched) telegram plugin
# server.ts to seed fixtures from. On a box where the live plugin is already
# patched (the common case once #956 lands), point these at a saved
# pre-patch backup instead of the live server.ts:
#   CHANNEL_PATCHES_TEST_PLUGIN_SRC       dir supplying package.json + node_modules
#                                          (default: newest live telegram plugin dir)
#   CHANNEL_PATCHES_TEST_PRISTINE_SERVER  the pristine server.ts to seed fixtures
#                                          with (default: $CHANNEL_PATCHES_TEST_PLUGIN_SRC/server.ts)
#   CHANNEL_PATCHES_TEST_BUN              bun binary (default: $HOME/.bun/bin/bun,
#                                          falls back to `command -v bun`)
#
# Run:  bash tests/test_install_channel_patches.sh
#       bash tests/test_install_channel_patches.sh -v   # verbose (show installer out)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
INSTALLER="$REPO_ROOT/scripts/install-channel-patches.sh"
REPO_BOOT_REARM="$REPO_ROOT/deploy/telegram-plugin/boot_rearm.ts"
REPO_INJECT_BLOCK="$REPO_ROOT/deploy/telegram-plugin/bubble-inject.block.ts"
BUN_BIN="${CHANNEL_PATCHES_TEST_BUN:-$HOME/.bun/bin/bun}"
[[ -x "$BUN_BIN" ]] || BUN_BIN="$(command -v bun 2>/dev/null || true)"

[[ -f "$INSTALLER" ]]         || { echo "FATAL: installer not found: $INSTALLER"; exit 2; }
[[ -f "$REPO_BOOT_REARM" ]]   || { echo "FATAL: repo boot_rearm.ts not found: $REPO_BOOT_REARM"; exit 2; }
[[ -f "$REPO_INJECT_BLOCK" ]] || { echo "FATAL: repo bubble-inject.block.ts not found: $REPO_INJECT_BLOCK"; exit 2; }
[[ -n "$BUN_BIN" && -x "$BUN_BIN" ]] || { echo "FATAL: bun not found (set CHANNEL_PATCHES_TEST_BUN)"; exit 2; }

# Locate a plugin dir to source package.json + node_modules from (deps only).
PLUGIN_SRC="${CHANNEL_PATCHES_TEST_PLUGIN_SRC:-}"
if [[ -z "$PLUGIN_SRC" ]]; then
  for d in $(ls -d "$HOME"/.claude/plugins/cache/claude-plugins-official/telegram/*/ 2>/dev/null | sort -V); do
    [[ -d "$d" ]] && PLUGIN_SRC="$d"
  done
fi
PLUGIN_SRC="${PLUGIN_SRC%/}"
[[ -d "$PLUGIN_SRC/node_modules" ]] || { echo "FATAL: no node_modules at $PLUGIN_SRC (needed for bun build)"; exit 2; }
[[ -f "$PLUGIN_SRC/package.json" ]] || { echo "FATAL: no package.json at $PLUGIN_SRC"; exit 2; }

# The PRISTINE server.ts to seed fixtures with (must carry NEITHER marker —
# this is what a genuinely fresh version-bump extraction looks like).
PRISTINE_SERVER="${CHANNEL_PATCHES_TEST_PRISTINE_SERVER:-$PLUGIN_SRC/server.ts}"
[[ -f "$PRISTINE_SERVER" ]] || { echo "FATAL: no pristine server.ts at $PRISTINE_SERVER"; exit 2; }
if grep -q "bootRearmNotification\|bubble-inject" "$PRISTINE_SERVER"; then
  echo "FATAL: $PRISTINE_SERVER is ALREADY patched (has bootRearmNotification and/or"
  echo "       bubble-inject) — cannot build a pristine fixture from it. Point"
  echo "       CHANNEL_PATCHES_TEST_PRISTINE_SERVER at a genuinely unpatched"
  echo "       server.ts (e.g. a pre-patch .bak-* file from the live plugin dir)."
  exit 2
fi
grep -qF 'await mcp.connect(new StdioServerTransport())' "$PRISTINE_SERVER" \
  || { echo "FATAL: pristine server.ts lacks the bubble-inject anchor line — wrong fixture source"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# make_fixture <root> [pristine|boot_rearm_only] -> echoes the plugin dir path.
# "boot_rearm_only" pre-wires boot_rearm (via install-boot-rearm.sh itself,
# so the fixture reflects a REAL post-patch server.ts, not a hand-rolled one).
make_fixture() {
  local root="$1" mode="${2:-pristine}"
  local tgt="$root/claude-plugins-official/telegram/9.9.9"
  mkdir -p "$tgt"
  cp "$PRISTINE_SERVER" "$tgt/server.ts"
  cp "$PLUGIN_SRC/package.json" "$tgt/package.json"
  ln -s "$PLUGIN_SRC/node_modules" "$tgt/node_modules"
  if [[ "$mode" == "boot_rearm_only" ]]; then
    BOOT_REARM_PLUGIN_GLOB="$root/claude-plugins-official/telegram/*/" \
      BOOT_REARM_BUN="$BUN_BIN" \
      bash "$REPO_ROOT/scripts/install-boot-rearm.sh" >/dev/null 2>&1
  fi
  echo "$tgt"
}

run_installer() {
  # usage: run_installer <glob> [extra args...]; sets RC + OUT
  local glob="$1"; shift || true
  OUT="$(CHANNEL_PATCHES_PLUGIN_GLOB="$glob" CHANNEL_PATCHES_BUN="$BUN_BIN" bash "$INSTALLER" "$@" 2>&1)"
  RC=$?
  [[ "$VERBOSE" == "1" ]] && { echo "---- installer output ----"; echo "$OUT"; echo "--------------------------"; }
}

echo "== test_install_channel_patches.sh =="
echo "   installer:        $INSTALLER"
echo "   plugin deps src:   $PLUGIN_SRC"
echo "   pristine server:   $PRISTINE_SERVER"
echo ""

# ── A. fresh/un-patched dir -> BOTH patches applied ─────────────────────────
echo "A. fresh (simulated version-bump) plugin dir gets BOTH patches"
ROOT_A="$WORK/a"; TGT_A="$(make_fixture "$ROOT_A")"
GLOB_A="$ROOT_A/claude-plugins-official/telegram/*/"
run_installer "$GLOB_A"
[[ "$RC" == "0" ]] && ok "installer exits 0 (default/hook mode)" || bad "installer exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_A/server.ts")" == "2" ]] && ok "boot_rearm wired (import + call site)" || bad "boot_rearm not wired"
grep -q "bubble-inject" "$TGT_A/server.ts" && ok "bubble-inject marker present" || bad "bubble-inject marker missing"
[[ -f "$TGT_A/boot_rearm.ts" ]] && ok "boot_rearm.ts copied" || bad "boot_rearm.ts not copied"
[[ "$(ls "$TGT_A"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "one boot_rearm backup" || bad "expected exactly one boot_rearm backup"
[[ "$(ls "$TGT_A"/server.ts.bak-bubble-inject-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "one bubble-inject backup" || bad "expected exactly one bubble-inject backup"
( cd "$TGT_A" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$WORK/a-build-check" ) >"$WORK/a-build-check.log" 2>&1
[[ "$?" == "0" ]] && ok "independently: patched server.ts still bun-builds" || bad "independent bun build FAILED (see $WORK/a-build-check.log)"

# ── B. idempotency — re-run on the now-patched dir is a clean no-op ─────────
echo "B. re-run on an already-fully-patched dir is a no-op"
run_installer "$GLOB_A"
[[ "$RC" == "0" ]] && ok "re-run exits 0" || bad "re-run exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_A/server.ts")" == "2" ]] && ok "boot_rearm still wired exactly once" || bad "boot_rearm wiring count changed"
[[ "$(ls "$TGT_A"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "no NEW boot_rearm backup" || bad "re-run created an extra boot_rearm backup"
[[ "$(ls "$TGT_A"/server.ts.bak-bubble-inject-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "no NEW bubble-inject backup" || bad "re-run created an extra bubble-inject backup"
echo "$OUT" | grep -q "bubble-inject: already present" && ok "reports bubble-inject no-op" || bad "missing bubble-inject no-op log line"

# ── C. partial state — boot_rearm present, bubble-inject missing ───────────
echo "C. partial state: only the MISSING patch gets applied"
ROOT_C="$WORK/c"; TGT_C="$(make_fixture "$ROOT_C" boot_rearm_only)"
GLOB_C="$ROOT_C/claude-plugins-official/telegram/*/"
[[ "$(grep -c bootRearmNotification "$TGT_C/server.ts")" == "2" ]] || { echo "FATAL: fixture setup for C failed (boot_rearm not pre-wired)"; exit 2; }
! grep -q "bubble-inject" "$TGT_C/server.ts" || { echo "FATAL: fixture setup for C failed (bubble-inject already present)"; exit 2; }
BEFORE_C_MD5="$(md5sum "$TGT_C/boot_rearm.ts" 2>/dev/null | cut -d' ' -f1)"
run_installer "$GLOB_C"
[[ "$RC" == "0" ]] && ok "partial-state run exits 0" || bad "partial-state run exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_C/server.ts")" == "2" ]] && ok "pre-existing boot_rearm wiring untouched" || bad "boot_rearm wiring changed unexpectedly"
grep -q "bubble-inject" "$TGT_C/server.ts" && ok "bubble-inject now applied" || bad "bubble-inject was not applied"
AFTER_C_MD5="$(md5sum "$TGT_C/boot_rearm.ts" 2>/dev/null | cut -d' ' -f1)"
[[ "$BEFORE_C_MD5" == "$AFTER_C_MD5" ]] && ok "boot_rearm.ts left byte-identical" || bad "boot_rearm.ts was rewritten unnecessarily"

# ── D. --dry-run touches nothing ────────────────────────────────────────────
echo "D. --dry-run footprint on a fresh dir"
ROOT_D="$WORK/d"; TGT_D="$(make_fixture "$ROOT_D")"
GLOB_D="$ROOT_D/claude-plugins-official/telegram/*/"
run_installer "$GLOB_D" --dry-run
[[ "$RC" == "0" ]] && ok "dry-run exits 0" || bad "dry-run exit was $RC"
[[ ! -f "$TGT_D/boot_rearm.ts" ]] && ok "dry-run did NOT copy boot_rearm.ts" || bad "dry-run copied boot_rearm.ts"
! grep -q "bootRearmNotification" "$TGT_D/server.ts" && ok "dry-run did NOT wire boot_rearm" || bad "dry-run wired boot_rearm"
! grep -q "bubble-inject" "$TGT_D/server.ts" && ok "dry-run did NOT wire bubble-inject" || bad "dry-run wired bubble-inject"
[[ -z "$(ls "$TGT_D"/server.ts.bak-* 2>/dev/null)" ]] && ok "dry-run created NO backups" || bad "dry-run created a backup"

# ── E. --strict surfaces failure; default mode stays fail-open ─────────────
echo "E. drift (missing anchor): --strict fails loudly, default mode fails open"
ROOT_E="$WORK/e"; TGT_E="$(make_fixture "$ROOT_E")"
GLOB_E="$ROOT_E/claude-plugins-official/telegram/*/"
# Corrupt the anchor so bubble-inject can't find its insertion point (simulates
# plugin drift), while leaving enough of the file intact for boot_rearm's OWN
# anchor (a different string) to still apply cleanly.
sed -i.orig "s/await mcp.connect(new StdioServerTransport())/await mcp_connect_renamed_by_test()/" "$TGT_E/server.ts"
run_installer "$GLOB_E" --strict
[[ "$RC" != "0" ]] && ok "--strict exits nonzero on drift" || bad "--strict should have failed (rc=$RC)"
run_installer "$GLOB_E"
[[ "$RC" == "0" ]] && ok "default (non-strict) mode still exits 0 on the same drift (fail-open)" || bad "default mode should stay fail-open (rc=$RC)"
! grep -q "bubble-inject" "$TGT_E/server.ts" && ok "bubble-inject correctly NOT applied (anchor missing)" || bad "bubble-inject was applied despite missing anchor"

# ── F. host-agnostic: a Mac-shaped glob (no /home/claude anywhere) works ───
echo "F. host-agnostic glob (Mac-shaped path, the actual #956 regression)"
ROOT_F="$WORK/Users/faketestuser/.claude/plugins/cache"; TGT_F="$(make_fixture "$ROOT_F")"
GLOB_F="$ROOT_F/claude-plugins-official/telegram/*/"
case "$GLOB_F" in
  */home/claude/*) echo "FATAL: test setup bug — glob still contains /home/claude"; exit 2 ;;
esac
run_installer "$GLOB_F"
[[ "$RC" == "0" ]] && ok "Mac-shaped glob: installer exits 0" || bad "Mac-shaped glob: installer exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_F/server.ts")" == "2" ]] && ok "Mac-shaped glob: boot_rearm applied" || bad "Mac-shaped glob: boot_rearm NOT applied"
grep -q "bubble-inject" "$TGT_F/server.ts" && ok "Mac-shaped glob: bubble-inject applied" || bad "Mac-shaped glob: bubble-inject NOT applied"

# ── G/H. CONCURRENCY (board #956 review fix) ────────────────────────────────
# The confirmed regression: on the VPS every dept runs as the same `claude`
# user / $HOME, so ALL depts share ONE telegram plugin server.ts. A fleet-wide
# restart launches N ExecStartPre invocations of this installer ~together
# against that SAME file with no coordination — a race (double-insert, a
# half-written file failing `bun build`, or one run's restore-on-failure
# reverting another run's good patch). The fix is a per-host exclusive lock
# (flock(1) when available, a portable mkdir-based lock otherwise) around the
# whole boot_rearm+bubble-inject critical section.
#
# run_concurrency_case <label> <extra-env-prefix-for-the-SLOW-run> — launches
# a SLOW run (holds the lock ~3s via CHANNEL_PATCHES_DEBUG_LOCK_SLEEP, which
# deterministically widens the race window instead of depending on scheduling
# luck) and a normal run ~simultaneously against the SAME fixture dir, then
# asserts: both exit 0, exactly ONE backup per patch type (not two — proving
# they serialized rather than both writing), both patches present exactly
# once, the result still `bun build`s, and (the actual proof of
# serialization, not just of no-corruption) the pair took at least as long as
# the slow run's own sleep — i.e. the fast run genuinely WAITED rather than
# running concurrently and getting lucky.
run_concurrency_case() {
  local label="$1" env_prefix="$2" root tgt glob t_start t_end elapsed
  root="$WORK/conc-$label"; tgt="$(make_fixture "$root")"
  glob="$root/claude-plugins-official/telegram/*/"

  t_start="$(date +%s)"
  ( eval "$env_prefix" CHANNEL_PATCHES_DEBUG_LOCK_SLEEP=3 \
      CHANNEL_PATCHES_PLUGIN_GLOB="'$glob'" CHANNEL_PATCHES_BUN="'$BUN_BIN'" \
      bash "$INSTALLER" > "$WORK/conc-$label-slow.log" 2>&1 ) &
  local slow_pid=$!
  sleep 0.3   # let the slow run win the race for the lock
  ( eval "$env_prefix" \
      CHANNEL_PATCHES_PLUGIN_GLOB="'$glob'" CHANNEL_PATCHES_BUN="'$BUN_BIN'" \
      bash "$INSTALLER" > "$WORK/conc-$label-fast.log" 2>&1 ) &
  local fast_pid=$!

  wait "$slow_pid"; local slow_rc=$?
  wait "$fast_pid"; local fast_rc=$?
  t_end="$(date +%s)"
  elapsed=$(( t_end - t_start ))

  [[ "$slow_rc" == "0" ]] && ok "$label: slow (lock-holding) run exits 0" || bad "$label: slow run exit was $slow_rc"
  [[ "$fast_rc" == "0" ]] && ok "$label: concurrent run exits 0" || bad "$label: concurrent run exit was $fast_rc"
  (( elapsed >= 2 )) && ok "$label: concurrent run actually SERIALIZED (elapsed ${elapsed}s >= the 3s hold, not two independent fast runs)" \
                      || bad "$label: elapsed only ${elapsed}s — looks like NO serialization happened (lock not effective)"
  [[ "$(grep -c bootRearmNotification "$tgt/server.ts")" == "2" ]] && ok "$label: boot_rearm present exactly once (no double-insert)" || bad "$label: boot_rearm wiring corrupted/duplicated"
  [[ "$(grep -c "BUBBLE-INJECT PATCH BEGIN" "$tgt/server.ts")" == "1" ]] && ok "$label: bubble-inject present exactly once (no double-insert)" || bad "$label: bubble-inject wiring corrupted/duplicated"
  [[ "$(ls "$tgt"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "$label: exactly one boot_rearm backup (not two concurrent writers)" || bad "$label: expected exactly one boot_rearm backup"
  [[ "$(ls "$tgt"/server.ts.bak-bubble-inject-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "$label: exactly one bubble-inject backup (not two concurrent writers)" || bad "$label: expected exactly one bubble-inject backup"
  ( cd "$tgt" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$WORK/conc-$label-build-check" ) >"$WORK/conc-$label-build-check.log" 2>&1
  [[ "$?" == "0" ]] && ok "$label: final server.ts is NOT corrupted — still bun-builds" || bad "$label: final server.ts FAILED to bun-build (corruption!)"
  [[ "$VERBOSE" == "1" ]] && { echo "  -- $label slow.log --"; cat "$WORK/conc-$label-slow.log"; echo "  -- $label fast.log --"; cat "$WORK/conc-$label-fast.log"; }
}

echo "G. concurrency: two invocations racing the SAME server.ts (native lock — $( command -v flock >/dev/null 2>&1 && echo flock || echo mkdir-fallback ) on this box)"
run_concurrency_case "native" ""

if ! command -v flock >/dev/null 2>&1; then
  echo "H. concurrency via the flock(1) code path, forced with a minimal test-only shim"
  echo "   (this box has no real flock — see WHY THIS EXISTS in install-channel-patches.sh;"
  echo "    the shim exercises the SAME 'exec fd> ...; flock -w SEC fd' logic real flock(1) uses,"
  echo "    just backed by Python's fcntl.flock instead of util-linux's C implementation)"
  SHIM_DIR="$WORK/shim-bin"; mkdir -p "$SHIM_DIR"
  cat > "$SHIM_DIR/flock" <<'SHIM'
#!/usr/bin/env python3
import fcntl, sys, time
args = sys.argv[1:]
timeout = None
fdnum = None
i = 0
while i < len(args):
    if args[i] == '-w':
        timeout = float(args[i + 1]); i += 2
    else:
        fdnum = int(args[i]); i += 1
deadline = None if timeout is None else time.time() + timeout
while True:
    try:
        fcntl.flock(fdnum, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if deadline is not None and time.time() >= deadline:
            sys.exit(1)
        time.sleep(0.05)
SHIM
  chmod +x "$SHIM_DIR/flock"
  [[ "$(PATH="$SHIM_DIR:$PATH" command -v flock)" == "$SHIM_DIR/flock" ]] && ok "H setup: flock shim is first on PATH" || bad "H setup: flock shim not resolving via PATH"
  run_concurrency_case "flockshim" "PATH='$SHIM_DIR:$PATH'"
fi

# ── I. GNU-mktemp regression (live-VPS dry-run finding) ─────────────────────
# A real dry-run on the VPS (GNU mktemp, via util-linux/coreutils) hit:
#   mktemp: too few X's in template 'install-channel-patches-boot-rearm'
#   ./scripts/install-channel-patches.sh: line 200: : No such file or directory
#   tail: cannot open '' for reading: No such file or directory
#   [install-channel-patches] boot_rearm: FAILED (rc=1)
# on a plugin that WAS already correctly patched — a false failure caused by
# `mktemp -t install-channel-patches-boot-rearm` (no XXXXXX): BSD mktemp
# (macOS, this dev box) treats `-t PREFIX` as a prefix and appends its own
# random suffix — no error — which is exactly why the 44 tests run on a Mac
# never caught this. GNU mktemp requires the template argument itself to end
# in a run of X's and errors loudly on a bare prefix. The fix drops `-t`
# entirely in favor of an explicit "$TMPDIR/name.XXXXXX" template, which both
# implementations handle identically.
#
# This section proves BOTH halves: (1) the shim below faithfully reproduces
# GNU mktemp's stricter behavior (so it would have caught the ORIGINAL bug —
# without this we can't trust it proves anything), and (2) the CURRENT
# (fixed) script produces a clean no-op (rc=0 even under --strict, not just
# fail-open masking a real failure) when boot_rearm+bubble-inject are BOTH
# already applied, under that same strict mktemp.
echo "I. GNU-mktemp regression (board #956 live-VPS dry-run finding)"
GNU_MKTEMP_SHIM_DIR="$WORK/gnu-mktemp-shim"; mkdir -p "$GNU_MKTEMP_SHIM_DIR"
cat > "$GNU_MKTEMP_SHIM_DIR/mktemp" <<'SHIM'
#!/usr/bin/env python3
# Minimal GNU-mktemp work-alike, just strict enough to reproduce the ONE
# behavioral difference this test cares about: `-t TEMPLATE` REQUIRES
# TEMPLATE to end in a run of X's (GNU errors "too few X's in template" on a
# bare prefix, unlike BSD's -t which silently appends its own suffix).
import os, re, sys, tempfile

args = sys.argv[1:]
directory = False
template = None
use_dash_t = False
i = 0
while i < len(args):
    a = args[i]
    if a == '-d':
        directory = True
    elif a == '-t':
        use_dash_t = True
    elif not a.startswith('-'):
        template = a
    i += 1

if use_dash_t:
    if not template or not re.search(r'X{3,}$', template):
        sys.stderr.write(f"mktemp: too few X's in template '{template}'\n")
        sys.exit(1)
    template = os.path.join(os.environ.get('TMPDIR', '/tmp'), template)

if template:
    if not re.search(r'X{3,}$', template):
        sys.stderr.write(f"mktemp: too few X's in template '{template}'\n")
        sys.exit(1)
    if directory:
        print(tempfile.mkdtemp(prefix=re.sub(r'X+$', '', os.path.basename(template)) or 'tmp',
                                dir=os.path.dirname(template) or None))
    else:
        fd, path = tempfile.mkstemp(prefix=re.sub(r'X+$', '', os.path.basename(template)) or 'tmp',
                                     dir=os.path.dirname(template) or None)
        os.close(fd)
        print(path)
else:
    if directory:
        print(tempfile.mkdtemp())
    else:
        fd, path = tempfile.mkstemp()
        os.close(fd)
        print(path)
SHIM
chmod +x "$GNU_MKTEMP_SHIM_DIR/mktemp"
[[ "$(PATH="$GNU_MKTEMP_SHIM_DIR:$PATH" command -v mktemp)" == "$GNU_MKTEMP_SHIM_DIR/mktemp" ]] \
  && ok "I setup: GNU-mktemp shim is first on PATH" || bad "I setup: GNU-mktemp shim not resolving via PATH"

# I1: prove the shim is FAITHFUL — it must reproduce the exact original error
# for the exact original (buggy) invocation pattern, or this test proves nothing.
SHIM_REPRO_OUT="$(PATH="$GNU_MKTEMP_SHIM_DIR:$PATH" mktemp -t install-channel-patches-boot-rearm 2>&1)"
SHIM_REPRO_RC=$?
[[ "$SHIM_REPRO_RC" != "0" ]] && ok "I1: shim reproduces GNU mktemp's failure on the ORIGINAL buggy template" \
  || bad "I1: shim did NOT fail on the buggy template — shim is not faithful, rest of section I is untrustworthy"
echo "$SHIM_REPRO_OUT" | grep -qi "too few X" && ok "I1b: shim's error message matches the real GNU mktemp wording" || bad "I1b: shim error message doesn't match"

# I2: build a fixture with BOTH patches already applied (using the real host
# mktemp — this setup step, not the thing under test).
ROOT_I="$WORK/i"; TGT_I="$(make_fixture "$ROOT_I")"
GLOB_I="$ROOT_I/claude-plugins-official/telegram/*/"
run_installer "$GLOB_I"   # first pass applies both patches (sanity: uses host mktemp)
[[ "$RC" == "0" ]] || { echo "FATAL: could not build the fully-patched fixture for section I (rc=$RC)"; exit 2; }
[[ "$(grep -c bootRearmNotification "$TGT_I/server.ts")" == "2" ]] || { echo "FATAL: fixture for I is missing boot_rearm"; exit 2; }
grep -q "bubble-inject" "$TGT_I/server.ts" || { echo "FATAL: fixture for I is missing bubble-inject"; exit 2; }

# I3: THE actual regression check — re-run against the fully-patched fixture
# with the GNU-mktemp shim first on PATH, under --strict so a masked (fail-open)
# failure can't hide as a false rc=0. This is the exact scenario from the live
# VPS dry-run: both patches already present, installer should cleanly no-op.
CONC_OUT="$(PATH="$GNU_MKTEMP_SHIM_DIR:$PATH" CHANNEL_PATCHES_PLUGIN_GLOB="$GLOB_I" CHANNEL_PATCHES_BUN="$BUN_BIN" \
  bash "$INSTALLER" --strict 2>&1)"
CONC_RC=$?
[[ "$VERBOSE" == "1" ]] && { echo "---- section I installer output (under GNU-mktemp shim) ----"; echo "$CONC_OUT"; echo "-------------------------------------------------------------"; }
[[ "$CONC_RC" == "0" ]] && ok "I3: --strict exits 0 under GNU mktemp on an already-fully-patched plugin (TRUE clean no-op, not fail-open masking)" \
  || bad "I3: --strict exit was $CONC_RC — boot_rearm regression is back (or a new mktemp issue)"
echo "$CONC_OUT" | grep -q "boot_rearm: OK" && ok "I3b: boot_rearm reports OK (not FAILED)" || bad "I3b: boot_rearm did not report OK — see output"
# Matches either failure wording: "FAILED" (install-boot-rearm.sh itself
# errored) or "mktemp failed" (the defensive guard added alongside the fix,
# which turns a future mktemp breakage into a clear message instead of the
# original confusing cascade — "tail: cannot open '' " etc.). Either one means
# the same regression.
echo "$CONC_OUT" | grep -qiE "boot_rearm: FAILED|boot_rearm: mktemp failed" \
  && bad "I3c: boot_rearm reports a failure — the live-VPS bug (or its defensive guard) fired" \
  || ok "I3c: no boot_rearm failure/mktemp-guard line"
echo "$CONC_OUT" | grep -q "bubble-inject: already present — no-op" && ok "I3d: bubble-inject still reports its own clean no-op" || bad "I3d: bubble-inject no-op line missing"
[[ "$(grep -c bootRearmNotification "$TGT_I/server.ts")" == "2" ]] && ok "I3e: boot_rearm wiring unchanged (still exactly once)" || bad "I3e: boot_rearm wiring was touched/corrupted"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
