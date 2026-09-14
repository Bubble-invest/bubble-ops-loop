#!/usr/bin/env bash
# #1122 regression under the no-rewrite contract: dirty work is left in place.
# #1305: a pure deferred_review outcome (no FAILED) exits 0 — it is preserved
# work, not a service failure — so it must not alarm systemd on every run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="${SCRIPT:-$ROOT/scripts/bubble-deploy.sh}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git_init_pair() {
    local name="$1"
    local origin="$WORK/$name.git" seed="$WORK/$name-seed" checkout="$WORK/$name"
    git init --bare -q "$origin"
    git clone -q "$origin" "$seed"
    git -C "$seed" checkout -qb main
    git -C "$seed" config user.email synthetic@example.invalid
    git -C "$seed" config user.name synthetic
    echo initial >"$seed/tracked.txt"
    git -C "$seed" add tracked.txt
    git -C "$seed" commit -qm initial
    git -C "$seed" push -q origin main
    git clone -q --branch main "$origin" "$checkout"
    echo upstream >"$seed/tracked.txt"
    git -C "$seed" commit -qam upstream
    git -C "$seed" push -q origin main
}

git_init_pair infra
git_init_pair dept
mkdir -p "$WORK/agents"
mv "$WORK/dept" "$WORK/agents/testdept"
echo local-wip >"$WORK/infra/tracked.txt"
echo local-untracked >"$WORK/infra/private-note.txt"
echo dept-wip >"$WORK/agents/testdept/tracked.txt"
echo dept-untracked >"$WORK/agents/testdept/private-note.txt"
infra_head=$(git -C "$WORK/infra" rev-parse HEAD)
dept_head=$(git -C "$WORK/agents/testdept" rev-parse HEAD)

mkdir -p "$WORK/bin"
cat >"$WORK/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  show) echo loaded ;;
  is-active) echo inactive ;;
  *) echo "forbidden systemctl mutation: $*" >&2; exit 90 ;;
esac
EOF
chmod +x "$WORK/bin/systemctl"

set +e
PATH="$WORK/bin:$PATH" \
BUBBLE_DEPLOY_INFRA_DIR="$WORK/infra" \
BUBBLE_DEPLOY_AGENTS_ROOT="$WORK/agents" \
BUBBLE_DEPLOY_LEGACY_AGENTS_ROOT="$WORK/legacy-empty" \
BUBBLE_DEPLOY_LOCK_FILE="$WORK/deploy.lock" \
    bash "$SCRIPT" --dept testdept >"$WORK/out" 2>"$WORK/err"
rc=$?
set -e

[[ $rc -eq 0 ]]
[[ "$(git -C "$WORK/infra" rev-parse HEAD)" == "$infra_head" ]]
[[ "$(git -C "$WORK/agents/testdept" rev-parse HEAD)" == "$dept_head" ]]
[[ "$(cat "$WORK/infra/tracked.txt")" == local-wip ]]
[[ "$(cat "$WORK/infra/private-note.txt")" == local-untracked ]]
[[ "$(cat "$WORK/agents/testdept/tracked.txt")" == dept-wip ]]
[[ "$(cat "$WORK/agents/testdept/private-note.txt")" == dept-untracked ]]
[[ -z "$(git -C "$WORK/infra" stash list)" ]]
[[ -z "$(git -C "$WORK/agents/testdept" stash list)" ]]
grep -q 'deferred_review=2' "$WORK/out"
echo "PASS: dirty tracked and untracked work remained in place; no stash/reset/rollback"
