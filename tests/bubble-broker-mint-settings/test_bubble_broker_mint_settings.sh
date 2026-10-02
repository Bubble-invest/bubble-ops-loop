#!/usr/bin/env bash
# =============================================================================
# test_bubble_broker_mint_settings.sh — TDD harness for the WS5 root broker-mint
# wrapper (bubble-broker-mint-settings-root.sh) + the claude-side shim.
#
# Runs WITHOUT root / without real secrets: it stubs the broker + sops, points
# the wrapper at fake tmpfs/age/pem paths via env overrides, and uses MINT_DRY=1
# (and a stub broker) so NOTHING hits GitHub and NO real key is touched.
#
# Asserts the wrapper (the sudo-able root script):
#   W1  REFUSES a non-`mint` subcommand.
#   W2  REFUSES --action outside {settings_pr, runtime_write_own} (open_priority_pr).
#   W12-W18 (#1619) runtime_write_own: own-dept only (SUDO_USER), policy-forced.
#   W3  REFUSES an unknown --dept (no installation id mapped).
#   W4  REFUSES a repo that isn't the dept's own bubble-ops-<dept>.
#   W5  REFUSES an unknown/extra flag (no arg smuggling into the broker).
#   W6  REFUSES missing required field (--repo absent).
#   W7  ACCEPTS a valid fixture settings_pr mint and resolves the RIGHT
#       APP_ID + per-dept INSTALLATION_ID + --no-sops/--pem-path (MINT_DRY).
#   W8  ACCEPTS a valid maya settings_pr mint and resolves INST_ID 135214360.
#   W9  Forwards --paths through to the broker (when minting for real, via stub).
#   W10 SECRET HYGIENE: the decrypted PEM tmpfs file is shredded/removed on exit
#       (no plaintext key left behind), even though we ran a "decrypt".
#   W11 The shim execs `sudo -n <root helper>` with argv intact.
#
# Run:  bash test_bubble_broker_mint_settings.sh [-v]
# =============================================================================
set -uo pipefail
VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
# Wrappers live in deploy/bin/ (test dir is tests/bubble-broker-mint-settings/).
# Resolve there by default; ROOT_WRAP/SHIM env overrides still win.
BINDIR="$(cd "$HERE/../../deploy/bin" && pwd)"
ROOT_WRAP="${ROOT_WRAP:-$BINDIR/bubble-broker-mint-settings-root.sh}"
SHIM="${SHIM:-$BINDIR/bubble-broker-mint-settings.sh}"

[[ -f "$ROOT_WRAP" ]] || { echo "FATAL: root wrapper not found: $ROOT_WRAP"; exit 2; }
[[ -f "$SHIM" ]]      || { echo "FATAL: shim not found: $SHIM"; exit 2; }
chmod +x "$ROOT_WRAP" "$SHIM"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- fakes -------------------------------------------------------------------
# A fake "age key" + a fake SOPS-encrypted PEM (content irrelevant; the stub
# sops just copies it). A stub sops that honors --output FILE. A stub broker
# that records argv + prints a fake token. A private tmpfs dir we can inspect.
AGE="$WORK/age.txt";      echo "AGE-KEY-FAKE" > "$AGE"
ENC="$WORK/enc.pem";      echo "ENCRYPTED-PEM-FAKE" > "$ENC"
TMPFS="$WORK/tmpfs";      mkdir -p "$TMPFS"
CAP="$WORK/broker_capture.log"; : > "$CAP"

STUB_SOPS="$WORK/sops"
cat > "$STUB_SOPS" <<EOF
#!/usr/bin/env bash
# stub sops: supports '--decrypt ... --output FILE <enc>' -> writes plaintext.
out=""; src=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --output) out="\$2"; shift 2 ;;
    --decrypt|--input-type|--output-type|binary) shift ;;
    *) src="\$1"; shift ;;
  esac
done
[[ -n "\$out" ]] || { echo "stub-sops: refuse decrypt-to-stdout" >&2; exit 1; }
printf 'DECRYPTED-PEM-PLAINTEXT\n' > "\$out"
exit 0
EOF
chmod +x "$STUB_SOPS"

STUB_BROKER="$WORK/broker"
cat > "$STUB_BROKER" <<EOF
#!/usr/bin/env bash
# stub broker: record argv (incl resolved --app-id/--installation-id/--pem-path),
# assert env GITHUB_APP_ID is set, print a fake ghs_ token.
{ printf 'BROKER'; printf ' %q' "\$@"; printf ' ENV_APP_ID=%s' "\${GITHUB_APP_ID:-UNSET}"; printf '\n'; } >> "$CAP"
echo "ghs_FAKETOKEN_FROM_STUB"
exit 0
EOF
chmod +x "$STUB_BROKER"

# common env that retargets the wrapper at the fakes
common_env() {
  BROKER_BIN="${BROKER_OVERRIDE:-$STUB_BROKER}" SOPS_BIN="$STUB_SOPS" \
  AGE_KEY_FILE="$AGE" SOPS_PEM_PATH="$ENC" \
  TMPFS_DIRS="$TMPFS" "$@"
}

run_wrap() {
  if [[ $VERBOSE == 1 ]]; then
    common_env "$ROOT_WRAP" "$@" > "$WORK/out" 2> >(tee "$WORK/err" >&2); RC=$?
  else
    common_env "$ROOT_WRAP" "$@" > "$WORK/out" 2> "$WORK/err"; RC=$?
  fi
  OUT="$(cat "$WORK/out")"; ERR="$(cat "$WORK/err")"
}

echo "== WS5 bubble-broker-mint-settings-root tests =="

# ---- W1: non-mint subcommand ------------------------------------------------
run_wrap check --dept fixture --action settings_pr --repo bubble-ops-fixture
if [[ $RC -eq 2 && "$ERR" == *"only the 'mint' subcommand"* ]]; then
  ok "W1 refuses non-mint subcommand"; else bad "W1 (rc=$RC err=$ERR)"; fi

# ---- W2: non-settings_pr action ---------------------------------------------
run_wrap mint --dept fixture --action open_priority_pr --repo bubble-ops-fixture
if [[ $RC -eq 2 && "$ERR" == *"must be 'settings_pr', 'runtime_write_own' or 'runtime_read'"* ]]; then
  ok "W2 refuses open_priority_pr action"; else bad "W2 (rc=$RC err=$ERR)"; fi

# ---- W3: unknown dept -------------------------------------------------------
run_wrap mint --dept eviltwin --action settings_pr --repo bubble-ops-eviltwin
if [[ $RC -eq 2 && "$ERR" == *"no installation id mapped"* ]]; then
  ok "W3 refuses unknown dept (no inst id)"; else bad "W3 (rc=$RC err=$ERR)"; fi

# ---- W4: cross-repo (repo != bubble-ops-<dept>) -----------------------------
run_wrap mint --dept fixture --action settings_pr --repo bubble-ops-maya
if [[ $RC -eq 2 && "$ERR" == *"not the dept's own repo"* ]]; then
  ok "W4 refuses repo that isn't the dept's own"; else bad "W4 (rc=$RC err=$ERR)"; fi

# ---- W5: unknown/extra flag -------------------------------------------------
run_wrap mint --dept fixture --action settings_pr --repo bubble-ops-fixture --evil-flag x
if [[ $RC -eq 2 && "$ERR" == *"disallowed/unknown argument"* ]]; then
  ok "W5 refuses unknown flag (no arg smuggling)"; else bad "W5 (rc=$RC err=$ERR)"; fi

# ---- W6: missing required field (--repo) ------------------------------------
run_wrap mint --dept fixture --action settings_pr
if [[ $RC -eq 2 && "$ERR" == *"--repo is required"* ]]; then
  ok "W6 refuses missing --repo"; else bad "W6 (rc=$RC err=$ERR)"; fi

# ---- W7: VALID fixture mint (MINT_DRY) resolves right APP_ID + INST_ID -------
MINT_DRY=1 run_wrap mint --dept fixture --action settings_pr --repo bubble-ops-fixture
if [[ $RC -eq 0 && "$OUT" == ghs_DRYRUN_* \
   && "$ERR" == *"APP_ID=3782718"* \
   && "$ERR" == *"GITHUB_APP_INSTALLATION_ID_FIXTURE=134075326"* ]]; then
  ok "W7 accepts fixture settings_pr; resolves APP_ID 3782718 + INST 134075326"
else bad "W7 (rc=$RC out=$OUT err=$ERR)"; fi

# ---- W8: VALID maya mint resolves INST 135214360 ----------------------------
MINT_DRY=1 run_wrap mint --dept maya --action settings_pr --repo bubble-ops-maya
if [[ $RC -eq 0 && "$ERR" == *"GITHUB_APP_INSTALLATION_ID_MAYA=135214360"* ]]; then
  ok "W8 accepts maya settings_pr; resolves INST 135214360"
else bad "W8 (rc=$RC err=$ERR)"; fi

# ---- W9: real (stub) mint forwards --paths + passes env + --no-sops/--pem ---
: > "$CAP"
run_wrap mint --dept fixture --action settings_pr --repo bubble-ops-fixture \
         --paths layers/1/PROMPT.md CLAUDE.md
BL="$(cat "$CAP")"
if [[ $RC -eq 0 && "$OUT" == "ghs_FAKETOKEN_FROM_STUB" \
   && "$BL" == "BROKER mint "* \
   && "$BL" == *"--paths layers/1/PROMPT.md CLAUDE.md"* \
   && "$BL" == *"--no-sops"* && "$BL" == *"--pem-path"* \
   && "$BL" == *"--installation-id 134075326"* \
   && "$BL" == *"--app-id 3782718"* \
   && "$BL" == *"ENV_APP_ID=3782718"* ]]; then
  ok "W9 real(stub) mint: forwards 'mint' subcmd + paths + env+flags, returns token"
else bad "W9 (rc=$RC out=$OUT broker_line=$BL)"; fi

# ---- W10: secret hygiene — no decrypted PEM left in the tmpfs dir -----------
# After all the above runs, the tmpfs dir must contain no surviving pem file.
LEFT="$(find "$TMPFS" -type f -name 'bubble-settings-pem.*' 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$LEFT" == "0" ]]; then
  ok "W10 secret hygiene: decrypted PEM shredded/removed (none left on tmpfs)"
else bad "W10 left $LEFT pem file(s) on tmpfs: $(find "$TMPFS" -type f)"; fi

# ---- W11: shim execs sudo -n <root helper> with argv intact -----------------
FAKE_SUDO_LOG="$WORK/sudo.log"; : > "$FAKE_SUDO_LOG"
FAKE_SUDO="$WORK/sudo"
cat > "$FAKE_SUDO" <<EOF
#!/usr/bin/env bash
{ printf 'SUDO'; printf ' %q' "\$@"; printf '\n'; } >> "$FAKE_SUDO_LOG"
exit 0
EOF
chmod +x "$FAKE_SUDO"
PATH="$WORK:$PATH" BUBBLE_BROKER_MINT_ROOT="/usr/local/bin/bubble-broker-mint-settings-root.sh" \
  "$SHIM" mint --dept fixture --action settings_pr --repo bubble-ops-fixture >/dev/null 2>&1
SL="$(cat "$FAKE_SUDO_LOG")"
if [[ "$SL" == *"-n /usr/local/bin/bubble-broker-mint-settings-root.sh mint --dept fixture --action settings_pr --repo bubble-ops-fixture"* ]]; then
  ok "W11 shim execs 'sudo -n <root helper>' with argv intact"
else bad "W11 sudo_line=$SL"; fi

# =============================================================================
# #1619 step 1: runtime_write_own, own-dept only, via the broker policy
# =============================================================================
POLDIR="$WORK/policies"; mkdir -p "$POLDIR"
cat > "$POLDIR/tony-policy.yaml" <<'EOF'
github_access:
  actor: ops-loop-tony
  own_repo: bubble-ops-tony
  write:
    - repo: bubble-ops-tony
      allowed_paths: ["outputs/**", "queues/**"]
      mode: direct_runtime_commit
EOF
cp "$POLDIR/tony-policy.yaml" "$POLDIR/maya-policy.yaml"
# run as if sudo'd by $1 (SUDO_USER); BUBBLE_MINT_TEST_NONROOT stands in for EUID 0
run_rw() { local su="$1"; shift
  SUDO_USER="$su" BUBBLE_MINT_TEST_NONROOT=1 POLICY_DIR="$POLDIR" run_wrap "$@"; }

# ---- W12: own-dept runtime_write_own accepted; broker forced onto the dept policy
: > "$CAP"
run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths outputs/a.md queues/x.json
BL="$(cat "$CAP")"
if [[ $RC -eq 0 && "$OUT" == "ghs_FAKETOKEN_FROM_STUB" \
   && "$BL" == *"--action runtime_write_own"* \
   && "$BL" == *"--paths outputs/a.md queues/x.json --policy $POLDIR/tony-policy.yaml"* \
   && "$BL" == *"--installation-id 135214360"* ]]; then
  ok "W12 own-dept runtime_write_own accepted; --policy forced to tony-policy.yaml"
else bad "W12 (rc=$RC out=$OUT bl=$BL err=$ERR)"; fi

# ---- W13: another dept's dept/repo denied (cross-dept)
run_rw agent-tony mint --dept maya --action runtime_write_own --repo bubble-ops-maya --paths outputs/a.md
[[ $RC -eq 2 && "$ERR" == *"may only mint for its own dept"* ]] \
  && ok "W13 agent-tony cannot mint for maya" || bad "W13 (rc=$RC err=$ERR)"
run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-maya --paths outputs/a.md
[[ $RC -eq 2 && "$ERR" == *"not the dept's own repo"* ]] \
  && ok "W13b agent-tony cannot target maya's repo" || bad "W13b (rc=$RC err=$ERR)"
run_rw agent-tony mint --dept maya --action settings_pr --repo bubble-ops-maya
[[ $RC -eq 2 && "$ERR" == *"may only mint for its own dept"* ]] \
  && ok "W13c settings_pr also bound to caller dept" || bad "W13c (rc=$RC err=$ERR)"

# ---- W14: missing --paths, and non-agent caller, refused
run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-tony
[[ $RC -eq 2 && "$ERR" == *"requires --paths"* ]] && ok "W14 runtime_write_own without --paths refused" || bad "W14 (rc=$RC err=$ERR)"
run_rw claude mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths outputs/a.md
[[ $RC -eq 2 && "$ERR" == *"requires an agent-<slug> caller"* ]] && ok "W14b non-agent caller (claude) refused" || bad "W14b (rc=$RC err=$ERR)"

# ---- W15: SUDO_USER spoof when NOT run via sudo (EUID != 0, no test override)
if [[ "$(id -u)" != "0" ]]; then
  SUDO_USER=agent-tony POLICY_DIR="$POLDIR" run_wrap mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths outputs/a.md
  [[ $RC -eq 2 && "$ERR" == *"must be invoked through sudo"* ]] \
    && ok "W15 spoofed SUDO_USER without sudo (EUID!=0) refused" || bad "W15 (rc=$RC err=$ERR)"
else echo "  SKIP: W15 (running as root)"; fi

# ---- W16: arg/action injection denied; caller policy env ignored
run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths outputs/a.md --policy /tmp/evil.yaml
[[ $RC -eq 2 && "$ERR" == *"disallowed/unknown argument"* ]] && ok "W16 caller --policy rejected" || bad "W16 (rc=$RC err=$ERR)"
run_rw agent-tony mint --dept tony --action "runtime_write_own --action settings_pr" --repo bubble-ops-tony --paths outputs/a.md
[[ $RC -eq 2 && "$ERR" == *"must be 'settings_pr', 'runtime_write_own' or 'runtime_read'"* ]] && ok "W16b action injection via value refused" || bad "W16b (rc=$RC err=$ERR)"
: > "$CAP"
BUBBLE_TOKEN_BROKER_POLICY=/tmp/evil.yaml run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths outputs/a.md
BL="$(cat "$CAP")"
[[ $RC -eq 0 && "$BL" == *"--policy $POLDIR/tony-policy.yaml"* ]] && ok "W16c caller BUBBLE_TOKEN_BROKER_POLICY ignored" || bad "W16c (rc=$RC bl=$BL)"

# ---- W17: missing policy file => refuse (fail-closed)
rm -f "$POLDIR/maya-policy.yaml"
run_rw agent-maya mint --dept maya --action runtime_write_own --repo bubble-ops-maya --paths outputs/a.md
[[ $RC -eq 2 && "$ERR" == *"no policy file"* ]] && ok "W17 no policy file => refused" || bad "W17 (rc=$RC err=$ERR)"

# ---- W18: structural / traversal path denied by the REAL broker policy.enforce
# (real broker CLI, --mock-github: no network). Skips if python yaml/broker src absent.
BROKER_SRC="$(cd "$HERE/../../token-broker" && pwd)"
if python3 -c "import yaml" 2>/dev/null && [[ -f "$BROKER_SRC/src/cli.py" ]]; then
  REALB="$WORK/realbroker"
  printf '#!/usr/bin/env bash\ncd "%s" && exec python3 -m src.cli "$@" --mock-github --audit-log "%s/audit.jsonl"\n' "$BROKER_SRC" "$WORK" > "$REALB"; chmod +x "$REALB"
  real_rw() { BROKER_OVERRIDE="$REALB" run_rw agent-tony mint --dept tony --action runtime_write_own --repo bubble-ops-tony --paths "$@"; }
  real_rw outputs/ok.md;         [[ $RC -eq 0 && "$OUT" == ghs_MOCK* ]] && ok "W18 real policy: outputs/ file issued" || bad "W18 (rc=$RC err=$ERR)"
  real_rw dept.yaml;             [[ $RC -ne 0 && "$ERR" == *"DENIED"* ]] && ok "W18b real policy: dept.yaml denied" || bad "W18b (rc=$RC err=$ERR)"
  real_rw outputs/../MANDATE.md; [[ $RC -ne 0 && "$ERR" == *"DENIED"* ]] && ok "W18c real policy: '..' traversal denied" || bad "W18c (rc=$RC err=$ERR)"
  real_rw outputs/ok.md CLAUDE.md; [[ $RC -ne 0 && "$ERR" == *"DENIED"* ]] && ok "W18d real policy: mixed batch denied" || bad "W18d (rc=$RC err=$ERR)"
else echo "  SKIP: W18 (python yaml / broker src unavailable)"; fi

# ---- W19: runtime_read (guard's pre-push base-fetch token): own dept, no --paths needed
: > "$CAP"
run_rw agent-tony mint --dept tony --action runtime_read --repo bubble-ops-tony
BL="$(cat "$CAP")"
[[ $RC -eq 0 && "$BL" == *"--action runtime_read"* && "$BL" == *"--policy $POLDIR/tony-policy.yaml"* ]] \
  && ok "W19 own-dept runtime_read accepted (policy forced)" || bad "W19 (rc=$RC bl=$BL err=$ERR)"
run_rw agent-tony mint --dept maya --action runtime_read --repo bubble-ops-maya
[[ $RC -eq 2 && "$ERR" == *"may only mint for its own dept"* ]] && ok "W19b cross-dept runtime_read denied" || bad "W19b (rc=$RC err=$ERR)"
run_rw claude mint --dept tony --action runtime_read --repo bubble-ops-tony
[[ $RC -eq 2 ]] && ok "W19c non-agent runtime_read denied" || bad "W19c (rc=$RC)"

# ---- W10b: no PEM left after the new paths either
LEFT="$(find "$TMPFS" -type f -name 'bubble-settings-pem.*' 2>/dev/null | wc -l | tr -d ' ')"
[[ "$LEFT" == "0" ]] && ok "W10b no PEM left after runtime_write_own runs" || bad "W10b left $LEFT pem file(s)"

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
