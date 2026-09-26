#!/usr/bin/env bash
# =============================================================================
# test_1529_bash32_wrapper_render.sh — regression test for board #1529.
#
# Bug: `install-local-loop.sh ... --vault <path>` run under macOS STOCK bash
# 3.2 (the Mac's /bin/bash — never upgraded by Apple since the GPLv3 switch)
# failed with:
#   local_loop_lib.sh: line 315: _f: unbound variable
# and left an EMPTY wrapper file on disk. Root cause: render_loop_wrapper
# (deploy/local/lib/local_loop_lib.sh) used to build its safe_loader string
# via `cat <<'SAFE_LOADER_EOF' ... SAFE_LOADER_EOF` INSIDE a `$(...)` command
# substitution. That heredoc body contains 4 literal `\'` (backslash-single-
# quote) sequences (the `_val` quote-stripping case arms). bash 3.2's parser
# mis-tracks the heredoc's true end in that shape, so part of the heredoc body
# leaks out and gets executed as LIVE script during `source` — under the
# caller's `set -u` (install-local-loop.sh always sets it) that means `$_f`
# (a var meant only to live inside the _lll_load_secrets_safe function body)
# gets evaluated standalone and aborts the source with "unbound variable",
# and `render_loop_wrapper` (and every function after it in the file) is left
# undefined — so `render_loop_wrapper ... > "$WRAPPER_PATH"` in
# install-local-loop.sh both fails AND has already truncated/created
# $WRAPPER_PATH via the `>` redirect before the failure is even detected.
# bash 5 (homebrew, CI, any dev shell) parses the same heredoc fine, which is
# why this was invisible everywhere except a real Mac's stock /bin/bash.
#
# Fix (this board): the safe-loader body now lives in its own template file
# (deploy/local/lib/safe_secrets_loader.sh.tmpl) that render_loop_wrapper
# reads with a plain `cat` inside the command substitution — no heredoc for
# bash 3.2 to mis-parse. install-local-loop.sh now also renders the wrapper to
# a TEMP file in the same dir and only `mv`s it into place once `bash -n`
# passes, so a bad render (any cause) can never leave an empty/partial/corrupt
# file at the live wrapper path.
#
# This test is a REGRESSION GUARD, not a general render test (see
# test_local_loop_wrapper_render.sh / test_local_loop_plist_render.sh for
# those) — it specifically exercises the ORIGINAL bug shape (--vault, under
# bash 3.x) and the atomic-write guarantee. It SKIPS gracefully (exit 0) on
# any host with no bash 3.x available (e.g. Linux CI, or a Mac with only
# homebrew bash) — the bug is bash-3.2-specific and cannot be exercised
# without that interpreter.
#
# Usage: bash tests/test_1529_bash32_wrapper_render.sh \
#          <local_loop_lib.sh> <install-local-loop.sh>
#
# Assertions:
#   T1  a bash 3.x interpreter is found, or the test SKIPS (exit 0) cleanly.
#   T2  render_loop_wrapper with LOOP_VAULT_PATH set, sourced+called under
#       bash 3.x, does NOT abort ("unbound variable") and produces a
#       non-empty, bash -n-valid wrapper.
#   T3  the rendered wrapper contains NO leaked heredoc terminator
#       ("SAFE_LOADER_EOF") — the literal corruption signature of the
#       original parser bug (reproduced against the pre-fix lib in the PR
#       description, not re-derived here).
#   T4  the rendered wrapper still carries the safe-loader function + the
#       vault decrypt block (functional parity with the generic render test).
#   T5  full `install-local-loop.sh --vault <path>` run end-to-end under
#       bash 3.x exits 0 and writes a non-empty, valid wrapper.
#   T6  re-running the installer with a DELIBERATELY BROKEN knob (an
#       unbalanced-quote --extra-export, which fails the install's own
#       `bash -n` gate) exits non-zero AND leaves the PREVIOUSLY-GOOD wrapper
#       from T5 completely untouched (byte-identical) — proof of the
#       temp-file + mv atomic-write fix (board #1529 step 3).
#   T7  the same broken-knob install, run against a slug with NO prior
#       wrapper, leaves NO file at all at the wrapper path (never an empty
#       file) — the original failure mode, closed.
# =============================================================================
set -uo pipefail

LIB="${1:?usage: test_1529_bash32_wrapper_render.sh <local_loop_lib.sh> <install-local-loop.sh>}"
INSTALLER="${2:?usage: test_1529_bash32_wrapper_render.sh <local_loop_lib.sh> <install-local-loop.sh>}"
[[ -f "$LIB" ]]       || { echo "FATAL: not found: $LIB"; exit 2; }
[[ -f "$INSTALLER" ]] || { echo "FATAL: not found: $INSTALLER"; exit 2; }

PASS=0; FAIL=0
want()   { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2')"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -qF -- "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2')"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }
ok()     { if [[ "$2" -eq 0 ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (rc=$2)"; FAIL=$((FAIL+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ── T1: locate a bash 3.x interpreter, or skip gracefully ───────────────────
# Try common locations without assuming any exist. macOS ships bash 3.2 at
# /bin/bash (never upgraded post-GPLv3); Linux CI has no bash 3.x at all.
BASH32=""
for cand in /bin/bash /usr/bin/bash /opt/local/bin/bash3 /usr/local/bin/bash3; do
    [[ -x "$cand" ]] || continue
    v="$("$cand" -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null || echo "")"
    if [[ "$v" =~ ^[0-9]+$ ]] && [[ "$v" -lt 4 ]]; then BASH32="$cand"; break; fi
done

if [[ -z "$BASH32" ]]; then
    echo "SKIP: no bash 3.x interpreter found on this host (board #1529 bug is"
    echo "      bash-3.2-specific; nothing to regress-test here — e.g. Linux CI"
    echo "      or a Mac with only homebrew bash 4+/5 on PATH)."
    echo
    echo "RESULT: 0 passed, 0 failed (skipped)"
    exit 0
fi
echo "== using bash 3.x interpreter: $BASH32 ($("$BASH32" -c 'echo "$BASH_VERSION"')) =="
echo "  PASS: T1 bash 3.x interpreter found"; PASS=$((PASS+1))

# ── T2-T4: render_loop_wrapper directly, --vault-shaped knobs, under bash 3.x ─
echo "== render_loop_wrapper with LOOP_VAULT_PATH under bash 3.x =="
OUT="$TMP/vault_bash32.sh"
LOOP_VAULT_PATH="/v/secrets.sops.env" LOOP_AGE_KEY_FILE="/k/age.txt" \
  "$BASH32" -c 'set -uo pipefail; source "$0"; render_loop_wrapper "$@"' \
    "$LIB" /tmp/dept demo /usr/bin/claude /usr/bin/tmux /tmp/tg /bin "" "" > "$OUT" 2>"$TMP/vault_bash32.err"
rc=$?
ok "T2a render under bash 3.x + set -u exits 0 (no 'unbound variable' abort)" "$rc"
if [[ "$rc" -ne 0 ]]; then cat "$TMP/vault_bash32.err"; fi
if [[ -s "$OUT" ]]; then echo "  PASS: T2b rendered wrapper is non-empty"; PASS=$((PASS+1)); else echo "  FAIL: T2b rendered wrapper is EMPTY"; FAIL=$((FAIL+1)); fi
"$BASH32" -n "$OUT" 2>"$TMP/lint.err"; ok "T2c rendered wrapper is valid bash (bash -n under bash 3.x)" $?
nowant "T3 no leaked heredoc terminator (parser-corruption signature)" "SAFE_LOADER_EOF" "$OUT"
want   "T4a safe loader function present"   "_lll_load_secrets_safe() {" "$OUT"
want   "T4b vault decrypt block present"    "sops --decrypt --output"    "$OUT"
want   "T4c age key exported"               'SOPS_AGE_KEY_FILE="/k/age.txt"' "$OUT"

# ── T5-T7: end-to-end install-local-loop.sh --vault under bash 3.x ──────────
echo "== install-local-loop.sh --vault end-to-end under bash 3.x =="
WORK="$TMP/work"
mkdir -p "$WORK/agents/demo/outputs" "$WORK/LA" "$WORK/logs" "$WORK/wrap" "$WORK/tg"
printf 'fake-vault-content\n' > "$WORK/vault.sops.env"

"$BASH32" "$INSTALLER" --dept-dir "$WORK/agents/demo" --slug demo \
    --launch-agents-dir "$WORK/LA" --log-dir "$WORK/logs" --wrapper-dir "$WORK/wrap" \
    --claude-bin /usr/bin/claude --tmux-bin /usr/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --vault "$WORK/vault.sops.env" \
    >"$WORK/install.log" 2>&1
rc=$?
WRAPPER="$WORK/wrap/ops-loop-demo-wrapper.sh"
ok "T5a install-local-loop.sh --vault exits 0 under bash 3.x" "$rc"
if [[ "$rc" -ne 0 ]]; then cat "$WORK/install.log"; fi
if [[ -s "$WRAPPER" ]]; then echo "  PASS: T5b wrapper written and non-empty"; PASS=$((PASS+1)); else echo "  FAIL: T5b wrapper missing or EMPTY"; FAIL=$((FAIL+1)); fi
"$BASH32" -n "$WRAPPER" 2>/dev/null; ok "T5c installed wrapper is valid bash" $?
GOOD_SUM="$(cksum "$WRAPPER" 2>/dev/null)"

# T6: a deliberately broken knob (unbalanced quote in --extra-export) must
# fail the installer's OWN bash -n gate — and must NOT touch the previously
# installed good wrapper (temp-file + mv atomicity, board #1529 step 3).
"$BASH32" "$INSTALLER" --dept-dir "$WORK/agents/demo" --slug demo \
    --launch-agents-dir "$WORK/LA" --log-dir "$WORK/logs" --wrapper-dir "$WORK/wrap" \
    --claude-bin /usr/bin/claude --tmux-bin /usr/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --vault "$WORK/vault.sops.env" \
    --extra-export "BROKEN=\"unbalanced" \
    >"$WORK/install-broken.log" 2>&1
rc=$?
if [[ "$rc" -ne 0 ]]; then echo "  PASS: T6a broken-knob install exits non-zero"; PASS=$((PASS+1)); else echo "  FAIL: T6a broken-knob install exited 0 (expected non-zero)"; FAIL=$((FAIL+1)); fi
NEW_SUM="$(cksum "$WRAPPER" 2>/dev/null)"
if [[ "$GOOD_SUM" == "$NEW_SUM" ]]; then echo "  PASS: T6b prior good wrapper left byte-identical after failed re-render"; PASS=$((PASS+1)); else echo "  FAIL: T6b prior good wrapper was modified/truncated by the failed install"; FAIL=$((FAIL+1)); fi
if [[ -s "$WRAPPER" ]]; then echo "  PASS: T6c wrapper still non-empty after failed re-render"; PASS=$((PASS+1)); else echo "  FAIL: T6c wrapper is now EMPTY (the original board #1529 symptom)"; FAIL=$((FAIL+1)); fi
# No leftover temp file from the failed render.
if ls "$WORK/wrap"/.ops-loop-demo-wrapper.* >/dev/null 2>&1; then echo "  FAIL: T6d leftover temp render file not cleaned up"; FAIL=$((FAIL+1)); else echo "  PASS: T6d no leftover temp render file"; PASS=$((PASS+1)); fi

# T7: same broken knob, but for a slug with NO prior wrapper at all — must
# leave NO file behind (never an empty one).
WRAPPER2="$WORK/wrap/ops-loop-fresh-wrapper.sh"
"$BASH32" "$INSTALLER" --dept-dir "$WORK/agents/demo" --slug fresh \
    --launch-agents-dir "$WORK/LA" --log-dir "$WORK/logs" --wrapper-dir "$WORK/wrap" \
    --claude-bin /usr/bin/claude --tmux-bin /usr/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --vault "$WORK/vault.sops.env" \
    --extra-export "BROKEN=\"unbalanced" \
    >"$WORK/install-broken-fresh.log" 2>&1
rc=$?
if [[ "$rc" -ne 0 ]]; then echo "  PASS: T7a broken-knob install (no prior wrapper) exits non-zero"; PASS=$((PASS+1)); else echo "  FAIL: T7a exited 0 (expected non-zero)"; FAIL=$((FAIL+1)); fi
if [[ ! -e "$WRAPPER2" ]]; then echo "  PASS: T7b no wrapper file left behind (never an empty one)"; PASS=$((PASS+1)); else echo "  FAIL: T7b a wrapper file was left at $WRAPPER2 (size $(wc -c <"$WRAPPER2" 2>/dev/null))"; FAIL=$((FAIL+1)); fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
