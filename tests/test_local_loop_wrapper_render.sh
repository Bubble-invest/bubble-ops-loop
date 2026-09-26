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
#   T13 board #1275: secrets are loaded via _lll_load_secrets_safe (rendered into
#       every wrapper), NEVER a bare `source`/`.` of untrusted decrypted content —
#       for both the vault path and the no-vault legacy-.env-only path.
#   T14 board #1275: a malformed secret value (unquoted spaces — the exact shape
#       that crashed Ellie's loop, exit 127) does NOT abort the safe loader under
#       `set -e`, and well-formed values (plain / double-quoted / single-quoted)
#       still load correctly (working case preserved).
#   T15 board #1275: a synthetic render using the accountant's exact production
#       knobs (RUNBOOK-748) is valid bash (bash -n) — proof the render lib does
#       not emit the line-74-class syntax error for that dept's shape.
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

echo "== auto-mode prompt auto-dismiss (in the --continue resume-gate) =="
want "T12a full-title match (not the changelog substring)" "auto mode your default permission mode" "$TMP/flags.sh"
want "T12b single-shot guard present"  "_am_done" "$TMP/flags.sh"
want "T12c declines (2nd option)"      "decline (2nd option)" "$TMP/flags.sh"
nowant "T12d not present without --continue" "auto mode your default" "$TMP/generic.sh"

echo "== board #1275: safe secrets loading (no bare source of untrusted content) =="
# vault path
want   "T13a vault: safe loader defined"        "_lll_load_secrets_safe() {" "$TMP/vault.sh"
want   "T13b vault: safe loader called on \$_sec"  '_lll_load_secrets_safe "$_sec"'      "$TMP/vault.sh"
want   "T13c vault: safe loader called on legacy"  '_lll_load_secrets_safe "$LEGACY_ENV"' "$TMP/vault.sh"
nowant "T13d vault: no bare source of \$_sec"      '. "$_sec"'                            "$TMP/vault.sh"
nowant "T13e vault: no bare source of LEGACY_ENV"  '. "$LEGACY_ENV"'                      "$TMP/vault.sh"
# no-vault (legacy-.env-only) path
want   "T13f no-vault: safe loader defined"     "_lll_load_secrets_safe() {" "$TMP/generic.sh"
want   "T13g no-vault: safe loader called"      "_lll_load_secrets_safe \"/tmp/tg/.env\"" "$TMP/generic.sh"
nowant "T13h no-vault: no bare source"          '. "/tmp/tg/.env"'            "$TMP/generic.sh"

echo "== board #1275: safe loader survives a malformed secret value =="
# Extract just the rendered _lll_load_secrets_safe function and exercise it
# under set -e against a secrets file shaped exactly like the Ellie crash
# (an unquoted value containing display-format app-password spaces), plus
# quoted/plain well-formed values (the working case must be preserved).
sed -n '/^_lll_load_secrets_safe() {/,/^}/p' "$TMP/vault.sh" > "$TMP/loader_only.sh"
cat > "$TMP/secrets_malformed.env" <<'ENVEOF'
TELEGRAM_BOT_TOKEN=1234:ABCDEF
IMAP_PASSWORD=klcn xxxx xxxx xxxx
QUOTED_VAL="hello world"
SINGLE_QUOTED='foo bar'
# a comment line

THIS IS NOT VALID
NOTION_API_KEY=secret_abc123
ENVEOF
cat > "$TMP/functest.sh" <<FUNCEOF
#!/bin/bash
set -e
source "$TMP/loader_only.sh"
_lll_load_secrets_safe "$TMP/secrets_malformed.env"
echo "SURVIVED"
[ "\$TELEGRAM_BOT_TOKEN" = "1234:ABCDEF" ] || { echo "BAD TELEGRAM_BOT_TOKEN"; exit 1; }
[ "\$IMAP_PASSWORD" = "klcn xxxx xxxx xxxx" ] || { echo "BAD IMAP_PASSWORD [\$IMAP_PASSWORD]"; exit 1; }
[ "\$QUOTED_VAL" = "hello world" ] || { echo "BAD QUOTED_VAL"; exit 1; }
[ "\$SINGLE_QUOTED" = "foo bar" ] || { echo "BAD SINGLE_QUOTED"; exit 1; }
[ "\$NOTION_API_KEY" = "secret_abc123" ] || { echo "BAD NOTION_API_KEY"; exit 1; }
FUNCEOF
bash "$TMP/functest.sh" >"$TMP/functest.out" 2>"$TMP/functest.err"
ok "T14a malformed secret value does not abort the loop (set -e survives)" $?
want "T14b well-formed values still load (working case preserved)" "SURVIVED" "$TMP/functest.out"
nowant "T14c decrypted VALUE never logged (only file/key names)" "klcn xxxx" "$TMP/functest.err"

echo "== board #1275: accountant's exact production knobs render valid bash =="
# Mirrors docs/RUNBOOK-748-mac-launcher-alignment.md's M5/accountant install
# command (vault + model pin + --chrome --continue + inline-env + env-unset +
# extra-export PYTHONPATH) — the exact shape whose STALE (pre-#748) M5 render
# carried the line-74 syntax error. Proves the CURRENT lib renders clean.
LOOP_VAULT_PATH="/v/secrets.sops.env" LOOP_AGE_KEY_FILE="/k/age.txt" \
LOOP_MODEL="claude-opus-4-8[1m]" LOOP_CHROME=1 LOOP_CONTINUE=1 \
LOOP_INLINE_ENV="TELEGRAM_BOT_TOKEN NOTION_API_KEY QONTO_LOGIN QONTO_SECRET_KEY IMAP_PASSWORD_FIRM YOUSIGN_API_KEY" \
LOOP_ENV_UNSET="CLAUDE_CODE_OAUTH_TOKEN" \
LOOP_EXTRA_EXPORTS='PYTHONPATH="$HOME/x:${PYTHONPATH:-}"' \
  render /tmp/dept accountant /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$TMP/accountant.sh"
bash -n "$TMP/accountant.sh"; ok "T15 accountant-shaped render is valid bash (bash -n)" $?

echo "== board #1529: missing/broken safe-loader template fails LOUD =="
# render_loop_wrapper reads deploy/local/lib/safe_secrets_loader.sh.tmpl as a
# SIBLING of local_loop_lib.sh (via $_LLL_DIR). Copy just the lib (no sibling
# template) into an isolated dir to simulate "template missing" without
# touching the real repo file. Under `set -uo pipefail` (install-local-loop.sh
# deliberately has NO `-e`, so it can check exit codes itself), a `cat` on a
# missing file must NOT be allowed to silently continue with an empty
# safe_loader — that would render a wrapper that CALLS
# `_lll_load_secrets_safe` with its definition missing: passes `bash -n` (an
# undefined function is not a syntax error), installs fine, fails only at
# runtime. render_loop_wrapper must instead return non-zero and emit NOTHING.
ISOLATED="$TMP/isolated-lib"; mkdir -p "$ISOLATED"
cp "$LIB" "$ISOLATED/local_loop_lib.sh"
bash -c 'set -uo pipefail; source "$0"; render_loop_wrapper "$@"' \
    "$ISOLATED/local_loop_lib.sh" /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" \
    > "$TMP/missing_tmpl.out" 2>"$TMP/missing_tmpl.err"
rc=$?
if [[ "$rc" -ne 0 ]]; then echo "  PASS: T16a missing template -> render_loop_wrapper returns non-zero"; PASS=$((PASS+1)); else echo "  FAIL: T16a missing template -> render_loop_wrapper returned 0 (expected non-zero)"; FAIL=$((FAIL+1)); fi
if [[ ! -s "$TMP/missing_tmpl.out" ]]; then echo "  PASS: T16b missing template -> nothing emitted on stdout (no silently-broken wrapper)"; PASS=$((PASS+1)); else echo "  FAIL: T16b missing template -> stdout is non-empty (a broken wrapper would be written)"; FAIL=$((FAIL+1)); fi
want "T16c missing template -> error is logged to stderr" "safe_secrets_loader.sh.tmpl" "$TMP/missing_tmpl.err"

# Same, for an EXISTING-but-empty/wrong-content template (e.g. truncated by a
# bad checkout) — must be treated the same as missing, never silently used.
printf '' > "$ISOLATED/safe_secrets_loader.sh.tmpl"
bash -c 'set -uo pipefail; source "$0"; render_loop_wrapper "$@"' \
    "$ISOLATED/local_loop_lib.sh" /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" \
    > "$TMP/empty_tmpl.out" 2>"$TMP/empty_tmpl.err"
rc=$?
if [[ "$rc" -ne 0 ]]; then echo "  PASS: T16d empty template -> render_loop_wrapper returns non-zero"; PASS=$((PASS+1)); else echo "  FAIL: T16d empty template -> returned 0 (expected non-zero)"; FAIL=$((FAIL+1)); fi
if [[ ! -s "$TMP/empty_tmpl.out" ]]; then echo "  PASS: T16e empty template -> nothing emitted on stdout"; PASS=$((PASS+1)); else echo "  FAIL: T16e empty template -> stdout is non-empty"; FAIL=$((FAIL+1)); fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
