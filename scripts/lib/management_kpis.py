#!/usr/bin/env python3
"""Department-owner child: eligibility, safe export inspection and KPI sidecar."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

# Isolated interpreter: import exclusively from the framework, never the dept cwd.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from management_export_shape import export_shape
    from mission_kpis import build, day
else:
    from .management_export_shape import export_shape
    from .mission_kpis import build, day
import yaml

CAP = 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Refusal(Exception):
    pass


def check_entry(fd, name, *, directory=False):
    try:
        info = os.lstat(name, dir_fd=fd)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise Refusal('symlink-' + name)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise Refusal('nonregular-' + name)
    return True


def read_file(fd, name):
    if not check_entry(fd, name):
        return None
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refusal('nonregular-' + name)
        if name == 'summary.md':
            # Determine nonblank presence without retaining an unbounded day report.
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return b''
                if chunk.strip():
                    return b'nonblank'
        return stream.read(CAP + 1)


def child(dept_dir: Path, report_day: str, *, dry_run=False,
          transcripts_dir=None, token_threshold=30_000_000) -> str:
    day(report_day)
    # Never run this child as root. Owner validation remains the parent's job.
    if os.geteuid() == 0:
        return 'error:uid-zero'
    fds = []
    try:
        if stat.S_ISLNK(os.lstat(dept_dir).st_mode):
            raise Refusal('symlink-dept')
        fd = os.open(dept_dir, DIR_FLAGS)
        fds.append(fd)
        raw_manifest = read_file(fd, 'dept.yaml')
        if raw_manifest is None or len(raw_manifest) > CAP:
            return 'skipped:invalid-manifest'
        try:
            manifest = yaml.safe_load(raw_manifest)
        except Exception:
            return 'skipped:invalid-manifest'
        if not isinstance(manifest, dict):
            return 'skipped:invalid-manifest'
        department = manifest.get('department', {})
        status = manifest.get('status', department.get('status') if isinstance(department, dict) else None)
        if not isinstance(status, str) or status.lower() != 'live':
            return 'skipped:not-live'
        layers = manifest.get('layers', {})
        subscribed = layers.get('subscribed', []) if isinstance(layers, dict) else []
        if not isinstance(subscribed, list) or not any(type(n) is int and n == 4 for n in subscribed):
            return 'skipped:no-l4'
        # Preflight all existing components and both model files before any write/KPI read.
        missing = False
        for component in ('outputs', report_day, '4'):
            if missing or not check_entry(fd, component, directory=True):
                missing = True
                continue
            fd = os.open(component, DIR_FLAGS, dir_fd=fd)
            fds.append(fd)
        if not missing:
            for name in ('management-export.yaml', 'summary.md', 'management-kpis.yaml'):
                check_entry(fd, name)
            export = read_file(fd, 'management-export.yaml')
            summary = read_file(fd, 'summary.md')
        else:
            export = summary = None
        if dry_run:
            return 'export_missing' if export is None else 'written'
        # Create missing paths safely, retaining directory descriptors across renames.
        fd = fds[0]
        for component in ('outputs', report_day, '4'):
            if not check_entry(fd, component, directory=True):
                try:
                    os.mkdir(component, mode=0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, DIR_FLAGS, dir_fd=fd)
            fds.append(next_fd)
            fd = next_fd
        for name in ('management-export.yaml', 'summary.md', 'management-kpis.yaml'):
            check_entry(fd, name)
        export = read_file(fd, 'management-export.yaml')
        summary = read_file(fd, 'summary.md')
        shape, note = export_shape(export)
        kpis = build(dept_dir, report_day, transcripts_dir=transcripts_dir,
                     token_threshold=token_threshold)
        attention = list(kpis['attention'])
        missing_sources = set(kpis['sources_missing'])
        def flag(identifier, summary_text):
            attention.append(dict(id=identifier, kind='mission_cost_quality', priority='low', summary=summary_text))
        if export is not None and shape != 'schema':
            flag('kpi-export-nonconforming', 'Management export is not schema-shaped: ' + note)
        links = {}
        if summary is not None and summary.strip():
            links['day_report'] = f'outputs/{report_day}/4/summary.md'
        else:
            missing_sources.add('day_report')
            flag('kpi-day-report-missing', f'Department day report is missing or empty for {report_day}')
        if export is None:
            missing_sources.add('management_export')
        doc = dict(dept=dept_dir.name, date=report_day,
                   generated_at=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
                   export_present=export is not None, export_shape=shape, export_shape_note=note,
                   top_kpis=kpis['top_kpis_flat'], missions=kpis['missions'],
                   dept_tokens_today=kpis['dept_tokens_today'], attention=attention,
                   links=links, sources_missing=sorted(missing_sources))
        # /proc/self/fd pins the Linux output directory even during a hostile rename.
        if sys.platform.startswith('linux'):
            handle, temporary = tempfile.mkstemp(prefix='.management-kpis.', dir=f'/proc/self/fd/{fd}')
        else:
            # macOS has no directory traversal through /dev/fd. Pin cwd for offline fixtures.
            saved_cwd = os.open('.', DIR_FLAGS)
            try:
                os.fchdir(fd)
                handle, temporary = tempfile.mkstemp(prefix='.management-kpis.', dir='.')
            finally:
                os.fchdir(saved_cwd)
                os.close(saved_cwd)
        temporary_name = Path(temporary).name
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, 'management-kpis.yaml', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=fd)
            except FileNotFoundError:
                pass
        return 'export_missing' if export is None else 'written'
    except Refusal as exc:
        return 'error:' + str(exc)
    except Exception:
        return 'error:child-failed'
    finally:
        for fd in fds:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dept-dir', type=Path, required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--transcripts-dir', type=Path)
    parser.add_argument('--token-threshold', type=int, default=30_000_000)
    args = parser.parse_args()
    status = child(args.dept_dir, args.day, dry_run=args.dry_run,
                   transcripts_dir=args.transcripts_dir, token_threshold=args.token_threshold)
    print(json.dumps({'status': status}, separators=(',', ':')))
    return int(status.startswith('error:'))


if __name__ == '__main__':
    raise SystemExit(main())
