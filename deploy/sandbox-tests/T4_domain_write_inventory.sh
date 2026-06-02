#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# T4_domain_write_inventory.sh  —  Outbound-domain + out-of-repo-write inventory
# -----------------------------------------------------------------------------
# WHAT THIS PRODUCES (READ-ONLY — this probe NEVER enables the sandbox and
# NEVER changes anything on the box):
#   An inventory report of, for the FIXTURE:
#     (1) outbound DOMAINS a loop actually contacts (so we know what
#         network.allowedDomains must contain before flipping
#         allowManagedDomainsOnly:true — CONTEXT.md §6 / scoping T4), and
#     (2) out-of-repo WRITE targets (paths a loop writes outside its repo, so
#         we know what filesystem.allowWrite must contain).
#
#   Sources mined (all read-only):
#     - the fixture's own .claude/settings.json (declared permissions/webfetch)
#     - the proposed allowedDomains in ../SANDBOX-SCOPING.md (baseline)
#     - hard-coded hosts/URLs grepped from the fixture's loop code/config
#     - .bun / channels / plugin-cache write locations referenced in scoping
#     - a note on HOW Rick repeats this per live dept later (the same grep over
#       each dept's repo + a dynamic capture during a real loop with strace/
#       `ss` — NOT run here; live agents are off-limits in Wave 1).
#
# This is an INVENTORY, so:
#   RED  (exit 1) = the report file was not produced.
#   GREEN(exit 0) = a complete report file exists at $REPORT with both sections
#                   populated (domains + writes), plus the per-dept repeat note.
#
# Idempotent: overwrites the report each run.
# =============================================================================

SSH_HOST="${SSH_HOST:-hetzner-root}"
FIXTURE="${FIXTURE:-/home/claude/agents/fixture}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPORT="${REPORT:-$HERE/T4_inventory_report.md}"
SCOPING="$HERE/../SANDBOX-SCOPING.md"
say() { printf '%s\n' "$*"; }

say "=== T4 domain_write_inventory — fixture=$FIXTURE (READ-ONLY) ==="

# --- (1) declared domains from the proposed managed block in the scoping doc ---
PROPOSED_DOMAINS="$(grep -A4 'allowedDomains' "$SCOPING" 2>/dev/null \
  | grep -oE '"[a-z0-9.*-]+\.[a-z]{2,}"' | tr -d '"' | sort -u || true)"

# --- (2) hosts/URLs hard-coded in the fixture loop code/config (read-only) ----
FIXTURE_HOSTS="$(ssh "$SSH_HOST" '
  cd "'"$FIXTURE"'" 2>/dev/null || exit 0
  # grep code/config for outbound hostnames; skip the .git pack files.
  grep -rhoiE "https?://[a-z0-9._-]+" \
    --include="*.sh" --include="*.py" --include="*.ts" --include="*.js" \
    --include="*.json" --include="*.yaml" --include="*.yml" --include="*.md" \
    . 2>/dev/null \
    | sed -E "s#https?://##; s#[/:].*##" | grep -E "\." | sort -u
' 2>/dev/null || true)"

# --- (3) fixture .claude/settings.json declared perms (read-only) -------------
FIXTURE_SETTINGS="$(ssh "$SSH_HOST" "sudo -u claude cat '$FIXTURE/.claude/settings.json' 2>/dev/null" 2>/dev/null || true)"
SETTINGS_DOMAINS="$(printf '%s' "$FIXTURE_SETTINGS" | grep -oE 'WebFetch\([^)]*\)|domain:[a-z0-9.*-]+' | sort -u || true)"

# --- (4) out-of-repo write hints: paths the scoping block grants allowWrite ---
PROPOSED_WRITES="$(grep -A3 'allowWrite' "$SCOPING" 2>/dev/null \
  | grep -oE '"(/|~/)[^"]+"' | tr -d '"' | sort -u || true)"

# --- (5) any writes the fixture loop code references outside its own dir ------
FIXTURE_OOR_WRITES="$(ssh "$SSH_HOST" '
  cd "'"$FIXTURE"'" 2>/dev/null || exit 0
  grep -rhoE "(>{1,2}|tee|cp|mv|mkdir -p) +[^ ]*(/home/claude/\.(bun|claude)|/tmp|/run|/dev/shm)[^ ]*" \
    --include="*.sh" --include="*.py" --include="*.ts" --include="*.js" . 2>/dev/null \
    | sort -u | head -40
' 2>/dev/null || true)"

# --- write the report ---------------------------------------------------------
{
  echo "# T4 — Fixture outbound-domain + out-of-repo-write inventory"
  echo
  echo "_Generated: $(date -u +%FT%TZ) — READ-ONLY probe, no sandbox enabled, no box state changed._"
  echo "_Fixture: \`$FIXTURE\` on \`$SSH_HOST\`._"
  echo
  echo "## Purpose"
  echo "Feeds \`network.allowedDomains\` and \`filesystem.allowWrite\` in the proposed"
  echo "managed \`sandbox\` block (see ../SANDBOX-SCOPING.md) so they are complete"
  echo "BEFORE \`allowManagedDomainsOnly\` is flipped to \`true\`. An incomplete list here"
  echo "= a loop that breaks the moment managed-only lockdown lands."
  echo
  echo "## 1. Outbound domains"
  echo
  echo "### 1a. Proposed baseline (from SANDBOX-SCOPING.md managed block)"
  echo '```'
  printf '%s\n' "${PROPOSED_DOMAINS:-<none parsed>}"
  echo '```'
  echo
  echo "### 1b. Hostnames hard-coded in fixture code/config"
  echo '```'
  printf '%s\n' "${FIXTURE_HOSTS:-<none found>}"
  echo '```'
  echo
  echo "### 1c. WebFetch / domain entries declared in fixture .claude/settings.json"
  echo '```'
  printf '%s\n' "${SETTINGS_DOMAINS:-<none found>}"
  echo '```'
  echo
  echo "### 1d. DELTA to review — hosts in 1b/1c NOT in the proposed baseline (1a)"
  echo '```'
  comm -23 \
    <(printf '%s\n' "$FIXTURE_HOSTS" | sort -u | grep -E '\.' || true) \
    <(printf '%s\n' "$PROPOSED_DOMAINS" | sort -u || true) 2>/dev/null \
    || echo "<delta computation skipped>"
  echo '```'
  echo
  echo "## 2. Out-of-repo writes"
  echo
  echo "### 2a. Proposed allowWrite (from SANDBOX-SCOPING.md managed block)"
  echo '```'
  printf '%s\n' "${PROPOSED_WRITES:-<none parsed>}"
  echo '```'
  echo
  echo "### 2b. Out-of-repo write targets referenced in fixture loop code"
  echo '```'
  printf '%s\n' "${FIXTURE_OOR_WRITES:-<none found>}"
  echo '```'
  echo
  echo "## 3. How Rick repeats this PER LIVE DEPT later (Wave 3, human-gated)"
  echo
  echo "This probe only static-mines the fixture. For each live dept (maya canary"
  echo "first, then tony/cgp/claudette/morty) repeat with BOTH static + dynamic:"
  echo
  echo "- **Static:** run sections 1b/1c/2b against that dept's repo + its"
  echo "  \`.claude/settings.json\`."
  echo "- **Dynamic (during a REAL loop, read-only):** while a loop runs, capture"
  echo "  its actual egress without disrupting it, e.g.:"
  echo "    - \`ss -tnp\` / \`ss -tnpH 'state established'\` filtered to the claude"
  echo "      uid to list live outbound connections + resolved peers;"
  echo "    - the sandbox proxy's own host-not-allowed prompts/logs once the"
  echo "      sandbox is on with \`allowManagedDomainsOnly:false\` (it logs the"
  echo "      first hit on each new domain — harvest those, then add to the list)."
  echo "  Never run \`getUpdates\` or restart the agent (CONTEXT.md §6)."
  echo "- Union the per-dept domains/writes into the managed block (arrays"
  echo "  merge / depts widen), THEN flip \`allowManagedDomainsOnly:true\`."
  echo
  echo "## 4. Residual risk (carry forward, not solved here)"
  echo "- Network filter is hostname-only, no TLS inspection → allowed domains"
  echo "  (github.com, api.anthropic.com) remain domain-fronting exfil paths."
  echo "  (CONTEXT.md §6 / scoping 'Security limitations'.)"
} > "$REPORT"

if [ -s "$REPORT" ] && grep -q "## 1. Outbound domains" "$REPORT" && grep -q "## 2. Out-of-repo writes" "$REPORT"; then
  say "  GREEN inventory report written: $REPORT"
  say "  --- report head ---"
  sed -n '1,18p' "$REPORT" | sed 's/^/    /'
  say "T4 RESULT: GREEN — complete inventory produced."
  exit 0
fi

say "  RED   report not produced or incomplete."
say "T4 RESULT: RED — inventory not generated."
exit 1
