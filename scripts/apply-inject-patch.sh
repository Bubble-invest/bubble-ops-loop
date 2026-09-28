#!/usr/bin/env bash
# Compatibility entry point: one canonical block, lock and build validation.
# Keeps the installer's default fail-open service-start contract.
set -uo pipefail
exec bash "$(dirname "$0")/install-channel-patches.sh" "$@"
