#!/usr/bin/env bash
# =============================================================================
# test_sync_local_dept_clones.sh — TDD harness for scripts/sync-local-dept-clones.sh
#
# B3 (Hybrid local/VPS agent): the VPS keeps READ-ONLY clones of host:local
# depts (e.g. Miranda's bubble-ops-content on {{OPERATOR_2}}'s Mac) fresh from GitHub so
# the disk-mode cockpit renders their latest gates/state/heartbeat. The dept
# NEVER executes on the VPS — only its files are mirrored via `git pull --ff-only`.
#
# Runs entirely against THROWAWAY fixtures under a mktemp dir. NEVER touches the
# real dept repos (/home/claude/agents/bubble-ops-*) — every invocation passes
# an explicit --agents-root pointing at the fixture tree. `git` is STUBBED via a
# PATH shim (no real remotes / network) so pull success/failure is deterministic.
#
# Assertions:
#   T1  ONLY host:local depts are pulled; host:vps + host-absent depts are NOT.
#   T2  a clear per-dept "pulled" log line names each local dept synced.
#   T3  FAIL-SAFE: a pull failure on one local dept logs + skips it but the OTHER
#       local depts STILL get pulled (one bad dept never wedges the run) and the
#       script still exits 0 (a transient mirror miss must not flap a timer).
#   T4  no host:local depts at all -> clean no-op exit 0.
#   T5  the fixture agents-root is a throwaway, NOT the live /home/claude/agents.
# =============================================================================
set -uo pipefail

SCRIPT_UNDER_TEST="${1:?usage: test_sync_local_dept_clones.sh <path-to-sync-local-dept-clones.sh>}"
[[ -f "$SCRIPT_UNDER_TEST" ]] || { echo "FATAL: script not found: $SCRIPT_UNDER_TEST"; exit 2; }

PASS=0; FAIL=0
chk()    { if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi; }
chk_eq() { if [[ "$2" == "$3" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1)); fi; }
want()   { if grep -q "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2' in $3)"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -q "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2' in $3)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

WORK="$(mktemp -d /tmp/sync-local.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

AGENTS="$WORK/agents"; mkdir -p "$AGENTS"

# ── git stub ────────────────────────────────────────────────────────────────
# Emulates `git -C <dir> pull --ff-only`. Records every pull's target dir to
# $GIT_LOG so the test can assert WHICH depts were pulled. A dept whose dir
# basename appears in $FAIL_FILE makes the pull exit 1 (models a pull conflict /
# transient error) so T3 can prove the fail-safe path. Any other git subcommand
# (none expected) just succeeds.
STUBS="$WORK/stubs"; mkdir -p "$STUBS"
GIT_LOG="$WORK/git-pulls.log";   : > "$GIT_LOG"
FAIL_FILE="$WORK/fail-depts.txt"; : > "$FAIL_FILE"
cat > "$STUBS/git" <<EOF
#!/usr/bin/env bash
# args we care about: git -C <dir> pull --ff-only
dir=""
i=1
for a in "\$@"; do
  if [[ "\$a" == "-C" ]]; then nxt=\$((i+1)); dir="\${!nxt}"; fi
  i=\$((i+1))
done
# is this a pull?
for a in "\$@"; do [[ "\$a" == "pull" ]] && { is_pull=1; break; }; done
if [[ "\${is_pull:-0}" == "1" ]]; then
  echo "\$dir" >> "$GIT_LOG"
  base="\$(basename "\$dir")"
  if grep -qx "\$base" "$FAIL_FILE" 2>/dev/null; then
    echo "fatal: Not possible to fast-forward, aborting." >&2
    exit 1
  fi
  echo "Already up to date."
  exit 0
fi
exit 0
EOF
chmod +x "$STUBS/git"
export PATH="$STUBS:$PATH"

# ── fixtures ────────────────────────────────────────────────────────────────
make_dept() {  # make_dept <slug> <vps|local|none>   (none = no host field)
  local slug="$1" host="$2"
  local root="$AGENTS/bubble-ops-$slug"
  mkdir -p "$root/onboarding"
  # A bare .git marker so the script treats the clone as present (git is stubbed,
  # so a real repo isn't needed — only the .git existence check matters).
  mkdir -p "$root/.git"
  if [[ "$host" == "none" ]]; then
    printf 'slug: %s\nstatus: Live\n' "$slug" > "$root/onboarding/STATE.yaml"
  else
    printf 'slug: %s\nstatus: Live\nhost: %s\n' "$slug" "$host" > "$root/onboarding/STATE.yaml"
  fi
}

run() { "$SCRIPT_UNDER_TEST" --agents-root "$AGENTS" >"$WORK/run.log" 2>&1; echo $?; }

echo "== sync-local-dept-clones.sh tests =="

# -----------------------------------------------------------------------------
# T1 + T2: only host:local depts are pulled; vps + host-absent are NOT.
# -----------------------------------------------------------------------------
rm -rf "$AGENTS"; mkdir -p "$AGENTS"; : > "$GIT_LOG"; : > "$FAIL_FILE"
make_dept content  local   # Miranda — MUST pull
make_dept media    local   # another local dept — MUST pull
make_dept ben      vps     # vps — MUST NOT pull
make_dept legacy   none    # no host field → vps default — MUST NOT pull
rc="$(run)"
pulled="$(sed 's#.*/##' "$GIT_LOG" | sed 's/^bubble-ops-//' | sort | tr '\n' ' ')"
chk_eq "T1 only host:local depts pulled (content+media), NOT vps/absent" "content media " "$pulled"
chk "T1b run exits 0 when all pulls succeed" 0 "$rc"
want "T2 per-dept pulled line names content" "content" "$WORK/run.log"
want "T2 per-dept pulled line names media"   "media"   "$WORK/run.log"
nowant "T2b vps dept 'ben' is never mentioned as pulled" "pull.*ben\|ben.*pulled" "$GIT_LOG"

# -----------------------------------------------------------------------------
# T3: FAIL-SAFE — a pull failure on ONE local dept must not wedge the others;
#     the run still pulls the rest and exits 0.
# -----------------------------------------------------------------------------
rm -rf "$AGENTS"; mkdir -p "$AGENTS"; : > "$GIT_LOG"; : > "$FAIL_FILE"
make_dept content local
make_dept media   local
make_dept extra   local
echo "bubble-ops-media" > "$FAIL_FILE"   # media's pull will exit 1
rc="$(run)"
pulled="$(sed 's#.*/##' "$GIT_LOG" | sed 's/^bubble-ops-//' | sort | tr '\n' ' ')"
chk_eq "T3 all three local depts were ATTEMPTED (failure didn't abort the loop)" "content extra media " "$pulled"
chk "T3b a single pull failure still exits 0 (transient miss must not flap the timer)" 0 "$rc"
want "T3c the failed dept is logged as skipped/failed (not silent)" "media" "$WORK/run.log"
# the OTHER local depts must have been synced despite media failing
want "T3d content still pulled after media failed" "content" "$WORK/run.log"
want "T3e extra still pulled after media failed"   "extra"   "$WORK/run.log"

# -----------------------------------------------------------------------------
# T4: no host:local depts at all → clean no-op, exit 0.
# -----------------------------------------------------------------------------
rm -rf "$AGENTS"; mkdir -p "$AGENTS"; : > "$GIT_LOG"; : > "$FAIL_FILE"
make_dept ben    vps
make_dept legacy none
rc="$(run)"
chk "T4 no local depts → exit 0" 0 "$rc"
chk_eq "T4b nothing pulled when there are no local depts" "" "$(cat "$GIT_LOG")"

# -----------------------------------------------------------------------------
# T5: fixture safety — the agents-root is a throwaway, not the live one.
# -----------------------------------------------------------------------------
case "$AGENTS" in
  /home/claude/agents) echo "  FAIL: fixture pointed at LIVE agents root!"; FAIL=$((FAIL+1));;
  *) echo "  PASS: T5 agents-root is a throwaway fixture ($AGENTS)"; PASS=$((PASS+1));;
esac

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
