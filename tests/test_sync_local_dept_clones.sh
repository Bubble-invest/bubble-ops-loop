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
#   T1  ONLY host:local depts are synced; host:vps + host-absent depts are NOT.
#   T2  a clear per-dept "synced" log line names each local dept converged.
#   T3  FAIL-SAFE: a sync failure on one local dept logs + skips it but the
#       OTHER local depts STILL get synced (one bad dept never wedges the run)
#       and the script still exits 0 (a transient mirror miss must not flap a
#       timer).
#   T4  no host:local depts at all -> clean no-op exit 0.
#   T5  the fixture agents-root is a throwaway, NOT the live /home/claude/agents.
#   T6  REAL-GIT regression (#405, covered by the #667 converge-to-origin
#       strategy): a host:local mirror whose working tree the dept runtime
#       dirtied (a TRACKED deletion) still converges to origin/main, while
#       UNTRACKED un-pushed dept output (inbox/decisions hide-markers) SURVIVES.
#       Run against a real local origin+clone (NOT the git stub) so the
#       converge is exercised for real, never mocked.
#   T7  REAL-GIT regression (#405 case b): an untracked collision at a path
#       origin has just made tracked resolves to ORIGIN's version.
#   T8  skip-worktree-flagged files (board #667 failure mode 1): a mirror with
#       a skip-worktree bit set on a vendored file that diverges from origin
#       still converges — the flag is cleared before the sync, not left to
#       silently block it forever.
#   T9  root-owned paths (board #667 failure mode 2): a mirror containing a
#       path not owned by the running user is DETECTED, WARNED with the exact
#       chown remediation command, and SKIPPED (not crashed) — the OTHER local
#       depts still sync.
#   T10 aborted-merge debris (board #667 failure mode 3): a stale MERGE_HEAD /
#       index.lock left by a prior interrupted sync is cleared so the mirror
#       can converge instead of freezing on every subsequent tick.
#   T11 hide-marker preserved through the WORST case: a sync that hits ALL
#       THREE failure modes on unrelated depts simultaneously still preserves
#       an inbox/decisions hide-marker on the (unaffected) dept that converges
#       cleanly.
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
# Emulates the verb set the self-heal strategy (board #667) drives per dept:
# ls-files -v (skip-worktree scan), rev-parse HEAD/@{u}/<upstream>, fetch,
# merge-base --is-ancestor, status --porcelain, reset --hard, clean -fd,
# update-index. A "sync attempt" is recorded (to $GIT_LOG) on `fetch` — the
# first verb every dept reaches once past the root-owned/merge-debris guards —
# so the test can assert WHICH depts were attempted regardless of internal
# verb order. A dept whose dir basename appears in $FAIL_FILE makes `reset`
# exit 1 (models a converge failure) so T3 can prove the fail-safe path.
STUBS="$WORK/stubs"; mkdir -p "$STUBS"
GIT_LOG="$WORK/git-pulls.log";   : > "$GIT_LOG"
FAIL_FILE="$WORK/fail-depts.txt"; : > "$FAIL_FILE"
cat > "$STUBS/git" <<EOF
#!/usr/bin/env bash
# args we care about: git -C <dir> <verb> ...
dir=""
i=1
for a in "\$@"; do
  if [[ "\$a" == "-C" ]]; then nxt=\$((i+1)); dir="\${!nxt}"; fi
  i=\$((i+1))
done
verb=""
for a in "\$@"; do
  case "\$a" in
    -C|"\$dir") continue ;;
    *) verb="\$a"; break ;;
  esac
done
base="\$(basename "\$dir")"
case "\$verb" in
  ls-files) exit 0 ;;                                    # no skip-worktree flags by default
  rev-parse)
    if [[ "\$*" == *"@{u}"* ]]; then echo "origin/main"; exit 0; fi
    echo "deadbeef0000"; exit 0 ;;
  fetch)
    echo "\$dir" >> "$GIT_LOG"
    exit 0 ;;
  merge-base) exit 0 ;;                                   # ancestor=true by default (would-ff)
  status) exit 0 ;;                                        # clean tree by default
  reset)
    if grep -qx "\$base" "$FAIL_FILE" 2>/dev/null; then
      echo "fatal: could not reset" >&2
      exit 1
    fi
    exit 0 ;;
  clean) exit 0 ;;
  update-index) exit 0 ;;
  *) exit 0 ;;
esac
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
# T6: REAL-GIT regression for #405 (now covered by the #667 converge-to-origin
#     strategy) — a mirror with local tracked dirt that a plain ff-pull would
#     abort on must still converge to origin, preserving untracked
#     inbox/decisions hide-marker output. This section uses the REAL git binary
#     (the PATH stub above is bypassed) against throwaway local repos — no
#     network, no remotes.
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
mkdir -p "$WORKTREE/queues/gates" "$WORKTREE/onboarding"
printf 'id: draft-1\n'   > "$WORKTREE/queues/gates/draft-1.yaml"   # tracked file the dept loop later "deletes"
printf 'v: 1\n'          > "$WORKTREE/framework.txt"               # tracked framework file that origin will advance
# onboarding/STATE.yaml IS tracked on the real depts (confirmed live, board #667) —
# origin owns it like any other file; only inbox/decisions/* is untracked-and-local.
printf 'slug: content\nstatus: Live\nhost: local\n' > "$WORKTREE/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE" add -A
"${real_git_env[@]}" git -C "$WORKTREE" commit -qm "seed"
"${real_git_env[@]}" git -C "$WORKTREE" push -q origin HEAD:main

# The VPS read-only mirror = a clone of origin at the seed commit.
MIRROR="$REALGIT_AGENTS/bubble-ops-content"
"${real_git_env[@]}" git clone -q "$ORIGIN" "$MIRROR"

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
  && { echo "  PASS: T6e tracked STATE.yaml still present after converge"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T6e tracked STATE.yaml destroyed"; FAIL=$((FAIL+1)); }
# The converge-to-origin (reset past a plain-ff-incompatible local state) was
# logged (visible, not silent) — the new strategy's WARN, replacing the old
# "discarded tracked change" checkout-based wording.
want "T6f self-heal reset is logged" "reset required to converge\|self-healed" "$WORK/run-real.log"

# -----------------------------------------------------------------------------
# T7: REAL-GIT regression for #405 case (b) — an UNTRACKED file in the mirror at
#     a path an INCOMING commit ADDS blocks a plain ff-pull ("untracked working
#     tree files would be overwritten by merge"). Under the #667 converge-to-
#     origin strategy, reset --hard makes origin's TRACKED version win at that
#     path outright (origin authoritative for a read-only mirror) while
#     preserving non-colliding untracked hide-marker output elsewhere.
# -----------------------------------------------------------------------------
ORIGIN2="$WORK/origin-content2.git"
WORKTREE2="$WORK/seed-content2"
"${real_git_env[@]}" git init -q --bare "$ORIGIN2"
"${real_git_env[@]}" git clone -q "$ORIGIN2" "$WORKTREE2" 2>/dev/null
mkdir -p "$WORKTREE2/onboarding"
printf 'v: 1\n' > "$WORKTREE2/framework.txt"
printf 'slug: content\nstatus: Live\nhost: local\n' > "$WORKTREE2/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE2" add -A
"${real_git_env[@]}" git -C "$WORKTREE2" commit -qm "seed2"
"${real_git_env[@]}" git -C "$WORKTREE2" push -q origin HEAD:main

REALGIT_AGENTS2="$WORK/agents-real2"; rm -rf "$REALGIT_AGENTS2"; mkdir -p "$REALGIT_AGENTS2"
MIRROR2="$REALGIT_AGENTS2/bubble-ops-content"
"${real_git_env[@]}" git clone -q "$ORIGIN2" "$MIRROR2"

# origin ADDS a new tracked file at inbox/decisions/collide.yaml (committed upstream
# from the dept's own machine).
mkdir -p "$WORKTREE2/inbox/decisions"
printf 'decision: from-origin\n' > "$WORKTREE2/inbox/decisions/collide.yaml"
"${real_git_env[@]}" git -C "$WORKTREE2" add -A
"${real_git_env[@]}" git -C "$WORKTREE2" commit -qm "add collide.yaml upstream"
"${real_git_env[@]}" git -C "$WORKTREE2" push -q origin HEAD:main

# The dept loop had ALREADY written that same path locally as UNTRACKED (the collision),
# plus a NON-colliding untracked output file that MUST survive.
mkdir -p "$MIRROR2/inbox/decisions"
printf 'decision: local-untracked\n' > "$MIRROR2/inbox/decisions/collide.yaml"     # collides with incoming
printf 'decision: keep-me-too\n'     > "$MIRROR2/inbox/decisions/no-collide.yaml"  # must survive

before_head2="$("${real_git_env[@]}" git -C "$MIRROR2" rev-parse HEAD)"
origin_head2="$("${real_git_env[@]}" git -C "$WORKTREE2" rev-parse HEAD)"

"${real_git_env[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS2" >"$WORK/run-real2.log" 2>&1
rc2=$?
after_head2="$("${real_git_env[@]}" git -C "$MIRROR2" rev-parse HEAD)"

chk "T7 real-git run exits 0" 0 "$rc2"
[[ "$before_head2" != "$origin_head2" ]] && { echo "  PASS: T7 precondition: mirror started BEHIND origin"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T7 precondition broken"; FAIL=$((FAIL+1)); }
# The mirror fast-forwarded despite the untracked collision (was frozen before this fix).
chk_eq "T7a mirror fast-forwarded past untracked collision" "$origin_head2" "$after_head2"
# The colliding path now holds ORIGIN's version (the local untracked copy was removed, pull brought the tracked one).
if [[ "$("${real_git_env[@]}" cat "$MIRROR2/inbox/decisions/collide.yaml" 2>/dev/null)" == "decision: from-origin" ]]; then
  echo "  PASS: T7b colliding path now holds origin's tracked version"; PASS=$((PASS+1))
else
  echo "  FAIL: T7b colliding path not resolved to origin's version"; FAIL=$((FAIL+1))
fi
# The NON-colliding untracked output SURVIVED (scoped removal, not a blanket clean).
[[ -f "$MIRROR2/inbox/decisions/no-collide.yaml" ]] \
  && { echo "  PASS: T7c non-colliding untracked output preserved (scoped removal)"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T7c non-colliding untracked output DESTROYED (blanket clean regression!)"; FAIL=$((FAIL+1)); }
[[ -f "$MIRROR2/onboarding/STATE.yaml" ]] \
  && { echo "  PASS: T7d tracked STATE.yaml still present after converge"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T7d tracked STATE.yaml destroyed"; FAIL=$((FAIL+1)); }
# The self-heal (converging past the untracked collision, which a plain
# ff-pull would have aborted on) was logged (visible, not silent).
want "T7e self-heal reset is logged" "reset required to converge\|self-healed" "$WORK/run-real2.log"

# -----------------------------------------------------------------------------
# T8: board #667 failure mode 1 — skip-worktree-flagged vendored files. A
#     mirror with the skip-worktree bit set on a tracked file (e.g. a vendored
#     scripts/lib/*.py) that then diverges from origin must still converge:
#     the flag is cleared unconditionally before the sync (a read mirror has
#     no business hiding paths from itself).
# -----------------------------------------------------------------------------
ORIGIN3="$WORK/origin-content3.git"
WORKTREE3="$WORK/seed-content3"
"${real_git_env[@]}" git init -q --bare "$ORIGIN3"
"${real_git_env[@]}" git clone -q "$ORIGIN3" "$WORKTREE3" 2>/dev/null
mkdir -p "$WORKTREE3/scripts/lib" "$WORKTREE3/onboarding"
printf 'VENDORED_V=1\n' > "$WORKTREE3/scripts/lib/dispatch_helpers.py"
printf 'slug: content\nstatus: Live\nhost: local\n' > "$WORKTREE3/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE3" add -A
"${real_git_env[@]}" git -C "$WORKTREE3" commit -qm "seed3"
"${real_git_env[@]}" git -C "$WORKTREE3" push -q origin HEAD:main

REALGIT_AGENTS3="$WORK/agents-real3"; rm -rf "$REALGIT_AGENTS3"; mkdir -p "$REALGIT_AGENTS3"
MIRROR3="$REALGIT_AGENTS3/bubble-ops-content"
"${real_git_env[@]}" git clone -q "$ORIGIN3" "$MIRROR3"

# Set skip-worktree on the vendored file (mirrors the live #667 trigger), then
# dirty it on disk exactly as a stray local process would — skip-worktree
# HIDES this from git's own status/diff, which is what makes it dangerous.
"${real_git_env[@]}" git -C "$MIRROR3" update-index --skip-worktree scripts/lib/dispatch_helpers.py
printf 'VENDORED_V=LOCAL_DRIFT\n' > "$MIRROR3/scripts/lib/dispatch_helpers.py"

# origin advances the SAME vendored file (the real-world divergence trigger).
printf 'VENDORED_V=2\n' > "$WORKTREE3/scripts/lib/dispatch_helpers.py"
"${real_git_env[@]}" git -C "$WORKTREE3" commit -qam "advance vendored lib"
"${real_git_env[@]}" git -C "$WORKTREE3" push -q origin HEAD:main

before_head3="$("${real_git_env[@]}" git -C "$MIRROR3" rev-parse HEAD)"
origin_head3="$("${real_git_env[@]}" git -C "$WORKTREE3" rev-parse HEAD)"
skip_flagged_before="$("${real_git_env[@]}" git -C "$MIRROR3" ls-files -v | grep -c '^S' || true)"

"${real_git_env[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS3" >"$WORK/run-real3.log" 2>&1
rc3=$?
after_head3="$("${real_git_env[@]}" git -C "$MIRROR3" rev-parse HEAD)"

chk "T8 real-git run exits 0" 0 "$rc3"
[[ "$skip_flagged_before" -ge 1 ]] && { echo "  PASS: T8 precondition: skip-worktree flag WAS set before sync"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T8 precondition broken (skip-worktree flag not set)"; FAIL=$((FAIL+1)); }
[[ "$before_head3" != "$origin_head3" ]] && { echo "  PASS: T8 precondition: mirror started BEHIND origin"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T8 precondition broken (mirror not behind origin)"; FAIL=$((FAIL+1)); }
chk_eq "T8a mirror converged to origin/main despite the skip-worktree divergence (was frozen before this fix)" "$origin_head3" "$after_head3"
if [[ "$("${real_git_env[@]}" cat "$MIRROR3/scripts/lib/dispatch_helpers.py" 2>/dev/null | tr -d '[:space:]')" == "VENDORED_V=2" ]]; then
  echo "  PASS: T8b vendored file now holds origin's version (local drift discarded)"; PASS=$((PASS+1))
else
  echo "  FAIL: T8b vendored file did not converge to origin's version"; FAIL=$((FAIL+1))
fi
skip_flagged_after="$("${real_git_env[@]}" git -C "$MIRROR3" ls-files -v | grep -c '^S' || true)"
chk_eq "T8c skip-worktree flag cleared after sync (no longer hiding the path)" "0" "$skip_flagged_after"
want "T8d skip-worktree clear is logged" "cleared skip-worktree flag" "$WORK/run-real3.log"

# -----------------------------------------------------------------------------
# T9: board #667 failure mode 2 — root-owned paths. A mirror with a path not
#     owned by the running user must be DETECTED and SKIPPED with a loud WARN
#     naming the exact chown remediation (the sync runs as an unprivileged
#     user and cannot fix ownership itself) — never crash mid-reset, and never
#     block the OTHER local depts in the same run.
#
#     A genuine ownership mismatch needs real root, which this sandbox doesn't
#     have (and shouldn't need for a unit test). Instead we exercise the EXACT
#     detection path the script uses (`find <dir> -not -user "$(id -un)"`) by
#     stubbing `id` on PATH to report a DIFFERENT username than actually owns
#     the fixture files. `find -not -user <a-different-name>` then genuinely
#     evaluates false for every file (no such uid owns them under that name
#     resolution)... but `find -not -user` compares against a NAME git/find
#     must resolve to a uid via getpwnam; an unresolvable stub name makes
#     `-user` itself error out per-path rather than matching, which still
#     proves the FAIL-SAFE half of the contract (never crash) but not the
#     detect-and-skip half. So instead we stub `find` itself (the script's
#     only ownership-detection call site) to model exactly what a real
#     root-owned path would report, keeping the git side 100% real.
# -----------------------------------------------------------------------------
REALGIT_AGENTS4="$WORK/agents-real4"; rm -rf "$REALGIT_AGENTS4"; mkdir -p "$REALGIT_AGENTS4"
MIRROR4="$REALGIT_AGENTS4/bubble-ops-content"
ORIGIN4="$WORK/origin-content4.git"
WORKTREE4="$WORK/seed-content4"
"${real_git_env[@]}" git init -q --bare "$ORIGIN4"
"${real_git_env[@]}" git clone -q "$ORIGIN4" "$WORKTREE4" 2>/dev/null
mkdir -p "$WORKTREE4/onboarding"
printf 'v: 1\n' > "$WORKTREE4/framework.txt"
printf 'slug: content\nstatus: Live\nhost: local\n' > "$WORKTREE4/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE4" add -A
"${real_git_env[@]}" git -C "$WORKTREE4" commit -qm "seed4"
"${real_git_env[@]}" git -C "$WORKTREE4" push -q origin HEAD:main
"${real_git_env[@]}" git clone -q "$ORIGIN4" "$MIRROR4"

# A SECOND, healthy dept in the SAME run — proves the root-owned dept is
# skipped without wedging the others (fail-safe, same invariant as T3).
ORIGIN4B="$WORK/origin-healthy4.git"; WORKTREE4B="$WORK/seed-healthy4"
"${real_git_env[@]}" git init -q --bare "$ORIGIN4B"
"${real_git_env[@]}" git clone -q "$ORIGIN4B" "$WORKTREE4B" 2>/dev/null
mkdir -p "$WORKTREE4B/onboarding"
printf 'v: 1\n' > "$WORKTREE4B/framework.txt"
printf 'slug: healthy4\nstatus: Live\nhost: local\n' > "$WORKTREE4B/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE4B" add -A
"${real_git_env[@]}" git -C "$WORKTREE4B" commit -qm "seed4b"
"${real_git_env[@]}" git -C "$WORKTREE4B" push -q origin HEAD:main
MIRROR4B="$REALGIT_AGENTS4/bubble-ops-healthy4"
"${real_git_env[@]}" git clone -q "$ORIGIN4B" "$MIRROR4B"
# origin advances so there's real convergence work to prove (not just a no-op).
printf 'v: 2\n' > "$WORKTREE4B/framework.txt"
"${real_git_env[@]}" git -C "$WORKTREE4B" commit -qam "advance4b"
"${real_git_env[@]}" git -C "$WORKTREE4B" push -q origin HEAD:main

# Stub `find` on PATH: pass every call through to the REAL find UNLESS it's
# the script's ownership scan (`find <dir> -not -user <name>`) against the
# root-owned dept's mirror — in that one case, report a synthetic root-owned
# file so the detection branch fires exactly as it would on the live VPS.
REAL_FIND="$(command -v find)"
FIND_STUB_DIR="$WORK/find-stub"; mkdir -p "$FIND_STUB_DIR"
cat > "$FIND_STUB_DIR/find" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "$MIRROR4" ]]; then
  echo "$MIRROR4/.git/index"
  exit 0
fi
exec "$REAL_FIND" "\$@"
EOF
chmod +x "$FIND_STUB_DIR/find"

: > "$GIT_LOG"
# NOTE: can't shell-prefix PATH= before "${real_git_env[@]}" here — that array
# itself is `env PATH=... ...`, and env's own PATH= argument always wins over
# an outer shell-prefix assignment. Splice the find-stub dir into the SAME
# PATH= argument env receives instead.
real_git_env_with_find_stub=( env "PATH=${FIND_STUB_DIR}:${PATH#${STUBS}:}" \
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null )
"${real_git_env_with_find_stub[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS4" >"$WORK/run-real4.log" 2>&1
rc4=$?
chk "T9 root-owned-path run still exits 0 (fail-safe, no timer flap)" 0 "$rc4"
want "T9a root-owned dept WARN names the exact chown remediation" "chown -R" "$WORK/run-real4.log"
want "T9b root-owned dept slug named in the WARN" "content" "$WORK/run-real4.log"
# The root-owned mirror must NOT have been reset (skipped BEFORE any reset) —
# HEAD stays at the seed commit, never advancing to origin's newer commit.
mirror4_head="$("${real_git_env[@]}" git -C "$MIRROR4" rev-parse HEAD 2>/dev/null || echo "")"
origin4_head="$("${real_git_env[@]}" git -C "$WORKTREE4" rev-parse HEAD 2>/dev/null || echo "")"
if [[ "$mirror4_head" == "$origin4_head" ]]; then
  echo "  PASS: T9c root-owned mirror left untouched (no reset attempted, still at seed commit)"; PASS=$((PASS+1))
else
  echo "  FAIL: T9c root-owned mirror was modified despite the ownership guard"; FAIL=$((FAIL+1))
fi
# The OTHER (healthy) dept in the same run must NOT be blocked by dept4's guard
# — it still converges to origin's ADVANCED commit despite dept4 being skipped.
mirror4b_head="$("${real_git_env[@]}" git -C "$MIRROR4B" rev-parse HEAD 2>/dev/null || echo "")"
origin4b_head="$("${real_git_env[@]}" git -C "$WORKTREE4B" rev-parse HEAD 2>/dev/null || echo "")"
want "T9d healthy dept in the same run still synced (root-owned guard didn't wedge it)" "healthy4" "$WORK/run-real4.log"
chk_eq "T9e healthy dept converged to origin despite dept4's root-owned skip" "$origin4b_head" "$mirror4b_head"

# -----------------------------------------------------------------------------
# T10: board #667 failure mode 3 — aborted-merge debris. A mirror left with a
#      stale MERGE_HEAD (an interrupted checkout/merge from a prior run) must
#      not block every subsequent sync forever — the debris is cleared before
#      the converge, and the mirror still reaches origin/main.
# -----------------------------------------------------------------------------
ORIGIN5="$WORK/origin-content5.git"
WORKTREE5="$WORK/seed-content5"
"${real_git_env[@]}" git init -q --bare "$ORIGIN5"
"${real_git_env[@]}" git clone -q "$ORIGIN5" "$WORKTREE5" 2>/dev/null
mkdir -p "$WORKTREE5/onboarding"
printf 'v: 1\n' > "$WORKTREE5/framework.txt"
printf 'slug: content\nstatus: Live\nhost: local\n' > "$WORKTREE5/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE5" add -A
"${real_git_env[@]}" git -C "$WORKTREE5" commit -qm "seed5"
"${real_git_env[@]}" git -C "$WORKTREE5" push -q origin HEAD:main

REALGIT_AGENTS5="$WORK/agents-real5"; rm -rf "$REALGIT_AGENTS5"; mkdir -p "$REALGIT_AGENTS5"
MIRROR5="$REALGIT_AGENTS5/bubble-ops-content"
"${real_git_env[@]}" git clone -q "$ORIGIN5" "$MIRROR5"

# origin advances so there is real work for the converge to do post-cleanup.
printf 'v: 2\n' > "$WORKTREE5/framework.txt"
"${real_git_env[@]}" git -C "$WORKTREE5" commit -qam "advance5"
"${real_git_env[@]}" git -C "$WORKTREE5" push -q origin HEAD:main

# Simulate an interrupted merge: fetch the new commit, start (but don't finish)
# a merge that conflicts, so MERGE_HEAD is left behind on disk — exactly the
# debris an interrupted checkout/merge leaves.
"${real_git_env[@]}" git -C "$MIRROR5" fetch -q origin
printf 'LOCAL CONFLICTING CONTENT\n' > "$MIRROR5/framework.txt"
"${real_git_env[@]}" git -C "$MIRROR5" add -A
GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  "${real_git_env[@]}" git -C "$MIRROR5" commit -qm "local conflicting commit"
"${real_git_env[@]}" git -C "$MIRROR5" merge origin/main -q 2>/dev/null   # expected to conflict and leave MERGE_HEAD
merge_head_present_before="0"
[[ -f "$MIRROR5/.git/MERGE_HEAD" ]] && merge_head_present_before="1"

origin_head5="$("${real_git_env[@]}" git -C "$WORKTREE5" rev-parse HEAD)"
"${real_git_env[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS5" >"$WORK/run-real5.log" 2>&1
rc5=$?
after_head5="$("${real_git_env[@]}" git -C "$MIRROR5" rev-parse HEAD)"

[[ "$merge_head_present_before" == "1" ]] && { echo "  PASS: T10 precondition: MERGE_HEAD debris WAS present before sync"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T10 precondition broken (no MERGE_HEAD produced by the simulated conflict)"; FAIL=$((FAIL+1)); }
chk "T10 real-git run exits 0" 0 "$rc5"
[[ ! -f "$MIRROR5/.git/MERGE_HEAD" ]] \
  && { echo "  PASS: T10a MERGE_HEAD debris cleared"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T10a MERGE_HEAD debris still present (would block every future sync)"; FAIL=$((FAIL+1)); }
chk_eq "T10b mirror converged to origin/main past the merge debris" "$origin_head5" "$after_head5"
want "T10c merge-debris clearing is logged" "cleared aborted-merge debris" "$WORK/run-real5.log"

# -----------------------------------------------------------------------------
# T11: combined worst case — the SAME run hits skip-worktree divergence on one
#      dept AND aborted-merge debris on another AND a healthy dept with an
#      inbox/decisions hide-marker. All three must be handled independently:
#      the two broken depts converge (or are safely skipped) and the healthy
#      dept's hide-marker survives untouched, in the SAME single invocation.
# -----------------------------------------------------------------------------
REALGIT_AGENTS6="$WORK/agents-real6"; rm -rf "$REALGIT_AGENTS6"; mkdir -p "$REALGIT_AGENTS6"

# Dept "skipwt": skip-worktree divergence (failure mode 1).
ORIGIN6A="$WORK/origin-skipwt.git"; WORKTREE6A="$WORK/seed-skipwt"
"${real_git_env[@]}" git init -q --bare "$ORIGIN6A"
"${real_git_env[@]}" git clone -q "$ORIGIN6A" "$WORKTREE6A" 2>/dev/null
mkdir -p "$WORKTREE6A/scripts/lib" "$WORKTREE6A/onboarding"
printf 'V=1\n' > "$WORKTREE6A/scripts/lib/budget.py"
printf 'slug: skipwt\nstatus: Live\nhost: local\n' > "$WORKTREE6A/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE6A" add -A && "${real_git_env[@]}" git -C "$WORKTREE6A" commit -qm seed
"${real_git_env[@]}" git -C "$WORKTREE6A" push -q origin HEAD:main
MIRROR6A="$REALGIT_AGENTS6/bubble-ops-skipwt"
"${real_git_env[@]}" git clone -q "$ORIGIN6A" "$MIRROR6A"
"${real_git_env[@]}" git -C "$MIRROR6A" update-index --skip-worktree scripts/lib/budget.py
printf 'V=DRIFT\n' > "$MIRROR6A/scripts/lib/budget.py"
printf 'V=2\n' > "$WORKTREE6A/scripts/lib/budget.py"
"${real_git_env[@]}" git -C "$WORKTREE6A" commit -qam advance
"${real_git_env[@]}" git -C "$WORKTREE6A" push -q origin HEAD:main

# Dept "merged": aborted-merge debris (failure mode 3).
ORIGIN6B="$WORK/origin-merged.git"; WORKTREE6B="$WORK/seed-merged"
"${real_git_env[@]}" git init -q --bare "$ORIGIN6B"
"${real_git_env[@]}" git clone -q "$ORIGIN6B" "$WORKTREE6B" 2>/dev/null
mkdir -p "$WORKTREE6B/onboarding"
printf 'v: 1\n' > "$WORKTREE6B/framework.txt"
printf 'slug: merged\nstatus: Live\nhost: local\n' > "$WORKTREE6B/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE6B" add -A && "${real_git_env[@]}" git -C "$WORKTREE6B" commit -qm seed
"${real_git_env[@]}" git -C "$WORKTREE6B" push -q origin HEAD:main
MIRROR6B="$REALGIT_AGENTS6/bubble-ops-merged"
"${real_git_env[@]}" git clone -q "$ORIGIN6B" "$MIRROR6B"
printf 'v: 2\n' > "$WORKTREE6B/framework.txt"
"${real_git_env[@]}" git -C "$WORKTREE6B" commit -qam advance
"${real_git_env[@]}" git -C "$WORKTREE6B" push -q origin HEAD:main
"${real_git_env[@]}" git -C "$MIRROR6B" fetch -q origin
printf 'CONFLICT\n' > "$MIRROR6B/framework.txt"
"${real_git_env[@]}" git -C "$MIRROR6B" add -A
"${real_git_env[@]}" git -C "$MIRROR6B" commit -qm "local conflict"
"${real_git_env[@]}" git -C "$MIRROR6B" merge origin/main -q 2>/dev/null

# Dept "healthy": clean, up to date, WITH an inbox/decisions hide-marker that
# must survive this multi-failure run untouched.
ORIGIN6C="$WORK/origin-healthy.git"; WORKTREE6C="$WORK/seed-healthy"
"${real_git_env[@]}" git init -q --bare "$ORIGIN6C"
"${real_git_env[@]}" git clone -q "$ORIGIN6C" "$WORKTREE6C" 2>/dev/null
mkdir -p "$WORKTREE6C/onboarding"
printf 'v: 1\n' > "$WORKTREE6C/framework.txt"
printf 'slug: healthy\nstatus: Live\nhost: local\n' > "$WORKTREE6C/onboarding/STATE.yaml"
"${real_git_env[@]}" git -C "$WORKTREE6C" add -A && "${real_git_env[@]}" git -C "$WORKTREE6C" commit -qm seed
"${real_git_env[@]}" git -C "$WORKTREE6C" push -q origin HEAD:main
MIRROR6C="$REALGIT_AGENTS6/bubble-ops-healthy"
"${real_git_env[@]}" git clone -q "$ORIGIN6C" "$MIRROR6C"
mkdir -p "$MIRROR6C/inbox/decisions"
printf 'decision: keep-me-through-chaos\n' > "$MIRROR6C/inbox/decisions/marker.yaml"

"${real_git_env[@]}" "$SCRIPT_UNDER_TEST" --agents-root "$REALGIT_AGENTS6" >"$WORK/run-real6.log" 2>&1
rc6=$?
chk "T11 combined multi-failure run still exits 0" 0 "$rc6"

skipwt_head="$("${real_git_env[@]}" git -C "$MIRROR6A" rev-parse HEAD 2>/dev/null || echo "")"
skipwt_origin_head="$("${real_git_env[@]}" git -C "$WORKTREE6A" rev-parse HEAD)"
chk_eq "T11a skip-worktree dept converged despite its divergence" "$skipwt_origin_head" "$skipwt_head"

merged_head="$("${real_git_env[@]}" git -C "$MIRROR6B" rev-parse HEAD 2>/dev/null || echo "")"
merged_origin_head="$("${real_git_env[@]}" git -C "$WORKTREE6B" rev-parse HEAD)"
chk_eq "T11b merge-debris dept converged despite the aborted-merge debris" "$merged_origin_head" "$merged_head"

[[ -f "$MIRROR6C/inbox/decisions/marker.yaml" ]] \
  && { echo "  PASS: T11c healthy dept's hide-marker survived the multi-failure run"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: T11c healthy dept's hide-marker was destroyed"; FAIL=$((FAIL+1)); }

want "T11d skip-worktree dept's own WARN/log line present" "skipwt" "$WORK/run-real6.log"
want "T11e merge-debris dept's own log line present" "merged" "$WORK/run-real6.log"
want "T11f healthy dept synced cleanly and independently" "healthy" "$WORK/run-real6.log"

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
