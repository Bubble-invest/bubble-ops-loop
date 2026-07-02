#!/usr/bin/env bash
# test_resolve_gh_token_host_local_fallback.sh — verify the _resolve_gh_token()
# ladder in emit_kanban_item.sh, specifically the host:local (Mac) SSH fallback
# added for board #463.
#
# Ladder under test (in order):
#   1. gh already authed (ambient)          -> skip (not exercised here)
#   2a. /run/bubble-board/token readable     -> use it if it starts ghs_
#   2b. sudo -n minter                       -> use its output if non-empty
#   2c. (NEW) host:local Mac SSH fallback    -> only reached when 1/2a/2b all
#       failed AND `uname -s` = Darwin; fetches
#       `ssh claude@joris-cx33 cat /run/bubble-board/token` and uses it if it
#       starts ghs_
#
# Real /run/bubble-board/token and /usr/local/bin/bubble-board-token.sh do not
# exist on this dev machine, so 2a/2b are naturally empty here — that lets us
# isolate and test exactly the new 2c branch: it must fire on Darwin and must
# NOT fire (and the function must return 1) on a non-Darwin uname.
#
# Run: bash tests/test_resolve_gh_token_host_local_fallback.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

[ -f "$EMITTER" ] || fail "emitter not found at $EMITTER"

# Sanity: confirm the real paths this test relies on being ABSENT are indeed
# absent on this machine — otherwise 2a/2b could mask the 2c behavior we want
# to isolate, and a pass here wouldn't mean anything.
[ ! -r /run/bubble-board/token ] || fail "precondition violated: /run/bubble-board/token is readable on this test host"
[ ! -x /usr/local/bin/bubble-board-token.sh ] || fail "precondition violated: minter exists on this test host"
pass "preconditions hold: no local token file, no minter on this host"

# Extract just the _resolve_gh_token function body so we can source it
# standalone and stub `uname`/`ssh` around it without running the whole script.
FUNC_SRC=$(awk '/^_resolve_gh_token\(\) \{/,/^\}$/' "$EMITTER")
[ -n "$FUNC_SRC" ] || fail "could not extract _resolve_gh_token() from $EMITTER"
echo "$FUNC_SRC" | grep -q 'ssh -o BatchMode=yes' || fail "extracted function does not contain the new SSH fallback — port missing?"
echo "$FUNC_SRC" | grep -q 'uname -s' || fail "extracted function does not gate on uname -s Darwin — port missing?"
pass "extracted _resolve_gh_token() body, contains new SSH fallback"

# Case 1: uname reports Darwin (Mac) -> SSH fallback fires, returns the token.
out1=$(
  bash -c "
    set -uo pipefail
    command() { [ \"\$2\" = gh ] && return 0 || return 1; }
    gh() { return 1; }  # gh present but NOT ambient-authed -> falls through the ladder
    uname() { echo Darwin; }
    ssh() { echo 'ghs_sshfallbacktoken123'; }
    $FUNC_SRC
    _resolve_gh_token
    echo \"STATUS=\$?\"
    echo \"TOKEN=\${GH_TOKEN:-}\"
  " 2>&1
)
echo "$out1" | grep -q '^STATUS=0$' || fail "Darwin case: expected STATUS=0, got: $out1"
echo "$out1" | grep -q '^TOKEN=ghs_sshfallbacktoken123$' || fail "Darwin case: expected GH_TOKEN=ghs_sshfallbacktoken123, got: $out1"
pass "Darwin + ssh yields ghs_ token -> fallback succeeds, GH_TOKEN exported"

# Case 2: uname reports Linux (VPS-like) -> SSH fallback must NOT fire, even
# though the ssh stub would happily hand back a valid token. Function returns 1.
out2=$(
  bash -c "
    set -uo pipefail
    command() { [ \"\$2\" = gh ] && return 0 || return 1; }
    gh() { return 1; }
    uname() { echo Linux; }
    ssh() { echo 'ghs_shouldneverbeused'; }
    $FUNC_SRC
    _resolve_gh_token
    echo \"STATUS=\$?\"
    echo \"TOKEN=\${GH_TOKEN:-}\"
  " 2>&1
)
echo "$out2" | grep -q '^STATUS=1$' || fail "Linux case: expected STATUS=1 (no SSH hop on non-Mac), got: $out2"
echo "$out2" | grep -q '^TOKEN=$' || fail "Linux case: GH_TOKEN should be unset, got: $out2"
pass "Linux (non-Darwin) -> no SSH hop attempted, function returns 1"

# Case 3: Darwin, but SSH returns nothing usable (empty / not ghs_-prefixed) -> returns 1.
out3=$(
  bash -c "
    set -uo pipefail
    command() { [ \"\$2\" = gh ] && return 0 || return 1; }
    gh() { return 1; }
    uname() { echo Darwin; }
    ssh() { echo ''; }
    $FUNC_SRC
    _resolve_gh_token
    echo \"STATUS=\$?\"
    echo \"TOKEN=\${GH_TOKEN:-}\"
  " 2>&1
)
echo "$out3" | grep -q '^STATUS=1$' || fail "Darwin+empty-ssh case: expected STATUS=1, got: $out3"
pass "Darwin + ssh yields nothing usable -> function returns 1 (no false success)"

echo ""
echo "All resolve_gh_token ladder tests passed."
