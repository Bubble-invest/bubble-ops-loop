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
OS_USER_OVERRIDE=""
OS_GROUP_OVERRIDE=""
WORKDIR_OVERRIDE=""
MODEL_OVERRIDE=""
OS_USER_SET=0

usage() {
  cat <<'USAGE'
Usage: deploy-to-morty.sh --slug=SLUG --tenant-yaml=PATH [options]

Renders with bubble-vps-platform/scripts/render-agent-units.py, verifies with
systemd-analyze, then installs bubble-agent@SLUG.service. This script contains
no unit templating or sed substitution.

Options:
  --dept-yaml=PATH       Default: agents/SLUG/dept.yaml
  --platform-root=PATH   Default: $BUBBLE_VPS_PLATFORM_ROOT or sibling clone
  --model=MODEL          Override department.model for this render
  --os-user=USER         Override department.os_user. Default is the dept.yaml
                         value, then legacy "claude" when unset.
  --os-group=GROUP       Override department.os_group. Default: OS user.
  --workdir=PATH         Override department.workdir. Default:
                         /home/claude/agents/SLUG for legacy "claude", or
                         /srv/agents/SLUG for an isolated user.
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
    --model=*) MODEL_OVERRIDE="${arg#*=}" ;;
    --os-user=*) OS_USER_OVERRIDE="${arg#*=}"; OS_USER_SET=1 ;;
    --os-group=*) OS_GROUP_OVERRIDE="${arg#*=}" ;;
    --workdir=*) WORKDIR_OVERRIDE="${arg#*=}" ;;
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
if (( OS_USER_SET == 1 )) && [[ -z "$OS_USER_OVERRIDE" ]]; then
  echo "--os-user must not be empty" >&2
  exit 2
fi

# Keep #1120's deploy-time migration interface. Explicit --os-user derives the
# same group/workdir defaults as the scaffolded legacy unit did; all values are
# passed to the canonical renderer, which writes both the systemd identity
# directives and the helper environment from one source.
if (( OS_USER_SET == 1 )); then
  OS_GROUP_OVERRIDE="${OS_GROUP_OVERRIDE:-$OS_USER_OVERRIDE}"
  if [[ -z "$WORKDIR_OVERRIDE" ]]; then
    if [[ "$OS_USER_OVERRIDE" == "claude" ]]; then
      WORKDIR_OVERRIDE="/home/claude/agents/${SLUG}"
    else
      WORKDIR_OVERRIDE="/srv/agents/${SLUG}"
    fi
  fi
fi

stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
renderer_args=(
  --tenant "$TENANT_YAML"
  --dept "$DEPT_YAML"
  --output-dir "$stage"
  --verify
)
[[ -z "$MODEL_OVERRIDE" ]] || renderer_args+=(--model "$MODEL_OVERRIDE")
[[ -z "$OS_USER_OVERRIDE" ]] || renderer_args+=(--os-user "$OS_USER_OVERRIDE")
[[ -z "$OS_GROUP_OVERRIDE" ]] || renderer_args+=(--os-group "$OS_GROUP_OVERRIDE")
[[ -z "$WORKDIR_OVERRIDE" ]] || renderer_args+=(--workdir "$WORKDIR_OVERRIDE")
"$RENDERER" "${renderer_args[@]}"

service="bubble-agent@${SLUG}.service"
dropin="${service}.d/${SLUG}.conf"
legacy_service="ops-loop-${SLUG}.service"

# Read the resolved identity back from the canonical drop-in. Deployment and
# provisioning therefore cannot drift from the User=/Group=/WorkingDirectory=
# values systemd will actually use.
OS_USER="$(sed -n 's/^User=//p' "$stage/$dropin")"
OS_GROUP="$(sed -n 's/^Group=//p' "$stage/$dropin")"
WORKDIR="$(sed -n 's/^WorkingDirectory=//p' "$stage/$dropin")"
[[ -n "$OS_USER" && -n "$OS_GROUP" && -n "$WORKDIR" ]] \
  || { echo "canonical renderer omitted service identity" >&2; exit 2; }

if (( RENDER_ONLY == 1 )); then
  printf 'rendered %s\n' "$stage/bubble-agent@.service" "$stage/$dropin"
  exit 0
fi

commands=(
  "ssh $REMOTE test -d $WORKDIR || ssh $REMOTE sudo git clone https://github.com/vdk888/bubble-ops-${SLUG} $WORKDIR"
  "ssh $REMOTE sudo chown -R ${OS_USER}:${OS_GROUP} $WORKDIR"
  "scp $stage/bubble-agent@.service $REMOTE:/tmp/bubble-agent@.service"
  "scp $stage/bubble-agent-prepare $REMOTE:/tmp/bubble-agent-prepare"
  "scp $stage/$dropin $REMOTE:/tmp/${SLUG}.conf"
  "ssh $REMOTE sudo install -m 0644 /tmp/bubble-agent@.service /etc/systemd/system/bubble-agent@.service"
  "ssh $REMOTE sudo install -m 0755 /tmp/bubble-agent-prepare /usr/local/libexec/bubble-agent-prepare"
  "ssh $REMOTE sudo install -d -m 0755 /etc/systemd/system/${service}.d"
  "ssh $REMOTE sudo install -m 0644 /tmp/${SLUG}.conf /etc/systemd/system/${dropin}"
  "ssh $REMOTE sudo systemd-analyze verify ${service}"
  "ssh $REMOTE sudo systemctl daemon-reload"
  # Ordered, idempotent cutover: the old poller is down before the canonical
  # one can start, preventing two Telegram getUpdates consumers (409).
  "ssh $REMOTE 'if sudo systemctl cat ${legacy_service} >/dev/null 2>&1; then sudo systemctl stop ${legacy_service}; fi'"
  "ssh $REMOTE 'if sudo systemctl cat ${legacy_service} >/dev/null 2>&1; then sudo systemctl disable ${legacy_service}; fi'"
  "ssh $REMOTE 'if sudo systemctl is-active --quiet ${legacy_service}; then echo legacy unit still active >&2; exit 1; fi'"
  "ssh $REMOTE sudo systemctl enable --now ${service}"
  "ssh $REMOTE sudo systemctl is-active ${service}"
)

if (( DRY_RUN == 1 )); then
  printf '# resolved User=%s Group=%s WorkingDirectory=%s\n' "$OS_USER" "$OS_GROUP" "$WORKDIR"
  if [[ "$OS_USER" != "claude" ]]; then
    printf '%s\n' "# prerequisite: scripts/bootstrap-os-user.sh --os-user=${OS_USER} --workdir=${WORKDIR}"
  fi
  printf '%s\n' "${commands[@]}"
else
  if [[ "$OS_USER" != "claude" ]] && ! ssh "$REMOTE" "id -u ${OS_USER}" >/dev/null 2>&1; then
    echo "OS user '${OS_USER}' does not exist on ${REMOTE}." >&2
    echo "Run scripts/bootstrap-os-user.sh --os-user=${OS_USER} --workdir=${WORKDIR} first." >&2
    exit 1
  fi
  for command in "${commands[@]}"; do
    /bin/sh -c "$command"
  done
fi
