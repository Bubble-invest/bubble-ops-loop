#!/bin/bash
# Standard-library installer; always acts in the owner's gui/<uid> domain.
set -euo pipefail
framework_root="$(cd "$(dirname "$0")/.." && pwd)"
exec /usr/bin/python3 "$framework_root/scripts/idle-compact/install.py" mac --framework-root "$framework_root" "$@"
