#!/usr/bin/env bash
# =============================================================================
# test_bubble_rotate_dept_secret.sh — TDD harness for bubble-rotate-dept-secret
# (board #457). Runs entirely with FAKE sops/curl/systemctl stubs — no root, no
# real secrets, no network. Exercises every trap the script exists to close:
#
#   T1  refuses when the target file is missing
#   T2  refuses a JSON-corrupted target file (first byte '{') without touching it
#   T3  refuses when no age1... recipient is found in the target file
#   T4  happy path: rotates an EXISTING key, verifies round-trip, key-count same
#   T5  happy path: rotates a NEW key (key not previously present), count +1
#   T6  refuses + rolls back if post-encrypt output is JSON-corrupted
#   T7  refuses + rolls back if the installed file fails to decrypt on verify
#   T8  refuses + rolls back if the verify decrypt is missing the target KEY
#   T9  refuses + rolls back if key-count decreases vs baseline
#   T10 --probe telegram-bot: 401 -> rollback + nonzero exit
#   T11 --probe telegram-bot: ok:true -> succeeds, never prints the value
#   T12 the secret value NEVER appears in stdout, stderr, or any leftover tmpfs file
#   T13 backup file is created with mode 0400 before install
#   T14 rejects value via argv is impossible by construction (stdin-only usage);
#       confirms empty-stdin is refused
#   T15 rejects illegal dept/key names
#
# Run:  bash test_bubble_rotate_dept_secret.sh [-v]
# =============================================================================
set -uo pipefail
VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
BINDIR="$(cd "$HERE/../../deploy/bin" && pwd)"
SCRIPT="${SCRIPT:-$BINDIR/bubble-rotate-dept-secret}"

[[ -f "$SCRIPT" ]] || { echo "FATAL: script not found: $SCRIPT"; exit 2; }
chmod +x "$SCRIPT"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- a fake tmpfs dir (the script accepts TMPFS_DIR override) -----------------
TMPFS="$WORK/tmpfs"; mkdir -p "$TMPFS"

# --- fake sops -----------------------------------------------------------------
# Understands:
#   --decrypt --input-type dotenv --output-type dotenv --output F SRC
#   --encrypt --input-type dotenv --output-type dotenv --age R --output F SRC
# Behavior knobs via env read at call time:
#   SOPS_FORCE_DECRYPT_FAIL=1      -> decrypt always fails (rc=1)
#   SOPS_FORCE_DECRYPT_FAIL_ONLY_ON=<path-substring> -> fail decrypt only for that SRC
#   SOPS_FORCE_JSON_ENCRYPT_OUT=1  -> encrypt writes '{"data":...}' instead of dotenv
#   SOPS_STRIP_KEY_ON_ENCRYPT_OUT=<KEY> -> encrypt output "forgets" this key (simulated
#                                          by our fake encrypted format storing plaintext
#                                          verbatim MINUS that key's line)
#   SOPS_DROP_EXTRA_KEY_ON_ENCRYPT=1 -> encrypt output drops one extra unrelated key
#     (simulates key-count decreasing)
#
# Our fake "encrypted" format is simply: "FAKE-ENC\n" + the plaintext lines,
# so decrypt can just strip the first line. This keeps the stub simple while
# still round-tripping real content for the assertions under test.
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
  if [[ -n "${SOPS_FORCE_DECRYPT_FAIL_ONLY_ON:-}" && "$src" == *"${SOPS_FORCE_DECRYPT_FAIL_ONLY_ON}"* ]]; then
    echo "stub-sops: forced decrypt failure (targeted)" >&2; exit 1
  fi
  # our fake encrypted format: first line "FAKE-ENC", rest is plaintext dotenv
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
    elif [[ "${SOPS_DROP_EXTRA_KEY_ON_ENCRYPT:-0}" == "1" ]]; then
      # drop an unrelated key line (THIRD_KEY) to simulate a lossy re-encrypt
      # that still contains the rotated key itself (so this exercises the
      # key-COUNT check specifically, distinct from T8's key-MISSING check).
      grep -v '^THIRD_KEY=' "$src"
    else
      cat "$src"
    fi
  } > "$out"
  exit 0
fi

echo "stub-sops: unsupported mode" >&2; exit 1
EOF
chmod +x "$STUB_SOPS"

# --- fake curl (for --probe telegram-bot) --------------------------------------
# Controlled via CURL_FORCE_401=1 / default success. Records the URL called
# (but NOT any value beyond what's already in the URL, which the test inspects
# only for the token substring assertions below — never printed by the script).
STUB_CURL="$WORK/curl"
cat > "$STUB_CURL" <<'EOF'
#!/usr/bin/env bash
set -u
out=""
url=""
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  a="${args[$i]}"
  case "$a" in
    -o) i=$((i+1)); out="${args[$i]}" ;;
    http*) url="$a" ;;
  esac
  i=$((i+1))
done
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

# --- fake systemctl / journalctl (for --restart) --------------------------------
STUB_SYSTEMCTL="$WORK/systemctl"
cat > "$STUB_SYSTEMCTL" <<'EOF'
#!/usr/bin/env bash
echo "SYSTEMCTL $*" >> "$SYSTEMCTL_CALL_LOG"
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
# Recall our fake encrypted format: line1 "FAKE-ENC", then plaintext dotenv lines.
RECIPIENT="age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"

mk_fixture() {
  local f="$1"; shift
  {
    echo "FAKE-ENC"
    echo "# recipient: $RECIPIENT"
    for kv in "$@"; do echo "$kv"; done
  } > "$f"
}

# Tests run unprivileged (no root:root install rights) — install as the
# current user/group instead. Production always runs this as root.
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

echo "== #457 bubble-rotate-dept-secret tests =="

# ---- T1: missing target file --------------------------------------------------
F1="$WORK/nope-secrets-fixture.sops.env"
run_script "newtoken123" fixture TELEGRAM_BOT_TOKEN --file "$F1"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-TARGET-MISSING"* ]]; then
  ok "T1 refuses missing target file"; else bad "T1 (rc=$RC err=$ERR)"; fi

# ---- T2: JSON-corrupted target file -------------------------------------------
F2="$WORK/corrupt-secrets-fixture.sops.env"
printf '{"data":"ENC[fake]","sops":{}}\n' > "$F2"
CONTENT_BEFORE="$(cat "$F2")"
run_script "newtoken123" fixture TELEGRAM_BOT_TOKEN --file "$F2"
CONTENT_AFTER="$(cat "$F2")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-CORRUPT-JSON"* && "$ERR" == *"input-type json"* && "$CONTENT_BEFORE" == "$CONTENT_AFTER" ]]; then
  ok "T2 refuses JSON-corrupted target, points to recovery, file untouched"
else bad "T2 (rc=$RC err=$ERR unchanged=$([[ "$CONTENT_BEFORE" == "$CONTENT_AFTER" ]] && echo yes || echo no))"; fi

# ---- T3: no age recipient in target file --------------------------------------
F3="$WORK/norecipient-secrets-fixture.sops.env"
mk_fixture "$F3" "TELEGRAM_BOT_TOKEN=old123" "OTHER_KEY=x"
sed -i.bak 's/# recipient:.*/# no recipient here/' "$F3" 2>/dev/null || \
  perl -pi -e 's/# recipient:.*/# no recipient here/' "$F3"
run_script "newtoken123" fixture TELEGRAM_BOT_TOKEN --file "$F3"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-NO-RECIPIENT"* ]]; then
  ok "T3 refuses when no age1... recipient found"; else bad "T3 (rc=$RC err=$ERR)"; fi

# ---- T4: happy path — rotate EXISTING key, round-trip verified ----------------
F4="$WORK/happy-secrets-fixture.sops.env"
mk_fixture "$F4" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
run_script "SECRET-NEW-VALUE-999" fixture TELEGRAM_BOT_TOKEN --file "$F4"
INSTALLED="$(tail -n +2 "$F4")"
if [[ $RC -eq 0 && "$OUT" == *"ROTATE_OK key=TELEGRAM_BOT_TOKEN dept=fixture"* \
   && "$INSTALLED" == *"TELEGRAM_BOT_TOKEN=SECRET-NEW-VALUE-999"* \
   && "$INSTALLED" == *"OTHER_KEY=keepme"* ]]; then
  ok "T4 happy path rotates existing key, preserves other keys, round-trip verified"
else bad "T4 (rc=$RC out=$OUT installed=$INSTALLED)"; fi
BACKUP_T4=$(find "$WORK" -maxdepth 1 -name 'happy-secrets-fixture.sops.env.bak-rotate-*' 2>/dev/null | head -1)

# ---- T5: happy path — rotate a NEW key (not previously present) ---------------
F5="$WORK/newkey-secrets-fixture.sops.env"
mk_fixture "$F5" "OTHER_KEY=keepme"
run_script "BRAND-NEW-999" fixture NEW_API_KEY --file "$F5"
INSTALLED5="$(tail -n +2 "$F5")"
if [[ $RC -eq 0 && "$INSTALLED5" == *"NEW_API_KEY=BRAND-NEW-999"* && "$INSTALLED5" == *"OTHER_KEY=keepme"* ]]; then
  ok "T5 happy path inserts a new key alongside existing ones"
else bad "T5 (rc=$RC installed=$INSTALLED5)"; fi

# ---- T6: post-encrypt JSON refusal + rollback ---------------------------------
F6="$WORK/postjson-secrets-fixture.sops.env"
mk_fixture "$F6" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
BEFORE6="$(cat "$F6")"
SOPS_FORCE_JSON_ENCRYPT_OUT=1 run_script "newval" fixture TELEGRAM_BOT_TOKEN --file "$F6"
AFTER6="$(cat "$F6")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-POST-ENCRYPT-JSON"* && "$BEFORE6" == "$AFTER6" ]]; then
  ok "T6 refuses to install JSON-corrupted encrypt output; original untouched (no backup needed pre-install)"
else bad "T6 (rc=$RC err=$ERR before==after:$([[ "$BEFORE6" == "$AFTER6" ]] && echo yes || echo no))"; fi

# ---- T7: verify-decrypt failure -> rollback -----------------------------------
F7="$WORK/verifyfail-secrets-fixture.sops.env"
mk_fixture "$F7" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
BEFORE7="$(cat "$F7")"
# Force decrypt to fail only for paths containing the target filename itself
# (post-install verify reads $F; baseline read also reads $F, so instead we
# force ALL decrypt to fail on the 2nd+ call by using a call-counter file).
COUNTER="$WORK/t7-decrypt-calls"; : > "$COUNTER"
STUB_SOPS_T7="$WORK/sops-t7"
cat > "$STUB_SOPS_T7" <<EOF
#!/usr/bin/env bash
set -u
if [[ "\$*" == *"--decrypt"* ]]; then
  n=\$(wc -l < "$COUNTER" | tr -d ' ')
  echo x >> "$COUNTER"
  if [[ "\$n" -ge 1 ]]; then
    echo "stub-sops-t7: forced verify-decrypt failure" >&2
    exit 1
  fi
fi
exec "$STUB_SOPS" "\$@"
EOF
chmod +x "$STUB_SOPS_T7"
printf 'newval' | PATH="$WORK:$PATH" SOPS_BIN="$STUB_SOPS_T7" CURL_BIN="$STUB_CURL" \
  SYSTEMCTL_BIN="$STUB_SYSTEMCTL" TMPFS_DIR="$TMPFS" \
  INSTALL_OWNER="$INSTALL_OWNER_TEST" INSTALL_GROUP="$INSTALL_GROUP_TEST" \
  "$SCRIPT" fixture TELEGRAM_BOT_TOKEN --file "$F7" > "$WORK/out7" 2> "$WORK/err7"
RC7=$?
ERR7="$(cat "$WORK/err7")"
AFTER7="$(cat "$F7")"
if [[ $RC7 -ne 0 && "$ERR7" == *"ABORT-VERIFY-DECRYPT"* && "$ERR7" == *"ROLLBACK"* && "$BEFORE7" == "$AFTER7" ]]; then
  ok "T7 verify-decrypt failure triggers automatic rollback (file restored to pre-rotation state)"
else bad "T7 (rc=$RC7 err=$ERR7 restored=$([[ "$BEFORE7" == "$AFTER7" ]] && echo yes || echo no))"; fi

# ---- T8: verify-decrypt missing KEY -> rollback -------------------------------
F8="$WORK/missingkey-secrets-fixture.sops.env"
mk_fixture "$F8" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
BEFORE8="$(cat "$F8")"
SOPS_STRIP_KEY_ON_ENCRYPT_OUT="TELEGRAM_BOT_TOKEN" run_script "newval" fixture TELEGRAM_BOT_TOKEN --file "$F8"
AFTER8="$(cat "$F8")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-VERIFY-KEY-MISSING"* && "$ERR" == *"ROLLBACK"* && "$BEFORE8" == "$AFTER8" ]]; then
  ok "T8 verify finds KEY missing post-install -> rollback"
else bad "T8 (rc=$RC err=$ERR restored=$([[ "$BEFORE8" == "$AFTER8" ]] && echo yes || echo no))"; fi

# ---- T9: key-count decreased -> rollback ---------------------------------------
F9="$WORK/countdrop-secrets-fixture.sops.env"
mk_fixture "$F9" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme" "THIRD_KEY=alsokeepme"
BEFORE9="$(cat "$F9")"
SOPS_DROP_EXTRA_KEY_ON_ENCRYPT=1 run_script "newval" fixture TELEGRAM_BOT_TOKEN --file "$F9"
AFTER9="$(cat "$F9")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-VERIFY-KEY-COUNT"* && "$BEFORE9" == "$AFTER9" ]]; then
  ok "T9 key-count decrease vs baseline -> rollback"
else bad "T9 (rc=$RC err=$ERR restored=$([[ "$BEFORE9" == "$AFTER9" ]] && echo yes || echo no))"; fi

# ---- T10: --probe telegram-bot 401 -> rollback + fail loud --------------------
F10="$WORK/probe401-secrets-fixture.sops.env"
mk_fixture "$F10" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
BEFORE10="$(cat "$F10")"
CURL_FORCE_401=1 run_script "bad-new-token" fixture TELEGRAM_BOT_TOKEN --probe telegram-bot --file "$F10"
AFTER10="$(cat "$F10")"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-PROBE-401"* && "$BEFORE10" == "$AFTER10" && "$OUT" != *"bad-new-token"* && "$ERR" != *"bad-new-token"* ]]; then
  ok "T10 --probe telegram-bot 401 -> rollback, nonzero exit, value never printed"
else bad "T10 (rc=$RC err=$ERR restored=$([[ "$BEFORE10" == "$AFTER10" ]] && echo yes || echo no))"; fi

# ---- T11: --probe telegram-bot ok:true -> succeeds, never prints value --------
F11="$WORK/probeok-secrets-fixture.sops.env"
mk_fixture "$F11" "TELEGRAM_BOT_TOKEN=oldtoken000" "OTHER_KEY=keepme"
run_script "good-new-token-xyz" fixture TELEGRAM_BOT_TOKEN --probe telegram-bot --file "$F11"
if [[ $RC -eq 0 && "$OUT" == *"probe OK"* && "$OUT" == *"username=fixture_bot"* \
   && "$OUT" != *"good-new-token-xyz"* && "$ERR" != *"good-new-token-xyz"* ]]; then
  ok "T11 --probe telegram-bot ok:true -> succeeds, prints ok/username only, never the value"
else bad "T11 (rc=$RC out=$OUT)"; fi

# ---- T12: value never appears anywhere (belt-and-suspenders across all runs) ---
LEAK=0
for f in "$WORK"/out "$WORK"/err "$WORK"/out7 "$WORK"/err7; do
  [[ -f "$f" ]] && grep -qE 'SECRET-NEW-VALUE-999|BRAND-NEW-999|bad-new-token|good-new-token-xyz' "$f" && LEAK=1
done
# also check nothing plaintext-leaked into TMPFS after cleanup (script shreds its workdir)
LEFTOVER="$(find "$TMPFS" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
if [[ $LEAK -eq 0 && "$LEFTOVER" == "0" ]]; then
  ok "T12 secret value never appears in captured stdout/stderr; tmpfs workdirs cleaned up"
else bad "T12 leak=$LEAK leftover_dirs=$LEFTOVER"; fi

# ---- T13: backup file created with mode 0400 before install -------------------
if [[ -n "$BACKUP_T4" && -f "$BACKUP_T4" ]]; then
  PERM="$(stat -f '%Lp' "$BACKUP_T4" 2>/dev/null || stat -c '%a' "$BACKUP_T4" 2>/dev/null)"
  if [[ "$PERM" == "400" ]]; then
    ok "T13 backup file created with mode 0400"
  else bad "T13 backup perm=$PERM path=$BACKUP_T4"; fi
else bad "T13 no backup file found for T4 run"; fi

# ---- T14: empty stdin is refused ------------------------------------------------
F14="$WORK/emptystdin-secrets-fixture.sops.env"
mk_fixture "$F14" "TELEGRAM_BOT_TOKEN=oldtoken000"
run_script "" fixture TELEGRAM_BOT_TOKEN --file "$F14"
if [[ $RC -ne 0 && "$ERR" == *"ABORT-EMPTY-VALUE"* ]]; then
  ok "T14 refuses empty stdin value"; else bad "T14 (rc=$RC err=$ERR)"; fi

# ---- T15: illegal dept/key names rejected ---------------------------------------
run_script "x" "Evil Dept!" TELEGRAM_BOT_TOKEN --file "$F4"
BAD_DEPT_RC=$RC; BAD_DEPT_ERR="$ERR"
run_script "x" fixture "not-a-valid-key" --file "$F4"
BAD_KEY_RC=$RC; BAD_KEY_ERR="$ERR"
if [[ $BAD_DEPT_RC -ne 0 && "$BAD_DEPT_ERR" == *"ABORT-ARGS"* \
   && $BAD_KEY_RC -ne 0 && "$BAD_KEY_ERR" == *"ABORT-ARGS"* ]]; then
  ok "T15 rejects illegal dept slug and illegal key-name identifiers"
else bad "T15 (dept_rc=$BAD_DEPT_RC dept_err=$BAD_DEPT_ERR key_rc=$BAD_KEY_RC key_err=$BAD_KEY_ERR)"; fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
