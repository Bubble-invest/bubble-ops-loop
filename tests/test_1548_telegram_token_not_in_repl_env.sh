#!/usr/bin/env bash
# =============================================================================
# test_1548_telegram_token_not_in_repl_env.sh — regression test for board #1548.
#
# Bug: subagents (Agent tool) and Bash-tool subshells of a Mac fleet agent
# inherit TELEGRAM_BOT_TOKEN, because the launcher used to put it into the
# claude REPL process env (via --inline-env, so it could be `set -a`-exported
# into the pane's shell before `exec`). A worker `cat` printed a live bot token
# into its transcript this way (#1520 worker smoke test, 2026-09-27).
#
# Fix: the telegram plugin (~/.claude/plugins/.../telegram/server.ts) already
# loads $TELEGRAM_STATE_DIR/.env into ITS OWN process.env ("real env wins") —
# it never needed the token in the REPL env at all. render_loop_wrapper now (a)
# filters TELEGRAM_BOT_TOKEN out of --inline-env even if the caller still lists
# it there, (b) writes its CURRENT value straight into <telegram-state-dir>/.env
# (atomically, 0600, preserving every other line already there), and (c) `env
# -u`'s it on the exec too, belt-and-suspenders against a stale value already
# sitting in the tmux SERVER's own persistent global env.
#
# This is an EXECUTION test (board #521 lesson: prove it, don't just grep it).
# It renders a real wrapper via install-local-loop.sh into scratch dirs (no
# --activate — never touches launchd/a real dept), with a stub tmux that runs
# the captured pane command for real under BOTH /bin/bash and /bin/zsh, and a
# stub "claude" that (1) dumps its own env to a file, proving what the REPL
# actually inherits, then (2) execs a stub "plugin" that loads
# $TELEGRAM_STATE_DIR/.env exactly the way server.ts does (chmod 600 + `real
# env wins` regex parse) and dumps what IT sees.
#
# Assertions:
#   T1  render + install exits 0, wrapper is valid bash.
#   T2  the pre-existing state-dir .env's unrelated line + comment survive the
#       wrapper's own rewrite, and the file stays mode 600.
#   T3  no token VALUE is ever written to wrapper stdout/stderr.
#   T4  under BOTH /bin/bash and /bin/zsh panes:
#       T4a the exec'd "claude" REPL's own env LACKS TELEGRAM_BOT_TOKEN;
#       T4b TELEGRAM_STATE_DIR reaches the REPL correctly;
#       T4c CLAUDE_CODE_OAUTH_TOKEN (still --inline-env'd) reaches the REPL;
#       T4d the child "plugin" stub — which loads $TELEGRAM_STATE_DIR/.env
#           itself, the same way server.ts does — DOES get the real token.
#   T5  the tmux argv the harness actually invoked (captured verbatim) never
#       contains the token value anywhere.
# =============================================================================
set -uo pipefail

INSTALLER="${1:?usage: test_1548_telegram_token_not_in_repl_env.sh <install-local-loop.sh>}"
[[ -f "$INSTALLER" ]] || { echo "FATAL: not found: $INSTALLER"; exit 2; }

PASS=0; FAIL=0
ok()     { if [[ "$2" -eq 0 ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (rc=$2)"; FAIL=$((FAIL+1)); fi; }
want()   { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2' in $3)"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2' in $3)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

FAKE_TELEGRAM_TOKEN="FAKE-1548-TELEGRAM-TOKEN-NOT-REAL"
FAKE_OAUTH_TOKEN="FAKE-1548-OAUTH-TOKEN-NOT-REAL"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for SH in bash zsh; do
  SH_BIN="/bin/$SH"
  if [[ ! -x "$SH_BIN" ]]; then
    echo "  SKIP: T4[$SH] $SH_BIN not present on this runner"; continue
  fi
  echo "== board #1548 execution test — pane shell: $SH_BIN =="

  WORK="$TMP/work-$SH"
  mkdir -p "$WORK/dept" "$WORK/LA" "$WORK/logs" "$WORK/wrap" "$WORK/tg" "$WORK/bin"

  # Pre-seed the state-dir .env exactly like a live dept: one unrelated key, a
  # comment, and a STALE token value that this run's fresh value must replace
  # (board #1548's "replaces only the managed key, preserves everything else").
  cat > "$WORK/tg/.env" <<ENVEOF
OTHER_KEY=keepme-$SH
# a pre-existing comment line
TELEGRAM_BOT_TOKEN=STALE-VALUE-FROM-A-PRIOR-RUN
ENVEOF
  chmod 600 "$WORK/tg/.env"

  # Stub sops: stands in for a real vault decrypt, always yielding a FRESH
  # TELEGRAM_BOT_TOKEN value (this is what the wrapper's own process env
  # should end up holding — NOT the stale value already sitting in the
  # pre-seeded state-dir .env above). `sops --decrypt --output <path> <in>`.
  cat > "$WORK/bin/sops" <<EOF
#!/bin/bash
out=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in --output) out="\$2"; shift 2 ;; *) shift ;; esac
done
[[ -n "\$out" ]] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$FAKE_TELEGRAM_TOKEN" > "\$out"
exit 0
EOF
  chmod +x "$WORK/bin/sops"
  printf 'fake-vault-ciphertext\n' > "$WORK/vault.sops.env"

  # Stub tmux: runs the captured pane command argv (the LAST arg to
  # new-session) for real, under THIS iteration's $SH_BIN — mirrors what a
  # real tmux pane does ($SHELL -c "<command>").
  cat > "$WORK/bin/tmux" <<EOF
#!/bin/bash
case "\$1" in
  new-session)
    last="\${@: -1}"
    printf '%s' "\$last" > "$WORK/logs/captured_argv.txt"
    "$SH_BIN" -c "\$last"
    exit \$?
    ;;
  kill-session) exit 0 ;;
  has-session)  exit 1 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$WORK/bin/tmux"

  # Stub claude (the REPL): dumps its OWN env (proving what a real Agent-tool
  # subagent / Bash subshell would inherit), then execs the plugin stub.
  cat > "$WORK/bin/claude" <<EOF
#!/bin/bash
{
  echo "TELEGRAM_BOT_TOKEN=\${TELEGRAM_BOT_TOKEN:-<UNSET>}"
  echo "TELEGRAM_STATE_DIR=\${TELEGRAM_STATE_DIR:-<UNSET>}"
  echo "CLAUDE_CODE_OAUTH_TOKEN=\${CLAUDE_CODE_OAUTH_TOKEN:-<UNSET>}"
} > "$WORK/logs/repl_env_dump.txt"
exec "$WORK/bin/plugin-stub"
EOF
  chmod +x "$WORK/bin/claude"

  # Stub telegram plugin (the MCP server child): loads \$TELEGRAM_STATE_DIR/.env
  # into ITS OWN process env the SAME WAY server.ts does — chmod 600, then a
  # per-line \w+=.* regex parse where "real env wins" (only fills a var that
  # ISN'T already set) — then dumps what it ends up with.
  cat > "$WORK/bin/plugin-stub" <<'EOF'
#!/bin/bash
ENV_FILE="$TELEGRAM_STATE_DIR/.env"
chmod 600 "$ENV_FILE" 2>/dev/null || true
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      [A-Za-z_]*=*)
        key="${line%%=*}"; val="${line#*=}"
        eval "already_set=\${$key+x}"
        [ -z "$already_set" ] && export "$key=$val"
        ;;
    esac
  done < "$ENV_FILE"
fi
echo "PLUGIN_TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-<UNSET>}" > "$TELEGRAM_STATE_DIR/../plugin_env_dump.txt.tmp"
mv "$TELEGRAM_STATE_DIR/../plugin_env_dump.txt.tmp" "$TELEGRAM_STATE_DIR/plugin_env_dump.txt" 2>/dev/null \
  || echo "PLUGIN_TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-<UNSET>}" > "$TELEGRAM_STATE_DIR/plugin_env_dump.txt"
EOF
  chmod +x "$WORK/bin/plugin-stub"

  # Render + install (no --activate: pure render, never touches launchd),
  # mirroring the REAL fleet shape (RUNBOOK-748: --vault + --inline-env
  # "TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN" — Rick's/Tonio's exact knobs).
  # The stub sops above always yields a FRESH token value on every decrypt —
  # that fresh value (not the STALE one already on disk in $WORK/tg/.env) is
  # what _lll_write_state_env must end up persisting.
  bash "$INSTALLER" --dept-dir "$WORK/dept" --slug "test1548$SH" \
      --launch-agents-dir "$WORK/LA" --log-dir "$WORK/logs" --wrapper-dir "$WORK/wrap" \
      --claude-bin "$WORK/bin/claude" --tmux-bin "$WORK/bin/tmux" \
      --telegram-state-dir "$WORK/tg" --extra-path "$WORK/bin" \
      --channel-patches-script "" --vault "$WORK/vault.sops.env" \
      --inline-env "TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN" \
      >"$WORK/install.log" 2>&1
  rc=$?
  ok "T1a[$SH] install-local-loop.sh exits 0" "$rc"
  if [[ "$rc" -ne 0 ]]; then cat "$WORK/install.log"; fi
  WRAPPER="$WORK/wrap/ops-loop-test1548$SH-wrapper.sh"
  bash -n "$WRAPPER" 2>"$WORK/lint.err"; ok "T1b[$SH] rendered wrapper is valid bash" $?

  # Run the wrapper for real, hermetically (env -i). CLAUDE_CODE_OAUTH_TOKEN
  # simulates the real per-agent env launchd would provide; TELEGRAM_BOT_TOKEN
  # is deliberately NOT passed here — it must come ONLY from the vault decrypt
  # (the stub sops above), never from the wrapper's calling env.
  env -i HOME="$HOME" PATH="/usr/bin:/bin" TMPDIR="${TMPDIR:-/tmp}" \
    CLAUDE_CODE_OAUTH_TOKEN="$FAKE_OAUTH_TOKEN" \
    "$WRAPPER" >"$WORK/logs/wrapper.out" 2>"$WORK/logs/wrapper.err"

  echo "-- T2: state-dir .env preserves unrelated lines, replaces the token, stays 0600 --"
  want   "T2a[$SH] unrelated OTHER_KEY line preserved"    "OTHER_KEY=keepme-$SH"                    "$WORK/tg/.env"
  want   "T2b[$SH] comment line preserved"                "# a pre-existing comment line"           "$WORK/tg/.env"
  want   "T2c[$SH] stale token replaced with the fresh current value" "TELEGRAM_BOT_TOKEN=$FAKE_TELEGRAM_TOKEN" "$WORK/tg/.env"
  nowant "T2d[$SH] stale token value no longer present"   "STALE-VALUE-FROM-A-PRIOR-RUN"            "$WORK/tg/.env"
  MODE="$(stat -f '%Lp' "$WORK/tg/.env" 2>/dev/null || stat -c '%a' "$WORK/tg/.env" 2>/dev/null)"
  if [[ "$MODE" == "600" ]]; then echo "  PASS: T2e[$SH] state-dir .env is mode 600 (got $MODE)"; PASS=$((PASS+1)); else echo "  FAIL: T2e[$SH] state-dir .env mode is '$MODE', expected 600"; FAIL=$((FAIL+1)); fi

  echo "-- T3: no token value ever printed by the wrapper itself --"
  nowant "T3a[$SH] token absent from wrapper stdout" "$FAKE_TELEGRAM_TOKEN" "$WORK/logs/wrapper.out"
  nowant "T3b[$SH] token absent from wrapper stderr" "$FAKE_TELEGRAM_TOKEN" "$WORK/logs/wrapper.err"

  echo "-- T4: the exec'd claude REPL's own env vs. the plugin child's env --"
  if [[ -f "$WORK/logs/repl_env_dump.txt" ]]; then
    want   "T4a[$SH] claude REPL env LACKS TELEGRAM_BOT_TOKEN" "TELEGRAM_BOT_TOKEN=<UNSET>" "$WORK/logs/repl_env_dump.txt"
    want   "T4b[$SH] claude REPL env has the correct TELEGRAM_STATE_DIR" "TELEGRAM_STATE_DIR=$WORK/tg" "$WORK/logs/repl_env_dump.txt"
    want   "T4c[$SH] claude REPL env still gets CLAUDE_CODE_OAUTH_TOKEN (inline-env unaffected)" "CLAUDE_CODE_OAUTH_TOKEN=$FAKE_OAUTH_TOKEN" "$WORK/logs/repl_env_dump.txt"
  else
    echo "  FAIL: T4[$SH] claude REPL never ran (no repl_env_dump.txt) — see $WORK/logs/wrapper.err"; FAIL=$((FAIL+3))
    tail -20 "$WORK/logs/wrapper.err"
  fi
  if [[ -f "$WORK/tg/plugin_env_dump.txt" ]]; then
    want "T4d[$SH] the telegram plugin child DOES get the real token from its state-dir .env" "PLUGIN_TELEGRAM_BOT_TOKEN=$FAKE_TELEGRAM_TOKEN" "$WORK/tg/plugin_env_dump.txt"
  else
    echo "  FAIL: T4d[$SH] plugin stub never ran (no plugin_env_dump.txt)"; FAIL=$((FAIL+1))
  fi

  echo "-- T5: the tmux argv actually invoked never carries the token value --"
  nowant "T5[$SH] captured pane argv contains no token value" "$FAKE_TELEGRAM_TOKEN" "$WORK/logs/captured_argv.txt"
done

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
