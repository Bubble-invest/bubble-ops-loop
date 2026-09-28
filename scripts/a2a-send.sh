#!/usr/bin/env bash
# Message bytes enter only on stdin; signing and SSH never put them in argv.
set -euo pipefail
exec python3 "$(dirname "$0")/a2a_send.py" "$@"
