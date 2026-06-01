#!/usr/bin/env bash
# bubble-git-guard wrapper script — installed to /opt/bubble-git-guard/bin/
#
# Notion v4 line 725: paths enforced by "wrapper local / git guard sur Morty".
# This script is THE wrapper. The /loop runtime invokes it instead of `git push`.
#
# Usage from inside ops-loop-fixture (replaces a raw `git push`):
#
#   /opt/bubble-git-guard/bin/bubble-git-guard push \
#       --dept fixture \
#       --action runtime_write_own \
#       --repo bubble-ops-fixture \
#       --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
#       --broker /opt/bubble-token-broker/bin/bubble-token-broker \
#       --audit-log /var/log/bubble-git-guard/audit.jsonl
#
# The guard:
#   1. Reads `git diff --cached` + `git diff @{upstream}..HEAD` for staged paths
#   2. Runs each path through the policy (fail-CLOSED on any deny)
#   3. Invokes the broker to mint a short-lived (≤60 min) installation token
#   4. Runs `git push` with the token via http.extraheader (process-private)
#   5. Drops the token reference and logs the outcome (status only)

set -euo pipefail

cd /opt/bubble-git-guard
exec python3 -m src.cli "$@"
