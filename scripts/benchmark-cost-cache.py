#!/usr/bin/env python3
"""Offline 200k-message cache benchmark; temporary fixtures stay in this checkout.

Each message has a distinct session (pessimistic runs-table size). Cold and warm
RSS are measured in separate processes. No transcript-sized Python collection.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]


def worker(root, warm):
    sys.path.insert(0, str(REPO))
    from console.services import cost_tracker as tracker
    tracker.PROJECTS_DIR = root / 'projects'
    tracker.CACHE_FILE = root / 'cache.sqlite3'
    if warm:
        def forbidden(*args):
            raise AssertionError('warm rebuild parsed an unchanged transcript')
        tracker._session_records = forbidden
    before = tracker.CACHE_FILE.stat().st_mtime_ns if warm else None
    day = datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()
    start = time.perf_counter()
    tracker.build_report(day=day)
    elapsed = time.perf_counter() - start
    start = time.perf_counter()
    tracker.build_report()
    start_ttl = time.perf_counter()
    tracker.build_report()
    ttl = time.perf_counter() - start_ttl
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin':
        rss *= 1024
    print(json.dumps({'seconds': round(elapsed, 4), 'peak_rss_mib': round(rss / 2**20, 2),
                      'cache_bytes': tracker.CACHE_FILE.stat().st_size,
                      'cache_mib': round(tracker.CACHE_FILE.stat().st_size / 2**20, 2),
                      'ttl_seconds': round(ttl, 6),
                      'warm_cache_unchanged': before == tracker.CACHE_FILE.stat().st_mtime_ns if warm else None}))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--messages', type=int, default=200_000)
    ap.add_argument('--worker', type=Path)
    ap.add_argument('--warm', action='store_true')
    args = ap.parse_args()
    if args.worker:
        worker(args.worker, args.warm)
        return
    with tempfile.TemporaryDirectory(prefix='.cost-benchmark-', dir=REPO) as temporary:
        root = Path(temporary)
        date = datetime.now(timezone.utc)
        # 20 files across 14 Paris days, two priced models; unique sessions.
        for file in range(20):
            path = root / 'projects/_vps-ben/work' / f'{file:02}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w') as stream:
                for n in range(file, args.messages, 20):
                    row = {'type': 'assistant', 'sessionId': f'session-{n}',
                           'timestamp': (date - timedelta(days=n % 14)).isoformat(),
                           'message': {'id': f'message-{n}', 'model': 'claude-sonnet-4-6' if n % 2 else 'claude-haiku-4-5',
                           'usage': {'input_tokens': 1200, 'output_tokens': 120,
                                     'cache_read_input_tokens': 2400, 'cache_creation_input_tokens': 300}}}
                    stream.write(json.dumps(row, separators=(',', ':')) + '\n')
        result = {'messages': args.messages, 'files': 20, 'distinct_sessions': args.messages,
                  'days': 14, 'models': 2, 'python': sys.version.split()[0]}
        for mode in ('cold', 'warm'):
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(root)]
            if mode == 'warm':
                command.append('--warm')
            run = subprocess.run(command, check=True, capture_output=True, text=True,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            result[mode] = json.loads(run.stdout)
        result['projected_mib_per_million'] = round(result['warm']['cache_bytes'] * 1_000_000 / args.messages / 2**20, 2)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
