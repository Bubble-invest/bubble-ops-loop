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
#   T6  independent-review follow-up (execution-confirmed bug #1): a
#       --vault-ONLY render (no --inline-env, no --env-unset at all) ALSO
#       keeps TELEGRAM_BOT_TOKEN out of the REPL and reaches the plugin —
#       the previous code only `env -u`'d token_state_vars when inline-env
#       or env-unset was ALSO requested, so a vault-only config still leaked
#       (a freshly-spawned tmux SERVER inherits the starting wrapper
#       process's own env as its global session environment).
#   T7  independent-review follow-up (execution-confirmed bug #2):
#       _lll_write_state_env (deploy/local/lib/token_state_env_writer.sh.tmpl)
#       must NEVER wipe a good existing token line just because THIS run's
#       fresh value is empty (e.g. an empty/failed vault decrypt) — the old
#       existing line must survive unchanged; a genuinely fresh non-empty
#       value must still replace it.
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

  # ── T6 (independent review follow-up, execution-confirmed bug #1) ─────────
  # A --vault-ONLY render (NO --inline-env, NO --env-unset at all) must ALSO
  # keep TELEGRAM_BOT_TOKEN out of the claude REPL env. _lll_load_secrets_safe
  # exports every vault key into the WRAPPER's own process env regardless of
  # any inline-env/env-unset knob, and a freshly-spawned tmux SERVER inherits
  # the STARTING client's env as its own global session environment — so a
  # vault-only config leaked the token even though it never appeared in any
  # tmux argv or inline-env tmpfile. Same stub chain, fresh scratch dir, but
  # the installer call below omits --inline-env entirely.
  echo "-- T6: --vault-ONLY (no --inline-env, no --env-unset) still keeps the token out of the REPL --"
  WORK6="$TMP/work6-$SH"
  mkdir -p "$WORK6/dept" "$WORK6/LA" "$WORK6/logs" "$WORK6/wrap" "$WORK6/tg" "$WORK6/bin"

  cat > "$WORK6/bin/sops" <<EOF
#!/bin/bash
out=""
while [[ \$# -gt 0 ]]; do case "\$1" in --output) out="\$2"; shift 2 ;; *) shift ;; esac; done
[[ -n "\$out" ]] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$FAKE_TELEGRAM_TOKEN" > "\$out"
exit 0
EOF
  chmod +x "$WORK6/bin/sops"
  printf 'fake-vault-ciphertext\n' > "$WORK6/vault.sops.env"

  cat > "$WORK6/bin/tmux" <<EOF
#!/bin/bash
case "\$1" in
  new-session)
    last="\${@: -1}"
    printf '%s' "\$last" > "$WORK6/logs/captured_argv.txt"
    "$SH_BIN" -c "\$last"
    exit \$?
    ;;
  kill-session) exit 0 ;;
  has-session)  exit 1 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$WORK6/bin/tmux"

  cat > "$WORK6/bin/claude" <<EOF
#!/bin/bash
{
  echo "TELEGRAM_BOT_TOKEN=\${TELEGRAM_BOT_TOKEN:-<UNSET>}"
  echo "TELEGRAM_STATE_DIR=\${TELEGRAM_STATE_DIR:-<UNSET>}"
} > "$WORK6/logs/repl_env_dump.txt"
exec "$WORK6/bin/plugin-stub"
EOF
  chmod +x "$WORK6/bin/claude"
  cp "$WORK/bin/plugin-stub" "$WORK6/bin/plugin-stub"
  chmod +x "$WORK6/bin/plugin-stub"

  # NB: no --inline-env, no --env-unset — --vault ONLY.
  bash "$INSTALLER" --dept-dir "$WORK6/dept" --slug "vaultonly$SH" \
      --launch-agents-dir "$WORK6/LA" --log-dir "$WORK6/logs" --wrapper-dir "$WORK6/wrap" \
      --claude-bin "$WORK6/bin/claude" --tmux-bin "$WORK6/bin/tmux" \
      --telegram-state-dir "$WORK6/tg" --extra-path "$WORK6/bin" \
      --channel-patches-script "" --vault "$WORK6/vault.sops.env" \
      >"$WORK6/install.log" 2>&1
  rc=$?
  ok "T6a[$SH] vault-only install exits 0" "$rc"
  if [[ "$rc" -ne 0 ]]; then cat "$WORK6/install.log"; fi
  WRAPPER6="$WORK6/wrap/ops-loop-vaultonly$SH-wrapper.sh"
  bash -n "$WRAPPER6"; ok "T6b[$SH] vault-only render is valid bash" $?

  env -i HOME="$HOME" PATH="/usr/bin:/bin" TMPDIR="${TMPDIR:-/tmp}" \
    "$WRAPPER6" >"$WORK6/logs/wrapper.out" 2>"$WORK6/logs/wrapper.err"

  if [[ -f "$WORK6/logs/repl_env_dump.txt" ]]; then
    want "T6c[$SH] vault-only: claude REPL env LACKS TELEGRAM_BOT_TOKEN" "TELEGRAM_BOT_TOKEN=<UNSET>" "$WORK6/logs/repl_env_dump.txt"
    want "T6d[$SH] vault-only: claude REPL env has the correct TELEGRAM_STATE_DIR" "TELEGRAM_STATE_DIR=$WORK6/tg" "$WORK6/logs/repl_env_dump.txt"
  else
    echo "  FAIL: T6c/d[$SH] claude REPL never ran (no repl_env_dump.txt)"; FAIL=$((FAIL+2))
    tail -20 "$WORK6/logs/wrapper.err"
  fi
  if [[ -f "$WORK6/tg/plugin_env_dump.txt" ]]; then
    want "T6e[$SH] vault-only: the telegram plugin child DOES get the real token" "PLUGIN_TELEGRAM_BOT_TOKEN=$FAKE_TELEGRAM_TOKEN" "$WORK6/tg/plugin_env_dump.txt"
  else
    echo "  FAIL: T6e[$SH] plugin stub never ran (no plugin_env_dump.txt)"; FAIL=$((FAIL+1))
  fi
  nowant "T6f[$SH] vault-only: no token value in captured pane argv" "$FAKE_TELEGRAM_TOKEN" "$WORK6/logs/captured_argv.txt"
  nowant "T6g[$SH] vault-only: no token value in wrapper stdout" "$FAKE_TELEGRAM_TOKEN" "$WORK6/logs/wrapper.out"
  nowant "T6h[$SH] vault-only: no token value in wrapper stderr" "$FAKE_TELEGRAM_TOKEN" "$WORK6/logs/wrapper.err"
done

# ── T7 (independent review follow-up, execution-confirmed bug #2) ──────────
# _lll_write_state_env must NEVER wipe a good existing token line just because
# THIS run's fresh value is empty (e.g. an empty/failed vault decrypt) — the
# template's own doc comment already promised this; the implementation didn't
# keep the promise. Source the template directly (never a heredoc-in-$(...)  —
# mirrors how render_loop_wrapper itself reads it) and call the function with
# the var deliberately UNSET.
echo "== T7: _lll_write_state_env with the var UNSET leaves the existing line intact =="
LIB_DIR="$(cd "$(dirname "$INSTALLER")/lib" && pwd)"
WRITER_TMPL="$LIB_DIR/token_state_env_writer.sh.tmpl"
if [[ -f "$WRITER_TMPL" ]]; then
  W7="$TMP/writer7"; mkdir -p "$W7"
  printf 'OTHER_KEY=keepme\n# a comment\nTELEGRAM_BOT_TOKEN=GOOD-EXISTING-TOKEN-1548\n' > "$W7/.env"
  chmod 600 "$W7/.env"
  env -i HOME="$HOME" PATH="/usr/bin:/bin" bash -c "
    source '$WRITER_TMPL'
    unset TELEGRAM_BOT_TOKEN
    _lll_write_state_env '$W7/.env' TELEGRAM_BOT_TOKEN
  "
  rc=$?
  ok "T7a writer call (var unset) exits 0" "$rc"
  want   "T7b existing OTHER_KEY line survives"          "OTHER_KEY=keepme"                       "$W7/.env"
  want   "T7c existing comment line survives"            "# a comment"                            "$W7/.env"
  want   "T7d existing GOOD token line is left UNCHANGED (the bug: it used to be wiped)" "TELEGRAM_BOT_TOKEN=GOOD-EXISTING-TOKEN-1548" "$W7/.env"
  MODE7="$(stat -f '%Lp' "$W7/.env" 2>/dev/null || stat -c '%a' "$W7/.env" 2>/dev/null)"
  if [[ "$MODE7" == "600" ]]; then echo "  PASS: T7e file stays mode 600 (got $MODE7)"; PASS=$((PASS+1)); else echo "  FAIL: T7e file mode is '$MODE7', expected 600"; FAIL=$((FAIL+1)); fi
  LINES7="$(wc -l < "$W7/.env" | tr -d ' ')"
  if [[ "$LINES7" == "3" ]]; then echo "  PASS: T7f exactly 3 lines (no duplicate TELEGRAM_BOT_TOKEN= line appended)"; PASS=$((PASS+1)); else echo "  FAIL: T7f expected 3 lines, got $LINES7"; FAIL=$((FAIL+1)); cat "$W7/.env"; fi

  # T7g: sanity-check the OTHER direction still works — a FRESH non-empty
  # value DOES replace the old one (no accidental always-preserve regression).
  env -i HOME="$HOME" PATH="/usr/bin:/bin" TELEGRAM_BOT_TOKEN="FRESH-REPLACEMENT-1548" bash -c "
    source '$WRITER_TMPL'
    _lll_write_state_env '$W7/.env' TELEGRAM_BOT_TOKEN
  "
  want   "T7g a FRESH non-empty value still replaces the old one" "TELEGRAM_BOT_TOKEN=FRESH-REPLACEMENT-1548" "$W7/.env"
  nowant "T7h old value is gone once a fresh one lands"            "GOOD-EXISTING-TOKEN-1548"                  "$W7/.env"
else
  echo "  FAIL: T7 template not found at $WRITER_TMPL"; FAIL=$((FAIL+1))
fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
