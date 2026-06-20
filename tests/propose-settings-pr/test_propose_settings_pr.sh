#!/usr/bin/env bash
# =============================================================================
# test_propose_settings_pr.sh — WS5 helper test harness (TDD).
#
# Stubs bubble-git-guard + bubble-gh on PATH so NO live push / PR / token-mint
# happens. Points the helper's structural-path check at the REAL broker policy.py
# (single source of STRUCTURAL_PATH_GLOBS). Builds a throwaway git repo with an
# `origin` remote so resolve_push_target derives slug=fixture/repo=bubble-ops-fixture.
#
# Asserts the helper:
#   T1 REFUSES a non-structural path (outputs/foo.md)
#   T2 REFUSES a cross-repo / path-escape (../other-repo/CLAUDE.md)
#   T3 REFUSES missing/empty --justification
#   T4 REFUSES targeting a non-main base (e.g. --base develop)
#   T5 REFUSES when origin is not a bubble-ops-<slug> repo
#   T6 ACCEPTS a valid own-repo structural diff (--dry-run) and emits the CORRECT
#      bubble-git-guard push + bubble-gh pr create invocations.
#   T7 REFUSES when --content-from is given for a path that doesn't change nothing
#      ... (empty change) — covered implicitly; main path is T6.
#
# Run:  bash test_propose_settings_pr.sh
#       bash test_propose_settings_pr.sh -v     # verbose (show helper stderr)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HELPER="${HELPER:-$(cd "$(dirname "$0")/../../deploy/bin" && pwd)/propose-settings-pr}"
REAL_POLICY_PY="${REAL_POLICY_PY:-/opt/bubble-token-broker/src/policy.py}"

[[ -f "$HELPER" ]] || { echo "FATAL: helper not found: $HELPER"; exit 2; }
[[ -f "$REAL_POLICY_PY" ]] || { echo "FATAL: broker policy.py not found: $REAL_POLICY_PY"; exit 2; }

PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- build PATH with stubs ---------------------------------------------------
STUBS="$WORK/stubs"
mkdir -p "$STUBS"
CAPTURE="$WORK/capture.log"
: > "$CAPTURE"

cat > "$STUBS/bubble-git-guard" <<EOF
#!/usr/bin/env bash
# stub: record argv, succeed. Honors --dry-run by just exiting 0.
{ printf 'GUARD_STUB'; printf ' %q' "\$@"; printf '\n'; } >> "$CAPTURE"
exit 0
EOF
cat > "$STUBS/bubble-gh" <<EOF
#!/usr/bin/env bash
# stub: record argv, print a fake PR URL.
{ printf 'GH_STUB'; printf ' %q' "\$@"; printf '\n'; } >> "$CAPTURE"
echo "https://github.com/Bubble-invest/bubble-ops-fixture/pull/999"
exit 0
EOF
chmod +x "$STUBS/bubble-git-guard" "$STUBS/bubble-gh"

# --- broker policy dir with a fixture policy (so the helper's policy lookup ok)
POLDIR="$WORK/policies"
mkdir -p "$POLDIR"
cat > "$POLDIR/fixture-policy.yaml" <<'EOF'
github_access:
  actor: ops-loop-fixture
  own_repo: bubble-ops-fixture
  read: [bubble-ops-fixture]
  write:
    - repo: bubble-ops-fixture
      allowed_paths: [outputs/**, queues/**]
      mode: direct_runtime_commit
  pull_requests:
    can_open_to: []
EOF

# common env for every invocation
export BUBBLE_BROKER_POLICY_PY="$REAL_POLICY_PY"
export BUBBLE_GIT_GUARD="bubble-git-guard"
export BUBBLE_GH="bubble-gh"
export BUBBLE_POLICY_DIR="$POLDIR"
export BUBBLE_BROKER_BIN="/opt/bubble-token-broker/bin/bubble-token-broker"
export PATH="$STUBS:$PATH"

# --- helper to build a fresh fixture repo ------------------------------------
make_repo() {
  local dir="$1" remote="$2"
  rm -rf "$dir"; mkdir -p "$dir"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.name t; git -C "$dir" config user.email t@t
  git -C "$dir" remote add origin "$remote"
  # seed a structural file + a runtime file
  mkdir -p "$dir/layers/1" "$dir/outputs"
  echo "orig prompt" > "$dir/layers/1/PROMPT.md"
  echo "runtime" > "$dir/outputs/foo.md"
  echo "# mission" > "$dir/CLAUDE.md"
  git -C "$dir" add -A; git -C "$dir" commit -qm seed
  # create a fake origin/main ref locally so set-upstream-to has a target,
  # AND so @{upstream}..HEAD is well-defined.
  git -C "$dir" update-ref "refs/remotes/origin/main" HEAD
}

run_helper() {
  # runs helper, capturing combined output to $OUT, rc to $RC
  if [[ $VERBOSE == 1 ]]; then
    "$HELPER" "$@" > "$WORK/out" 2> >(tee "$WORK/err" >&2); RC=$?
  else
    "$HELPER" "$@" > "$WORK/out" 2> "$WORK/err"; RC=$?
  fi
  OUT="$(cat "$WORK/out")"; ERR="$(cat "$WORK/err")"
}

REPO="$WORK/bubble-ops-fixture"
CONTENT="$WORK/newprompt.txt"
echo "new prompt content" > "$CONTENT"

echo "== WS5 propose-settings-pr tests =="

# ---- T3 FIRST (RED->GREEN demo): missing justification ----------------------
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic test \
           --content-from "$CONTENT" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"justification is REQUIRED"* ]]; then
  ok "T3 refuses missing --justification (rc=$RC)"
else
  bad "T3 should refuse missing justification (rc=$RC, err=$ERR)"
fi

# empty justification (whitespace only)
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic test \
           --justification "   " --content-from "$CONTENT" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"justification is REQUIRED"* ]]; then
  ok "T3b refuses whitespace-only --justification"
else
  bad "T3b should refuse whitespace justification (rc=$RC)"
fi

# ---- T1: non-structural path ------------------------------------------------
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths outputs/foo.md --topic test \
           --justification "valid reason" --content-from "$CONTENT" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"NOT structural"* ]]; then
  ok "T1 refuses non-structural path (outputs/foo.md)"
else
  bad "T1 should refuse non-structural path (rc=$RC, err=$ERR)"
fi

# ---- T2: cross-repo / escape path -------------------------------------------
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths "../bubble-ops-maya/CLAUDE.md" --topic test \
           --justification "valid reason" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"OUTSIDE the dept repo"* ]]; then
  ok "T2 refuses cross-repo / ../ escape path"
else
  bad "T2 should refuse cross-repo path (rc=$RC, err=$ERR)"
fi

# absolute path outside repo
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths "/etc/passwd" --topic test \
           --justification "valid reason" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"OUTSIDE the dept repo"* ]]; then
  ok "T2b refuses absolute path outside repo"
else
  bad "T2b should refuse absolute outside path (rc=$RC, err=$ERR)"
fi

# ---- T4: non-main base ------------------------------------------------------
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic test \
           --justification "valid reason" --content-from "$CONTENT" \
           --base develop --dry-run
if [[ $RC -ne 0 && "$ERR" == *"--base must be 'main'"* ]]; then
  ok "T4 refuses non-main base (develop)"
else
  bad "T4 should refuse non-main base (rc=$RC, err=$ERR)"
fi

# ---- T5: origin not a bubble-ops repo --------------------------------------
make_repo "$REPO" "https://github.com/Bubble-invest/some-other-repo.git"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic test \
           --justification "valid reason" --content-from "$CONTENT" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"not a bubble-ops-<slug>"* ]]; then
  ok "T5 refuses non-bubble-ops origin remote"
else
  bad "T5 should refuse non-bubble-ops origin (rc=$RC, err=$ERR)"
fi

# ---- T6: VALID structural diff -> correct invocations -----------------------
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
: > "$CAPTURE"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic ipo-watch-cleanup \
           --justification "Remove the stale IPO Watch topic baked into L1." \
           --content-from "$CONTENT" --dry-run
if [[ $RC -eq 0 && "$OUT" == *"PR_URL=DRY_RUN"* ]]; then
  ok "T6 accepts valid own-repo structural diff (rc=0, emits PR_URL)"
else
  bad "T6 should accept valid diff (rc=$RC, out=$OUT, err=$ERR)"
fi

# assert the emitted GUARD_CMD has the right action/repo/policy/ref
GUARD_LINE="$(grep '^GUARD_CMD' "$WORK/out" || true)"
if   [[ "$GUARD_LINE" == *"--action settings_pr"* ]] \
  && [[ "$GUARD_LINE" == *"--dept fixture"* ]] \
  && [[ "$GUARD_LINE" == *"--repo bubble-ops-fixture"* ]] \
  && [[ "$GUARD_LINE" == *"fixture-policy.yaml"* ]] \
  && [[ "$GUARD_LINE" == *"--remote origin"* ]] \
  && [[ "$GUARD_LINE" == *"--ref settings/ipo-watch-cleanup-"* ]]; then
  ok "T6a emitted bubble-git-guard push invocation is correct"
else
  bad "T6a wrong guard invocation: $GUARD_LINE"
fi

# assert the emitted GH_CMD is a pr create against main with the settings head
GH_LINE="$(grep '^GH_CMD' "$WORK/out" || true)"
if   [[ "$GH_LINE" == *"pr create"* ]] \
  && [[ "$GH_LINE" == *"--repo Bubble-invest/bubble-ops-fixture"* ]] \
  && [[ "$GH_LINE" == *"--base main"* ]] \
  && [[ "$GH_LINE" == *"--head settings/ipo-watch-cleanup-"* ]]; then
  ok "T6b emitted bubble-gh pr create invocation is correct"
else
  bad "T6b wrong gh invocation: $GH_LINE"
fi

# assert the guard's OWN dry-run stub was actually invoked (policy exercised)
if grep -q '^GUARD_STUB.*--dry-run' "$CAPTURE"; then
  ok "T6c helper invoked the guard dry-run (policy path exercised)"
else
  bad "T6c helper did not invoke guard dry-run; capture=$(cat "$CAPTURE")"
fi

# assert the structural file was actually changed + committed on a settings branch
BR="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
if [[ "$BR" == settings/ipo-watch-cleanup-* ]] \
  && git -C "$REPO" log -1 --pretty=%B | grep -q "Justification: Remove the stale IPO Watch"; then
  ok "T6d committed the structural change on a settings/ branch with justification in msg"
else
  bad "T6d branch/commit wrong: branch=$BR"
fi

# ---- T7: empty change refused ----------------------------------------------
# content identical to existing file -> nothing staged -> refuse
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
SAME="$WORK/same.txt"; cp "$REPO/layers/1/PROMPT.md" "$SAME"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic noop \
           --justification "valid reason" --content-from "$SAME" --dry-run
if [[ $RC -ne 0 && "$ERR" == *"no staged changes"* ]]; then
  ok "T7 refuses an empty (no-op) change"
else
  bad "T7 should refuse empty change (rc=$RC, err=$ERR)"
fi

# ---- T8: .git ownership restored to claude after a run ----------------------
# Verifies the fix for issue #202: propose-settings-pr must restore .git
# ownership to claude on EXIT so the dept repo isn't broken after each run.
#
# T8 (happy-path): asserts the successful run exits 0 AND .git ownership is
#   restored (root path) / restoration log line emitted (non-root path).
# T8a: asserts the restoration log line appears on stderr (trap fired).
# T8b (exit-code-masking regression): triggers a post-trap-arm failure (no
#   staged changes → die) and asserts the script still exits NON-zero.
#   This pins the bug where the EXIT trap's return value replaced the real
#   exit code, making every die() after trap-arm exit 0 (false SUCCESS).
#
# When running as root (as on the VPS), the test poisons .git to root:root,
# runs the helper, and asserts .git/HEAD ends up claude-owned and
# `sudo -u claude git rev-parse HEAD` succeeds.
# When running as a non-root CI user (e.g. on a Mac), it only checks that
# the restoration log line appears on stderr (proving the trap fired).
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
if [[ "$(id -u)" -eq 0 ]]; then
  # Running as root — poison .git to root:root, then verify the trap heals it.
  chown -R root:root "$REPO/.git"
  run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic ownership-heal \
             --justification "Test that .git ownership is restored to claude." \
             --content-from "$CONTENT" --dry-run
  # (a) assert the happy-path run exits 0
  if [[ $RC -eq 0 ]]; then
    ok "T8 happy-path exits 0 after .git ownership heal"
  else
    bad "T8 happy-path should exit 0 (rc=$RC)"
  fi
  OWNER="$(stat -c '%U' "$REPO/.git/HEAD" 2>/dev/null || stat -f '%Su' "$REPO/.git/HEAD" 2>/dev/null || echo unknown)"
  if [[ "$OWNER" == "claude" ]]; then
    ok "T8 .git/HEAD ownership restored to claude after run (was root)"
  else
    bad "T8 .git/HEAD still owned by $OWNER after run (expected claude)"
  fi
  if [[ "$ERR" == *"ownership restored to claude:claude"* ]]; then
    ok "T8a trap emitted the restoration log line on stderr"
  else
    bad "T8a restoration log line missing from stderr (trap may not have fired); err=$ERR"
  fi
else
  # Non-root: just verify the trap fires and logs the restoration message.
  run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic ownership-smoke \
             --justification "Smoke test: ownership self-heal message emitted." \
             --content-from "$CONTENT" --dry-run
  # (a) assert the happy-path run exits 0
  if [[ $RC -eq 0 ]]; then
    ok "T8 happy-path exits 0"
  else
    bad "T8 happy-path should exit 0 (rc=$RC, err=$ERR)"
  fi
  if [[ "$ERR" == *"ownership restored to claude:claude"* ]]; then
    ok "T8a (non-root) chown trap fired and emitted restoration log line"
  else
    bad "T8a (non-root) restoration log line missing from stderr; err=$ERR"
  fi
fi

# ---- T8b: exit-code-masking regression — post-trap-arm failure exits NON-zero
# After the trap is armed (post git checkout -b), trigger a die() by supplying
# content identical to the existing file so nothing is staged.
# Before the fix, _restore_git_ownership ended with `echo`, and the EXIT
# trap's exit status replaced the script's real exit code → the script exited
# 0 even after die(). This test pins that regression.
make_repo "$REPO" "https://github.com/Bubble-invest/bubble-ops-fixture.git"
SAME="$WORK/same2.txt"; cp "$REPO/layers/1/PROMPT.md" "$SAME"
run_helper --repo-dir "$REPO" --paths layers/1/PROMPT.md --topic noop-posttrap \
           --justification "Trigger post-trap-arm die via empty change." \
           --content-from "$SAME" --dry-run
if [[ $RC -ne 0 ]]; then
  ok "T8b post-trap-arm failure (no staged changes) exits NON-zero (exit-code masking fixed)"
else
  bad "T8b post-trap-arm die() should exit non-zero but got rc=$RC (exit-code masking regression!)"
fi
if [[ "$ERR" == *"no staged changes"* ]]; then
  ok "T8b correct error message emitted (no staged changes)"
else
  bad "T8b expected 'no staged changes' in stderr; err=$ERR"
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
