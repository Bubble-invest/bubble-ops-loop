#!/usr/bin/env bash
# test_1573_telegram_token_argv_leak.sh — regression test for board #1573.
#
# Bug: several fleet shell scripts called curl with the Telegram bot token
# baked directly into the URL argv element
# (https://api.telegram.org/bot${TOKEN}/sendMessage) — visible to any other
# uid on the multi-uid VPS for the life of the process via `ps` or
# /proc/<pid>/cmdline. Found by the independent review of ops-loop#527
# (board #1572), root cause in skills/cloud-wiki-compile/scripts/
# cloud-wiki-compile.sh, predating #527 (#1482/#1493).
#
# Fix (board #1573): every shell curl call now feeds the URL to curl via
# `-K -` (a curl config read from stdin, fed by a bash here-string) instead
# of a literal argv element — the token never appears in argv. The
# deploy/bin/bubble-secrets + deploy/bin/bubble-rotate-dept-secret probes
# already used the sibling pattern (a 0600 -K config FILE) from an earlier
# fix; this test also protects that shape.
#
# T1 (STATIC): grep every shell script under scripts/, skills/, tools/,
#     console/, deploy/ for the dangerous shape — a curl invocation with
#     the Telegram bot-token URL as a literal argv element
#     ("https://api.telegram.org/bot$TOKEN..." or "...bot${TOKEN}...")
#     NOT going through -K/stdin or a printf-to-file config. tests/ itself
#     is out of scope (tests/bubble-rotate-dept-secret/'s T16 deliberately
#     re-creates the vulnerable shape against a STUB curl to prove the
#     leak-scan catches it — that's a fixture, not a live sender).
#
# T2 (EXECUTION): source tools/kanban/emit_kanban_item.sh, put a stub curl
#     on PATH that dumps its own argv (nothing else) to a file, invoke the
#     real _budget_reject_alert() sender with a fixed fake token, and
#     assert the token substring never appears in the stub's captured "$@".
#
# Run: bash tests/test_1573_telegram_token_argv_leak.sh
# Returns 0 on pass, 1 on any failure.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "== test_1573_telegram_token_argv_leak.sh =="

# ── T1: static scan ─────────────────────────────────────────────────────────
echo "-- T1: static scan for token-in-argv curl calls --"

SCAN_DIRS=(scripts skills tools console deploy)
VIOLATIONS=0
VIOLATION_DETAIL=""

is_shell_script() {
  # Classify by shebang, not extension — several offenders here
  # (deploy/bin/bubble-secrets, deploy/local/bubble-switch-harness-mac) carry
  # no extension at all.
  local f="$1" first_line
  first_line="$(head -n1 "$f" 2>/dev/null)"
  case "$first_line" in
    '#!'*bash*|'#!'*/sh|'#!'*' sh') return 0 ;;
    *) return 1 ;;
  esac
}

while IFS= read -r -d '' f; do
  is_shell_script "$f" || continue
  rel="${f#"$REPO_ROOT"/}"

  # Walk the file tracking whether we're inside a multi-line heredoc BODY
  # (cat <<'EOG' ... EOG — used for printed operator instructions, e.g.
  # deploy/bin/bubble-rotate-dept-secret's example commands) — those bodies
  # are documentation text, never executed, so they're out of scope.
  in_heredoc=0
  heredoc_term=""
  line_no=0
  while IFS= read -r line; do
    line_no=$((line_no + 1))

    if [[ "$in_heredoc" -eq 1 ]]; then
      # Terminator must appear alone on its own line (allowing trailing ws).
      if [[ "$line" =~ ^[[:space:]]*${heredoc_term}[[:space:]]*$ ]]; then
        in_heredoc=0
        heredoc_term=""
      fi
      continue
    fi

    # Skip pure comment lines.
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ "$trimmed" == \#* ]] && continue

    # Detect entry into a multi-line heredoc body (<<EOF / <<'EOF' / <<"EOF"),
    # but NOT a here-STRING (<<<), which is the safe pattern this fix uses.
    if [[ "$line" != *'<<<'* ]] && [[ "$line" =~ \<\<[-]?[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]? ]]; then
      in_heredoc=1
      heredoc_term="${BASH_REMATCH[1]}"
      # The line that OPENS the heredoc can itself still contain code before
      # the '<<' token (rare here) — fall through to still check it below.
    fi

    # The dangerous shape: a real shell variable interpolated right after
    # ".../bot" (bot${VAR} or bot$VAR) — NOT the doc placeholder "bot<TOKEN>"
    # (no '$', so it never matches) — and NOT fed to curl via a here-string
    # (<<<) on the same line, which is the safe -K/stdin pattern.
    if [[ "$line" == *'<<<'* ]]; then
      continue
    fi
    if [[ "$line" =~ api\.telegram\.org/bot\$\{?[A-Za-z_] ]]; then
      VIOLATIONS=$((VIOLATIONS + 1))
      VIOLATION_DETAIL="${VIOLATION_DETAIL}${rel}:${line_no}: ${trimmed}"$'\n'
    fi
  done < "$f"
done < <(
  for d in "${SCAN_DIRS[@]}"; do
    [[ -d "$REPO_ROOT/$d" ]] && find "$REPO_ROOT/$d" -type f -print0
  done
)

if [[ "$VIOLATIONS" -eq 0 ]]; then
  ok "T1: no shell script under ${SCAN_DIRS[*]} passes the Telegram bot token as a literal curl argv element"
else
  bad "T1: found $VIOLATIONS token-in-argv curl call(s):"
  printf '%s' "$VIOLATION_DETAIL" | sed 's/^/    /'
fi

# ── T2: execution — fake curl on PATH, real sender, assert argv is clean ───
echo "-- T2: execution test — emit_kanban_item.sh's _budget_reject_alert sender --"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAKE_TOKEN="FAKE-1573-BOT-TOKEN-NOT-REAL-abcdef123456"
CURL_ARGV_LOG="$WORK/curl_argv.log"
: > "$CURL_ARGV_LOG"

mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<EOF
#!/usr/bin/env bash
# Stub curl: record every argv element verbatim (one per line) and, if a -K
# config is given (file or '-' for stdin), record its contents too — this
# is what the real fix's stdin-fed config actually carries the token in, and
# we still want to assert it's never on argv while confirming stdin *is*
# how the token travels.
{
  for a in "\$@"; do printf 'ARGV: %s\n' "\$a"; done
} >> "$CURL_ARGV_LOG"
# Drain stdin (the -K config, if any) into the log under a separate marker
# so T2 can positively confirm the token travels via stdin, not argv.
if ! [ -t 0 ]; then
  { printf 'STDIN: '; cat; printf '\n'; } >> "$CURL_ARGV_LOG"
fi
echo '{"ok":true,"result":{"message_id":1}}'
EOF
chmod +x "$WORK/bin/curl"

# emit_kanban_item.sh is a top-level CLI script (no --source-only guard), so
# we don't source the whole thing — that would run its arg-parsing / gh calls.
# _budget_reject_alert() is self-contained (only locals + 3 env vars, no
# calls into the rest of the script), so extract just that function body and
# source the fragment.
FN_FRAGMENT="$WORK/budget_reject_alert_fn.sh"
awk '/^_budget_reject_alert\(\)/,/^}/' "$REPO_ROOT/tools/kanban/emit_kanban_item.sh" > "$FN_FRAGMENT"
if [[ ! -s "$FN_FRAGMENT" ]]; then
  bad "T2: could not extract _budget_reject_alert() from emit_kanban_item.sh (function renamed/moved?)"
else
  (
    set -eu
    PATH="$WORK/bin:$PATH"
    export PATH
    export TELEGRAM_BOT_TOKEN="$FAKE_TOKEN"
    export KANBAN_ALERT_CHAT_ID="123456789"
    # shellcheck source=/dev/null
    source "$FN_FRAGMENT"
    _budget_reject_alert "T1573 test card" "tester" "unit-test" '$0.00'
  ) 2>"$WORK/sender.err" || true
fi

if [[ ! -s "$CURL_ARGV_LOG" ]]; then
  bad "T2: stub curl was never invoked (sender.err: $(tail -c 300 "$WORK/sender.err" 2>/dev/null))"
else
  if grep -qF "$FAKE_TOKEN" "$CURL_ARGV_LOG"; then
    if grep "^ARGV:" "$CURL_ARGV_LOG" | grep -qF "$FAKE_TOKEN"; then
      bad "T2: token FOUND in curl argv (leak reproduced) — $(grep '^ARGV:' "$CURL_ARGV_LOG" | grep -F "$FAKE_TOKEN")"
    else
      ok "T2: token travels via stdin (-K config) only, never in curl argv"
    fi
  else
    bad "T2: stub curl ran but the token never showed up anywhere (sender likely silently skipped — check TELEGRAM_BOT_TOKEN/KANBAN_ALERT_CHAT_ID wiring)"
  fi
fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
