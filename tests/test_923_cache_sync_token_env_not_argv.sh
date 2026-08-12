#!/usr/bin/env bash
# =============================================================================
# test_923_cache_sync_token_env_not_argv.sh — board #923 (same class as
# #921/PR#310): deploy/templates/bubble-cache-sync.sh's clone/fetch steps
# must never place the broker-minted GitHub token in subprocess argv (or in
# the remote URL written to `.git/config`). The token must travel ONLY via
# a GIT_CONFIG_* extraHeader env-var triad passed to that one `git
# clone`/`git fetch` invocation (see the script's `_git_authed` helper).
#
# This is a LIVE smoke test: it copies the real script, patches only its
# hardcoded prod paths (CACHE_DIR/LOG_DIR/BROKER) and the github.com URL to
# point at fixtures under a tmp dir (a stub broker binary + a local bare
# git repo standing in for the real remote), and executes it twice — once
# so it takes the CLONE branch (fresh REPO_DIR), once so it takes the
# FETCH+RESET branch (REPO_DIR already populated) — with a `git` shim
# ahead of the real git on PATH that logs every invocation's argv (not
# env) to a file. It then asserts the fake token substring never appears
# in ANY logged argv line, mirroring
# scripts/lib/tests/test_dispatch_921_token_env_not_argv.py's "assert the
# token substring is absent from the constructed argv" approach, but
# exercised live through the actual shell script rather than by mocking
# subprocess calls.
#
# A live push/fetch against the REAL github.com with a REAL broker token
# still needs before/after validation per the PR caveat — this test proves
# the token never touches argv/on-disk config, not that the resulting
# Authorization header authenticates against GitHub.
#
# Run:  bash tests/test_923_cache_sync_token_env_not_argv.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="$REPO_ROOT/deploy/templates/bubble-cache-sync.sh"

[[ -f "$SCRIPT" ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAKE_TOKEN="ghs_ThisIsATotallyFakeTestTokenNotReal1234567890"

echo "== test_923_cache_sync_token_env_not_argv.sh =="
echo "   script: $SCRIPT"
echo ""

# ── A. static regression guard: the old URL-embedded-token pattern is gone ──
echo "A. source no longer builds an x-access-token:<token>@ URL"
if grep -q 'x-access-token:\${_TOKEN}@' "$SCRIPT"; then
  bad "found the old x-access-token:\${_TOKEN}@ URL-embedded-credential pattern"
else
  ok "no x-access-token:\${_TOKEN}@ pattern in the script source"
fi
if grep -q '_git_authed' "$SCRIPT"; then
  ok "clone/fetch route through the env-based _git_authed helper"
else
  bad "expected a _git_authed (or equivalent env-based auth) helper in the script"
fi

# ── B. live smoke test: patch fixture paths, run clone then fetch, capture argv ──
echo "B. live run (clone then fetch) never puts the token in git argv"

mkdir -p "$WORK/cache" "$WORK/log" "$WORK/bin" "$WORK/remote" "$WORK/git-shim-bin"

# Stub broker: real BROKER binary mints a short-lived ghs_* token to stdout.
# The script only cares that stdout starts with "ghs_".
cat > "$WORK/bin/bubble-token-broker" <<EOF
#!/usr/bin/env bash
echo "$FAKE_TOKEN"
EOF
chmod +x "$WORK/bin/bubble-token-broker"

# git shim: logs every invocation's argv (space-joined, NOT env) then execs
# the real git. Lets us assert on exactly what the patched script hands to
# git as arguments.
REAL_GIT="$(command -v git)"
[[ -n "$REAL_GIT" ]] || { echo "FATAL: git not found on PATH"; exit 2; }
GIT_ARGV_LOG="$WORK/git_argv.log"
cat > "$WORK/git-shim-bin/git" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$GIT_ARGV_LOG"
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$WORK/git-shim-bin/git"

# Local bare repo standing in for github.com/<org>/bubble-ops-fixture.git —
# seeded with a real commit on main so the FETCH branch (second run) has
# something to fetch+reset to.
BARE_REPO="$WORK/remote/bubble-ops-fixture.git"
"$REAL_GIT" init --bare -q "$BARE_REPO"
SEED_DIR="$WORK/seed"
"$REAL_GIT" clone -q "$BARE_REPO" "$SEED_DIR"
(
  cd "$SEED_DIR"
  "$REAL_GIT" config user.email t@t
  "$REAL_GIT" config user.name t
  echo x > README.md
  "$REAL_GIT" add -A
  "$REAL_GIT" commit -qm init
  "$REAL_GIT" push -q origin HEAD:main
)

# Patch the script: redirect hardcoded prod paths + the github.com URL to
# the fixtures above. Only path/URL constants change — the auth LOGIC
# (the code under test) is untouched.
PATCHED="$WORK/bubble-cache-sync.sh"
sed \
  -e "s#^CACHE_DIR=.*#CACHE_DIR=$WORK/cache#" \
  -e "s#^LOG_DIR=.*#LOG_DIR=$WORK/log#" \
  -e "s#^BROKER=.*#BROKER=$WORK/bin/bubble-token-broker#" \
  -e "s#https://github.com/\${GITHUB_ORG}/\${REPO}.git#$WORK/remote/\${REPO}.git#g" \
  "$SCRIPT" > "$PATCHED"
chmod +x "$PATCHED"

export PATH="$WORK/git-shim-bin:$PATH"

# Run 1: REPO_DIR does not exist yet -> CLONE branch.
OUT1="$(bash "$PATCHED" 2>&1)"; RC1=$?
[[ "$RC1" == "0" ]] && ok "run 1 (clone) exits 0" || bad "run 1 (clone) exited $RC1:\n$OUT1"
[[ -d "$WORK/cache/bubble-ops-fixture/.git" ]] && ok "run 1 populated the cache dir (clone happened)" || bad "run 1 did not create $WORK/cache/bubble-ops-fixture/.git"

# Run 2: REPO_DIR now exists -> FETCH+RESET branch.
OUT2="$(bash "$PATCHED" 2>&1)"; RC2=$?
[[ "$RC2" == "0" ]] && ok "run 2 (fetch+reset) exits 0" || bad "run 2 (fetch+reset) exited $RC2:\n$OUT2"

# ── C. assert on the captured argv log ───────────────────────────────────────
echo "C. captured git argv never contains the token"
[[ -f "$GIT_ARGV_LOG" ]] || { echo "FATAL: no argv log captured — git shim never ran"; exit 2; }

LEAKED=0
while IFS= read -r line; do
  case "$line" in
    *"$FAKE_TOKEN"*) echo "  LEAK: $line"; LEAKED=1 ;;
    *"x-access-token:"*) echo "  LEAK (credential-in-URL pattern): $line"; LEAKED=1 ;;
  esac
done < "$GIT_ARGV_LOG"
[[ "$LEAKED" == "0" ]] && ok "no token / x-access-token: pattern in any logged git argv line" || bad "token leaked into git argv (see LEAK lines above)"

grep -q '^clone ' "$GIT_ARGV_LOG" && ok "a \`git clone ...\` invocation was logged (branch actually exercised)" || bad "no \`git clone ...\` logged"
grep -q '^-C .* fetch ' "$GIT_ARGV_LOG" && ok "a \`git fetch ...\` invocation was logged (branch actually exercised)" || bad "no \`git fetch ...\` logged"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
