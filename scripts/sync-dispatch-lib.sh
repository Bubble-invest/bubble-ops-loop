#!/usr/bin/env bash
# =============================================================================
# sync-dispatch-lib.sh — WS2: propagate the canonical dispatch_helpers.py
# (+ its sibling dispatch test files) from the framework repo into each
# bubble-ops-<slug> dept working tree, so the four copies never drift again.
#
# THE PROBLEM THIS SOLVES
#   `decide_dispatch` / `build_dispatch_ctx` lived as FOUR hand-evolved copies
#   (tony/maya/cgp + framework) with no source of truth. WS1 canonicalized the
#   file in the framework repo; this script is the propagation mechanism that
#   keeps the dept copies byte-identical to that canonical.
#
# SOURCE OF TRUTH (canonical)
#   <framework>/scripts/lib/dispatch_helpers.py  (WS1 branch
#   ws1-canonical-dispatch-l1-floor, md5 d8a099d45253aed14215c9957698e563).
#   Plus the sibling dispatch test files in <framework>/scripts/lib/tests/.
#
# MODES
#   (default / run)  Copy canonical -> each dept, run the dept's dispatch test
#                    suite, report per-dept md5 match, then STAGE + COMMIT the
#                    change in the dept working tree (git add + git commit ONLY).
#                    This script NEVER pushes. The guarded push onto the PR
#                    branch is a SEPARATE deferred step the operator (Rick) runs
#                    later via `bubble-git-guard push --dept <slug> ...`.
#
#                    NOTE on bubble-git-guard: it is a guarded-PUSH wrapper —
#                    its ONLY subcommand is `push` (staged-path policy check ->
#                    broker token mint -> git push). It has NO add/commit
#                    subcommands. So local staging+commit here is plain git;
#                    the guard is invoked only at push time (by the operator).
#   --check          Read-only drift audit. Diffs each dept's dispatch_helpers.py
#                    (and sibling tests) against the canonical and exits NON-ZERO
#                    if ANY dept has drifted. Modifies NOTHING. For the daily
#                    audit / CI.
#
# USAGE
#   sync-dispatch-lib.sh [--check] [--depts="maya tony cgp"]
#                        [--framework=/home/claude/bubble-ops-loop]
#                        [--agents-root=/home/claude/agents]
#                        [--python=<py-with-pytest>] [--no-commit] [--no-tests]
#
# IDEMPOTENT: a second run with no upstream change copies identical bytes,
# tests stay green, and `git diff` finds nothing to commit (no-op).
#
# EXIT CODES
#   0  success / in-sync (--check)
#   1  drift detected (--check) OR a dept test suite failed (run)
#   2  usage / environment error
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Defaults (overridable by flags — fixtures pass their own paths).
# -----------------------------------------------------------------------------
FRAMEWORK="${BUBBLE_FRAMEWORK_DIR:-/home/claude/bubble-ops-loop}"
AGENTS_ROOT="${BUBBLE_AGENTS_ROOT:-/home/claude/agents}"
DEPTS_ARG=""
CHECK_MODE=0
NO_COMMIT=0
NO_TESTS=0
# Python used to run the dept dispatch tests. Depts carry no venv of their
# own, so default to the framework venv python (has pytest). Override for
# fixtures / CI.
PYTHON_BIN="${BUBBLE_SYNC_PYTHON:-}"

# Canonical lib file (relative path inside both framework and dept trees).
LIB_REL="scripts/lib/dispatch_helpers.py"
TESTS_REL="scripts/lib/tests"

# Plain framework lib files that travel WITH the dispatch lib (no test pairing,
# no md5 gate — just keep every dept current). Added 2026-06-06 (Rick, Joris
# msg 3898): the per-layer Telegram notifier. Depts were missing these, so
# "work done" pings had no code to call.
NOTIFY_LIB_FILES=(
  "notify.py"
  "loop_notify.py"
)

# The sibling dispatch test files that travel WITH dispatch_helpers.py.
# (Only these are synced; other tests in the dept are left untouched.)
DISPATCH_TEST_FILES=(
  "test_build_dispatch_ctx.py"
  "test_dispatch_layer1_daily.py"
  "test_dispatch_retry_and_push.py"
  "test_layer1_data_sources.py"
  "test_loop_dispatch_layer1.py"
)

# -----------------------------------------------------------------------------
# logging
# -----------------------------------------------------------------------------
log()  { printf '[sync-dispatch] %s\n' "$*" >&2; }
ok()   { printf '[sync-dispatch] \033[32mOK\033[0m   %s\n' "$*" >&2; }
warn() { printf '[sync-dispatch] \033[33mWARN\033[0m %s\n' "$*" >&2; }
err()  { printf '[sync-dispatch] \033[31mERR\033[0m  %s\n' "$*" >&2; }

# -----------------------------------------------------------------------------
# arg parsing
# -----------------------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --check)          CHECK_MODE=1 ;;
    --depts=*)        DEPTS_ARG="${arg#*=}" ;;
    --framework=*)    FRAMEWORK="${arg#*=}" ;;
    --agents-root=*)  AGENTS_ROOT="${arg#*=}" ;;
    --python=*)       PYTHON_BIN="${arg#*=}" ;;
    --no-commit)      NO_COMMIT=1 ;;
    --no-tests)       NO_TESTS=1 ;;
    -h|--help)
      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) err "unknown argument: $arg"; exit 2 ;;
  esac
done

# -----------------------------------------------------------------------------
# resolve canonical + sanity checks
# -----------------------------------------------------------------------------
CANON_LIB="$FRAMEWORK/$LIB_REL"
CANON_TESTS="$FRAMEWORK/$TESTS_REL"

if [[ ! -f "$CANON_LIB" ]]; then
  err "canonical lib not found: $CANON_LIB (is --framework correct?)"
  exit 2
fi

md5_of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
CANON_MD5="$(md5_of "$CANON_LIB")"
log "canonical: $CANON_LIB (md5 $CANON_MD5)"

# Resolve python for tests (run mode only, unless --no-tests).
resolve_python() {
  if [[ -n "$PYTHON_BIN" ]]; then echo "$PYTHON_BIN"; return; fi
  if [[ -x "$FRAMEWORK/venv/bin/python" ]]; then echo "$FRAMEWORK/venv/bin/python"; return; fi
  command -v python3 || true
}

# -----------------------------------------------------------------------------
# dept discovery
# -----------------------------------------------------------------------------
discover_depts() {
  if [[ -n "$DEPTS_ARG" ]]; then
    echo "$DEPTS_ARG"
    return
  fi
  local found=()
  if [[ -d "$AGENTS_ROOT" ]]; then
    local d
    for d in "$AGENTS_ROOT"/bubble-ops-*/; do
      [[ -d "$d" ]] || continue
      local base; base="$(basename "$d")"
      found+=("${base#bubble-ops-}")
    done
  fi
  if [[ ${#found[@]} -eq 0 ]]; then
    # Default live set per the plan.
    echo "maya tony cgp"
  else
    echo "${found[*]}"
  fi
}

DEPTS="$(discover_depts)"
log "depts: $DEPTS"

# -----------------------------------------------------------------------------
# --check : read-only drift audit. Exit NON-ZERO on any drift. No writes.
# -----------------------------------------------------------------------------
if [[ "$CHECK_MODE" == "1" ]]; then
  drift=0
  for slug in $DEPTS; do
    dept_lib="$AGENTS_ROOT/bubble-ops-$slug/$LIB_REL"
    if [[ ! -f "$dept_lib" ]]; then
      err "$slug: MISSING $dept_lib"
      drift=1
      continue
    fi
    dmd5="$(md5_of "$dept_lib")"
    if [[ "$dmd5" == "$CANON_MD5" ]]; then
      ok "$slug: dispatch_helpers.py in sync ($dmd5)"
    else
      err "$slug: DRIFT dispatch_helpers.py ($dmd5 != $CANON_MD5)"
      drift=1
    fi
    # Also audit the sibling test files (if present canonically).
    for tf in "${DISPATCH_TEST_FILES[@]}"; do
      [[ -f "$CANON_TESTS/$tf" ]] || continue
      dept_tf="$AGENTS_ROOT/bubble-ops-$slug/$TESTS_REL/$tf"
      if [[ ! -f "$dept_tf" ]]; then
        err "$slug: MISSING test $tf"
        drift=1
      elif [[ "$(md5_of "$dept_tf")" != "$(md5_of "$CANON_TESTS/$tf")" ]]; then
        err "$slug: DRIFT test $tf"
        drift=1
      fi
    done
  done
  if [[ "$drift" == "0" ]]; then
    ok "all depts in sync with canonical"
    exit 0
  fi
  err "drift detected — run sync-dispatch-lib.sh (no --check) to re-sync"
  exit 1
fi

# -----------------------------------------------------------------------------
# RUN mode : copy canonical -> dept, run tests, stage per-repo commit.
# -----------------------------------------------------------------------------
PY="$(resolve_python)"
if [[ "$NO_TESTS" != "1" && -z "$PY" ]]; then
  err "no python with pytest found; pass --python=<bin> or --no-tests"
  exit 2
fi

overall_rc=0
for slug in $DEPTS; do
  dept_root="$AGENTS_ROOT/bubble-ops-$slug"
  dept_lib="$dept_root/$LIB_REL"
  dept_tests="$dept_root/$TESTS_REL"

  if [[ ! -d "$dept_root" ]]; then
    err "$slug: dept root not found: $dept_root — skipping"
    overall_rc=1
    continue
  fi

  log "=== $slug ==="

  # 1. Copy canonical lib (idempotent — install -m preserves a clean perm).
  mkdir -p "$(dirname "$dept_lib")"
  cp -f "$CANON_LIB" "$dept_lib"

  # 1b. Copy plain notify libs (best-effort; no md5 gate).
  for nf in "${NOTIFY_LIB_FILES[@]}"; do
    if [[ -f "$FRAMEWORK/scripts/lib/$nf" ]]; then
      cp -f "$FRAMEWORK/scripts/lib/$nf" "$dept_root/scripts/lib/$nf"
    fi
  done

  # 1c. Copy the notify_layer.py CLI wrapper (tools/) — what CLAUDE.md calls.
  if [[ -f "$FRAMEWORK/tools/notify_layer.py" ]]; then
    mkdir -p "$dept_root/tools"
    cp -f "$FRAMEWORK/tools/notify_layer.py" "$dept_root/tools/notify_layer.py"
    chmod +x "$dept_root/tools/notify_layer.py" 2>/dev/null || true
  fi

  # 2. Copy sibling dispatch test files.
  mkdir -p "$dept_tests"
  [[ -f "$CANON_TESTS/__init__.py" ]] && cp -f "$CANON_TESTS/__init__.py" "$dept_tests/__init__.py" 2>/dev/null || true
  for tf in "${DISPATCH_TEST_FILES[@]}"; do
    if [[ -f "$CANON_TESTS/$tf" ]]; then
      cp -f "$CANON_TESTS/$tf" "$dept_tests/$tf"
    fi
  done

  # 3. md5 match assertion.
  dmd5="$(md5_of "$dept_lib")"
  if [[ "$dmd5" == "$CANON_MD5" ]]; then
    ok "$slug: dispatch_helpers.py md5 match ($dmd5)"
  else
    err "$slug: md5 MISMATCH after copy ($dmd5 != $CANON_MD5)"
    overall_rc=1
    continue
  fi

  # 4. Run the dept's dispatch test suite.
  if [[ "$NO_TESTS" == "1" ]]; then
    warn "$slug: tests skipped (--no-tests)"
  else
    log "$slug: running dispatch tests ($PY -m pytest $TESTS_REL)"
    if ( cd "$dept_root" && "$PY" -m pytest -q "$TESTS_REL" ) ; then
      ok "$slug: dispatch tests green"
    else
      err "$slug: dispatch tests FAILED — not staging a commit"
      overall_rc=1
      continue
    fi
  fi

  # 5. STAGE + COMMIT in the dept working tree. LOCAL ONLY — NEVER pushes.
  #    The guarded push onto the PR branch is a deferred step the operator
  #    runs via `bubble-git-guard push --dept <slug> --repo bubble-ops-<slug>
  #    --action <class> --policy <yaml> --ref HEAD:<pr-branch>` in Wave C.
  if [[ "$NO_COMMIT" == "1" ]]; then
    warn "$slug: commit skipped (--no-commit) — changes left staged-pending in working tree"
    continue
  fi
  # Build the EXACT list of files we synced (never whole dirs — avoids picking
  # up pytest's __pycache__/*.pyc, which would otherwise churn every run and
  # break idempotency).
  commit_paths=("$LIB_REL")
  [[ -f "$dept_tests/__init__.py" ]] && commit_paths+=("$TESTS_REL/__init__.py")
  for tf in "${DISPATCH_TEST_FILES[@]}"; do
    [[ -f "$CANON_TESTS/$tf" && -f "$dept_tests/$tf" ]] && commit_paths+=("$TESTS_REL/$tf")
  done

  # Idempotent: nothing changed vs HEAD for the synced files => no-op commit.
  if ( cd "$dept_root" && git diff --quiet HEAD -- "${commit_paths[@]}" 2>/dev/null ) \
     && ( cd "$dept_root" && git ls-files --error-unmatch -- "${commit_paths[@]}" >/dev/null 2>&1 ); then
    ok "$slug: no change to commit (already in sync)"
    continue
  fi
  log "$slug: git add + git commit (LOCAL only, NO push)"
  ( cd "$dept_root" \
      && git add -- "${commit_paths[@]}" \
      && git commit -q \
           -m "sync(dispatch): propagate canonical dispatch_helpers.py (md5 $CANON_MD5)

Propagated by scripts/sync-dispatch-lib.sh (WS2). Push to the PR branch is
a separate operator step via bubble-git-guard push." ) \
    && ok "$slug: committed locally on $(cd "$dept_root" && git branch --show-current) (push left to operator)" \
    || { err "$slug: git add/commit failed"; overall_rc=1; }
done

if [[ "$overall_rc" == "0" ]]; then
  ok "sync complete — all depts md5-match canonical; commits staged (NOT pushed)"
else
  err "sync finished with errors (see above)"
fi
exit "$overall_rc"
