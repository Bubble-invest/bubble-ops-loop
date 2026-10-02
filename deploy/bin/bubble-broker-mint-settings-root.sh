#!/bin/bash
# =============================================================================
# bubble-broker-mint-settings-root.sh — ROOT-owned broker-mint wrapper for the
# WS5 `settings_pr` path. Invoked ONLY via `sudo -n` by the claude-side shim
# (bubble-broker-mint-settings.sh), which the git-guard calls as its --broker.
# =============================================================================
#
# WHY THIS EXISTS
# ---------------
# `propose-settings-pr` -> `bubble-git-guard push --action settings_pr` ->
# broker `mint`. The broker mint needs THREE things the `claude` user cannot
# provide on its own:
#   1. GITHUB_APP_ID                         (non-secret constant = 3782718)
#   2. GITHUB_APP_INSTALLATION_ID_<DEPT>     (non-secret per-dept constant)
#   3. the GitHub App private key, which is SOPS-encrypted and only decryptable
#      with /etc/age/key.txt — and /etc/age/key.txt is ROOT-ONLY (0400 root).
# The runtime push path solves (3) for *long-running dept systemd units* by
# pre-decrypting the PEM to a per-dept tmpfs at unit start (ExecStartPre). But
# the settings_pr path can be driven by an ad-hoc / sandboxed `claude -p` that
# is NOT inside a dept unit (no pre-decrypted PEM, no APP_ID/INST_ID env). So
# this wrapper mints with ROOT access to the age key, exactly mirroring the
# bubble-gh-credential-helper.sh sudoers pattern (the established chokepoint).
#
# SECURITY MODEL (this script is sudo-able by `claude` — treat every arg hostile)
# -----------------------------------------------------------------------------
#  * It is NOT a general token-minting oracle. It accepts ONLY:
#       mint --dept <slug> --action settings_pr|runtime_write_own
#            --repo <repo> [--paths ...]
#    Any other subcommand, any --action outside that pair, an unknown
#    --dept, or an unknown flag => HARD REFUSE (exit 2). Both actions only ever
#    mint a token scoped to the dept's OWN repo, and the git-guard re-checks the
#    policy locally before this is even reached; this wrapper is the third gate.
#  * runtime_write_own (#1619 step 1; see the Chesterton note below) is the
#    broker-served replacement for the direct contents:write the credential
#    helper mints. It is stricter than settings_pr because it is the
#    direct-commit path:
#       - the dept is DERIVED from the calling uid (SUDO_USER=agent-<slug>,
#         set by sudo itself) and must equal --dept; never trusted from argv;
#         a non-agent caller (claude/Rick) or a non-root EUID (not run via
#         sudo, so SUDO_USER is attacker-controlled) is REFUSED;
#       - >=1 --paths is mandatory and the broker is forced to run
#         policy.enforce against THIS wrapper's root-owned
#         <POLICY_DIR>/<dept>-policy.yaml (path allow-list, structural paths
#         denied, '..' rejected); BUBBLE_TOKEN_BROKER_POLICY from the caller is
#         ignored/unset;
#       - the minted permission set is the broker's runtime_write_own class
#         (contents:write + metadata:read) on that single repo.
#    runtime_read (contents:read + metadata:read, own repo, same gates minus
#    --paths) is accepted only because the git-guard mints its pre-push base-fetch
#    token with it; without it a private-repo fetch behind this wrapper fails.
#
# CHESTERTON NOTE (why this was settings_pr-only until now)
# ---------------------------------------------------------
# 2ab2bd4 (WS5, 2026-06-02) built this wrapper ONLY for the sandboxed
# settings-PR path, which has no dept-unit env to mint with. The comment then was
# "runtime pushes go via the cred-helper / dept-unit env path; they must NEVER
# come through here": least privilege at the time - runtime pushes already had a
# working chokepoint (bubble-gh-credential-helper.sh), so a second sudo-able
# write-mint path was pure added surface. That reason is what #1619 inverts: the
# helper mints repo-wide contents:write with only a best-effort delta check, so
# the plan is to make it read-only for agent-* and serve runtime pushes from the
# broker, which enforces the path policy in code. Step 1 (this change) adds the
# broker path with NO reduction of the helper; step 2 removes the helper's write.
#  * The non-secret constants (APP_ID + per-dept INSTALLATION_ID) are baked in
#    here, same as the cred-helper hardcodes APP_ID=3782718 / the INST_ID switch.
#  * Secret hygiene mirrors bubble-gh-credential-helper.sh EXACTLY:
#       - PEM decrypted to a tmpfs file (/run/lock or /dev/shm), NEVER /tmp.
#       - `--output FILE` (the sops-guard wrapper BLOCKS decrypt-to-stdout).
#       - `trap cleanup EXIT INT TERM` shred-removes the PEM on every exit path.
#       - umask 077; the file is 0600.
#       - The MINTED TOKEN is the broker's stdout and is passed straight through
#         on OUR stdout (the git-guard captures it into a local var, never logs
#         it). We NEVER print the token, the PEM, or the age key ourselves.
#  * --no-sops + --pem-path point the broker at the already-decrypted tmpfs PEM
#    so the broker does NOT re-invoke the stdout-blocking sops path.
#
# CONTRACT
# --------
#   stdin : ignored.
#   args  : passthrough broker `mint` args from the git-guard, e.g.
#           mint --dept fixture --action settings_pr --repo bubble-ops-fixture --paths a b
#   stdout: the broker's stdout VERBATIM (the ghs_ token on success).
#   stderr: broker stderr verbatim + our own refusal/diagnostic text (no secrets).
#   exit  : broker's exit code on success-path; 2 on a validation refusal.
#
# Overridable for tests (env): BROKER_BIN, SOPS_PEM_PATH, AGE_KEY_FILE,
#   TMPFS_DIRS (space-sep), MINT_DRY (1 => print resolved env+args, do not mint).
# =============================================================================

set -euo pipefail
umask 077

PROG="bubble-broker-mint-settings-root"
die() { echo "${PROG}: REFUSED: $*" >&2; exit 2; }

# --- non-secret constants (same class as cred-helper's APP_ID/INST_ID) -------
APP_ID="${BUBBLE_BROKER_APP_ID:-3782718}"

# Per-dept installation-id map. Bubble-invest org install = 135214360 covers the
# real dept repos (maya/tony/cgp). vdk888 install = 134075326 covers the fixture
# (vdk888/bubble-ops-fixture). Same mapping logic as the cred-helper path switch.
# A case-statement (not an associative array) keeps this portable to bash 3.2.
inst_id_for_dept() {
  case "$1" in
    fixture) echo 134075326 ;;
    maya|tony|cgp|ben) echo 135214360 ;;
    *) echo "" ;;
  esac
}

BROKER_BIN="${BROKER_BIN:-/opt/bubble-token-broker/bin/bubble-token-broker}"
SOPS_PEM_PATH="${SOPS_PEM_PATH:-/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem}"
AGE_KEY_FILE="${AGE_KEY_FILE:-/etc/age/key.txt}"
SOPS_BIN="${SOPS_BIN:-/usr/local/bin/sops}"
MINT_DRY="${MINT_DRY:-0}"

# --- argument allow-listing (fail-closed) ------------------------------------
# First positional MUST be `mint`.
[[ "${1:-}" == "mint" ]] || die "only the 'mint' subcommand is permitted (got: '${1:-<none>}')."
shift

DEPT=""
ACTION=""
REPO=""
PATHS=()
# Rebuilt, validated args we forward to the broker. Starts with the `mint`
# subcommand (the broker CLI requires it as the first positional) so we never
# forward unvalidated argv — only this allow-listed reconstruction.
PASS_ARGS=(mint)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dept)
      DEPT="${2:-}"; PASS_ARGS+=(--dept "$DEPT"); shift 2 ;;
    --action)
      ACTION="${2:-}"; PASS_ARGS+=(--action "$ACTION"); shift 2 ;;
    --repo)
      REPO="${2:-}"; PASS_ARGS+=(--repo "$REPO"); shift 2 ;;
    --paths)
      PASS_ARGS+=(--paths); shift
      while [[ $# -gt 0 && "$1" != --* ]]; do PATHS+=("$1"); PASS_ARGS+=("$1"); shift; done
      [[ ${#PATHS[@]} -ge 1 ]] || die "--paths given with no values." ;;
    *)
      die "disallowed/unknown argument: '$1' (this wrapper accepts ONLY mint --dept/--action/--repo/--paths)." ;;
  esac
done

# --- mandatory-field + value validation --------------------------------------
[[ -n "$DEPT" ]]   || die "--dept is required."
[[ -n "$ACTION" ]] || die "--action is required."
[[ -n "$REPO" ]]   || die "--repo is required."

# Hard pin: this sudo-able wrapper mints ONLY settings_pr, runtime_write_own and
# (#1619) runtime_read. runtime_read is the git-guard's pre-push base-fetch token
# for runtime_write_own (guard.py mint_read_token): it is the same own-dept gate,
# contents:read only. open_priority_pr stays refused.
[[ "$ACTION" == "settings_pr" || "$ACTION" == "runtime_write_own" || "$ACTION" == "runtime_read" ]] \
  || die "--action must be 'settings_pr', 'runtime_write_own' or 'runtime_read' (got '$ACTION')."

# Caller identity comes from sudo, never from argv. An agent-<slug> caller may
# only act for ITS OWN dept (applies to both actions: closes cross-dept mints).
CALLER_DEPT=""
if [[ "${SUDO_USER:-}" =~ ^agent-([a-z0-9][a-z0-9_-]*)$ ]]; then
  CALLER_DEPT="${BASH_REMATCH[1]}"
  [[ "$DEPT" == "$CALLER_DEPT" ]] \
    || die "caller '${SUDO_USER}' may only mint for its own dept '${CALLER_DEPT}' (got --dept '$DEPT')."
fi
if [[ "$ACTION" == "runtime_write_own" || "$ACTION" == "runtime_read" ]]; then
  # SUDO_USER is only trustworthy when sudo set it, i.e. we are root.
  # (BUBBLE_MINT_TEST_NONROOT=1 exists for the unit tests only; a non-root run
  # cannot decrypt the age key anyway, so it grants nothing.)
  if [[ "$(id -u)" != "0" && "${BUBBLE_MINT_TEST_NONROOT:-0}" != "1" ]]; then
    die "$ACTION must be invoked through sudo (EUID != 0)."
  fi
  [[ -n "$CALLER_DEPT" ]] || die "$ACTION requires an agent-<slug> caller (SUDO_USER='${SUDO_USER:-}')."
  if [[ "$ACTION" == "runtime_write_own" ]]; then
    [[ ${#PATHS[@]} -ge 1 ]] || die "runtime_write_own requires --paths (policy path allow-list)."
  fi
  POLICY_DIR="${POLICY_DIR:-/opt/bubble-token-broker/deploy/policies}"
  POLICY_FILE="${POLICY_DIR}/${DEPT}-policy.yaml"
  [[ -f "$POLICY_FILE" ]] || die "no policy file for dept '${DEPT}' (${POLICY_FILE})."
  # Force the broker to enforce THIS root-owned policy; drop any caller override.
  unset BUBBLE_TOKEN_BROKER_POLICY
  PASS_ARGS+=(--policy "$POLICY_FILE")
fi

# Dept must be a known slug with a baked-in installation id.
[[ "$DEPT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "dept slug '$DEPT' has illegal characters."
INSTALL_ID="$(inst_id_for_dept "$DEPT")"
[[ -n "$INSTALL_ID" ]] || die "no installation id mapped for dept '$DEPT' (known: fixture, maya, tony, cgp)."

# Repo must be the dept's own bubble-ops repo (defense-in-depth; the policy +
# git-guard already enforce own-repo, this is the belt).
[[ "$REPO" == "bubble-ops-${DEPT}" ]] || die "repo '$REPO' is not the dept's own repo (expected bubble-ops-${DEPT})."

# --- decrypt the PEM to a tmpfs file (mirror cred-helper hygiene) -------------
TMPFS_DIRS="${TMPFS_DIRS:-/run/lock /dev/shm}"
PEM=""
for d in $TMPFS_DIRS; do
  PEM="$(mktemp --tmpdir="$d" bubble-settings-pem.XXXXXX 2>/dev/null)" && break || PEM=""
done
[[ -n "$PEM" ]] || die "could not create a tmpfs temp file in: $TMPFS_DIRS."
cleanup() { [[ -n "${PEM:-}" && -f "$PEM" ]] && { shred -u "$PEM" 2>/dev/null || rm -f "$PEM"; }; }
trap cleanup EXIT INT TERM

# `--output FILE` is the sops-guard-approved form (decrypt-to-stdout is blocked).
SOPS_AGE_KEY_FILE="$AGE_KEY_FILE" "$SOPS_BIN" --decrypt \
  --input-type binary --output-type binary --output "$PEM" "$SOPS_PEM_PATH" >&2 \
  || die "SOPS decrypt of the GitHub App key failed (age key / pem path)."
chmod 600 "$PEM"
[[ -s "$PEM" ]] || die "decrypted PEM is empty — refusing to mint."

# --- mint via the broker, with the resolved env, plaintext PEM, no SOPS ------
# Export the non-secret IDs + the tmpfs PEM path; tell the broker --no-sops so it
# reads the already-plaintext file (does NOT re-hit the stdout-blocking sops).
export GITHUB_APP_ID="$APP_ID"
INST_ENV="GITHUB_APP_INSTALLATION_ID_$(printf '%s' "$DEPT" | tr '[:lower:]-' '[:upper:]_')"
export "$INST_ENV"="$INSTALL_ID"
export GITHUB_APP_PRIVATE_KEY_PATH="$PEM"

if [[ "$MINT_DRY" == "1" ]]; then
  # Test/diagnostic mode: prove resolution WITHOUT minting or touching GitHub.
  echo "[mint-dry] APP_ID=${APP_ID} ${INST_ENV}=${INSTALL_ID} DEPT=${DEPT} REPO=${REPO} ACTION=${ACTION}" >&2
  echo "[mint-dry] broker_args:$(printf ' %q' "${PASS_ARGS[@]}") --no-sops --pem-path <tmpfs> --installation-id ${INSTALL_ID} --app-id ${APP_ID}" >&2
  echo "ghs_DRYRUN_$(date +%s)"
  exit 0
fi

# stdout of the broker (the token) flows straight to OUR stdout — the git-guard
# captures it into a local variable. We never echo/log it. We do NOT `exec` here:
# exec would skip the `trap cleanup` and leave the decrypted PEM on tmpfs. So we
# run, capture the exit code, and exit with it — the EXIT trap then shreds the PEM.
set +e
"$BROKER_BIN" "${PASS_ARGS[@]}" \
  --no-sops --pem-path "$PEM" \
  --installation-id "$INSTALL_ID" \
  --app-id "$APP_ID"
BROKER_RC=$?
set -e
exit "$BROKER_RC"
