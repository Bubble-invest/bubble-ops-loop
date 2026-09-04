#!/usr/bin/env bash
# =============================================================================
# test_1122_bubble_deploy_rollback_dataloss.sh — RED/GREEN fixture for board #1122.
#
# Bug: scripts/bubble-deploy.sh `sync_dept_ff` stashes a dirty dept tree before
# a ff-only merge, then POPS the stash back onto the ff'd HEAD. If the
# post-restart health check then fails, the rollback path does
# `git reset --hard $pre_sha` — which now wipes the just-restored (popped,
# no-longer-stashed) uncommitted work with NOTHING left to recover it from.
# `sync_repo_reset` (infra sync) has the same class of gap: it resets --hard
# origin/main with only an "ahead" check, no dirty-check/stash at all.
#
# T1 — sync_dept_ff: dirty dept tree + good ff + a merged commit that fails
#      the post-restart health check (bad python syntax in the entrypoint)
#      must NOT lose the dirty edit: it must be recoverable via `git stash`
#      after the rollback reset.
# T2 — sync_repo_reset (infra path, --infra-only): a dirty infra checkout
#      (should never happen, but must not be nuked) must survive a
#      reset --hard to origin/main, recoverable via `git stash`.
#
# Hermetic: real bare-origin + clone git fixtures under a mktemp dir; `sudo`
# and `systemctl` are PATH-stubbed (no root, no real systemd) — `sudo -u X cmd`
# just strips `-u X` and execs `cmd` for real, `systemctl is-active` always
# reports "active", `stop`/`start` are no-ops. HOME is redirected into the
# fixture so `git config --global --add safe.directory` never touches the
# real developer's ~/.gitconfig.
#
# Run:  bash tests/test_1122_bubble_deploy_rollback_dataloss.sh
#       bash tests/test_1122_bubble_deploy_rollback_dataloss.sh -v
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$REPO_ROOT/scripts/bubble-deploy.sh}"
[[ -f "$SCRIPT" ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── stubs ────────────────────────────────────────────────────────────────────
STUBDIR="$WORK/bin"; mkdir -p "$STUBDIR"

cat > "$STUBDIR/sudo" <<'EOF'
#!/usr/bin/env bash
# Test-only sudo shim: this harness doesn't run as root, so a real
# `sudo -u claude <cmd>` would fail/prompt. Strip a leading "-u <user>" and
# exec the real command directly under the current (test-runner) user.
args=("$@")
if [[ "${args[0]:-}" == "-u" ]]; then
    args=("${args[@]:2}")
fi
exec "${args[@]}"
EOF
chmod +x "$STUBDIR/sudo"

cat > "$STUBDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
# Test-only systemctl shim: no real units exist in this sandbox.
case "$1" in
    is-active) echo active; exit 0 ;;
    stop|start) exit 0 ;;
    *) exit 0 ;;
esac
EOF
chmod +x "$STUBDIR/systemctl"

FAKE_HOME="$WORK/home"; mkdir -p "$FAKE_HOME"

run_deploy() {
    # run_deploy <args...> — PATH-shimmed, isolated HOME, real bash.
    PATH="$STUBDIR:$PATH" HOME="$FAKE_HOME" \
        bash "$SCRIPT" "$@" >"$WORK/out" 2>"$WORK/err"
    RC=$?
    OUT="$(cat "$WORK/out")"; ERR="$(cat "$WORK/err")"
    if [[ $VERBOSE == 1 ]]; then echo "--- deploy stdout+stderr (rc=$RC) ---"; cat "$WORK/out" "$WORK/err"; echo "--- end ---"; fi
}

git_q() { git "$@" >/dev/null 2>&1; }

# =============================================================================
# T1 — sync_dept_ff rollback must not discard the just-restored stash
# =============================================================================
echo "T1: sync_dept_ff — dirty tree + good ff + failing health check"

AGENTS_ROOT="$WORK/agents"; mkdir -p "$AGENTS_ROOT"
INFRA_ORIGIN="$WORK/infra-origin.git"; INFRA_DIR="$WORK/infra"

# infra fixture: trivial, already-current, clean (so sync_repo_reset is a
# no-op success and T1 isolates the dept-ff path).
git init --bare -q "$INFRA_ORIGIN"
git clone -q "$INFRA_ORIGIN" "$WORK/infra-seed"
( cd "$WORK/infra-seed" && git config user.email t@t.test && git config user.name t \
  && echo infra > f.txt && git add f.txt && git commit -qm c1 && git push -q origin main )
git clone -q "$INFRA_ORIGIN" "$INFRA_DIR"

# dept origin: c1 (valid main.py) then c2 (BAD main.py — syntax error), pushed.
DEPT_ORIGIN="$WORK/dept-origin.git"
git init --bare -q "$DEPT_ORIGIN"
git clone -q "$DEPT_ORIGIN" "$WORK/dept-seed"
(
  cd "$WORK/dept-seed"
  git config user.email t@t.test; git config user.name t
  printf 'def ok():\n    pass\n' > main.py
  printf 'original-wip-baseline\n' > file2.txt
  git add -A; git commit -qm c1; git push -q origin main
)

# dept clone made HERE, at c1 (behind by 1 once c2 lands on origin below) —
# this is the live agent tree bubble-deploy will ff.
DEPT_DIR="$AGENTS_ROOT/bubble-ops-testdept"
git clone -q "$DEPT_ORIGIN" "$DEPT_DIR"

(
  cd "$WORK/dept-seed"
  # c2: the "merged commit" that will fail the post-deploy health check.
  printf 'def ok():\n    pass\n\ndef broken(:\n    pass\n' > main.py
  git add -A; git commit -qm "c2 (introduces a syntax error)"; git push -q origin main
)

(
  cd "$DEPT_DIR"
  git config user.email t@t.test; git config user.name t
  # Dirty the tree: a TRACKED-file edit, uncommitted — the "live agent WIP"
  # from the audit (Claudette had 43 uncommitted files at the time PR #265
  # was written).
  printf 'WIP-EDIT-MUST-SURVIVE\n' > file2.txt
)
PRE_SHA="$(git -C "$DEPT_DIR" rev-parse HEAD)"

BUBBLE_DEPLOY_INFRA_DIR="$INFRA_DIR" \
BUBBLE_DEPLOY_AGENTS_ROOT="$AGENTS_ROOT" \
BUBBLE_DEPLOY_DEPT_PREFIX="bubble-ops-" \
BUBBLE_DEPLOY_UNIT_PREFIX="ops-loop-" \
BUBBLE_DEPLOY_ENTRYPOINT="main.py" \
  run_deploy --dept testdept

FINAL_SHA="$(git -C "$DEPT_DIR" rev-parse HEAD)"
STASH_LIST="$(git -C "$DEPT_DIR" stash list 2>/dev/null || true)"
WORKING_FILE2="$(cat "$DEPT_DIR/file2.txt" 2>/dev/null || true)"

if [[ "$FINAL_SHA" != "$PRE_SHA" ]]; then
    bad "T1 setup sanity: expected rollback to land back on pre_sha ($PRE_SHA), got $FINAL_SHA"
else
    ok "T1 setup sanity: health check failed and rolled back to pre_sha as expected"
fi

# The core assertion: the WIP edit must be recoverable SOMEWHERE (working
# tree, or a stash) after the rollback. It must NOT simply be gone.
RECOVERABLE=0
[[ "$WORKING_FILE2" == "WIP-EDIT-MUST-SURVIVE" ]] && RECOVERABLE=1
if [[ -n "$STASH_LIST" ]]; then
    if git -C "$DEPT_DIR" stash show -p 2>/dev/null | grep -q "WIP-EDIT-MUST-SURVIVE"; then
        RECOVERABLE=1
    fi
fi

if [[ "$RECOVERABLE" == "1" ]]; then
    ok "T1 dirty WIP edit is recoverable after the health-check rollback (stash or worktree)"
else
    bad "T1 DATA LOSS: dirty WIP edit ('WIP-EDIT-MUST-SURVIVE') is GONE — no stash, not in worktree (post-rollback SHA=$FINAL_SHA, stash list='$STASH_LIST')"
fi

# =============================================================================
# T2 — sync_repo_reset (infra path) must not discard a dirty infra checkout
# =============================================================================
echo "T2: sync_repo_reset (--infra-only) — dirty infra tree must survive reset --hard"

INFRA_ORIGIN2="$WORK/infra-origin2.git"; INFRA_DIR2="$WORK/infra2"
git init --bare -q "$INFRA_ORIGIN2"
git clone -q "$INFRA_ORIGIN2" "$WORK/infra-seed2"
(
  cd "$WORK/infra-seed2"
  git config user.email t@t.test; git config user.name t
  echo v1 > shared.txt; git add -A; git commit -qm c1; git push -q origin main
  echo v2 > shared.txt; git add -A; git commit -qm c2; git push -q origin main
)
git clone -q "$INFRA_ORIGIN2" "$INFRA_DIR2"
git -C "$INFRA_DIR2" reset -q --hard "$(git -C "$INFRA_DIR2" rev-list origin/main | tail -1)"  # back to c1
(
  cd "$INFRA_DIR2"
  git config user.email t@t.test; git config user.name t
  printf 'INFRA-WIP-EDIT-MUST-SURVIVE\n' > shared.txt   # dirty, uncommitted
)

BUBBLE_DEPLOY_INFRA_DIR="$INFRA_DIR2" \
BUBBLE_DEPLOY_AGENTS_ROOT="$WORK/agents-empty" \
  run_deploy --infra-only

WORKING_SHARED="$(cat "$INFRA_DIR2/shared.txt" 2>/dev/null || true)"
STASH_LIST2="$(git -C "$INFRA_DIR2" stash list 2>/dev/null || true)"
RECOVERABLE2=0
[[ "$WORKING_SHARED" == "INFRA-WIP-EDIT-MUST-SURVIVE" ]] && RECOVERABLE2=1
if [[ -n "$STASH_LIST2" ]] && git -C "$INFRA_DIR2" stash show -p 2>/dev/null | grep -q "INFRA-WIP-EDIT-MUST-SURVIVE"; then
    RECOVERABLE2=1
fi

if [[ "$RECOVERABLE2" == "1" ]]; then
    ok "T2 dirty infra WIP edit is recoverable after reset --hard (stash or worktree)"
else
    bad "T2 DATA LOSS: dirty infra WIP edit is GONE — sync_repo_reset reset --hard with no dirty-check (worktree='$WORKING_SHARED', stash='$STASH_LIST2')"
fi

echo
echo "== $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
