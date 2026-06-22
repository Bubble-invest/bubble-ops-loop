#!/usr/bin/env bash
# =============================================================================
# cabinet-ready.sh — TDD checklist for "Morty cabinet ready to host Maya+Tony"
#
# Runs from the operator Mac. SSHes to hetzner, asserts state of every
# primitive that must be in place before we can éclore the first real agent.
#
# Exit codes:
#   0 = all 16 tests green
#   1+ = number of failing tests
#
# Usage:
#   bash console/deploy/tests/cabinet-ready.sh
#   bash console/deploy/tests/cabinet-ready.sh --skip-iphone   # skip T16
# =============================================================================
set -uo pipefail

HOST="hetzner"
TLS_HOST="${BUBBLE_VPS_HOST:?set BUBBLE_VPS_HOST}"
PASS=0; FAIL=0; SKIP=0
RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; RESET=$'\e[0m'

trap 'echo; echo "Summary: ${PASS}/${PASS}+${FAIL} passed, ${SKIP} skipped"' EXIT

assert_pass() { PASS=$((PASS+1)); echo "  ${GREEN}✓${RESET} $1"; }
assert_fail() { FAIL=$((FAIL+1)); echo "  ${RED}✗${RESET} $1${2:+: $2}"; }
assert_skip() { SKIP=$((SKIP+1)); echo "  ${YELLOW}-${RESET} $1 (skipped: $2)"; }

# --- Read the bearer token from SOPS for tests that need authenticated GETs.
# Cached for the duration of the run; never echoed.
get_bearer_token() {
  # Layer-2 compliant: --output to file, then grep the file, then shred.
  ssh "$HOST" '
    sudo bash -c "
      TMP=\$(mktemp /root/cabinet-test.XXXXXX)
      chmod 600 \"\$TMP\"
      SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt --output \"\$TMP\" /etc/bubble/secrets.sops.env 2>/dev/null
      grep -E \"^CONSOLE_BEARER_TOKEN=\" \"\$TMP\" | head -1 | cut -d= -f2-
      shred -uz \"\$TMP\"
    "
  ' 2>/dev/null
}

# T1 — Framework code present
test_T1() {
  echo "${BLUE}T1${RESET} Code framework présent sur Morty"
  out=$(ssh "$HOST" 'ls /home/claude/bubble-ops-loop/console/main.py 2>/dev/null && ls /home/claude/bubble-ops-loop/skills/department-onboarding-guide/SKILL.md 2>/dev/null' 2>/dev/null)
  if echo "$out" | grep -q "console/main.py" && echo "$out" | grep -q "department-onboarding-guide"; then
    assert_pass "code framework déployé"
  else
    assert_fail "code framework absent"
  fi
}

# T2 — Python venv with deps
test_T2() {
  echo "${BLUE}T2${RESET} Venv Python avec deps"
  out=$(ssh "$HOST" 'cd /home/claude/bubble-ops-loop && ./venv/bin/python -c "import fastapi, uvicorn, jinja2, yaml, httpx, multipart; print(\"OK\")" 2>&1' 2>/dev/null)
  if [[ "$out" == *"OK"* ]]; then
    assert_pass "venv + 6 deps (fastapi/uvicorn/jinja2/pyyaml/httpx/python-multipart)"
  else
    assert_fail "venv ou deps incomplets" "$(echo "$out" | head -1)"
  fi
}

# T3 — console module imports
test_T3() {
  echo "${BLUE}T3${RESET} Console app importable"
  out=$(ssh "$HOST" 'cd /home/claude/bubble-ops-loop && PYTHONPATH=. ./venv/bin/python -c "from console.main import app; print(type(app).__name__, len(app.routes))" 2>&1' 2>/dev/null)
  if [[ "$out" == *"FastAPI"* ]]; then
    assert_pass "console.main:app loaded (routes=$(echo "$out" | awk '{print $2}'))"
  else
    assert_fail "console.main:app fails to import" "$(echo "$out" | head -1)"
  fi
}

# T4 — CONSOLE_BEARER_TOKEN in SOPS
test_T4() {
  echo "${BLUE}T4${RESET} CONSOLE_BEARER_TOKEN dans SOPS env"
  # Layer-2 compliant: decrypt with --output, grep -c the file, shred.
  out=$(ssh "$HOST" '
    sudo bash -c "
      TMP=\$(mktemp /root/cabinet-T4.XXXXXX); chmod 600 \"\$TMP\"
      SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt --output \"\$TMP\" /etc/bubble/secrets.sops.env 2>/dev/null
      grep -cE \"^CONSOLE_BEARER_TOKEN=\" \"\$TMP\"
      shred -uz \"\$TMP\"
    "
  ' 2>/dev/null)
  if [[ "$out" == "1" ]]; then
    assert_pass "CONSOLE_BEARER_TOKEN présent (chiffré)"
  else
    assert_fail "CONSOLE_BEARER_TOKEN absent du SOPS env"
  fi
}

# T5 — MAYA_TELEGRAM_BOT_TOKEN in SOPS
test_T5() {
  echo "${BLUE}T5${RESET} MAYA_TELEGRAM_BOT_TOKEN dans SOPS env"
  out=$(ssh "$HOST" '
    sudo bash -c "
      TMP=\$(mktemp /root/cabinet-T5.XXXXXX); chmod 600 \"\$TMP\"
      SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt --output \"\$TMP\" /etc/bubble/secrets.sops.env 2>/dev/null
      grep -cE \"^MAYA_TELEGRAM_BOT_TOKEN=\" \"\$TMP\"
      shred -uz \"\$TMP\"
    "
  ' 2>/dev/null)
  if [[ "$out" == "1" ]]; then
    assert_pass "MAYA_TELEGRAM_BOT_TOKEN présent (chiffré)"
  else
    assert_fail "MAYA_TELEGRAM_BOT_TOKEN absent du SOPS env"
  fi
}

# T6 — systemd unit installed and running
test_T6() {
  echo "${BLUE}T6${RESET} bubble-ops-console.service active"
  out=$(ssh "$HOST" 'systemctl is-active bubble-ops-console.service 2>&1; systemctl is-enabled bubble-ops-console.service 2>&1' 2>/dev/null)
  active=$(echo "$out" | sed -n '1p')
  enabled=$(echo "$out" | sed -n '2p')
  if [[ "$active" == "active" && "$enabled" == "enabled" ]]; then
    assert_pass "service active + enabled"
  else
    assert_fail "service not active/enabled" "active=$active enabled=$enabled"
  fi
}

# T7 — port 8642 on 127.0.0.1 only
test_T7() {
  echo "${BLUE}T7${RESET} Port 8642 sur 127.0.0.1 (jamais 0.0.0.0)"
  out=$(ssh "$HOST" 'sudo ss -tlnp 2>/dev/null | grep -E ":8642\s"' 2>/dev/null)
  if echo "$out" | grep -qE "127\.0\.0\.1:8642"; then
    if echo "$out" | grep -qE "0\.0\.0\.0:8642|\[::\]:8642"; then
      assert_fail "port 8642 leaks on 0.0.0.0 or [::]"
    else
      assert_pass "port 8642 bound 127.0.0.1 only"
    fi
  else
    assert_fail "port 8642 not listening"
  fi
}

# T8 — Tailscale serve config
test_T8() {
  echo "${BLUE}T8${RESET} Tailscale serve HTTPS 8443 → 127.0.0.1:8642"
  out=$(ssh "$HOST" 'sudo tailscale serve status 2>/dev/null' 2>/dev/null)
  if echo "$out" | grep -q ":8443" && echo "$out" | grep -q "127.0.0.1:8642"; then
    assert_pass "tailscale serve configuré"
  else
    assert_fail "tailscale serve manquant" "$(echo "$out" | head -2)"
  fi
}

# T9 — /health-noauth returns 200 (from Morty, via loopback)
test_T9() {
  echo "${BLUE}T9${RESET} GET /health-noauth (sans auth) → 200"
  out=$(ssh "$HOST" 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8642/health-noauth' 2>/dev/null)
  if [[ "$out" == "200" ]]; then
    assert_pass "health-noauth OK"
  else
    assert_fail "/health-noauth returned HTTP $out (expected 200)"
  fi
}

# T10 — / without auth → 401
test_T10() {
  echo "${BLUE}T10${RESET} GET / sans Bearer → 401"
  out=$(ssh "$HOST" 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8642/' 2>/dev/null)
  if [[ "$out" == "401" || "$out" == "403" ]]; then
    assert_pass "auth refuse (HTTP $out)"
  else
    assert_fail "missing-auth returned HTTP $out (expected 401/403)"
  fi
}

# T11 — / with Bearer → 200 + page mentions "Fixture"
test_T11() {
  echo "${BLUE}T11${RESET} GET / avec Bearer → 200 + Fixture visible"
  TOKEN=$(get_bearer_token)
  if [[ -z "$TOKEN" ]]; then
    assert_skip "cannot read CONSOLE_BEARER_TOKEN" "T4 prerequisite"
    return
  fi
  # ssh-side curl
  out=$(ssh "$HOST" "curl -s -w \"|%{http_code}\" -H \"Authorization: Bearer ${TOKEN}\" http://127.0.0.1:8642/" 2>/dev/null)
  http_code="${out##*|}"
  body="${out%|*}"
  if [[ "$http_code" == "200" ]]; then
    if echo "$body" | grep -qi "fixture"; then
      assert_pass "200 + Fixture rendered on page"
    else
      assert_fail "200 but no 'fixture' string in body"
    fi
  else
    assert_fail "authed GET / returned HTTP $http_code"
  fi
}

# T12 — bubble-ops-maya repo exists with bootstrap files
test_T12() {
  echo "${BLUE}T12${RESET} Repo vdk888/bubble-ops-maya existe avec scaffold"
  out=$(gh repo view vdk888/bubble-ops-maya --json visibility,defaultBranchRef 2>/dev/null)
  if echo "$out" | grep -q '"visibility":"PRIVATE"'; then
    # Check files
    files_out=$(gh api repos/vdk888/bubble-ops-maya/contents 2>/dev/null | python3 -c "import sys,json; print(','.join(f['name'] for f in json.load(sys.stdin)))" 2>/dev/null)
    if echo "$files_out" | grep -q "dept.yaml.draft\|dept.yaml" && echo "$files_out" | grep -q "onboarding"; then
      assert_pass "repo privé + dept.yaml.draft + onboarding/"
    else
      assert_fail "repo existe mais scaffold incomplet" "$files_out"
    fi
  else
    assert_fail "repo bubble-ops-maya absent ou public"
  fi
}

# T13 — clone on Morty
test_T13() {
  echo "${BLUE}T13${RESET} Clone Morty /home/claude/agents/maya/"
  # Check both files exist via test -f, returning a single OK/FAIL line.
  out=$(ssh "$HOST" '
    if [ -f /home/claude/agents/maya/dept.yaml.draft ] && [ -f /home/claude/agents/maya/onboarding/STATE.yaml ]; then
      echo OK
    else
      echo MISSING
    fi
  ' 2>/dev/null)
  if [[ "$out" == "OK" ]]; then
    assert_pass "clone Morty avec dept.yaml.draft + onboarding/STATE.yaml"
  else
    assert_fail "clone Morty incomplet"
  fi
}

# T14 — /agents shows Maya in "À éclore"
test_T14() {
  echo "${BLUE}T14${RESET} GET /agents mentionne Maya en 'À éclore'"
  TOKEN=$(get_bearer_token)
  if [[ -z "$TOKEN" ]]; then
    assert_skip "cannot read CONSOLE_BEARER_TOKEN" "T4 prerequisite"
    return
  fi
  out=$(ssh "$HOST" "curl -s -H \"Authorization: Bearer ${TOKEN}\" http://127.0.0.1:8642/agents" 2>/dev/null)
  if echo "$out" | grep -qi "maya" && echo "$out" | grep -qiE "éclore|eclore|onboarding"; then
    assert_pass "Maya visible dans la zone Agents à éclore"
  else
    assert_fail "Maya n'apparait pas dans /agents en 'À éclore'"
  fi
}

# T15 — GitHub App installation for bubble-ops-maya
test_T15() {
  echo "${BLUE}T15${RESET} GitHub App installée sur bubble-ops-maya"
  out=$(ssh "$HOST" '
    sudo bash -c "
      TMP=\$(mktemp /root/cabinet-T15.XXXXXX); chmod 600 \"\$TMP\"
      SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt --output \"\$TMP\" /etc/bubble/secrets.sops.env 2>/dev/null
      grep -cE \"^GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_MAYA=\" \"\$TMP\"
      shred -uz \"\$TMP\"
    "
  ' 2>/dev/null)
  if [[ "$out" == "1" ]]; then
    assert_pass "GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_MAYA présent dans SOPS"
  else
    assert_fail "App installation ID manquant pour Maya"
  fi
}

# T16 — manual smoke from operator phone (optional)
test_T16() {
  echo "${BLUE}T16${RESET} Smoke depuis iPhone (manuel)"
  if [[ "${1:-}" == "--skip-iphone" ]]; then
    assert_skip "iPhone smoke" "--skip-iphone"
    return
  fi
  assert_skip "iPhone smoke" "manuel par {{OPERATOR}} quand il a un moment"
}

# T17 — /srv/bubble-ops/repos/ exists, owned claude:claude 0750, has bubble-ops-fixture/
test_T17() {
  echo "${BLUE}T17${RESET} /srv/bubble-ops/repos/ existe, owned claude:claude 0750, contient bubble-ops-fixture/"
  out=$(ssh "$HOST" '
    # Check dir exists
    if [ ! -d /srv/bubble-ops/repos/ ]; then echo "NODIR"; exit; fi
    # Check owner
    owner=$(stat -c "%U:%G" /srv/bubble-ops/repos)
    perms=$(stat -c "%a" /srv/bubble-ops/repos)
    # Check clone present
    if [ -d /srv/bubble-ops/repos/bubble-ops-fixture/.git ]; then
      clone_ok=1
    else
      clone_ok=0
    fi
    echo "owner=$owner perms=$perms clone=$clone_ok"
  ' 2>/dev/null)
  if echo "$out" | grep -q "NODIR"; then
    assert_fail "/srv/bubble-ops/repos/ n'existe pas"
  elif echo "$out" | grep -q "owner=claude:claude" && echo "$out" | grep -q "perms=750" && echo "$out" | grep -q "clone=1"; then
    assert_pass "/srv/bubble-ops/repos/ owned claude:claude 0750, bubble-ops-fixture cloné"
  else
    assert_fail "/srv/bubble-ops/repos/ mal configurée" "$out"
  fi
}

# T18 — bubble-cache-sync.timer active + enabled, next-elapse <= 10 min
test_T18() {
  echo "${BLUE}T18${RESET} bubble-cache-sync.timer actif + enabled, prochain elapse <= 10 min"
  # All parsing done on the remote (Linux) side.
  # Uses systemctl list-timers --output=json (available on systemd >= 245).
  result=$(ssh "$HOST" '
    active=$(systemctl is-active bubble-cache-sync.timer 2>/dev/null)
    enabled=$(systemctl is-enabled bubble-cache-sync.timer 2>/dev/null)
    if [[ "$active" != "active" ]] || [[ "$enabled" != "enabled" && "$enabled" != "static" ]]; then
      echo "NOTACTIVE active=$active enabled=$enabled"
      exit 0
    fi
    # Use list-timers --output=json: "next" and "last" are realtime usec integers.
    json=$(systemctl list-timers bubble-cache-sync.timer --no-pager --output=json 2>/dev/null)
    if [[ -z "$json" || "$json" == "[]" ]]; then
      echo "NOJSON active=$active enabled=$enabled"
      exit 0
    fi
    next_usec=$(echo "$json" | python3 -c "import json,sys; t=json.load(sys.stdin); print(t[0][\"next\"] if t else 0)" 2>/dev/null)
    now_usec=$(python3 -c "import time; print(int(time.time()*1e6))" 2>/dev/null)
    if [[ -z "$next_usec" || "$next_usec" == "0" ]]; then
      echo "NOUSEC active=$active enabled=$enabled"
      exit 0
    fi
    diff_sec=$(python3 -c "print(int(($next_usec - $now_usec) / 1e6))" 2>/dev/null)
    echo "OK active=$active enabled=$enabled diff_sec=$diff_sec"
  ' 2>/dev/null)
  if echo "$result" | grep -q "^OK"; then
    diff_sec=$(echo "$result" | sed 's/.*diff_sec=//')
    if [[ "$diff_sec" -le 600 && "$diff_sec" -ge 0 ]]; then
      assert_pass "timer actif + enabled, prochain elapse dans ${diff_sec}s"
    else
      assert_fail "prochain elapse trop loin (${diff_sec}s > 600s)"
    fi
  elif echo "$result" | grep -q "^NOTACTIVE"; then
    assert_fail "bubble-cache-sync.timer pas actif/enabled" "$result"
  else
    assert_fail "impossible de lire le timer" "$result"
  fi
}

# T19 — /usr/local/bin/bubble-git-guard --help exits 0 + mentions open_priority_pr
test_T19() {
  echo "${BLUE}T19${RESET} bubble-git-guard --help exit 0 + mentionne 'open_priority_pr'"
  # Run on Morty (Linux) and parse the result there
  result=$(ssh "$HOST" '
    help_out=$(bubble-git-guard --help 2>&1)
    exit_code=$?
    if [[ "$exit_code" == "0" ]] && echo "$help_out" | grep -q "open_priority_pr"; then
      echo "OK"
    elif [[ "$exit_code" != "0" ]]; then
      echo "EXITFAIL exit=$exit_code"
    else
      echo "NOOPR"
    fi
  ' 2>/dev/null)
  if [[ "$result" == "OK" ]]; then
    assert_pass "bubble-git-guard --help OK + open_priority_pr présent"
  elif echo "$result" | grep -q "^EXITFAIL"; then
    assert_fail "bubble-git-guard --help exit non-zero" "$result"
  elif [[ "$result" == "NOOPR" ]]; then
    assert_fail "bubble-git-guard --help ne mentionne pas open_priority_pr"
  else
    assert_fail "bubble-git-guard introuvable sur PATH" "$result"
  fi
}

main() {
  echo "=== cabinet-ready.sh — TDD pre-éclosion checklist ==="
  echo "Target: $HOST ($TLS_HOST:8443 via Tailscale-HTTPS)"
  echo ""
  test_T1
  test_T2
  test_T3
  test_T4
  test_T5
  test_T6
  test_T7
  test_T8
  test_T9
  test_T10
  test_T11
  test_T12
  test_T13
  test_T14
  test_T15
  test_T16 "$@"
  test_T17
  test_T18
  test_T19
  echo ""
  echo "Done."
  exit $FAIL
}

main "$@"
