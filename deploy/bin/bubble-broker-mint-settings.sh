#!/bin/bash
# =============================================================================
# bubble-broker-mint-settings.sh — claude-side shim for the WS5 settings_pr
# broker mint. This is what `propose-settings-pr` hands to the git-guard as its
# `--broker`. The git-guard execs it as `<this> mint --dept ... --action
# settings_pr --repo ... [--paths ...]` and captures stdout (the token).
#
# It does ONE thing: re-exec the SAME argv under `sudo -n` against the root-owned
# bubble-broker-mint-settings-root.sh, which has the age-key access needed to
# decrypt the GitHub App PEM and mint. This mirrors the established
# bubble-gh-credential-helper.sh pattern (claude execs a script that sudo's to a
# root chokepoint), and keeps ALL secret handling on the root side.
#
# Sudoers grant (tightly scoped, /etc/sudoers.d/bubble-broker-mint):
#   claude ALL=(root) NOPASSWD: /usr/local/bin/bubble-broker-mint-settings-root.sh
#
# stdout: the root script's stdout VERBATIM (the ghs_ token). No token logging.
# =============================================================================
set -euo pipefail

ROOT_HELPER="${BUBBLE_BROKER_MINT_ROOT:-/usr/local/bin/bubble-broker-mint-settings-root.sh}"

# Pass argv straight through. `sudo -n` = non-interactive (fail, don't prompt) —
# the same flag the cred-helper path relies on. Token comes back on stdout.
exec sudo -n "$ROOT_HELPER" "$@"
