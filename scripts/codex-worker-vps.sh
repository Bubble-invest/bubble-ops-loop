#!/usr/bin/env bash
# Run one disposable Codex worker in a private clone on the VPS.
#
# Board #1139: ad hoc /tmp/codex-* clones inherited umask 0022, leaving
# tracked files world-readable and producing false plaintext-secret alerts.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  codex-worker-vps.sh --repo <git-url-or-path> [--ref <ref>]
                      [--sandbox read-only|workspace-write]

The task is read from stdin. Codex runs from a fresh private clone with the
fleet coding floor: gpt-5.6-sol and high reasoning.
USAGE
}

REPO_SOURCE=""
REPO_REF=""
SANDBOX="read-only"

while (($#)); do
  case "$1" in
    --repo)
      (($# >= 2)) || { echo "ERROR: --repo needs a value" >&2; exit 64; }
      REPO_SOURCE="$2"
      shift 2
      ;;
    --ref)
      (($# >= 2)) || { echo "ERROR: --ref needs a value" >&2; exit 64; }
      REPO_REF="$2"
      shift 2
      ;;
    --sandbox)
      (($# >= 2)) || { echo "ERROR: --sandbox needs a value" >&2; exit 64; }
      SANDBOX="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[[ -n "$REPO_SOURCE" ]] || { echo "ERROR: --repo is required" >&2; exit 64; }
case "$SANDBOX" in
  read-only|workspace-write) ;;
  *) echo "ERROR: --sandbox must be read-only or workspace-write" >&2; exit 64 ;;
esac

CODEX_BIN="${CODEX_WORKER_CODEX_BIN:-codex}"
command -v "$CODEX_BIN" >/dev/null 2>&1 \
  || { echo "ERROR: Codex executable not found" >&2; exit 3; }

# This must precede every write: task brief, mktemp directory and git clone.
umask 077

TMP_BASE="${CODEX_WORKER_TMPDIR:-${TMPDIR:-/tmp}}"
[[ -d "$TMP_BASE" ]] || { echo "ERROR: scratch base is not a directory" >&2; exit 2; }
[[ -w "$TMP_BASE" ]] || { echo "ERROR: scratch base is not writable" >&2; exit 2; }
TMP_BASE="$(cd "$TMP_BASE" && pwd -P)"

WORK_ROOT="$(mktemp -d "${TMP_BASE%/}/codex-worker.XXXXXX")"
chmod 700 "$WORK_ROOT"
OWNER_MARKER="$WORK_ROOT/.bubble-codex-worker-owned"
: > "$OWNER_MARKER"
chmod 600 "$OWNER_MARKER"

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ -n "${WORK_ROOT:-}" \
        && -d "$WORK_ROOT" \
        && -f "$OWNER_MARKER" \
        && "$WORK_ROOT" == "${TMP_BASE%/}/codex-worker."* ]]; then
    rm -rf -- "$WORK_ROOT"
  else
    echo "WARN: refusing cleanup of unverified Codex work directory" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

TASK_FILE="$WORK_ROOT/task.md"
cat > "$TASK_FILE"
chmod 600 "$TASK_FILE"

REPO_DIR="$WORK_ROOT/repo"
clone_args=()
if [[ -n "$REPO_REF" ]]; then
  clone_args+=(--branch "$REPO_REF")
fi
git clone --quiet "${clone_args[@]}" -- "$REPO_SOURCE" "$REPO_DIR"

# Defense in depth against a caller or git configuration changing directory
# creation semantics. Files remain governed by umask 077.
chmod 700 "$REPO_DIR"

(
  cd "$REPO_DIR"
  CODEX_WORK_ROOT="$WORK_ROOT" \
  CODEX_WORK_REPO="$REPO_DIR" \
  CODEX_WORKER_TASK_FILE="$TASK_FILE" \
    "$CODEX_BIN" exec \
      -m gpt-5.6-sol \
      -c model_reasoning_effort=high \
      -s "$SANDBOX" \
      --skip-git-repo-check - < "$TASK_FILE"
)
