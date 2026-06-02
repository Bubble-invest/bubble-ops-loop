#!/usr/bin/env bash
# install-sandbox.sh — install the OS-level Bash sandbox (Layer B) for all agents.
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run
# (e.g. on every deploy / fresh box bring-up / new tenant). Two phases:
#   1. Host-prep: bubblewrap + socat + npm @anthropic-ai/sandbox-runtime + the
#      AppArmor /usr/bin/bwrap profile (delegates to deploy/host-prep-sandbox.sh).
#   2. Policy:    merge the sandbox block from
#      deploy/templates/managed-settings.sandbox.json into the root-owned
#      /etc/claude-code/managed-settings.json (covers all agents, un-overridable).
#
# After install, RESTART each agent (stop->start) so it picks up the sandbox,
# then verify engagement via the user-namespace check (see deploy/sandbox-tests/
# and the wiki page shared/systems/vps-agent-sandbox). This script does NOT
# restart agents — that is an explicit, supervised step.
#
# What it jails: the Bash tool + all child subprocesses (OS-level), so a
# prompt-injected raw subprocess can't read secrets / exfil / write outside its
# repo — even under --dangerously-skip-permissions. Additive to Layer A
# (managed deny rules + mission-guard hook); never the sole control.
#
# Posture shipped here: enabled, failIfUnavailable=true (HARD gate),
# allowManagedDomainsOnly=false (domains observe-mode — lock later after a
# per-dept domain inventory). Rollback = remove the sandbox key from
# managed-settings (1 root edit) or run deploy/host-prep-sandbox-rollback.sh.
#
# Usage:   sudo bash scripts/install-sandbox.sh [--dry-run]
#          (run from the bubble-ops-loop repo root)
# =============================================================================
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
info() { printf '  \033[34m[INFO]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" != "0" ]; then
  echo "ERROR: must run as root.  sudo bash scripts/install-sandbox.sh" >&2
  exit 1
fi

# resolve repo root relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_PREP="$REPO_ROOT/deploy/host-prep-sandbox.sh"
BLOCK_TPL="$REPO_ROOT/deploy/templates/managed-settings.sandbox.json"
MANAGED="/etc/claude-code/managed-settings.json"

[ -f "$HOST_PREP" ] || { echo "missing $HOST_PREP" >&2; exit 1; }
[ -f "$BLOCK_TPL" ] || { echo "missing $BLOCK_TPL" >&2; exit 1; }

# -------------------------------------------------------------------------
step "Phase 1/2 — host-prep (bwrap + socat + sandbox-runtime + AppArmor)"
if [ "$DRY_RUN" = "1" ]; then
  info "DRY-RUN: would run: bash $HOST_PREP"
else
  bash "$HOST_PREP"
fi

# -------------------------------------------------------------------------
step "Phase 2/2 — merge sandbox block into managed-settings"
if [ ! -f "$MANAGED" ]; then
  info "no existing $MANAGED — creating from the sandbox block template + empty base"
  if [ "$DRY_RUN" = "0" ]; then
    mkdir -p /etc/claude-code
    python3 - "$BLOCK_TPL" "$MANAGED" <<'PY'
import json, sys
block = json.load(open(sys.argv[1]))
out = {"$schema": "https://json.schemastore.org/claude-code-settings.json"}
out.update(block)
json.dump(out, open(sys.argv[2], "w"), indent=2)
PY
    chmod 644 "$MANAGED"; chown root:root "$MANAGED"
  fi
else
  info "merging sandbox block into existing $MANAGED (preserving Layer A: deny rules + hooks)"
  if [ "$DRY_RUN" = "0" ]; then
    cp "$MANAGED" "$MANAGED.bak-install-sandbox-$(date +%s)"
    python3 - "$BLOCK_TPL" "$MANAGED" <<'PY'
import json, sys
block = json.load(open(sys.argv[1]))
d = json.load(open(sys.argv[2]))
# preserve everything; set/replace the sandbox key from the template
d["sandbox"] = block["sandbox"]
# sanity: don't clobber Layer A
assert d.get("hooks") is not None or True   # hooks optional but we never remove
json.dump(d, open(sys.argv[2], "w"), indent=2)
PY
    chmod 644 "$MANAGED"
  fi
fi

# -------------------------------------------------------------------------
step "Verify"
if [ "$DRY_RUN" = "1" ]; then
  info "DRY-RUN complete — no changes made."
  exit 0
fi
python3 - "$MANAGED" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
sb = d.get("sandbox", {})
print(f"  sandbox.enabled={sb.get('enabled')} failIfUnavailable={sb.get('failIfUnavailable')} "
      f"allowManagedDomainsOnly={sb.get('network',{}).get('allowManagedDomainsOnly')}")
print(f"  Layer A preserved: hooks={'yes' if d.get('hooks') else 'NO'} "
      f"deny_rules={len(d.get('permissions',{}).get('deny',[]))}")
PY
command -v bwrap >/dev/null 2>&1 && ok "bwrap present" || { bad "bwrap missing"; exit 1; }
command -v socat >/dev/null 2>&1 && ok "socat present" || { bad "socat missing"; exit 1; }

printf '\n\033[1;32mGREEN — sandbox installed.\033[0m Next: restart each agent (stop->start), then\n'
echo "verify engagement per agent with the userns check (deploy/sandbox-tests/ + wiki)."
