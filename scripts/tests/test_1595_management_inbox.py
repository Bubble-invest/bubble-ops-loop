"""Real Git round-trips against a local bare remote; no fleet writes."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('management_inbox', Path(__file__).parents[1] / 'management_inbox.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    bare = tmp_path / 'remote.git'
    m.git(tmp_path, 'init', '--bare', str(bare))
    work = tmp_path / 'seed'
    m.git(tmp_path, 'clone', str(bare), str(work))
    m.git(work, 'checkout', '-b', 'main')
    m.git(work, 'config', 'user.name', 'Test')
    m.git(work, 'config', 'user.email', 'test@example.invalid')
    inbox = work / m.INBOX
    inbox.mkdir(parents=True)
    for actor in ('rnd', 'tonio'):
        (inbox / f'directive-{actor}.yaml').write_text(yaml.safe_dump({
            'directive_id': actor, 'from': 'tony', 'target_dept': 'rnd',
            'audience': [actor], 'created_at': '2000-01-01', 'body': 'synthetic test'}))
    (inbox / '.last-mgmt-scan').write_text('2099-01-01T00:00:00Z')
    m.git(work, 'add', '.')
    m.git(work, 'commit', '-qm', 'fixture')
    m.git(work, 'push', 'origin', 'main')
    monkeypatch.setattr(m, 'REPO', str(bare))
    return bare, work


def ack(actor, note):
    return m.transact(actor, 'ack', note_id=note['id'], digest=note['sha256'],
                      action='applied', outcome='synthetic outcome')


def test_scan_ack_roundtrip_and_late_delivery(remote):
    rnd = m.transact('rnd', 'scan')
    tonio = m.transact('tonio', 'scan')
    assert [n['id'] for n in rnd] == ['rnd']
    assert [n['id'] for n in tonio] == ['tonio']
    assert ack('rnd', rnd[0])['consumed']
    assert ack('rnd', rnd[0])['already_consumed']
    assert ack('tonio', tonio[0])['consumed']
    assert m.transact('rnd', 'scan') == []
    ledger = json.loads(m.git(remote[0], 'show', 'main:queues/management/.consumed.json'))
    assert set(ledger) == {'rnd', 'tonio'}


def test_ack_wrong_actor_or_changed_payload_refused(remote):
    note = m.transact('rnd', 'scan')[0]
    with pytest.raises(ValueError):
        ack('tonio', note)
    note['sha256'] = 'stale'
    with pytest.raises(ValueError):
        ack('rnd', note)
    assert len(m.transact('rnd', 'scan')) == 1


def test_retry_preserves_concurrent_ack(remote, monkeypatch):
    note = m.transact('rnd', 'scan')[0]
    other = m.transact('tonio', 'scan')[0]
    original = m.git
    raced = []
    def racing_git(root, *args):
        if args[0] == 'push' and not raced:
            raced.append(True)
            ack('tonio', other)
        return original(root, *args)
    monkeypatch.setattr(m, 'git', racing_git)
    ack('rnd', note)
    ledger = json.loads(original(remote[0], 'show', 'main:queues/management/.consumed.json'))
    assert set(ledger) == {'rnd', 'tonio'}


def test_defaults_id_precedence_outbound_and_bad_ledger(tmp_path):
    inbox = tmp_path / m.INBOX
    inbox.mkdir(parents=True)
    (inbox / 'legacy.yaml').write_text('id: primary\ndirective_id: secondary\nfrom: tony\n')
    (inbox / 'outbound.yaml').write_text('id: self\ncreated_by: rnd\n')
    pending, _ = m.update(tmp_path, 'rnd', 'scan')
    assert [n['id'] for n in pending] == ['primary']
    (inbox / '.consumed.json').write_text('["primary"]')
    assert m.update(tmp_path, 'rnd', 'scan')[0] == []
    (inbox / '.consumed.json').write_text('broken')
    with pytest.raises(ValueError):
        m.update(tmp_path, 'rnd', 'scan')


def test_ambiguous_audience_and_symlinks_refused(tmp_path):
    inbox = tmp_path / m.INBOX
    inbox.mkdir(parents=True)
    note = inbox / 'bad.yaml'
    note.write_text('id: x\nfrom: tony\naudience: [rnd, tonio]\n')
    with pytest.raises(ValueError):
        m.update(tmp_path, 'rnd', 'scan')
    note.unlink()
    note.symlink_to(tmp_path / 'outside')
    (tmp_path / 'outside').write_text('id: x\nfrom: tony\n')
    with pytest.raises(ValueError):
        m.update(tmp_path, 'rnd', 'scan')


def test_failed_push_is_not_reported_as_ack(remote, monkeypatch):
    note = m.transact('rnd', 'scan')[0]
    original = m.git
    def fail(root, *args):
        if args[0] == 'push':
            raise RuntimeError('unavailable')
        return original(root, *args)
    monkeypatch.setattr(m, 'git', fail)
    with pytest.raises(RuntimeError):
        ack('rnd', note)
    assert '.consumed.json' not in original(remote[0], 'ls-tree', '-r', '--name-only', 'main')
