#!/usr/bin/env python3
"""Root coordinator: directory metadata, bounded owner children, alarm receipts."""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mission_kpis import atomic_write, day
else:
    from .mission_kpis import atomic_write, day

ROOT = Path(__file__).resolve().parents[2]
SLUG = re.compile(r'[a-z][a-z0-9-]{0,79}')
STATUS = re.compile(r'(?:written|export_missing|skipped:(?:not-live|no-l4|invalid-manifest)|error:[a-zA-Z0-9_.-]{1,80})')
CHECK_STATUS = re.compile(r'(?:export_present|export_missing|mirror_stale|skipped:(?:not-live|no-l4|invalid-manifest)|error:[a-zA-Z0-9_.-]{1,80})')
MAX_REPLY = 4096
CHILD_TIMEOUT = 120
EMIT_TIMEOUT = 60
NOTHING_PROCESSED = 2


def owner_for(dept, overrides, *, lstat=os.lstat, lookup=pwd.getpwuid):
    """Only lstat the directory itself; never inspect any department file."""
    if not SLUG.fullmatch(dept.name):
        return None, 'invalid-slug'
    try:
        info = lstat(dept)
        if stat.S_ISLNK(info.st_mode):
            return None, 'symlink-dept'
        if not stat.S_ISDIR(info.st_mode):
            return None, 'not-directory'
        if info.st_uid == 0:
            return None, 'uid-zero'
        owner = lookup(info.st_uid)
        if owner.pw_uid != info.st_uid:
            return None, 'owner-uid-mismatch'
        expected = overrides.get(dept.name, 'agent-' + dept.name)
        if owner.pw_name != expected:
            return None, 'owner-name-mismatch'
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,79}', owner.pw_name):
            return None, 'invalid-owner-name'
        return owner.pw_name, None
    except (OSError, KeyError):
        return None, 'owner-unavailable'


def mirror_for(entry, mirror_root, mirror_owner, *, lstat=os.lstat,
               readlink=os.readlink, lookup=pwd.getpwuid):
    """Inspect names, symlink strings and directory metadata only, never files.

    Resolve bounded symlink chains using path strings, then lstat only the
    final directory if it is a direct child of the trusted mirror root.
    """
    if not entry.name.startswith('bubble-ops-'):
        return None, None, 'not-mirror'
    slug = entry.name[len('bubble-ops-'):]
    if not SLUG.fullmatch(slug):
        return None, None, 'invalid-slug'
    try:
        if not stat.S_ISLNK(lstat(entry).st_mode):
            return None, None, 'not-symlink'
        # A real department takes precedence even if its owner is refused.
        try:
            real = lstat(entry.parent / slug)
        except FileNotFoundError:
            real = None
        if real is not None and stat.S_ISDIR(real.st_mode):
            return None, None, 'real-department'
        link = Path(readlink(entry))
        target = Path(os.path.abspath(link if link.is_absolute() else entry.parent / link))
        mirror_root = Path(os.path.abspath(mirror_root))
        seen = {Path(os.path.abspath(entry))}
        for _ in range(40):
            if target in seen:
                return None, None, 'symlink-loop'
            seen.add(target)
            try:
                link = Path(readlink(target))
            except OSError as exc:
                if exc.errno != errno.EINVAL:  # EINVAL means the target is not a symlink.
                    raise
                break
            target = Path(os.path.abspath(link if link.is_absolute() else target.parent / link))
        else:
            return None, None, 'symlink-loop'
        if target.parent != mirror_root:
            return None, None, 'outside-mirror-root'
        info = lstat(target)
        if not stat.S_ISDIR(info.st_mode):
            return None, None, 'not-directory'
        if info.st_uid == 0:
            return None, None, 'uid-zero'
        owner = lookup(info.st_uid)
        if owner.pw_uid != info.st_uid:
            return None, None, 'owner-uid-mismatch'
        if owner.pw_name != mirror_owner:
            return None, None, 'owner-name-mismatch'
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,79}', owner.pw_name):
            return None, None, 'invalid-owner-name'
        return target, owner.pw_name, None
    except (OSError, KeyError):
        return None, None, 'owner-unavailable'


def parse_reply(code, raw, *, check_only=False):
    if not isinstance(raw, bytes) or len(raw) > MAX_REPLY:
        return 'error:invalid-reply'
    try:
        # Exactly one JSON line, one expected key, no untrusted log content.
        lines = raw.decode('utf-8').splitlines()
        if len(lines) != 1:
            raise ValueError
        def unique(pairs):
            if len(pairs) != 1 or pairs[0][0] != 'status':
                raise ValueError
            return dict(pairs)
        doc = json.loads(lines[0], object_pairs_hook=unique)
        status = doc['status']
        contract = CHECK_STATUS if check_only else STATUS
        if not isinstance(status, str) or not contract.fullmatch(status):
            raise ValueError
        if code != (1 if status.startswith('error:') else 0):
            return 'error:child-exit'
        return status
    except (ValueError, TypeError, KeyError, UnicodeError):
        return 'error:invalid-reply'


def run_child(command, *, timeout=CHILD_TIMEOUT):
    """Bound both execution time and stdout memory; discard untrusted stderr."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, MAX_REPLY + 1 - len(data))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(chunk)
                        if len(data) > MAX_REPLY:
                            raise ValueError('oversized-reply')
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return parse_reply(code, bytes(data), check_only='--check-only' in command)
    except subprocess.TimeoutExpired:
        return 'error:child-timeout'
    except ValueError:
        return 'error:oversized-reply'
    finally:
        # Kill an unreaped runuser group, including inherited pipe holders.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        process.stdout.close()


def select_day(now, requested=None):
    now = now.astimezone(ZoneInfo('Europe/Paris'))
    report_day = day(requested) if requested else now.date() - dt.timedelta(days=int(now.hour < 12))
    alarm_due = report_day < now.date() or (report_day == now.date() and now.time() >= dt.time(23, 30))
    return report_day.isoformat(), alarm_due


def mirror_alarm_due(now, report):
    """Only the morning activation checking yesterday tolerates mirror lag."""
    local = now.astimezone(ZoneInfo('Europe/Paris'))
    return 6 <= local.hour < 12 and day(report) == local.date() - dt.timedelta(days=1)


def emit_alarm(env, emit_runner, slug, report, task, title, body):
    emit_runner([env.get('EMIT_BIN', str(ROOT / 'tools/kanban/emit_kanban_item.sh')),
                 f'task={task}', f'title={title}',
                 'type=incident', 'priority=high', f'owner={slug}', 'budget=5',
                 'intent=system-convergence-north-star', f'body={body}',
                 f'context_url=outputs/{report}/4/management-export.yaml'],
                check=True, timeout=EMIT_TIMEOUT, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)


def mirror_receipt(path, *, first_key='first_missing_day'):
    """Read a bounded receipt in root's own state directory, never the mirror."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPLY:
            raise ValueError('nonregular or oversized receipt')
        with path.open('rb') as stream:
            raw = stream.read(MAX_REPLY + 1)
        if len(raw) > MAX_REPLY:
            raise ValueError('oversized receipt')
        doc = json.loads(raw)
        if not isinstance(doc, dict) or set(doc) != {first_key, 'last_alarm'}:
            raise ValueError('invalid mirror receipt')
        day(doc[first_key])
        if doc['last_alarm'] is not None:
            day(doc['last_alarm'])
        return doc
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        print(f'WARN ignoring unreadable or invalid mirror receipt: {path.name}', file=sys.stderr)
        return None


def check_mirrors(entries, state, report, *, env, dry_run, mirror_check,
                  child_runner, emit_runner, mirror_max_age_hours=6):
    failed = False
    processed = 0
    for entry in entries:
        if not entry.name.startswith('bubble-ops-'):
            continue
        target, owner, reason = mirror_check(
            entry, Path(env.get('FLEET_EXPORT_MIRROR_ROOT', '/home/claude/agents')),
            env.get('FLEET_EXPORT_MIRROR_OWNER', 'claude'))
        if reason:
            print(f'SKIP mirror {entry.name!r}: {reason}')
            continue
        # Revalidate even an injected checker; alarm text is root-derived only.
        slug = entry.name[len('bubble-ops-'):]
        if not entry.name.startswith('bubble-ops-') or not SLUG.fullmatch(slug):
            failed = True
            continue
        command = [env.get('RUNUSER_BIN', 'runuser'), '-u', owner, '--',
                   env.get('PYTHON_BIN', 'python3'), '-I', '-B',
                   str(ROOT / 'scripts/lib/management_kpis.py'),
                   '--dept-dir', str(target), '--day', report,
                   '--mirror-max-age-hours', str(mirror_max_age_hours), '--check-only']
        try:
            status = child_runner(command, timeout=CHILD_TIMEOUT)
            if not isinstance(status, str) or not CHECK_STATUS.fullmatch(status):
                status = 'error:invalid-reply'
            print(f'mirror {slug} {report}: {status}')
            if status.startswith('error:'):
                failed = True
            if status not in ('export_present', 'export_missing', 'mirror_stale'):
                continue
            processed += 1
            stale = status == 'mirror_stale'
            stale_receipt = state / f'fleet-mirror-stale-{slug}.mirror.json'
            if not stale and not dry_run:
                stale_receipt.unlink(missing_ok=True)
            task = f'fleet-mirror-stale-{slug}' if stale else f'fleet-export-missing-{slug}'
            receipt = state / f'{task}.mirror.json'
            if status == 'export_present':
                if not dry_run:
                    receipt.unlink(missing_ok=True)
                continue
            first_key = 'first_stale_day' if stale else 'first_missing_day'
            previous = mirror_receipt(receipt, first_key=first_key)
            if previous is not None and previous['last_alarm'] is not None:
                continue
            outage = previous or {first_key: report, 'last_alarm': None}
            first = outage[first_key]
            print(f'{"DRY RUN alarm" if dry_run else "ALARM"} mirror {slug} since {first}')
            if dry_run:
                continue
            if previous is None:
                # Invalid receipts are absent, including refused symlinks/FIFOs.
                # Replace them without carrying their metadata into the new file.
                receipt.unlink(missing_ok=True)
            # Persist the first observation before emission so failed emits on
            # subsequent days retain the original outage start. Retry until sent.
            atomic_write(receipt, json.dumps(outage) + '\n')
            if stale:
                title = f'VPS mirror of {slug} is not updating since {first}'
                body = (f'VPS mirror of {slug} is stale since {first}; latest check day is {report}. '
                        'Check the VPS mirror sync job and file ownership. The Mac export status is unknown.')
            else:
                title = f'Missing daily management export: {slug} since {first}'
                body = (f'Mandatory daily export is missing for {slug} since {first}; '
                        f'latest missing day is {report}. Check the Mac runtime and its outputs publication.')
            emit_alarm(env, emit_runner, slug, report, task, title, body)
            outage['last_alarm'] = report
            atomic_write(receipt, json.dumps(outage) + '\n')
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            failed = True
            print(f'ERROR mirror {slug} {report}: child-or-emit-failed', file=sys.stderr)
    return processed, failed


def run_loop(agents, state, report, alarm_due, *, env, dry_run=False,
             owner_check=owner_for, child_runner=run_child, emit_runner=subprocess.run,
             mirror_due=False, mirror_check=mirror_for, mirror_max_age_hours=6):
    overrides = {}
    for entry in env.get('FLEET_EXPORT_OWNER_OVERRIDES', '').split(','):
        if entry:
            slug, sep, user = entry.partition('=')
            if not sep or not SLUG.fullmatch(slug) or not user:
                raise ValueError('invalid owner override')
            overrides[slug] = user
    failed = False
    processed = 0
    lock = None
    try:
        if not dry_run:
            state.mkdir(parents=True, exist_ok=True)
            lock = (state / 'check.lock').open('a')
            fcntl.flock(lock, fcntl.LOCK_EX)
        # iterdir lists names only. No is_dir/stat-follow/dept.yaml checks here.
        entries = sorted(agents.iterdir())
        for dept in entries:
            owner, reason = owner_check(dept, overrides)
            if reason:
                print(f'SKIP {dept.name!r}: {reason}')
                continue
            slug = dept.name
            command = [env.get('RUNUSER_BIN', 'runuser'), '-u', owner, '--',
                       env.get('PYTHON_BIN', 'python3'), '-I', '-B',
                       str(ROOT / 'scripts/lib/management_kpis.py'),
                       '--dept-dir', str(dept), '--day', report,
                       '--token-threshold', env.get('TOKEN_THRESHOLD', '30000000')]
            if env.get('TRANSCRIPTS_ROOT'):
                command += ['--transcripts-dir', str(Path(env['TRANSCRIPTS_ROOT']) / slug)]
            if dry_run:
                command.append('--dry-run')
            try:
                status = child_runner(command, timeout=CHILD_TIMEOUT)
                # Apply the same contract even to an injected runner.
                if not isinstance(status, str) or not STATUS.fullmatch(status):
                    status = 'error:invalid-reply'
                print(f'{slug} {report}: {status}')
                if status.startswith('error:'):
                    failed = True
                if status in ('written', 'export_missing'):
                    processed += 1
                if status != 'export_missing':
                    continue
                if not alarm_due:
                    print(f'Export pending {slug} {report}; deadline is 23:30 Europe/Paris')
                    continue
                task = f'fleet-export-missing-{slug}-{report}'
                receipt = state / f'{task}.sent'
                if not dry_run and receipt.exists():
                    continue
                print(f'{"DRY RUN alarm" if dry_run else "ALARM"} {slug} {report}')
                if dry_run:
                    continue
                # Every character comes from validated directory slug + canonical day.
                emit_alarm(env, emit_runner, slug, report, task,
                           f'Missing daily management export: {slug} {report}',
                           f'Mandatory daily export is missing for {slug} on {report}; check Layer 4 and restore its day report.')
                atomic_write(receipt, 'emitter accepted\n')
            except (OSError, ValueError, subprocess.SubprocessError):
                failed = True
                print(f'ERROR {slug} {report}: child-or-emit-failed', file=sys.stderr)
        if alarm_due and mirror_due:
            mirror_processed, mirror_failed = check_mirrors(
                entries, state, report, env=env, dry_run=dry_run,
                mirror_check=mirror_check, child_runner=child_runner, emit_runner=emit_runner,
                mirror_max_age_hours=mirror_max_age_hours)
            processed += mirror_processed
            failed = failed or mirror_failed
    finally:
        if lock is not None:
            lock.close()
    if processed == 0:
        print('ERROR fleet: nothing processed (no department returned written or export_missing)', file=sys.stderr)
        return NOTHING_PROCESSED
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--day')
    parser.add_argument('--mirror-max-age-hours', type=float, default=6)
    args = parser.parse_args()
    if not math.isfinite(args.mirror_max_age_hours) or args.mirror_max_age_hours <= 0:
        parser.error('--mirror-max-age-hours must be finite and positive')
    text = os.environ.get('FLEET_EXPORT_NOW')
    now = dt.datetime.fromisoformat(text) if text else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        parser.error('FLEET_EXPORT_NOW must include a timezone')
    report, alarm_due = select_day(now, args.day)
    try:
        return run_loop(Path(os.environ.get('AGENTS_ROOT', '/srv/agents')),
                        Path(os.environ.get('FLEET_EXPORT_STATE_DIR', '/var/lib/bubble-fleet-export-check')),
                        report, alarm_due, env=os.environ, dry_run=args.dry_run,
                        mirror_max_age_hours=args.mirror_max_age_hours,
                        mirror_due=mirror_alarm_due(now, report))
    except (OSError, ValueError):
        print('ERROR fleet configuration or directory enumeration', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
