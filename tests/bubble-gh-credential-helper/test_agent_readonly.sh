#!/usr/bin/env bash
# =============================================================================
# test_agent_readonly.sh — #1619 step 2: the root credential helper mints
# contents:READ (scoped to one repo) for agent-<slug> callers, and is unchanged
# for claude/root. No root, no network, no real key: the production script is
# copied and its absolute paths sed-rewritten at stubs; openssl/curl/logger are
# PATH stubs that record the token-request body. (No test switch exists in the
# production helper.)
# Run: bash test_agent_readonly.sh [-v]
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HELPER_SRC="${HELPER_SRC:-$(cd "$HERE/../../token-broker/deploy" && pwd)/bubble-gh-credential-helper.sh}"
[[ -f "$HELPER_SRC" ]] || { echo "FATAL: helper not found: $HELPER_SRC"; exit 2; }
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/tmpfs" "$WORK/bin"
BODY="$WORK/curl_body"; LOG="$WORK/logger.log"; STRUCT_RC="$WORK/struct_rc"

cat > "$WORK/sops" <<'S'
#!/usr/bin/env bash
out=""; while [[ $# -gt 0 ]]; do case "$1" in --output) out="$2"; shift 2;; *) shift;; esac; done
printf 'FAKE-PEM\n' > "$out"
S
cat > "$WORK/struct.py" <<S
import sys
sys.exit(int(open("$STRUCT_RC").read().strip() or "1"))
S
printf '#!/usr/bin/env bash\ncat >/dev/null\nprintf "SIG"\n' > "$WORK/bin/openssl"
cat > "$WORK/bin/curl" <<S
#!/usr/bin/env bash
while [[ \$# -gt 0 ]]; do [[ "\$1" == "-d" ]] && printf '%s' "\$2" > "$BODY"; shift; done
printf '{"token":"ghs_FAKETOKEN"}'
S
printf '#!/usr/bin/env bash\necho "$*" >> "%s"\n' "$LOG" > "$WORK/bin/logger"
chmod +x "$WORK/sops" "$WORK/bin/"*

H="$WORK/helper-copy.sh"
sed -e "s#/run/lock#$WORK/tmpfs#g" -e "s#/dev/shm#$WORK/tmpfs#g" \
    -e "s#/usr/local/bin/sops#$WORK/sops#g" -e "s#/etc/age/key.txt#$WORK/age#g" \
    -e "s#/srv/bubble-secrets/[^ ]*#$WORK/enc.pem#" -e "s#/etc/bubble/cred-readonly-agents#$WORK/ro-agents#" \
    -e "s#STRUCTURAL_CHECK=/usr/local/bin/bubble-is-structural-push.py#STRUCTURAL_CHECK=$WORK/struct.py#" \
    "$HELPER_SRC" > "$H"
chmod +x "$H"; touch "$WORK/age" "$WORK/enc.pem"
grep -q "$WORK/sops" "$H" && grep -q "$WORK/struct.py" "$H" || { echo "FATAL: sed rewrite of the helper failed"; exit 2; }

# run_h <SUDO_USER or -> <path line> [structural rc: 1 = not structural]
run_h() {
  local su="$1" path="$2" struct="${3:-1}"
  : > "$BODY"; : > "$LOG"; echo "$struct" > "$STRUCT_RC"
  local envs=(env -u SUDO_USER)
  [[ "$su" != "-" ]] && envs=(env SUDO_USER="$su")
  OUT="$(printf 'protocol=https\nhost=github.com\n%s\n' "$path" | PATH="$WORK/bin:$PATH" "${envs[@]}" bash "$H" get 2>/dev/null)"
  B="$(cat "$BODY")"
}
perm() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["permissions"].get("contents","?"), d["permissions"].get("pull_requests","-"), ",".join(d.get("repositories",[])) or "-")' "$B"; }

echo "== #1619 step 2: credential helper, agent callers read-only =="
P_TONY="path=Bubble-invest/bubble-ops-tony.git"

run_h agent-tony "$P_TONY"
[[ "$OUT" == *"password=ghs_FAKETOKEN"* && "$(perm)" == "read write bubble-ops-tony" ]] \
  && ok "H1 agent-tony: contents:read + pull_requests:write, scoped to bubble-ops-tony" || bad "H1 ($(perm)) out=$OUT"
grep -q "agent-readonly: caller=agent-tony" "$LOG" && ok "H1b read-only grant is logged (metadata only, no token)" || bad "H1b log=$(cat "$LOG")"
grep -q "ghs_" "$LOG" && bad "H1c token leaked to log" || ok "H1c no token in log"

run_h claude "$P_TONY"
[[ "$(perm)" == "write write -" ]] && ok "H2 claude caller unchanged: contents:write, repo-wide" || bad "H2 ($(perm))"
run_h - "$P_TONY"
[[ "$(perm)" == "write write -" ]] && ok "H3 root-direct (no SUDO_USER) unchanged" || bad "H3 ($(perm))"

run_h agent-morty "path=Bubble-invest/bubble-ops-morty.git"
[[ "$(perm)" == "write write -" ]] && ok "H4 agent-morty documented exception: unchanged" || bad "H4 ($(perm))"

run_h agent-ben "path=Bubble-invest/bubble-ben-vault.git"
[[ "$(perm)" == "write write bubble-ben-vault" ]] && ok "H5 agent-ben own vault: write kept, scoped to the vault repo only" || bad "H5 ($(perm))"
run_h agent-ben "path=Bubble-invest/bubble-maya-vault.git"
[[ "$(perm)" == "read write bubble-maya-vault" ]] && ok "H6 agent-ben on ANOTHER dept's vault: read-only" || bad "H6 ($(perm))"
run_h agent-ben "path=Bubble-invest/bubble-ops-maya.git"
[[ "$(perm)" == "read write bubble-ops-maya" ]] && ok "H6b agent-ben on another dept's repo: read-only" || bad "H6b ($(perm))"

run_h agent-tony "protocol=https"
[[ "$(perm)" == "read write -" ]] && ok "H7 agent with no path=: read-only, never a repo-wide write" || bad "H7 ($(perm))"

run_h agent-ben "path=Bubble-invest/bubble-ben-vault.git" 0
[[ "$(perm)" == "read write bubble-ben-vault" ]] && ok "H8 vault exception still honours the structural downgrade" || bad "H8 ($(perm))"

run_h "agent-x; id" "$P_TONY"
[[ "$(perm)" == "write write -" ]] && ok "H9 malformed SUDO_USER does not match the agent pattern (falls to legacy)" || bad "H9 ($(perm))"

run_h agent-tony "path=Bubble-invest/bubble-ops-tony.git\"],\"permissions\":{\"contents\":\"write"
[[ "$(perm)" == "read write -" || "$(perm)" == "read write bubble-ops-tony" ]] \
  && ok "H10 hostile path= cannot inject JSON into the token request" || bad "H10 ($(perm)) body=$B"

# ---- staged rollout list: /etc/bubble/cred-readonly-agents
printf 'tony\n' > "$WORK/ro-agents"
run_h agent-tony "$P_TONY"
[[ "$(perm)" == "read write bubble-ops-tony" ]] && ok "H11 listed slug (tony) is read-only" || bad "H11 ($(perm))"
run_h agent-ben "path=Bubble-invest/bubble-ops-ben.git"
[[ "$(perm)" == "write write -" ]] && ok "H12 unlisted slug (ben) keeps the legacy write during the canary" || bad "H12 ($(perm))"
printf 'tonyx\n' > "$WORK/ro-agents"
run_h agent-tony "$P_TONY"
[[ "$(perm)" == "write write -" ]] && ok "H13 list matching is exact-line (tony != tonyx)" || bad "H13 ($(perm))"
: > "$WORK/ro-agents"
run_h agent-tony "$P_TONY"
[[ "$(perm)" == "write write -" ]] && ok "H14 empty list file = nobody switched" || bad "H14 ($(perm))"
rm -f "$WORK/ro-agents"
run_h agent-tony "$P_TONY"
[[ "$(perm)" == "read write bubble-ops-tony" ]] && ok "H15 list file absent = every agent switched (stricter default)" || bad "H15 ($(perm))"

echo; echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
