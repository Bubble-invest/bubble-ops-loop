#!/usr/bin/env bash
# =============================================================================
# install-onfailure-dropins.sh — wire OnFailure=cron-failure-alert@%n.service
# onto units that today alert on nothing, via systemd drop-ins (no ownership
# of the base unit file). Idempotent: safe to re-run.
#
# Source: deploy/templates/onfailure-dropins/<unit>.service.d/override.conf
# Dest:   /etc/systemd/system/<unit>.service.d/override.conf
#
# Requires cron-failure-alert@.service + /home/claude/scripts/cron-failure-alert.sh
# already present on the box.
#
# Usage:
#   install-onfailure-dropins.sh              # install all
#   install-onfailure-dropins.sh --dry-run     # preview, no writes
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/deploy/templates/onfailure-dropins"
UNIT_DIR="/etc/systemd/system"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

if [[ ! -d "$SRC_DIR" ]]; then
  echo "[install-onfailure-dropins] FATAL: $SRC_DIR not found" >&2
  exit 1
fi

changed=0
for d in "$SRC_DIR"/*.service.d; do
  [[ -d "$d" ]] || continue
  unit_dropin="$(basename "$d")"   # e.g. bubble-deploy-full.service.d
  dest="$UNIT_DIR/$unit_dropin"
  src_conf="$d/override.conf"
  [[ -f "$src_conf" ]] || { echo "[install-onfailure-dropins] skip $unit_dropin (no override.conf)"; continue; }

  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ -f "$dest/override.conf" ]] && cmp -s "$src_conf" "$dest/override.conf"; then
      echo "[install-onfailure-dropins] [dry-run] $unit_dropin already current"
    else
      echo "[install-onfailure-dropins] [dry-run] would install $unit_dropin/override.conf"
    fi
    continue
  fi

  sudo mkdir -p "$dest"
  if [[ -f "$dest/override.conf" ]] && cmp -s "$src_conf" "$dest/override.conf"; then
    echo "[install-onfailure-dropins] $unit_dropin already current"
  else
    sudo cp "$src_conf" "$dest/override.conf"
    echo "[install-onfailure-dropins] installed $unit_dropin/override.conf"
    changed=1
  fi
done

if [[ "$DRY_RUN" != "1" && "$changed" == "1" ]]; then
  sudo systemctl daemon-reload
  echo "[install-onfailure-dropins] daemon-reload done"
fi

echo "[install-onfailure-dropins] done"
