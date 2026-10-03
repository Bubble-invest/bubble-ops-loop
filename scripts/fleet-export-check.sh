#!/usr/bin/env bash
# Independent fleet check; framework libraries execute as each department owner.
set -euo pipefail
FRAMEWORK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export FRAMEWORK_ROOT
exec "${PYTHON_BIN:-python3}" - "$@" <<'PY'
import argparse
import datetime as dt
import fcntl
import os
import pwd
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(os.environ['FRAMEWORK_ROOT'])
sys.path.insert(0, str(root / 'scripts/lib'))
from enrich_management_export import load_export
from mission_kpis import atomic_write, day

parser = argparse.ArgumentParser(description='Enrich fleet exports and alarm on missing daily exports')
parser.add_argument('--dry-run', action='store_true')
parser.add_argument('--day')
args = parser.parse_args()
paris = ZoneInfo('Europe/Paris')
now_text = os.environ.get('FLEET_EXPORT_NOW')  # Offline clock injection, ISO with offset.
now = dt.datetime.fromisoformat(now_text) if now_text else dt.datetime.now(paris)
if now.tzinfo is None:
    parser.error('FLEET_EXPORT_NOW must include a timezone')
now = now.astimezone(paris)
report_day = day(args.day) if args.day else now.date() - dt.timedelta(days=int(now.hour < 12))
alarm_due = report_day < now.date() or (report_day == now.date() and now.time() >= dt.time(23, 30))
agents = Path(os.environ.get('AGENTS_ROOT', '/srv/agents'))
state = Path(os.environ.get('FLEET_EXPORT_STATE_DIR', '/var/lib/bubble-fleet-export-check'))
python = os.environ.get('PYTHON_BIN', 'python3')
emit = os.environ.get('EMIT_BIN', str(root / 'tools/kanban/emit_kanban_item.sh'))
runuser = os.environ.get('RUNUSER_BIN', 'runuser')
failed = False

# Serialize real checks and keep alarm receipts outside all department repos.
lock = None
if not args.dry_run:
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'check.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
try:
    departments = sorted(agents.iterdir())
except OSError as exc:
    parser.exit(1, f'Cannot enumerate departments: {exc}\n')
for dept in departments:
    if dept.is_symlink() or not dept.is_dir() or not (dept / 'dept.yaml').is_file():
        continue
    slug = dept.name
    report = report_day.isoformat()
    export = dept / 'outputs' / report / '4/management-export.yaml'
    try:
        owner = pwd.getpwuid(dept.stat().st_uid).pw_name
        if export.is_file():
            doc = load_export(export)
            if str(doc['date']) != report:
                raise ValueError('export date differs from requested day')
            stamp = doc['top_kpis'].get('export_enriched')
            if type(stamp) is not int or stamp != 1:
                if args.dry_run:
                    print(f'DRY RUN enrich {slug} {report} as {owner}')
                    continue
                prefix = [runuser, '-u', owner, '--'] if os.geteuid() == 0 else []
                kpis = export.with_name('mission-kpis.json')
                command = [python, str(root / 'scripts/lib/mission_kpis.py'), '--dept-dir', str(dept),
                           '--day', report, '--out', str(kpis)]
                # Optional fixture root mirrors /home/agent-<slug>/.claude/projects.
                if os.environ.get('TRANSCRIPTS_ROOT'):
                    command += ['--transcripts-dir', str(Path(os.environ['TRANSCRIPTS_ROOT']) / slug)]
                command += ['--token-threshold', os.environ.get('TOKEN_THRESHOLD', '30000000')]
                subprocess.run(prefix + command, check=True)
                subprocess.run(prefix + [python, str(root / 'scripts/lib/enrich_management_export.py'),
                    '--export', str(export), '--kpis', str(kpis), '--dept-dir', str(dept), '--day', report], check=True)
                print(f'Enriched {slug} {report} as {owner}')
            continue
        if not alarm_due:
            print(f'Export pending {slug} {report}; deadline is 23:30 Europe/Paris')
            continue
        task = f'fleet-export-missing-{slug}-{report}'
        title = f'Missing daily management export: {slug} {report}'
        print(f'{"DRY RUN alarm" if args.dry_run else "ALARM"} {slug} {report}: {export} missing')
        receipt = state / f'{task}.sent'
        if args.dry_run or receipt.exists():
            continue
        subprocess.run([emit, f'task={task}', f'title={title}', 'type=incident', 'priority=high',
                        f'owner={slug}', 'budget=5', 'intent=system-convergence-north-star',
                        f'body=Mandatory daily export is missing for {slug} on {report}; check Layer 4 and restore its day report.',
                        f'context_url=outputs/{report}/4/management-export.yaml'], check=True)
        atomic_write(receipt, 'emitter accepted\n')
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError) as exc:
        failed = True
        print(f'ERROR {slug} {report}: {exc}', file=sys.stderr)
    except Exception as exc:
        # Malformed YAML in one department must not suppress another's alarm.
        failed = True
        print(f'ERROR {slug} {report}: {exc}', file=sys.stderr)
sys.exit(int(failed))
PY
