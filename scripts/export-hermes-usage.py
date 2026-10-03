#!/usr/bin/env python3
"""Root-run, read-only Hermes counters export; no session text or prompts.

Each profile is isolated: failed reads preserve the last good export, log an
error and return non-zero. A transcript-sync caller may treat this as non-fatal.
"""
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'console' / 'services'))
from cost_io import atomic_json

FIELDS = ('model', 'input_tokens', 'output_tokens', 'cache_read_tokens',
          'cache_write_tokens', 'reasoning_tokens', 'estimated_cost_usd', 'actual_cost_usd')


class UsageReadError(RuntimeError):
    """Publication must keep the previous good export on read failure."""


def _columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _rows(db, table, fields):
    # Identifiers come only from constants / PRAGMA, never profile/user input.
    names = ','.join(f'"{name}"' for name in fields)
    return [dict(row) for row in db.execute(f'SELECT {names} FROM "{table}"')]


def _clean(row):
    result = {'model': row.get('model') or 'unknown'}
    for field in FIELDS[1:]:
        value = row.get(field)
        try:
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0)
        except OverflowError:
            valid = False
        if field.endswith('_tokens'):
            valid = valid and int(value) == value
        result[field] = value if valid else None
    return result


def read_usage(path: Path) -> dict:
    rows, notes = [], []
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing regular database')
        with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')  # consistent read snapshot, no writes
            columns = _columns(db, 'sessions')
            identity = next((key for key in ('id', 'session_id') if key in columns), None)
            if identity is None or not {'started_at', 'input_tokens', 'output_tokens'} <= columns:
                raise ValueError('missing sessions schema')
            if not set(FIELDS) <= columns:
                notes.append('Hermes sessions schema has missing optional usage/cost columns; unavailable values are null.')
            sessions = _rows(db, 'sessions', [identity, 'started_at'] + [f for f in FIELDS if f in columns])
            by_session = {}
            model_columns = _columns(db, 'session_model_usage')
            if {'session_id', 'model', 'input_tokens', 'output_tokens'} <= model_columns:
                if not set(FIELDS) <= model_columns:
                    notes.append('Hermes model schema has missing optional usage/cost columns; unavailable values are null.')
                for usage in _rows(db, 'session_model_usage', ['session_id'] + [f for f in FIELDS if f in model_columns]):
                    by_session.setdefault(str(usage['session_id']), []).append(usage)
            else:
                notes.append('Hermes session_model_usage absent/incomplete; using session counters once per session.')
            for session in sessions:
                sid = session[identity]
                if sid is None:
                    notes.append('Hermes session without an identity skipped.')
                    continue
                usage_rows = by_session.get(str(sid)) or [session]
                for usage in usage_rows:
                    clean = _clean(usage)
                    if clean['input_tokens'] is None or clean['output_tokens'] is None:
                        notes.append('Hermes row with invalid required token counters skipped.')
                        continue
                    # A single model can inherit the session bill. Multiple models
                    # must never each inherit the whole session bill.
                    if len(usage_rows) == 1:
                        session_clean = _clean(session)
                        for field in ('actual_cost_usd', 'estimated_cost_usd'):
                            if clean[field] is None:
                                clean[field] = session_clean[field]
                    session_clean = _clean(session)
                    rows.append({'session_id': str(sid), 'started_at': session['started_at'],
                                 'session_actual_cost_usd': session_clean['actual_cost_usd'],
                                 'session_estimated_cost_usd': session_clean['estimated_cost_usd'], **clean})
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
        raise UsageReadError(f'Hermes database read failed: {path}: {exc}') from exc
    return {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'rows': rows, 'notes': sorted(set(notes))}


def export_all(home: Path, projects: Path) -> int:
    failures = 0
    try:
        agents = sorted(p for p in home.iterdir() if p.name.startswith('agent-') and p.is_dir())
        for agent in agents:
            profiles = agent / '.hermes' / 'profiles'
            if agent.is_symlink() or (agent / '.hermes').is_symlink() or profiles.is_symlink():
                continue
            try:
                candidates = sorted(profiles.iterdir())
            except FileNotFoundError:
                continue
            except OSError as exc:
                logging.error('Hermes profile discovery failed for %s: %s', agent.name, exc)
                failures += 1
                continue
            for profile in candidates:
                if profile.is_symlink() or not profile.is_dir():
                    continue
                slug = profile.name
                if not all(c.isascii() and (c.isalnum() or c in '-_') for c in slug):
                    continue
                try:
                    atomic_json(projects / f'_vps-{slug}-hermes' / 'hermes-usage.json',
                                read_usage(profile / 'state.db'))
                except (UsageReadError, OSError, ValueError, TypeError) as exc:
                    logging.error('Hermes usage publication failed for profile %s; previous export preserved: %s', slug, exc)
                    failures += 1
    except OSError as exc:
        logging.error('Hermes usage discovery failed: %s', exc)
        failures += 1
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home-dir', type=Path, default=Path('/home'))
    ap.add_argument('--projects-dir', type=Path, default=Path('/home/claude/.claude/projects'))
    args = ap.parse_args()
    return export_all(args.home_dir, args.projects_dir)


if __name__ == '__main__':
    raise SystemExit(main())
