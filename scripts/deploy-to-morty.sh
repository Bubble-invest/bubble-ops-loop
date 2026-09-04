#!/usr/bin/env bash
# Transport wrapper for bubble-vps-platform's sole agent-unit renderer (#1119).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLATFORM_ROOT="${BUBBLE_VPS_PLATFORM_ROOT:-$(cd "$PROJECT_ROOT/../bubble-vps-platform" 2>/dev/null && pwd || true)}"
TENANT_YAML="${BUBBLE_TENANT_YAML:-}"
DEPT_YAML=""
REMOTE="${BUBBLE_MORTY_HOST:-claude@morty}"
SLUG=""
DRY_RUN=0
RENDER_ONLY=0

usage() {
  cat <<'USAGE'
Usage: deploy-to-morty.sh --slug=SLUG --tenant-yaml=PATH [options]

Renders with bubble-vps-platform/scripts/render-agent-units.py, verifies with
systemd-analyze, then installs bubble-agent@SLUG.service. This script contains
no unit templating or sed substitution.

Options:
  --dept-yaml=PATH       Default: agents/SLUG/dept.yaml
  --platform-root=PATH   Default: $BUBBLE_VPS_PLATFORM_ROOT or sibling clone
  --remote=USER@HOST     Default: $BUBBLE_MORTY_HOST or claude@morty
  --dry-run              Render + verify and print transport commands
  --render-only          Render + verify; never contact a host
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --tenant-yaml=*) TENANT_YAML="${arg#*=}" ;;
    --dept-yaml=*) DEPT_YAML="${arg#*=}" ;;
    --platform-root=*) PLATFORM_ROOT="${arg#*=}" ;;
    --remote=*) REMOTE="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --render-only) RENDER_ONLY=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$SLUG" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "--slug must be kebab-case" >&2; exit 2; }
[[ "$REMOTE" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]] \
  || { echo "--remote must be USER@HOST" >&2; exit 2; }
DEPT_YAML="${DEPT_YAML:-$PROJECT_ROOT/agents/$SLUG/dept.yaml}"
RENDERER="$PLATFORM_ROOT/scripts/render-agent-units.py"
[[ -x "$RENDERER" ]] || { echo "canonical renderer not found: $RENDERER" >&2; exit 2; }
[[ -f "$TENANT_YAML" ]] || { echo "--tenant-yaml is required" >&2; exit 2; }
[[ -f "$DEPT_YAML" ]] || { echo "dept.yaml not found: $DEPT_YAML" >&2; exit 2; }

stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
"$RENDERER" --tenant "$TENANT_YAML" --dept "$DEPT_YAML" \
  --output-dir "$stage" --verify

service="bubble-agent@${SLUG}.service"
dropin="${service}.d/${SLUG}.conf"
if (( RENDER_ONLY == 1 )); then
  printf 'rendered %s\n' "$stage/bubble-agent@.service" "$stage/$dropin"
  exit 0
fi

commands=(
  "scp $stage/bubble-agent@.service $REMOTE:/tmp/bubble-agent@.service"
  "scp $stage/bubble-agent-prepare $REMOTE:/tmp/bubble-agent-prepare"
  "scp $stage/$dropin $REMOTE:/tmp/${SLUG}.conf"
  "ssh $REMOTE sudo install -m 0644 /tmp/bubble-agent@.service /etc/systemd/system/bubble-agent@.service"
  "ssh $REMOTE sudo install -m 0755 /tmp/bubble-agent-prepare /usr/local/libexec/bubble-agent-prepare"
  "ssh $REMOTE sudo install -d -m 0755 /etc/systemd/system/${service}.d"
  "ssh $REMOTE sudo install -m 0644 /tmp/${SLUG}.conf /etc/systemd/system/${dropin}"
  "ssh $REMOTE sudo systemd-analyze verify ${service}"
  "ssh $REMOTE sudo systemctl daemon-reload"
  "ssh $REMOTE sudo systemctl enable --now ${service}"
)

if (( DRY_RUN == 1 )); then
  printf '%s\n' "${commands[@]}"
else
  for command in "${commands[@]}"; do
    /bin/sh -c "$command"
  done
fi
