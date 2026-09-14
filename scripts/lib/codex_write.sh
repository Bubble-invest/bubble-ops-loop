#!/usr/bin/env bash
# codex_write.sh — the ONE mandatory Codex-CLI writing call for all Bubble writing skills.
# Codex writes the prose; Miranda orchestrates (select, brief, verify, gate, post).
# Established 2026-09-04 (Joris). Docs: skills/codex-write/SKILL.md + shared-wiki miranda_socials/codex-cli-model-and-usage.md
#
# Usage:
#   scripts/lib/codex_write.sh --brief BRIEF.txt --out OUT.txt [--model gpt-5.6-terra] [--effort high]
#   (BRIEF.txt = the assembled writing brief: topic + selected pool item + imposed voice-file reading + hard rules)
#
# Exit codes:
#   0  = success, OUT.txt holds Codex's draft
#   3  = FAIL-SOFT: Codex unavailable / model refused / empty output -> caller MUST fall back to drafting
#        directly (and flag it to operators). NEVER let writing halt.
set -uo pipefail

MODEL="gpt-5.6-terra"; EFFORT="high"; BRIEF=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --brief) BRIEF="$2"; shift 2;;
    --out)   OUT="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --effort) EFFORT="$2"; shift 2;;
    *) echo "codex_write: unknown arg $1" >&2; exit 2;;
  esac
done
[ -n "$BRIEF" ] && [ -f "$BRIEF" ] || { echo "codex_write: --brief FILE required and must exist" >&2; exit 2; }
[ -n "$OUT" ] || { echo "codex_write: --out FILE required" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v codex >/dev/null 2>&1 || { echo "codex_write: FAIL-SOFT codex CLI not found -> draft directly" >&2; exit 3; }

rm -f "$OUT"
# Read-only sandbox: Codex reads our BRAND.md / STYLE_EXAMPLES.md / VOICE files, no writes/network.
ERR="$(codex exec -m "$MODEL" -c model_reasoning_effort="$EFFORT" -s read-only --skip-git-repo-check \
        -C "$REPO" -o "$OUT" - < "$BRIEF" 2>&1)"
RC=$?

# FAIL-SOFT triggers: non-zero rc, server model refusal, or empty output file.
if [ $RC -ne 0 ] || printf '%s' "$ERR" | grep -qiE '"type":"error"|not supported|invalid_request_error'; then
  echo "codex_write: FAIL-SOFT codex call failed (rc=$RC). Draft directly + flag operators." >&2
  printf '%s\n' "$ERR" | tail -3 >&2
  exit 3
fi
if [ ! -s "$OUT" ]; then
  echo "codex_write: FAIL-SOFT empty output -> draft directly + flag operators." >&2
  exit 3
fi
echo "codex_write: OK ($MODEL/$EFFORT) -> $OUT" >&2
exit 0
