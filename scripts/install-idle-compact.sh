#!/bin/bash
# Run as root on the VPS to install units; the compaction service drops to agent UID.
set -euo pipefail
framework_root="$(cd "$(dirname "$0")/.." && pwd)"
exec /usr/bin/python3 "$framework_root/scripts/idle-compact/install.py" vps --framework-root "$framework_root" "$@"
