#!/usr/bin/env python3
"""#1595: scan/ack the existing Mac management inbox. Requires PyYAML + Git auth.

Fresh private clones avoid touching either agent's working tree or the publisher.
Run scan at tick start; ack a returned ID+sha256 only AFTER recording its outcome.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

REPO = 'https://github.com/Bubble-invest/bubble-ops-rnd.git'
INBOX = Path('queues/management')
TRAILER = 'Co-Authored-By: Codex (gpt-6-astra) <noreply@openai.com>'


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if result.returncode:
        # Never echo credential-helper output, remote URLs or directive content.
        raise RuntimeError('Git operation failed: ' + args[0])
    return result.stdout


def read_notes(root):
    notes = {}
    for path in sorted((root / INBOX).glob('*.yaml')):
        if path.name.startswith('.') or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Symlink in management inbox')
        raw = path.read_bytes()
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError('Malformed management note')
        actor = data.get('created_by') or data.get('from')
        if actor not in ('tony', 'joris', 'jade'):
            continue  # own outbound notes are not management instructions
        audience = data.get('audience', ['rnd'])
        if audience not in (['rnd'], ['tonio']):
            raise ValueError('Mac inbox requires exactly one audience: rnd or tonio')
        note_id = data.get('id') or data.get('directive_id')
        if not isinstance(note_id, str) or not note_id or note_id in notes:
            raise ValueError('Missing or duplicate management note ID')
        notes[note_id] = {'id': note_id, 'recipient': audience[0],
                          'sha256': hashlib.sha256(raw).hexdigest(), 'note': data}
    return notes


def update(root, actor, command, note_id=None, digest=None, action=None, outcome=None):
    inbox = root / INBOX
    if inbox.is_symlink() or (root / 'queues').is_symlink():
        raise ValueError('Symlink inbox refused')
    inbox.mkdir(parents=True, exist_ok=True)
    ledger_path = inbox / '.consumed.json'
    marker = inbox / '.last-mgmt-scan'
    if ledger_path.is_symlink() or marker.is_symlink():
        raise ValueError('Symlink bookkeeping refused')
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    if isinstance(ledger, list):
        ledger = {str(key): {} for key in ledger}
    if not isinstance(ledger, dict):
        raise ValueError('Malformed consumed ledger')
    notes = read_notes(root)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if command == 'scan':
        pending = [n for key, n in notes.items() if n['recipient'] == actor and key not in ledger]
        marker.write_text(now + '\n')
        return pending, [str(INBOX / '.last-mgmt-scan')]
    note = notes.get(note_id)
    if not note or note['recipient'] != actor or note['sha256'] != digest:
        raise ValueError('Ack does not match the scanned note and recipient')
    if note_id in ledger:
        return {'id': note_id, 'already_consumed': True}, []
    if action not in ('applied', 'deferred', 'conflict') or not outcome:
        raise ValueError('Ack requires a recorded outcome')
    ledger[note_id] = {'ts': now, 'actor': actor, 'action': action, 'note': outcome}
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + '\n')
    return {'id': note_id, 'consumed': True}, [str(INBOX / '.consumed.json')]


def transact(actor, command, **kwargs):
    # Each retry re-reads remote ledger, preserving the other agent's ack.
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix='management-inbox-') as tmp:
            root = Path(tmp) / 'repo'
            git(Path(tmp), 'clone', '--quiet', '--depth', '1', '--branch', 'main', REPO, str(root))
            result, paths = update(root, actor, command, **kwargs)
            if not paths:
                return result
            git(root, 'add', '--', *paths)
            git(root, '-c', 'user.name=management-inbox', '-c',
                'user.email=ops-loop-bot@bubble.invest', 'commit', '--quiet', '-m',
                f'fix: #1595 {actor} management inbox {command}\n\n{TRAILER}')
            try:
                git(root, 'push', '--quiet', 'origin', 'HEAD:main')
            except RuntimeError:
                if attempt == 2:
                    raise
                continue
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor', choices=['rnd', 'tonio'], required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('scan')
    ack = sub.add_parser('ack')
    ack.add_argument('--id', dest='note_id', required=True)
    ack.add_argument('--sha256', dest='digest', required=True)
    ack.add_argument('--action', choices=['applied', 'deferred', 'conflict'], required=True)
    ack.add_argument('--outcome', required=True, help='short non-sensitive outcome or local evidence reference')
    try:
        print(json.dumps(transact(**vars(parser.parse_args())), indent=2, default=str))
    except (RuntimeError, ValueError, OSError, yaml.YAMLError):
        parser.exit(1, '[context-skip directives] scan/ack failed; retry next tick; no success claimed\n')


if __name__ == '__main__':
    main()
