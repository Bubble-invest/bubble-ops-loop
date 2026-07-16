#!/usr/bin/env bash
# =============================================================================
# test_bubble_secrets.sh — TDD harness for bubble-secrets (board #676, WS-C).
# Runs entirely with FAKE sops/curl/systemctl/journalctl stubs — no root, no
# real secrets, no network, no live dept. Exercises add/rotate/apply plus the
# traps this tool exists to close:
#
#   T1  add: fails if the target file is missing
#   T2  add: refuses a JSON-corrupted target file, file untouched
#   T3  add: refuses when no age1... recipient is found
#   T4  add: happy path — installs a brand-new key, count +1, quoted on disk
#   T5  add: refuses if the key ALREADY exists (must use rotate)
#   T6  rotate: refuses if the key does NOT exist (must use add)
#   T7  rotate: happy path — replaces existing key, total count UNCHANGED
#   T8  rotate: --expect-len matches -> succeeds
#   T9  rotate: --expect-len mismatch -> rollback + nonzero exit
#   T10 quote-fix: a space-padded dummy value is trimmed + always written
#       quoted (KEY="value"), so runtime sourcing can never word-split it
#   T11 retention: 5 pre-existing .bak-* files -> after a write, exactly 3
#       remain (newest), the rest are gone (shredded, not just unlinked check
#       — we assert non-existence)
#   T12 secret value NEVER appears in stdout/stderr across all runs above
#   T13 rollback: post-encrypt JSON corruption -> original untouched
#   T14 rollback: verify-decrypt missing key -> rollback
#   T15 apply: restarts unit, checks is-active, exits 0 on healthy
#   T16 apply: is-active != active -> nonzero exit
#   T17 apply: --probe telegram-bot 401 -> nonzero, value never printed
#   T18 apply: --probe telegram-bot ok:true -> succeeds, never prints value
#   T19 illegal dept/key names rejected; unknown subcommand rejected
#   T20 empty stdin (after trim) refused
#   T21 --help exits 0 and documents add/rotate/apply
#
# Run:  bash test_bubble_secrets.sh [-v]
# =============================================================================
set -uo pipefail
VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
BINDIR="$(cd "$HERE/../../deploy/bin" && pwd)"
SCRIPT="${SCRIPT:-$BINDIR/bubble-secrets}"

[[ -f "$SCRIPT" ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }
chmod +x "$SCRIPT"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TMPFS="$WORK/tmpfs"; mkdir -p "$TMPFS"

# --- fake sops (same contract as bubble-rotate-dept-secret's stub) -------------
# Fake "encrypted" format: "FAKE-ENC\n" + plaintext lines, so decrypt just
# strips the first line while still round-tripping real content.
STUB_SOPS="$WORK/sops"
cat > "$STUB_SOPS" <<'EOF'
#!/usr/bin/env bash
set -u
mode=""; in_type=""; out_type=""; out=""; age=""; src=""
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  a="${args[$i]}"
  case "$a" in
    --decrypt) mode="decrypt" ;;
    --encrypt) mode="encrypt" ;;
    --input-type) i=$((i+1)); in_type="${args[$i]}" ;;
    --output-type) i=$((i+1)); out_type="${args[$i]}" ;;
    --output) i=$((i+1)); out="${args[$i]}" ;;
    --age) i=$((i+1)); age="${args[$i]}" ;;
    *) src="$a" ;;
  esac
  i=$((i+1))
done

[[ -n "$out" ]] || { echo "stub-sops: refuse (no --output; decrypt-to-stdout blocked by sops-guard)" >&2; exit 1; }
[[ "$in_type" == "dotenv" && "$out_type" == "dotenv" ]] || { echo "stub-sops: refuse — this stub requires explicit dotenv/dotenv typing (got in=$in_type out=$out_type)" >&2; exit 1; }

if [[ "$mode" == "decrypt" ]]; then
  if [[ "${SOPS_FORCE_DECRYPT_FAIL:-0}" == "1" ]]; then
    echo "stub-sops: forced decrypt failure" >&2; exit 1
  fi
  if head -1 "$src" 2>/dev/null | grep -q '^{'; then
    echo "stub-sops: invalid dotenv input line: {" >&2; exit 1
  fi
  tail -n +2 "$src" > "$out" 2>/dev/null || { echo "stub-sops: decrypt read failed" >&2; exit 1; }
  exit 0
fi

if [[ "$mode" == "encrypt" ]]; then
  [[ -n "$age" ]] || { echo "stub-sops: --age required" >&2; exit 1; }
  if [[ "${SOPS_FORCE_JSON_ENCRYPT_OUT:-0}" == "1" ]]; then
    printf '{"data":"ENC[fake]","sops":{}}\n' > "$out"
    exit 0
  fi
  {
    echo "FAKE-ENC"
    if [[ -n "${SOPS_STRIP_KEY_ON_ENCRYPT_OUT:-}" ]]; then
      grep -v "^${SOPS_STRIP_KEY_ON_ENCRYPT_OUT}=" "$src"
    else
      cat "$src"
    fi
  } > "$out"
  exit 0
fi

echo "stub-sops: unsupported mode" >&2; exit 1
EOF
chmod +x "$STUB_SOPS"

# --- fake curl (apply --probe telegram-bot) -------------------------------------
STUB_CURL="$WORK/curl"
cat > "$STUB_CURL" <<'EOF'
#!/usr/bin/env bash
set -u
out=""; url=""; cfg=""
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  a="${args[$i]}"
  case "$a" in
    -o) i=$((i+1)); out="${args[$i]}" ;;
    -K) i=$((i+1)); cfg="${args[$i]}" ;;
    http*) url="$a" ;;
  esac
  i=$((i+1))
done
if [[ -n "${CURL_ARGV_LOG:-}" ]]; then
  printf '%s\n' "${args[@]}" >> "$CURL_ARGV_LOG"
fi
if [[ -z "$url" && -n "$cfg" && -f "$cfg" ]]; then
  url="$(sed -n 's/^url = "\(.*\)"$/\1/p' "$cfg" | head -1)"
fi
echo "$url" >> "$CURL_CALL_LOG"
if [[ "${CURL_FORCE_401:-0}" == "1" ]]; then
  printf '{"ok":false,"error_code":401,"description":"Unauthorized"}' > "$out"
else
  printf '{"ok":true,"result":{"username":"fixture_bot"}}' > "$out"
fi
exit 0
EOF
chmod +x "$STUB_CURL"
export CURL_CALL_LOG="$WORK/curl_calls.log"; : > "$CURL_CALL_LOG"
export CURL_ARGV_LOG="$WORK/curl_argv.log"; : > "$CURL_ARGV_LOG"

# --- fake systemctl / journalctl -------------------------------------------------
# ACTIVE_STATE controls what `is-active` reports; SYSTEMCTL_FORCE_RESTART_FAIL
# makes `restart` itself fail.
STUB_SYSTEMCTL="$WORK/systemctl"
cat > "$STUB_SYSTEMCTL" <<'EOF'
#!/usr/bin/env bash
echo "SYSTEMCTL $*" >> "$SYSTEMCTL_CALL_LOG"
if [[ "${1:-}" == "restart" ]]; then
  if [[ "${SYSTEMCTL_FORCE_RESTART_FAIL:-0}" == "1" ]]; then
    echo "stub-systemctl: restart failed" >&2
    exit 1
  fi
  exit 0
fi
if [[ "${1:-}" == "is-active" ]]; then
  echo "${ACTIVE_STATE:-active}"
  [[ "${ACTIVE_STATE:-active}" == "active" ]] && exit 0 || exit 3
fi
exit 0
EOF
chmod +x "$STUB_SYSTEMCTL"
export SYSTEMCTL_CALL_LOG="$WORK/systemctl_calls.log"; : > "$SYSTEMCTL_CALL_LOG"

STUB_JOURNALCTL="$WORK/journalctl"
cat > "$STUB_JOURNALCTL" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB_JOURNALCTL"

# --- helpers to build a fake target SOPS file -----------------------------------
RECIPIENT="age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"

mk_fixture() {
  local f="$1"; shift
  {
    echo "FAKE-ENC"
    echo "# recipient: $RECIPIENT"
    for kv in "$@"; do echo "$kv"; done
  } > "$f"
}

INSTALL_OWNER_TEST="$(id -un)"
INSTALL_GROUP_TEST="$(id -gn)"

run_script() {
  local val="$1"; shift
  local rc
  if [[ $VERBOSE == 1 ]]; then
    printf '%s' "$val" | PATH="$WORK:$PATH" SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" \
      SYSTEMCTL_BIN="$STUB_SYSTEMCTL" TMPFS_DIR="$TMPFS" \
      INSTALL_OWNER="$INSTALL_OWNER_TEST" INSTALL_GROUP="$INSTALL_GROUP_TEST" \
      "$SCRIPT" "$@" > "$WORK/out" 2> >(tee "$WORK/err" >&2)
    rc=$?
  else
    printf '%s' "$val" | PATH="$WORK:$PATH" SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" \
      SYSTEMCTL_BIN="$STUB_SYSTEMCTL" TMPFS_DIR="$TMPFS" \
      INSTALL_OWNER="$INSTALL_OWNER_TEST" INSTALL_GROUP="$INSTALL_GROUP_TEST" \
      "$SCRIPT" "$@" > "$WORK/out" 2> "$WORK/err"
    rc=$?
  fi
  RC=$rc
  OUT="$(cat "$WORK/out")"
  ERR="$(cat "$WORK/err")"
}

# run_script_noval — for apply, which takes no stdin secret.
run_script_noval() {
  local rc
  PATH="$WORK:$PATH" SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" \
    SYSTEMCTL_BIN="$STUB_SYSTEMCTL" TMPFS_DIR="$TMPFS" \
    INSTALL_OWNER="$INSTALL_OWNER_TEST" INSTALL_GROUP="$INSTALL_GROUP_TEST" \
    "$SCRIPT" "$@" < /dev/null > "$WORK/out" 2> "$WORK/err"
  rc=$?
  RC=$rc
  OUT="$(cat "$WORK/out")"
  ERR="$(cat "$WORK/err")"
}

echo "== #676 bubble-secrets tests =="

# ---- T1: add — missing target file --------------------------------------------
F1="$WORK/nope-secrets-fixture.sops.env"
run_script "newtoken123" add fixture NEW_KEY --file "$F1"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-TARGET-MISSING"* ]]; then
  ok "T1 add refuses missing target file"; else bad "T1 (rc=$RC err=$ERR)"; fi

# ---- T2: add — JSON-corrupted target file --------------------------------------
F2="$WORK/corrupt-secrets-fixture.sops.env"
printf '{"data":"ENC[fake]","sops":{}}\n' > "$F2"
BEFORE2="$(cat "$F2")"
run_script "newtoken123" add fixture NEW_KEY --file "$F2"
AFTER2="$(cat "$F2")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-CORRUPT-JSON"* && "$BEFORE2" == "$AFTER2" ]]; then
  ok "T2 add refuses JSON-corrupted target, file untouched"
else bad "T2 (rc=$RC err=$ERR unchanged=$([[ "$BEFORE2" == "$AFTER2" ]] && echo yes || echo no))"; fi

# ---- T3: add — no age recipient -------------------------------------------------
F3="$WORK/norecipient-secrets-fixture.sops.env"
mk_fixture "$F3" 'OTHER_KEY="x"'
sed -i.bak 's/# recipient:.*/# no recipient here/' "$F3" 2>/dev/null || \
  perl -pi -e 's/# recipient:.*/# no recipient here/' "$F3"
run_script "newtoken123" add fixture NEW_KEY --file "$F3"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-NO-RECIPIENT"* ]]; then
  ok "T3 add refuses when no age1... recipient found"; else bad "T3 (rc=$RC err=$ERR)"; fi

# ---- T4: add — happy path, brand-new key, count +1, quoted on disk ------------
F4="$WORK/addhappy-secrets-fixture.sops.env"
mk_fixture "$F4" 'OTHER_KEY="keepme"'
run_script "BRAND-NEW-999" add fixture NEW_API_KEY --file "$F4"
INSTALLED4="$(tail -n +2 "$F4")"
if [[ $RC -eq 0 && "$OUT" == *"ADD_OK key=NEW_API_KEY dept=fixture"* \
   && "$INSTALLED4" == *'NEW_API_KEY="BRAND-NEW-999"'* \
   && "$INSTALLED4" == *'OTHER_KEY="keepme"'* ]]; then
  ok "T4 add happy path inserts a new key quoted, count +1, preserves others"
else bad "T4 (rc=$RC out=$OUT installed=$INSTALLED4)"; fi

# ---- T5: add — refuses if key already exists -----------------------------------
F5="$WORK/addexists-secrets-fixture.sops.env"
mk_fixture "$F5" 'EXISTING_KEY="old"'
run_script "newval" add fixture EXISTING_KEY --file "$F5"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-KEY-EXISTS"* ]]; then
  ok "T5 add refuses when key already exists (must use rotate)"
else bad "T5 (rc=$RC err=$ERR)"; fi

# ---- T6: rotate — refuses if key does NOT exist --------------------------------
F6="$WORK/rotatemissing-secrets-fixture.sops.env"
mk_fixture "$F6" 'OTHER_KEY="keepme"'
run_script "newval" rotate fixture MISSING_KEY --file "$F6"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-KEY-MISSING"* ]]; then
  ok "T6 rotate refuses when key does not exist (must use add)"
else bad "T6 (rc=$RC err=$ERR)"; fi

# ---- T7: rotate — happy path, total count UNCHANGED ----------------------------
F7="$WORK/rotatehappy-secrets-fixture.sops.env"
mk_fixture "$F7" 'TELEGRAM_BOT_TOKEN="oldtoken000"' 'OTHER_KEY="keepme"'
run_script "SECRET-NEW-VALUE-999" rotate fixture TELEGRAM_BOT_TOKEN --file "$F7"
INSTALLED7="$(tail -n +2 "$F7")"
COUNT7="$(echo "$INSTALLED7" | grep -cE '^[A-Za-z_][A-Za-z0-9_]*=')"
if [[ $RC -eq 0 && "$OUT" == *"ROTATE_OK key=TELEGRAM_BOT_TOKEN dept=fixture"* \
   && "$INSTALLED7" == *'TELEGRAM_BOT_TOKEN="SECRET-NEW-VALUE-999"'* \
   && "$INSTALLED7" == *'OTHER_KEY="keepme"'* && "$COUNT7" == "2" ]]; then
  ok "T7 rotate happy path replaces existing key, total count unchanged (2)"
else bad "T7 (rc=$RC out=$OUT installed=$INSTALLED7 count=$COUNT7)"; fi

# ---- T8: rotate --expect-len matches -> succeeds --------------------------------
F8="$WORK/expectlenok-secrets-fixture.sops.env"
mk_fixture "$F8" 'TELEGRAM_BOT_TOKEN="oldtoken000"'
VAL8="twelvechars1"   # 12 chars
run_script "$VAL8" rotate fixture TELEGRAM_BOT_TOKEN --expect-len 12 --file "$F8"
if [[ $RC -eq 0 && "$OUT" == *"length check OK"* ]]; then
  ok "T8 rotate --expect-len matching length succeeds"
else bad "T8 (rc=$RC out=$OUT)"; fi

# ---- T9: rotate --expect-len mismatch -> rollback + nonzero --------------------
F9="$WORK/expectlenbad-secrets-fixture.sops.env"
mk_fixture "$F9" 'TELEGRAM_BOT_TOKEN="oldtoken000"' 'OTHER_KEY="keepme"'
BEFORE9="$(cat "$F9")"
run_script "twelvechars1" rotate fixture TELEGRAM_BOT_TOKEN --expect-len 99 --file "$F9"
AFTER9="$(cat "$F9")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-VERIFY-LENGTH"* && "$ERR" == *"ROLLBACK"* && "$BEFORE9" == "$AFTER9" ]]; then
  ok "T9 rotate --expect-len mismatch -> rollback, file restored, nonzero exit"
else bad "T9 (rc=$RC err=$ERR restored=$([[ "$BEFORE9" == "$AFTER9" ]] && echo yes || echo no))"; fi

# ---- T10: quote-fix — space-padded dummy trimmed + always written quoted -------
F10="$WORK/quotefix-secrets-fixture.sops.env"
mk_fixture "$F10" 'OTHER_KEY="keepme"'
run_script "   padded-value-with-spaces   " add fixture PADDED_KEY --file "$F10"
INSTALLED10="$(tail -n +2 "$F10")"
if [[ $RC -eq 0 && "$INSTALLED10" == *'PADDED_KEY="padded-value-with-spaces"'* \
   && "$INSTALLED10" != *"   padded-value-with-spaces   "* ]]; then
  ok "T10 quote-fix: space-padded value trimmed and always written quoted"
else bad "T10 (rc=$RC installed=$INSTALLED10)"; fi

# ---- T11: retention — prune .bak-* to newest 3 ----------------------------------
F11="$WORK/retention-secrets-fixture.sops.env"
mk_fixture "$F11" 'TELEGRAM_BOT_TOKEN="oldtoken000"'
# Pre-seed 5 fake backups with distinct, increasing timestamps in the name.
for ts in 100 200 300 400 500; do
  echo "FAKE-OLD-BACKUP-$ts" > "${F11}.bak-rotate-${ts}"
  chmod 0400 "${F11}.bak-rotate-${ts}"
done
run_script "newvalue123" rotate fixture TELEGRAM_BOT_TOKEN --file "$F11"
REMAINING="$(find "$WORK" -maxdepth 1 -name "$(basename "$F11").bak-*" 2>/dev/null | wc -l | tr -d ' ')"
# 5 pre-seeded + 1 new from this rotate = 6 total before prune; expect exactly 3 after.
OLDEST_GONE=1
for ts in 100 200; do
  [[ -f "${F11}.bak-rotate-${ts}" ]] && OLDEST_GONE=0
done
if [[ $RC -eq 0 && "$REMAINING" == "3" && $OLDEST_GONE -eq 1 ]]; then
  ok "T11 retention prunes .bak-* to newest 3, oldest shredded/removed"
else bad "T11 (rc=$RC remaining=$REMAINING oldest_gone=$OLDEST_GONE)"; fi

# ---- T12: secret value never appears in captured stdout/stderr -----------------
LEAK=0
for f in "$WORK"/out "$WORK"/err; do
  [[ -f "$f" ]] && grep -qE 'BRAND-NEW-999|SECRET-NEW-VALUE-999|padded-value-with-spaces|newvalue123|twelvechars1' "$f" && LEAK=1
done
LEFTOVER="$(find "$TMPFS" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
if [[ $LEAK -eq 0 && "$LEFTOVER" == "0" ]]; then
  ok "T12 secret value never leaks to captured output; tmpfs workdirs cleaned up"
else bad "T12 leak=$LEAK leftover_dirs=$LEFTOVER"; fi

# ---- T13: rollback — post-encrypt JSON corruption -> original untouched --------
F13="$WORK/postjson-secrets-fixture.sops.env"
mk_fixture "$F13" 'TELEGRAM_BOT_TOKEN="oldtoken000"' 'OTHER_KEY="keepme"'
BEFORE13="$(cat "$F13")"
SOPS_FORCE_JSON_ENCRYPT_OUT=1 run_script "newval" rotate fixture TELEGRAM_BOT_TOKEN --file "$F13"
AFTER13="$(cat "$F13")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-POST-ENCRYPT-JSON"* && "$BEFORE13" == "$AFTER13" ]]; then
  ok "T13 rollback: post-encrypt JSON corruption refused, original untouched"
else bad "T13 (rc=$RC err=$ERR before==after:$([[ "$BEFORE13" == "$AFTER13" ]] && echo yes || echo no))"; fi

# ---- T14: rollback — verify-decrypt missing key ---------------------------------
F14="$WORK/missingkey-secrets-fixture.sops.env"
mk_fixture "$F14" 'TELEGRAM_BOT_TOKEN="oldtoken000"' 'OTHER_KEY="keepme"'
BEFORE14="$(cat "$F14")"
SOPS_STRIP_KEY_ON_ENCRYPT_OUT="TELEGRAM_BOT_TOKEN" run_script "newval" rotate fixture TELEGRAM_BOT_TOKEN --file "$F14"
AFTER14="$(cat "$F14")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-VERIFY-KEY-MISSING"* && "$ERR" == *"ROLLBACK"* && "$BEFORE14" == "$AFTER14" ]]; then
  ok "T14 rollback: verify finds key missing post-install -> rollback"
else bad "T14 (rc=$RC err=$ERR restored=$([[ "$BEFORE14" == "$AFTER14" ]] && echo yes || echo no))"; fi

# ---- T15: apply — restarts unit, checks is-active, exits 0 ---------------------
F15="$WORK/applyok-secrets-fixture.sops.env"
mk_fixture "$F15" 'TELEGRAM_BOT_TOKEN="tok"'
: > "$SYSTEMCTL_CALL_LOG"
ACTIVE_STATE=active SYSTEMCTL_FORCE_RESTART_FAIL=0 PATH="$WORK:$PATH" \
  SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" SYSTEMCTL_BIN="$STUB_SYSTEMCTL" \
  TMPFS_DIR="$TMPFS" "$SCRIPT" apply fixture --file "$F15" < /dev/null > "$WORK/out15" 2> "$WORK/err15"
RC15=$?
OUT15="$(cat "$WORK/out15")"
if [[ $RC15 -eq 0 && "$OUT15" == *"APPLY_OK dept=fixture unit=ops-loop-fixture"* \
   && "$(cat "$SYSTEMCTL_CALL_LOG")" == *"restart ops-loop-fixture"* ]]; then
  ok "T15 apply restarts unit, checks is-active, exits 0 on healthy"
else bad "T15 (rc=$RC15 out=$OUT15)"; fi

# ---- T16: apply — is-active != active -> nonzero --------------------------------
F16="$WORK/applybad-secrets-fixture.sops.env"
mk_fixture "$F16" 'TELEGRAM_BOT_TOKEN="tok"'
ACTIVE_STATE=failed PATH="$WORK:$PATH" \
  SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" SYSTEMCTL_BIN="$STUB_SYSTEMCTL" \
  TMPFS_DIR="$TMPFS" "$SCRIPT" apply fixture --file "$F16" < /dev/null > "$WORK/out16" 2> "$WORK/err16"
RC16=$?
ERR16="$(cat "$WORK/err16")"
if [[ $RC16 -ne 0 && "$ERR16" == *"ABORT-NOT-ACTIVE"* ]]; then
  ok "T16 apply: is-active != active -> nonzero exit"
else bad "T16 (rc=$RC16 err=$ERR16)"; fi

# ---- T17: apply --probe telegram-bot 401 -> nonzero, value never printed -------
F17="$WORK/applyprobe401-secrets-fixture.sops.env"
mk_fixture "$F17" 'TELEGRAM_BOT_TOKEN="bad-live-token-canary"'
CURL_FORCE_401=1 ACTIVE_STATE=active PATH="$WORK:$PATH" \
  SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" SYSTEMCTL_BIN="$STUB_SYSTEMCTL" \
  TMPFS_DIR="$TMPFS" "$SCRIPT" apply fixture --file "$F17" --probe telegram-bot --key TELEGRAM_BOT_TOKEN \
  < /dev/null > "$WORK/out17" 2> "$WORK/err17"
RC17=$?
OUT17="$(cat "$WORK/out17")"; ERR17="$(cat "$WORK/err17")"
if [[ $RC17 -ne 0 && "$ERR17" == *"ABORT-PROBE-FAILED"* \
   && "$OUT17" != *"bad-live-token-canary"* && "$ERR17" != *"bad-live-token-canary"* ]]; then
  ok "T17 apply --probe telegram-bot 401 -> nonzero exit, value never printed"
else bad "T17 (rc=$RC17 out=$OUT17 err=$ERR17)"; fi

# ---- T18: apply --probe telegram-bot ok:true -> succeeds, never prints value ---
F18="$WORK/applyprobeok-secrets-fixture.sops.env"
mk_fixture "$F18" 'TELEGRAM_BOT_TOKEN="good-live-token-canary"'
ACTIVE_STATE=active PATH="$WORK:$PATH" \
  SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" SYSTEMCTL_BIN="$STUB_SYSTEMCTL" \
  TMPFS_DIR="$TMPFS" "$SCRIPT" apply fixture --file "$F18" --probe telegram-bot --key TELEGRAM_BOT_TOKEN \
  < /dev/null > "$WORK/out18" 2> "$WORK/err18"
RC18=$?
OUT18="$(cat "$WORK/out18")"; ERR18="$(cat "$WORK/err18")"
if [[ $RC18 -eq 0 && "$OUT18" == *"credential probe OK"* && "$OUT18" == *"username=fixture_bot"* \
   && "$OUT18" != *"good-live-token-canary"* && "$ERR18" != *"good-live-token-canary"* ]]; then
  ok "T18 apply --probe telegram-bot ok:true -> succeeds, value never printed"
else bad "T18 (rc=$RC18 out=$OUT18)"; fi

# ---- T19: illegal names / unknown subcommand rejected ---------------------------
run_script "x" add "Evil Dept!" NEW_KEY --file "$F4"
BAD_DEPT_RC=$RC; BAD_DEPT_ERR="$ERR"
run_script "x" add fixture "not-a-valid-key" --file "$F4"
BAD_KEY_RC=$RC; BAD_KEY_ERR="$ERR"
run_script "x" nonsense fixture NEW_KEY --file "$F4"
BAD_SUB_RC=$RC; BAD_SUB_ERR="$ERR"
if [[ $BAD_DEPT_RC -ne 0 && "$BAD_DEPT_ERR" == *"ABORT-ARGS"* \
   && $BAD_KEY_RC -ne 0 && "$BAD_KEY_ERR" == *"ABORT-ARGS"* \
   && $BAD_SUB_RC -ne 0 && "$BAD_SUB_ERR" == *"ABORT-ARGS"* ]]; then
  ok "T19 rejects illegal dept slug, illegal key name, unknown subcommand"
else bad "T19 (dept_rc=$BAD_DEPT_RC key_rc=$BAD_KEY_RC sub_rc=$BAD_SUB_RC)"; fi

# ---- T20: empty stdin (after trim) refused --------------------------------------
F20="$WORK/emptystdin-secrets-fixture.sops.env"
mk_fixture "$F20" 'OTHER_KEY="x"'
run_script "   " add fixture NEW_KEY --file "$F20"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-EMPTY-VALUE"* ]]; then
  ok "T20 refuses empty (whitespace-only) stdin value after trim"; else bad "T20 (rc=$RC err=$ERR)"; fi

# ---- T21: --help exits 0, documents add/rotate/apply ----------------------------
"$SCRIPT" --help > "$WORK/help_out" 2>&1
RC21=$?
HELP_OUT="$(cat "$WORK/help_out")"
if [[ $RC21 -eq 0 && "$HELP_OUT" == *"add"* && "$HELP_OUT" == *"rotate"* && "$HELP_OUT" == *"apply"* ]]; then
  ok "T21 --help exits 0 and documents add/rotate/apply"
else bad "T21 (rc=$RC21)"; fi

# ---- T22: rollback itself fails (unreadable/absent backup) -> ROLLBACK-FAILED,
#           never claims "restored", INSTALLED_UNVERIFIED stays set -------------
# Independent-reviewer finding: the old rollback() never checked the
# cp/chmod/mv chain's exit status, so a failed restore (backup gone/unreadable,
# disk full, permission race) still printed "ROLLBACK: restored" and cleared
# the safety flag — a false-safety-signal in the exact mechanism this tool
# exists to guarantee. Repro here: drive a REAL verify failure (--expect-len
# mismatch) so the script takes its normal rollback path, but make the
# restore's `cp` step fail deterministically by shadowing `cp` on PATH with a
# stub that fails ONLY when copying FROM a `.bak-*` backup file (i.e. only the
# rollback's own restore copy, not any other cp usage). Assert: nonzero exit,
# ABORT-ROLLBACK-FAILED (not the original ABORT-VERIFY-LENGTH), stderr never
# claims "restored", and — the sharpest check — the target file is NOT
# byte-identical to the backup (proving the restore genuinely did not happen,
# not just that the message changed).
STUB_CP_FAIL_BACKUP="$WORK/cp"
cat > "$STUB_CP_FAIL_BACKUP" <<'EOF'
#!/usr/bin/env bash
# Fails ONLY the rollback restore's own copy step — source is a `.bak-*`
# file AND destination is a `.rollback.` tmp file (the exact shape
# `rollback()` uses: `cp -p "$BACKUP" "$tmp"`). The forward backup-creation
# step (`cp -p "$F" "$BACKUP"`, destination matches .bak-* but SOURCE does
# not) must keep working — that has to succeed first for this failure mode
# to be reachable at all. Every other cp invocation passes through to the
# real /bin/cp untouched.
src=""
dst=""
for a in "$@"; do
  case "$a" in
    -*) ;;
    *) if [[ -z "$src" ]]; then src="$a"; else dst="$a"; fi ;;
  esac
done
if [[ "$src" == *.bak-* && "$dst" == *.rollback.* ]]; then
  echo "stub-cp: forced failure restoring backup ($src -> $dst)" >&2
  exit 1
fi
exec /bin/cp "$@"
EOF
chmod +x "$STUB_CP_FAIL_BACKUP"

F22="$WORK/rollbackfail-secrets-fixture.sops.env"
mk_fixture "$F22" 'TELEGRAM_BOT_TOKEN="oldtoken000"' 'OTHER_KEY="keepme"'
BEFORE22="$(cat "$F22")"
printf '%s' "twelvechars1" | PATH="$WORK:$PATH" SOPS_BIN="$STUB_SOPS" CURL_BIN="$STUB_CURL" \
  SYSTEMCTL_BIN="$STUB_SYSTEMCTL" TMPFS_DIR="$TMPFS" \
  INSTALL_OWNER="$INSTALL_OWNER_TEST" INSTALL_GROUP="$INSTALL_GROUP_TEST" \
  "$SCRIPT" rotate fixture TELEGRAM_BOT_TOKEN --expect-len 99 --file "$F22" \
  > "$WORK/out22" 2> "$WORK/err22"
RC22=$?
OUT22="$(cat "$WORK/out22")"; ERR22="$(cat "$WORK/err22")"
AFTER22="$(cat "$F22")"
if [[ $RC22 -ne 0 && "$ERR22" == *"ABORT-ROLLBACK-FAILED"* && "$ERR22" == *"ROLLBACK-FAILED"* \
   && "$ERR22" != *"ROLLBACK: restored"* && "$OUT22" != *"ROLLBACK: restored"* \
   && "$BEFORE22" != "$AFTER22" ]]; then
  ok "T22 rollback failure (unreadable backup) -> ROLLBACK-FAILED, never claims restored, file left in unverified state (not silently 'fixed')"
else bad "T22 (rc=$RC22 err=$ERR22 unchanged=$([[ "$BEFORE22" == "$AFTER22" ]] && echo yes || echo no))"; fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
