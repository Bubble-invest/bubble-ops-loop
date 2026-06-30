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
#   T6  REAL-GIT regression (#405): a host:local mirror whose working tree the
#       dept runtime dirtied (a TRACKED deletion) is cleaned before the ff-pull,
#       so the pull fast-forwards to origin/main — while UNTRACKED un-pushed dept
#       output SURVIVES. Run against a real local origin+clone (NOT the git stub)
#       so the cleanup + ff-pull are exercised for real, never mocked.
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

# -----------------------------------------------------------------------------
# T6: REAL-GIT regression for #405 — clean tracked dirt in a read-only mirror
#     before the ff-pull, preserve untracked output, and prove the pull actually
#     fast-forwards. This section uses the REAL git binary (the PATH stub above
#     is bypassed) against throwaway local repos — no network, no remotes.
# -----------------------------------------------------------------------------
REALGIT_AGENTS="$WORK/agents-real"; rm -rf "$REALGIT_AGENTS"; mkdir -p "$REALGIT_AGENTS"
# Run with the stub REMOVED from PATH and a clean git identity so commits work in CI.
real_git_env=( env "PATH=${PATH#${STUBS}:}" \
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null )

ORIGIN="$WORK/origin-content.git"
WORKTREE="$WORK/seed-content"
"${real_git_env[@]}" git init -q --bare "$ORIGIN"
"${real_git_env[@]}" git clone -q "$ORIGIN" "$WORKTREE" 2>/dev/null  # empty-repo clone warning is expected
mkdir -p "$WORKTREE/queues/gates"
printf 'id: draft-1\n'   > "$WORKTREE/queues/gates/draft-1.yaml"   # tracked file the dept loop later "deletes"
printf 'v: 1\n'          > "$WORKTREE/framework.txt"               # tracked framework file that origin will advance
"${real_git_env[@]}" git -C "$WORKTREE" add -A
"${real_git_env[@]}" git -C "$WORKTREE" commit -qm "seed"
"${real_git_env[@]}" git -C "$WORKTREE" push -q origin HEAD:main

# The VPS read-only mirror = a clone of origin at the seed commit.
MIRROR="$REALGIT_AGENTS/bubble-ops-content"
"${real_git_env[@]}" git clone -q "$ORIGIN" "$MIRROR"
mkdir -p "$MIRROR/onboarding"
printf 'slug: content\nstatus: Live\nhost: local\n' > "$MIRROR/onboarding/STATE.yaml"

# origin advances one commit (fresh framework code the mirror must fast-forward to).
printf 'v: 2\n' > "$WORKTREE/framework.txt"
"${real_git_env[@]}" git -C "$WORKTREE" commit -qam "advance framework"
"${real_git_env[@]}" git -C "$WORKTREE" push -q origin HEAD:main

# Simulate the dept runtime dirtying its supposedly-read-only mirror:
#   (a) a TRACKED deletion (the bug's trigger) — must be restored before pull.
rm -f "$MIRROR/queues/gates/draft-1.yaml"
#   (b) an UNTRACKED un-pushed output file — must SURVIVE the cleanup.
mkdir -p "$MIRROR/inbox/decisions"
printf 'decision: keep-me\n' > "$MIRROR/inbox/decisions/unpushed.yaml"

# onboarding/STATE.yaml is also untracked here (origin never had it) — it too must survive.
before_head="$("${real_git_env[@]}" git -C "$MIRROR" rev-parse HEAD)"
origin_head="$("${real_git_env[@]}" git -C "$WORKTREE" rev-parse HEAD)"

"${real_git_env[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS" >"$WORK/run-real.log" 2>&1
rc=$?
after_head="$("${real_git_env[@]}" git -C "$MIRROR" rev-parse HEAD)"

chk "T6 real-git run exits 0" 0 "$rc"
# The mirror must have fast-forwarded to origin's new commit (was frozen before the fix).
chk_eq "T6a mirror fast-forwarded to origin/main after cleanup" "$origin_head" "$after_head"
[[ "$before_head" != "$origin_head" ]] && echo "  PASS: T6 precondition: mirror started BEHIND origin" && PASS=$((PASS+1)) \
  || { echo "  FAIL: T6 precondition broken (mirror not behind origin)"; FAIL=$((FAIL+1)); }
# Fresh framework code actually arrived.
if [[ "$("${real_git_env[@]}" cat "$MIRROR/framework.txt" 2>/dev/null | tr -d '[:space:]')" == "v:2" ]]; then
  echo "  PASS: T6b fresh framework code pulled into mirror"; PASS=$((PASS+1))
else
  echo "  FAIL: T6b mirror did not receive fresh framework code"; FAIL=$((FAIL+1))
fi
# The tracked deletion was restored (file is back).
[[ -f "$MIRROR/queues/gates/draft-1.yaml" ]] \
  && { echo "  PASS: T6c tracked deletion restored before pull"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T6c tracked deletion NOT restored (pull would have aborted)"; FAIL=$((FAIL+1)); }
# The UNTRACKED un-pushed output SURVIVED the cleanup (the core guarantee).
[[ -f "$MIRROR/inbox/decisions/unpushed.yaml" ]] \
  && { echo "  PASS: T6d untracked un-pushed dept output preserved"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T6d untracked dept output was DESTROYED (regression!)"; FAIL=$((FAIL+1)); }
[[ -f "$MIRROR/onboarding/STATE.yaml" ]] \
  && { echo "  PASS: T6e untracked STATE.yaml preserved"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T6e untracked STATE.yaml destroyed"; FAIL=$((FAIL+1)); }
# The discard was logged (visible, not silent).
want "T6f discard of tracked changes is logged" "discarded .* local tracked change" "$WORK/run-real.log"

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
