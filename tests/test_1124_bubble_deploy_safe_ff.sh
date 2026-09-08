#!/usr/bin/env bash
# Hermetic contract tests for owner-aware, no-restart, safe fast-forward deploys.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$ROOT/scripts/bubble-deploy.sh}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"
REAL_GIT="$(command -v git)"

cat >"$BIN/stat" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "-c" && "$2" == "%U" ]]; then echo fixture-owner; exit 0; fi
exec /usr/bin/stat "$@"
EOF
cat >"$BIN/runuser" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$TEST_RUNUSER_LOG"
[[ "$1" == "-u" && "$3" == "--" ]] || exit 91
shift 3
exec "$@"
EOF
cat >"$BIN/git" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"\$TEST_GIT_LOG"
exec "$REAL_GIT" "\$@"
EOF
cat >"$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$TEST_SYSTEMCTL_LOG"
case "$1" in
  show) echo loaded ;;
  is-active)
    [[ "${TEST_UNIT_STATE:-inactive}" == query-fail ]] && exit 1
    echo "${TEST_UNIT_STATE:-inactive}" ;;
  *) exit 92 ;;
esac
EOF
chmod +x "$BIN"/*

new_pair() {
    local base="$1"
    local origin="$base-origin.git" seed="$base-seed"
    git init --bare -q "$origin"
    git clone -q "$origin" "$seed"
    git -C "$seed" checkout -qb main
    git -C "$seed" config user.email synthetic@example.invalid
    git -C "$seed" config user.name synthetic
    echo initial >"$seed/file.txt"
    git -C "$seed" add file.txt
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

run_case() {
    local name="$1" state="$2"
    shift 2
    : >"$WORK/$name.gitlog"; : >"$WORK/$name.runuser"; : >"$WORK/$name.systemctl"
    set +e
    PATH="$BIN:$PATH" \
    TEST_GIT_LOG="$WORK/$name.gitlog" \
    TEST_RUNUSER_LOG="$WORK/$name.runuser" \
    TEST_SYSTEMCTL_LOG="$WORK/$name.systemctl" \
    TEST_UNIT_STATE="$state" \
    BUBBLE_DEPLOY_INFRA_DIR="$WORK/infra" \
    BUBBLE_DEPLOY_AGENTS_ROOT="$WORK/agents" \
    BUBBLE_DEPLOY_LEGACY_AGENTS_ROOT="$WORK/legacy" \
    BUBBLE_DEPLOY_LOCK_FILE="$WORK/$name.lock" \
        bash "$SCRIPT" "$@" >"$WORK/$name.out" 2>"$WORK/$name.err"
    CASE_RC=$?
    set -e
}

new_pair "$WORK/infra"
mkdir -p "$WORK/agents" "$WORK/legacy"

echo "T1 clean canonical main fast-forwards as its directory owner"
push_upstream "$WORK/infra"
run_case clean inactive --infra-only
[[ $CASE_RC -eq 0 ]]
[[ "$(git -C "$WORK/infra" rev-parse HEAD)" == "$(git -C "$WORK/infra" rev-parse origin/main)" ]]
grep -q -- '-u fixture-owner -- git -C' "$WORK/clean.runuser"
grep -q 'UPDATED framework' "$WORK/clean.out"
! grep -Eq '(^| )(reset|stash|config)($| )' "$WORK/clean.gitlog"

echo "T2 ahead and non-main states are preserved and require review"
git -C "$WORK/infra" config user.email synthetic@example.invalid
git -C "$WORK/infra" config user.name synthetic
echo local >>"$WORK/infra/file.txt"; git -C "$WORK/infra" commit -qam local
head_before=$(git -C "$WORK/infra" rev-parse HEAD)
run_case ahead inactive --infra-only
[[ $CASE_RC -eq 2 && "$(git -C "$WORK/infra" rev-parse HEAD)" == "$head_before" ]]
grep -q 'DEFER_REVIEW framework: 1 commits ahead' "$WORK/ahead.out"
git -C "$WORK/infra" checkout -qb review-branch
run_case branch inactive --infra-only
[[ $CASE_RC -eq 2 && "$(git -C "$WORK/infra" branch --show-current)" == review-branch ]]
grep -q 'DEFER_REVIEW framework: branch review-branch' "$WORK/branch.out"

echo "T3 active primary is not fast-forwarded or restarted"
git -C "$WORK/infra" checkout -q main
git -C "$WORK/infra" reset -q --hard origin/main
new_pair "$WORK/agents/maya"
push_upstream "$WORK/agents/maya"
dept_before=$(git -C "$WORK/agents/maya" rev-parse HEAD)
run_case active active --dept maya
[[ $CASE_RC -eq 0 && "$(git -C "$WORK/agents/maya" rev-parse HEAD)" == "$dept_before" ]]
grep -q 'DEFER_ACTIVE maya' "$WORK/active.out"
! grep -q ' fetch origin main ' "$WORK/active.gitlog"
! grep -Eq '^(start|stop|restart|try-restart) ' "$WORK/active.systemctl"

echo "T4 inactive primary fast-forwards without being started"
run_case inactive inactive --dept maya
[[ $CASE_RC -eq 0 ]]
[[ "$(git -C "$WORK/agents/maya" rev-parse HEAD)" == "$(git -C "$WORK/agents/maya" rev-parse origin/main)" ]]
grep -q 'UPDATED maya' "$WORK/inactive.out"
! grep -Eq '^(start|stop|restart|try-restart) ' "$WORK/inactive.systemctl"

echo "T5 legacy layout remains a fallback and operator-stopped state stays stopped"
new_pair "$WORK/legacy/bubble-ops-legacydept"
push_upstream "$WORK/legacy/bubble-ops-legacydept"
run_case legacy inactive --dept legacydept
[[ $CASE_RC -eq 0 ]]
[[ "$(git -C "$WORK/legacy/bubble-ops-legacydept" rev-parse HEAD)" == "$(git -C "$WORK/legacy/bubble-ops-legacydept" rev-parse origin/main)" ]]
grep -q 'ops-loop-legacydept.service' "$WORK/legacy.systemctl"
! grep -Eq '^(start|stop|restart|try-restart) ' "$WORK/legacy.systemctl"

echo "T6 dry-run reports a safe update without changing HEAD"
push_upstream "$WORK/agents/maya"
dept_before=$(git -C "$WORK/agents/maya" rev-parse HEAD)
run_case dry inactive --dry-run --dept maya
[[ $CASE_RC -eq 0 && "$(git -C "$WORK/agents/maya" rev-parse HEAD)" == "$dept_before" ]]
grep -q 'DRY_RUN maya' "$WORK/dry.out"
grep -q 'updated=0 would_update=1' "$WORK/dry.out"

echo "T7 an unreadable primary state fails closed without changing HEAD"
run_case unknown query-fail --dept maya
[[ $CASE_RC -eq 1 && "$(git -C "$WORK/agents/maya" rev-parse HEAD)" == "$dept_before" ]]
grep -q 'cannot prove primary' "$WORK/unknown.out"

echo "PASS: 7 safe deploy contract cases"
