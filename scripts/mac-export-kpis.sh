#!/bin/bash
# Single-tenant Mac runner. Only the existing owner child may write outputs.
set -euo pipefail
framework_root="$(cd "$(dirname "$0")/.." && pwd)"
dept_dir=""; transcripts_dir=""; report_day=""; yesterday=0; dry_run=0
usage() {
    echo "usage: $0 --dept-dir DIR --transcripts-dir DIR [--day YYYY-MM-DD] [--also-yesterday] [--dry-run]" >&2
    exit 2
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir|--transcripts-dir|--day)
            [[ $# -ge 2 && -n "$2" ]] || usage
            case "$1" in
                --dept-dir) dept_dir="$2" ;;
                --transcripts-dir) transcripts_dir="$2" ;;
                --day) report_day="$2" ;;
            esac
            shift 2 ;;
        --also-yesterday) yesterday=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        *) usage ;;
    esac
done
[[ -n "$dept_dir" && -n "$transcripts_dir" ]] || usage
# The system python3 on the fleet's Macs has no PyYAML visible in isolated
# mode (-I ignores the user site); the framework checkout's own virtualenv
# does. Prefer it, allow an explicit override, fall back to python3.
if [[ -n "${PYTHON_BIN:-}" ]]; then
    py="$PYTHON_BIN"
elif [[ -x "$framework_root/.venv/bin/python3" ]]; then
    py="$framework_root/.venv/bin/python3"
else
    py="python3"
fi
if ! echo 'import yaml, zoneinfo' | "$py" -I - >/dev/null 2>&1; then
    echo '{"status": "error:python-missing-yaml"}'
    echo "mac-export-kpis: $py cannot import yaml in isolated mode" >&2
    exit 1
fi
# Python handles BSD-date portability, Paris midnight and month/DST boundaries.
days="$("$py" -I - "$report_day" "$yesterday" <<'PY'
import datetime as dt
import sys
from zoneinfo import ZoneInfo
value = sys.argv[1]
date = dt.date.fromisoformat(value) if value else dt.datetime.now(ZoneInfo('Europe/Paris')).date()
if value and date.isoformat() != value:
    raise SystemExit('day must be canonical YYYY-MM-DD')
print(date.isoformat())
if sys.argv[2] == '1':
    print((date - dt.timedelta(days=1)).isoformat())
PY
)"
failed=0
while IFS= read -r date; do
    command=("$py" -I "$framework_root/scripts/lib/management_kpis.py"
        --dept-dir "$dept_dir" --day "$date" --transcripts-dir "$transcripts_dir")
    if (( dry_run )); then command+=(--dry-run); fi
    # The child prints its sole JSON status and returns 1 for error:*.
    if ! "${command[@]}"; then
        failed=1
    fi
done <<< "$days"
exit "$failed"
