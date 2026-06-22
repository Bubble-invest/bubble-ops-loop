#!/usr/bin/env bash
# =============================================================================
# pre-publish-scan.sh — systematic pre-OSS leak gate for a Bubble repo.
#
# WHY: before any Bubble repo goes (or stays) public, it MUST be scanned for
# operator PII, internal infra identifiers, internal DB IDs, secret-map docs,
# and raw secret-prefix patterns — in BOTH the working tree AND git history
# (a value scrubbed from HEAD but left in history is still public to a cloner).
# {{OPERATOR}} (2026-06-22): "make the security scan systematic at each step."
#
# This is a GATE, not a fixer. Exit 0 = clean. Exit 2 = findings (block publish).
#
# USAGE:
#   pre-publish-scan.sh [<repo-dir>]      # default: cwd
#   pre-publish-scan.sh --history          # also scan ALL git history (slower)
#   pre-publish-scan.sh --json             # machine-readable output
#
# SAFETY: never prints a matched secret VALUE. Findings show file:line + the
# pattern class + a short redacted prefix only (first ~8 chars). Mirrors the
# discipline of transcript-leak-scan.sh and the never-print-decrypted rule.
# =============================================================================
set -uo pipefail

REPO="."; SCAN_HISTORY=0; JSON=0
for a in "$@"; do case "$a" in
  --history) SCAN_HISTORY=1 ;;
  --json) JSON=1 ;;
  -* ) ;;
  *) REPO="$a" ;;
esac; done
cd "$REPO" 2>/dev/null || { echo "pre-publish-scan: cannot cd $REPO" >&2; exit 1; }

findings=0
red(){ printf '\033[31m%s\033[0m\n' "$*"; }
note(){ [ "$JSON" -eq 0 ] && echo "$*"; }

# ── Pattern classes (the leak taxonomy from the VOIE3 gap report) ───────────
# Each entry: "LABEL|SEVERITY|EXTENDED-REGEX". The regex matches the LOCATION,
# and we redact the value when printing. NEVER widen a pattern such that the
# printed line would expose a real secret value.
#
# OPERATOR-SPECIFIC NEEDLES (the exact Telegram IDs to hunt) are NOT hardcoded
# here — that would make THIS file unpublishable. They come from the env var
# BUBBLE_OPERATOR_IDS (pipe-separated, e.g. "111|222"), set privately at runtime
# (e.g. from SOPS). If unset, the scanner falls back to a generic "long bare
# digit-run in a chat_id/CHAT_ID/user_id context" heuristic — less precise but
# leaks nothing in the tool itself.
# Operator-specific NEEDLES (TG IDs + usernames) come from env so THIS file
# ships clean. BUBBLE_OPERATOR_IDS = pipe-separated chat IDs; BUBBLE_OPERATOR_USERS
# = pipe-separated usernames/personal-path tokens. Unset → generic heuristics.
OP_IDS="${BUBBLE_OPERATOR_IDS:-}"
if [ -n "$OP_IDS" ]; then OP_ID_PAT="$OP_IDS"
else OP_ID_PAT="(chat_id|CHAT_ID|user_id|USER_ID|telegram[_-]?id)[\"'= :]+[0-9]{9,11}"; fi
OP_NAMES="${BUBBLE_OPERATOR_NAMES:-}"   # personal + machine names, pipe-separated (e.g. "{{OPERATOR}}|{{OPERATOR_2}}|{{VPS_HOST}}")
# NOTE: agent/persona names (Ben/Maya/Tony/Miranda/Morty/Claudette) are the PRODUCT
# identity, NOT leaks — never add them here.
OP_USERS="${BUBBLE_OPERATOR_USERS:-}"
if [ -n "$OP_USERS" ]; then OP_USER_PAT="$OP_USERS"
else OP_USER_PAT="/Users/[a-z]+/(claude-workspaces|Documents)|/home/claude/agents/|[a-z]+@[a-z0-9-]+\\.ts\\.net"; fi
PATTERNS=(
  "operator-telegram-id|CRITICAL|${OP_ID_PAT}"                     # operator TG IDs (needles via env)
  "internal-tailscale-fqdn|CRITICAL|[a-z0-9-]+\\.tail[0-9a-f]+\\.ts\\.net"  # private tailnet hostnames
  "internal-tailscale-ip|CRITICAL|100\\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\\.[0-9]{1,3}\\.[0-9]{1,3}"  # CGNAT 100.64/10 range
  "operator-username-path|HIGH|${OP_USER_PAT}"                    # personal paths/users (needles via env)
  "operator-name|HIGH|${OP_NAMES:-__NONE_MATCH_SENTINEL_XYZ__}"   # personal + machine names (ONLY when BUBBLE_OPERATOR_NAMES set)
  "notion-id|HIGH|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}"  # notion DB/page UUIDs
  "secret-prefix|CRITICAL|sk-ant-[a-z0-9]|ghp_[A-Za-z0-9]|ghs_[A-Za-z0-9]|xox[baprs]-|AKIA[0-9A-Z]|-----BEGIN [A-Z]+ PRIVATE KEY|age1[a-z0-9]{20}"
  "secrets-map-doc|HIGH|inventory-[0-9]{4}-[0-9]{2}-[0-9]{2}|secrets-port/|agent-repos-audit"  # docs that map where secrets live

  # ── PII controls (client/personal data) ─────────────────────────────────
  # Structured PII a public repo must never carry. Regexes reused from Bubble
  # Shield's battle-tested recognizers (bubble_shield/recognizers.py). Names &
  # postal addresses are the hard, low-recall part — a grep gate can't catch
  # them reliably; that's what Bubble Shield's NER is for. For a hard pre-publish
  # name check, run the repo's text through bubble-shield (see PII NOTE below).
  "pii-email|HIGH|[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}"        # email addresses
  "pii-iban|HIGH|\\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{2,4}){2,8}\\b"           # IBAN
  "pii-isin|HIGH|\\b[A-Z]{2}[A-Z0-9]{9}[0-9]\\b"                              # ISIN (security identifier)
  "pii-siret-siren|HIGH|\\b[0-9]{3}[ ]?[0-9]{3}[ ]?[0-9]{3}([ ]?[0-9]{5})?\\b"  # FR SIRET/SIREN
  "pii-fr-ssn|CRITICAL|\\b[12][ ]?[0-9]{2}[ ]?[0-9]{2}[ ]?[0-9]{2}[ ]?[0-9]{3}[ ]?[0-9]{3}([ ]?[0-9]{2})?\\b"  # FR numéro de sécu
  "pii-fr-phone|HIGH|(?:(?:\\+33|0033)[ .\\-]?[1-9]|0[1-9])(?:[ .\\-]?[0-9]{2}){4}\\b"  # FR phone
  "pii-credit-card|CRITICAL|\\b(?:[0-9]{4}[ \\-]?){3}[0-9]{4}\\b"             # 16-digit card-like
)

# Allow callers to point at a known-PII allowlist (e.g. a fixture email that is
# intentionally synthetic) so true synthetic test data doesn't block a publish.
# BUBBLE_PII_ALLOW = pipe-separated literal strings to ignore.
PII_ALLOW="${BUBBLE_PII_ALLOW:-}"

note "═══ pre-publish-scan: $(basename "$(pwd)") ═══"
note "scope: working tree${SCAN_HISTORY:+ + full git history}"
note ""

scan_text() {  # $1 = source label (tree|history) ; reads from stdin a grep -rn-style stream
  local src="$1"
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    findings=$((findings+1))
    # entry already redacted upstream
    red "  [$src] $entry"
  done
}

# Build an allowlist grep filter (drop lines containing an allowlisted literal,
# e.g. a synthetic fixture email) BEFORE redaction.
_allow_filter() { if [ -n "$PII_ALLOW" ]; then grep -vE "$PII_ALLOW"; else cat; fi; }

# ── Working-tree scan ───────────────────────────────────────────────────────
for p in "${PATTERNS[@]}"; do
  IFS='|' read -r label sev regex <<< "$p"
  # grep -rnI: recursive, line numbers, skip binary. Exclude .git, node_modules, venvs, lockfiles.
  hits=$(grep -rnIE "$regex" . \
        --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir='.venv*' \
        --exclude='*.lock' --exclude='package-lock.json' --exclude='pre-publish-scan.sh' 2>/dev/null \
        | _allow_filter \
        | sed -E 's/(:[0-9]+:).*/\1 «match redacted»/' | sort -u | head -50)
  if [ -n "$hits" ]; then
    note "▶ $label ($sev):"
    printf '%s\n' "$hits" | sed 's/^/  [tree] /' | while IFS= read -r l; do red "$l"; done
    n=$(printf '%s\n' "$hits" | grep -c .); findings=$((findings+n))
  fi
done

# ── Git-history scan (opt-in; a value purged from HEAD may persist in history) ──
if [ "$SCAN_HISTORY" -eq 1 ] && [ -d .git ]; then
  note ""
  note "── git history (all blobs) ──"
  for p in "${PATTERNS[@]}"; do
    IFS='|' read -r label sev regex <<< "$p"
    # search all history text; print commit+path only, value redacted
    h=$(git -C . grep -nIE "$regex" $(git rev-list --all 2>/dev/null | head -500) -- 2>/dev/null \
        | sed -E 's/(:[0-9]+:).*/\1 «match redacted»/' | sort -u | head -20)
    if [ -n "$h" ]; then
      note "▶ $label ($sev) — IN HISTORY (needs git-filter-repo, not just a working-tree fix):"
      printf '%s\n' "$h" | while IFS= read -r l; do red "  [history] $l"; done
      n=$(printf '%s\n' "$h" | grep -c .); findings=$((findings+n))
    fi
  done
fi

note ""
if [ "$findings" -eq 0 ]; then
  note "✅ CLEAN — no leak patterns found. Safe to publish."
  [ "$JSON" -eq 1 ] && echo '{"clean":true,"findings":0}'
  exit 0
else
  red "🔴 $findings finding(s) — DO NOT PUBLISH until resolved."
  note "   Working-tree findings: replace with {{PLACEHOLDER}} or env vars."
  note "   History findings: require git-filter-repo (see reference_git_history_secret_purge)."
  [ "$JSON" -eq 1 ] && echo "{\"clean\":false,\"findings\":$findings}"
  exit 2
fi
