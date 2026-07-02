#!/usr/bin/env bash
# =============================================================================
# run-dry-run.sh - UX-4 Component B: full 4-layer dry-run CLI.
#
# Exercises a dept's onboarding round-trip end-to-end with fake data, writes
# artifacts under outputs/dry-run/<ts>/, validates every artifact against
# schemas-draft/, and prints a structured JSON result to stdout.
#
# Exit codes (Notion v5 line 946):
#   0 - PASSED, OR WARNING + --accept-warnings
#   1 - FAILED, OR WARNING without --accept-warnings
#
# Usage:
#   run-dry-run.sh --dept-root=<path> [--fake-queue-item=<path>] [--seed=42] \
#                  [--accept-warnings] [--verbose]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: run-dry-run.sh --dept-root=<path> [options]

Options:
  --dept-root=<path>         REQUIRED. Path to the dept-repo skeleton.
  --fake-queue-item=<path>   Optional. YAML file with a fake queue item.
                             If omitted, synthesized from dept.yaml.draft's
                             recurring_missions[0] or the generic template.
  --seed=<int>               Optional. When set, two runs with the same seed
                             produce byte-identical artifacts.
  --accept-warnings          Operator explicit override: allow WARNING-only
                             results to advance (exit 0).
  --verbose                  Print every artifact path written.
  --help                     Show this message.

Effect:
  Writes outputs/dry-run/<ts>/{1,2,3,4}/ + outputs/dry-run/<ts>/inbox/ under
  --dept-root. Prints the DryRunResult as JSON on stdout. Exits 0 iff the
  result can_advance_to_ready.

Example:
  run-dry-run.sh --dept-root=/tmp/bubble-ops-miranda --seed=42

USAGE
}

DEPT_ROOT=""
FAKE_QI=""
SEED=""
ACCEPT_WARNINGS="false"
VERBOSE="false"

for arg in "$@"; do
  case "$arg" in
    --dept-root=*)        DEPT_ROOT="${arg#*=}" ;;
    --fake-queue-item=*)  FAKE_QI="${arg#*=}" ;;
    --seed=*)             SEED="${arg#*=}" ;;
    --accept-warnings)    ACCEPT_WARNINGS="true" ;;
    --verbose)            VERBOSE="true" ;;
    --help|-h)            usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$DEPT_ROOT" ]] && { echo "ERROR: --dept-root required" >&2; usage >&2; exit 64; }
[[ ! -d "$DEPT_ROOT" ]] && { echo "ERROR: dept-root not found: $DEPT_ROOT" >&2; exit 64; }

# Delegate to Python — the simulator lives in the skill_lib.
SKILL_LIB="$PROJECT_ROOT/skills/department-onboarding-guide"

# Use the calling environment's interpreter (honors $PYTHON, e.g. set by a
# Python caller to sys.executable), falling back to whatever `python3` PATH
# resolves to. On the VPS this stays "python3" (system interpreter, which
# already has the deps) — only dev/CI callers that export PYTHON change
# behavior.
exec "${PYTHON:-python3}" - <<PYEOF
import json, sys, yaml
from pathlib import Path
sys.path.insert(0, "$SKILL_LIB")
from skill_lib.dry_run import run_dry_run_full

dept_root = Path("$DEPT_ROOT")
seed = int("$SEED") if "$SEED" else None
fake_qi = None
fake_qi_path = "$FAKE_QI"
if fake_qi_path:
    fake_qi = yaml.safe_load(Path(fake_qi_path).read_text(encoding="utf-8"))
accept = ("$ACCEPT_WARNINGS" == "true")
verbose = ("$VERBOSE" == "true")

result = run_dry_run_full(
    dept_root=dept_root,
    fake_queue_item=fake_qi,
    operator_accepts_warnings=accept,
    seed=seed,
)
out = result.to_dict()
print(json.dumps(out, indent=2, sort_keys=True))

if verbose:
    print("=== verbose: artifacts written ===", file=sys.stderr)
    for layer_id, lr in sorted(result.layer_results.items()):
        for art in lr.artifacts_written:
            print(f"  L{layer_id}: {art}", file=sys.stderr)

sys.exit(0 if result.can_advance_to_ready else 1)
PYEOF
