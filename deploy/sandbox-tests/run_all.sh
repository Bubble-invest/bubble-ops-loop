#!/usr/bin/env bash
set -uo pipefail   # NOT -e: we want to run every probe and tally, not abort on first RED
# =============================================================================
# run_all.sh  —  run sandbox probes T0–T5 in order, print a summary table.
# -----------------------------------------------------------------------------
# Exit 0 only if EVERY probe is GREEN; exit 1 if ANY probe is RED.
# Wave 1: expected to be ALL RED (T0–T3) until host-prep + sandbox land.
# T4 (inventory) and T5 (topology) can be GREEN earlier — they are read-only
# and don't depend on the sandbox existing, only on the box/live state.
#
# Usage:  ./run_all.sh            # run T0..T5
#         FIXTURE=... SSH_HOST=... ./run_all.sh   # override defaults
#
# Note: bash 3.2 compatible (macOS default) — uses indexed arrays only, no
# associative arrays.
# =============================================================================

HERE="$(cd "$(dirname "$0")" && pwd)"
PROBES="T0_deps_present T1_sandbox_init T2_skip_perms_holds T3_git_push T4_domain_write_inventory T5_poller_topology"

results=""   # newline-joined "PROBE<TAB>STATE<TAB>EVIDENCE"
any_red=0

for p in $PROBES; do
  script="$HERE/$p.sh"
  printf '\n========================================================\n'
  printf '>>> %s\n' "$p"
  printf '========================================================\n'
  [ -x "$script" ] || chmod +x "$script" 2>/dev/null || true
  out="$(bash "$script" 2>&1)"; code=$?
  printf '%s\n' "$out"
  line="$(printf '%s\n' "$out" | grep -E 'RESULT:' | tail -1)"
  [ -n "$line" ] || line="<no RESULT line>"
  if [ "$code" -eq 0 ]; then state="GREEN"; else state="RED"; any_red=1; fi
  results="${results}${p}	${state}	${line}
"
done

printf '\n\n========================  SUMMARY  ========================\n'
printf '%-28s %-6s %s\n' "PROBE" "STATE" "EVIDENCE"
printf '%-28s %-6s %s\n' "-----" "-----" "--------"
printf '%s' "$results" | while IFS='	' read -r p state line; do
  [ -n "$p" ] || continue
  printf '%-28s %-6s %s\n' "$p" "$state" "$line"
done
printf '==========================================================\n'

if [ "$any_red" -ne 0 ]; then
  printf '\nOVERALL: RED — at least one probe failed (expected in Wave 1 pre-host-prep).\n'
  exit 1
fi
printf '\nOVERALL: GREEN — all probes passed.\n'
exit 0
