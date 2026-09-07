#!/usr/bin/env bash
# =============================================================================
# test_local_loop_wrapper_render.sh — TDD harness for render_loop_wrapper's
# ALIGNMENT knobs (#748) + the #1133 harness selector.
#
# render_loop_wrapper (deploy/local/lib/local_loop_lib.sh) folds every per-agent
# customization that used to be hand-edited into each live Mac wrapper (SOPS
# vault, inline-env, model pin, --chrome, --continue, env -u) into optional,
# backward-compatible knobs, PLUS the harness selector. This harness renders it
# under representative knob combos and asserts the output is a VALID wrapper with
# the right shape. Pure render — nothing is executed, no launchctl, no tmux.
#
# Assertions:
#   T1  no-knobs render is a valid bash script (bash -n) ...
#   T2  ... and is BACKWARD-COMPATIBLE: a BARE `exec '<claude>'` launch with NO
#       secret baked into the tmux argv (the pre-alignment generic behaviour).
#   T3  every render carries the harness selector + a hermes branch, defaulting
#       to claude.
#   T4  --inline-env "A B" bakes A and B as runtime-expanded single-quoted
#       assignments into the tmux command (the tmux-server-env fix).
#   T5  --inline-env "" (explicit empty) yields NO inline tokens.
#   T6  --model / --chrome / --continue add their flags; --continue adds the
#       resume-gate + fresh-fallback, guarded with `|| true` under set -e.
#   T7  --vault renders the SOPS decrypt block (chmod 600 tmpfile + shred +
#       legacy .env fallback) and exports SOPS_AGE_KEY_FILE; no vault => sources
#       the legacy .env only.
#   T8  --env-unset CLAUDE_CODE_OAUTH_TOKEN renders `exec env -u ...` (keychain
#       override) and does NOT also inline that var.
#   T9  the hermes branch cd's + sets PATH inline (tmux server env != wrapper env).
#   T10 NO secret VALUE is ever embedded — only `${VAR:-}` runtime refs (rendered
#       under `env -i`).
# =============================================================================
set -uo pipefail

LIB="${1:?usage: test_local_loop_wrapper_render.sh <local_loop_lib.sh>}"
[[ -f "$LIB" ]] || { echo "FATAL: not found: $LIB"; exit 2; }

PASS=0; FAIL=0
want()   { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2')"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2')"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }
ok()     { if [[ "$2" -eq 0 ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (rc=$2)"; FAIL=$((FAIL+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Render helper: runs in a clean bash so word-splitting matches production; the
# LOOP_* knobs are passed as the caller's env.
render() { bash -c 'source "$0"; render_loop_wrapper "$@"' "$LIB" "$@"; }

echo "== no-knobs (backward compat) =="
render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/generic.sh"
bash -n "$TMP/generic.sh"; ok "T1 no-knobs render is valid bash" $?
want   "T2a bare exec claude"            "exec '/usr/bin/claude'" "$TMP/generic.sh"
nowant "T2b no token baked into argv"    "TELEGRAM_BOT_TOKEN='"   "$TMP/generic.sh"
want   "T3a selector present"            'SELECTOR="'             "$TMP/generic.sh"
want   "T3b selector default file"       "harness-demo"           "$TMP/generic.sh"
want   "T3c hermes branch present"       'HARNESS" = "hermes"'    "$TMP/generic.sh"
want   "T3d selector defaults to claude" 'HARNESS="claude"'       "$TMP/generic.sh"

echo "== inline-env =="
LOOP_INLINE_ENV="TELEGRAM_BOT_TOKEN CLAUDE_CODE_OAUTH_TOKEN" \
  render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/inline.sh"
bash -n "$TMP/inline.sh"; ok "T4a inline render valid" $?
want "T4b TELEGRAM_BOT_TOKEN inlined" "TELEGRAM_BOT_TOKEN='\${TELEGRAM_BOT_TOKEN:-}'" "$TMP/inline.sh"
want "T4c OAUTH inlined"              "CLAUDE_CODE_OAUTH_TOKEN='\${CLAUDE_CODE_OAUTH_TOKEN:-}'" "$TMP/inline.sh"
LOOP_INLINE_ENV="" render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/noinline.sh"
nowant "T5 explicit empty inline => no tokens" "TELEGRAM_BOT_TOKEN='" "$TMP/noinline.sh"

echo "== model / chrome / continue =="
LOOP_MODEL="claude-opus-4-8[1m]" LOOP_CHROME=1 LOOP_CONTINUE=1 \
  render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/flags.sh"
bash -n "$TMP/flags.sh"; ok "T6a flags render valid" $?
want "T6b model flag"        "--model 'claude-opus-4-8[1m]'" "$TMP/flags.sh"
want "T6c chrome flag"       " --chrome"                     "$TMP/flags.sh"
want "T6d continue flag set" 'CONT_FLAG="--continue"'        "$TMP/flags.sh"
want "T6e resume-gate"       "Resume full session as-is"     "$TMP/flags.sh"
want "T6f fresh-fallback"    "retrying fresh"                "$TMP/flags.sh"
want "T6g capture-pane guarded" "capture-pane -t \"\$SESSION\" -p 2>/dev/null || true" "$TMP/flags.sh"

echo "== vault + env-unset =="
LOOP_VAULT_PATH="/v/secrets.sops.env" LOOP_AGE_KEY_FILE="/k/age.txt" LOOP_ENV_UNSET="CLAUDE_CODE_OAUTH_TOKEN" LOOP_INLINE_ENV="TELEGRAM_BOT_TOKEN" \
  render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/vault.sh"
bash -n "$TMP/vault.sh"; ok "T7a vault render valid" $?
want "T7b age key exported"  'SOPS_AGE_KEY_FILE="/k/age.txt"' "$TMP/vault.sh"
want "T7c vault decrypt"     "sops --decrypt --output"        "$TMP/vault.sh"
want "T7d tmpfile chmod 600" 'chmod 600 "$_sec"'              "$TMP/vault.sh"
want "T7e tmpfile shredded"  'rm -f "$_sec"'                  "$TMP/vault.sh"
want "T7f legacy fallback"   "falling back to legacy .env"    "$TMP/vault.sh"
want "T8a env -u rendered"   "exec env -u CLAUDE_CODE_OAUTH_TOKEN " "$TMP/vault.sh"
nowant "T8b unset var not also inlined" "CLAUDE_CODE_OAUTH_TOKEN='\${" "$TMP/vault.sh"
nowant "T7g no-vault only for no-vault" "sops --decrypt" "$TMP/generic.sh"

echo "== hermes branch shape =="
want "T9a hermes cd + PATH inline" "PATH='/bin:\$PATH' exec hermes -p 'demo' gateway run --replace" "$TMP/flags.sh"

echo "== secrecy: no VALUES embedded (env -i render) =="
env -i bash -c 'source "$0"; TELEGRAM_BOT_TOKEN=SUPERSECRET LOOP_INLINE_ENV="TELEGRAM_BOT_TOKEN" render_loop_wrapper /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" ""' "$LIB" > "$TMP/secref.sh" 2>/dev/null
nowant "T10 no secret value embedded at render time" "SUPERSECRET" "$TMP/secref.sh"

echo "== extra-export (per-agent wrapper exports, e.g. Géraldine PYTHONPATH) =="
LOOP_EXTRA_EXPORTS='PYTHONPATH="$HOME/x:${PYTHONPATH:-}"' \
  render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/xexp.sh"
bash -n "$TMP/xexp.sh"; ok "T11a extra-export render valid" $?
want "T11b export emitted verbatim (runtime refs literal)" 'export PYTHONPATH="$HOME/x:${PYTHONPATH:-}"' "$TMP/xexp.sh"
want "T11c export sits before cd" "export PYTHONPATH" "$TMP/xexp.sh"
# multiple entries (newline-joined) both emitted
printf 'A=1\nB="two"' > "$TMP/multi"
LOOP_EXTRA_EXPORTS="$(cat "$TMP/multi")" render /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/xexp2.sh"
want "T11d multi export A" "export A=1"     "$TMP/xexp2.sh"
want "T11e multi export B" 'export B="two"' "$TMP/xexp2.sh"
nowant "T11f no extra-export by default" "export PYTHONPATH" "$TMP/generic.sh"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
