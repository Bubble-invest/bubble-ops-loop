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
#   1. Resolves the source commit once, read-only, in the actor checkout
#   2. Imports it into a guard-owned temporary bare repository
#   3. Reads/diffs the literal policy-derived GitHub destination there
#   4. Runs each committed path through policy (fail-CLOSED on any deny)
#   5. Mints a short-lived token and pushes only from the temporary repo
#   6. Removes the temporary repo and logs status only

set -euo pipefail

cd /opt/bubble-git-guard
exec python3 -m src.cli "$@"
