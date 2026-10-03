"""Offline fixtures for #1708: real ledger shapes, owner writes, independent alarms."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.lib import mission_kpis as kpi
from scripts.lib import management_kpis as sidecar
from scripts.lib import fleet_export_check as fleet_check

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/fleet-export'
DAY = '2026-10-03'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def ledger(dept, date, value):
    write_json(dept / 'outputs' / date / 'dispatch.json', value)


def manifest(dept, ids=('inbox_watch', 'idle')):
    dept.mkdir(parents=True, exist_ok=True)
    (dept / 'dept.yaml').write_text(yaml.safe_dump({'status': 'live', 'layers': {'subscribed': [1, 4]}, 'recurring_missions': [{'id': m} for m in ids]}))


def message(identifier='a', stamp='2026-10-03T08:08:20Z', *, model='claude-sonnet-4', **usage):
    return {'type': 'assistant', 'timestamp': stamp, 'message': {
        'id': identifier, 'model': model, 'usage': usage or {
            'input_tokens': 10, 'cache_read_input_tokens': 20,
            'cache_creation_input_tokens': 30, 'output_tokens': 40}}}


def transcript(path, calls):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(c) + '\n' for c in calls))


def valid_export(dept='tony'):
    return dict(dept=dept, date=DAY, status='warning', last_successful_layer=4,
                open_gates=0, open_exceptions=0, top_kpis={'model_kpi': {'status': 'fine'}},
                needs_management_attention=['Keep this free-form model note.'], links={'gates': 'queues/gates/'})


def export_file(dept, value=None, date=DAY):
    path = dept / 'outputs' / date / '4/management-export.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value or valid_export(dept.name), sort_keys=False))
    return path


@pytest.fixture
def evidence(tmp_path):
    dept = tmp_path / 'tony'
    manifest(dept)
    start = dt.date.fromisoformat(DAY) - dt.timedelta(days=27)
    for offset in range(28):
        ledger(dept, (start + dt.timedelta(days=offset)).isoformat(), {})
    ledger(dept, DAY, json.loads((FIXTURES / 'dispatch-tony.json').read_text()))
    sessions = tmp_path / 'sessions'
    transcript(sessions / 'main.jsonl', [message()])
    return dept, sessions


@pytest.mark.parametrize('slug,artifacts', [('tony', 0), ('ben', 3), ('maya', 28)])
def test_real_shapes(evidence, slug, artifacts):
    dept, sessions = evidence
    raw = json.loads((FIXTURES / f'dispatch-{slug}.json').read_text())
    ledger(dept, DAY, raw)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert sum(doc['missions'][m]['artifacts_count'] for m in raw) == artifacts
    for mission in raw:
        data = doc['missions'][mission]
        assert data['days_dispatched'] == data['runs_dispatched'] == data['runs_completed'] == 1
        assert data['runs_incomplete'] == data['runs_unattributed'] == 0
    assert doc['missions']['idle']['runs_dispatched'] == 0


def test_inclusive_window_and_incomplete(evidence):
    dept, sessions = evidence
    for date in ('2026-09-05', '2026-09-06', '2026-10-04'):
        ledger(dept, date, {'inbox_watch': {'dispatched_at': f'{date}T09:00:00Z', 'artifacts': []}})
    # materialized_at is not a dispatch; no invented run status or counter.
    ledger(dept, '2026-10-02', {'inbox_watch': {'dispatched_at': '2026-10-02T09:00:00Z'},
                              'materialized_only': {'materialized_at': '2026-10-02T09:00:00Z'}})
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    data = doc['missions']['inbox_watch']
    assert (data['days_dispatched'], data['runs_dispatched'], data['runs_completed'],
            data['runs_incomplete'], data['runs_unattributed']) == (3, 3, 1, 2, 2)
    flag = next(a for a in doc['attention'] if a['id'] == 'kpi-inbox-watch-incomplete-runs')
    assert flag['priority'] == 'high'
    assert doc['missions']['materialized_only']['runs_dispatched'] == 0


def test_tokens_windows_subagents_dedupe_and_paris_day(evidence):
    dept, sessions = evidence
    calls = [message('before', '2026-10-03T08:07:59Z'), message('start', '2026-10-03T08:08:00Z'),
             message('end', '2026-10-03T08:08:43.517525Z'), message('after', '2026-10-03T08:08:44Z'),
             message('paris-today', '2026-10-02T22:05:00Z'), message('paris-tomorrow', '2026-10-03T22:05:00Z')]
    # A fuller repeated assistant id supersedes a streaming fragment.
    transcript(sessions / 'main.jsonl', [message('start', input_tokens=1)] + calls)
    transcript(sessions / 'rotated.jsonl', calls)
    transcript(sessions / 'main/subagents/agent-worker.jsonl', [message('subagent')])
    doc = kpi.build(dept, DAY, transcripts_dir=sessions, token_threshold=600)
    data = doc['missions']['inbox_watch']
    assert data['total_tokens_approx'] == 300
    assert data['input_tokens_approx'] == 30
    assert data['cache_read_tokens_approx'] == 60
    assert data['cache_write_tokens_approx'] == 90
    assert data['output_tokens_approx'] == 120
    total = doc['dept_tokens_today']
    assert total['total_tokens'] == 600
    assert total['model_calls'] == 6
    assert total['cost_usd_estimate'] == pytest.approx(0.004491)
    assert data['cost_usd_estimate_approx'] == pytest.approx(0.0022455, abs=1e-6)
    assert doc['top_kpis_flat']['tokens_today_total_mtok'] == 0.0006
    assert any(a['id'] == 'kpi-dept-token-use' and a['priority'] == 'medium' for a in doc['attention'])


def test_missing_message_ids_are_not_deduped(evidence):
    dept, sessions = evidence
    first = message(None)
    transcript(sessions / 'main.jsonl', [first, first])
    assert kpi.build(dept, DAY, transcripts_dir=sessions)['dept_tokens_today']['model_calls'] == 2


def test_missing_sources_are_absent_not_zero(tmp_path):
    dept = tmp_path / 'tony'
    manifest(dept)
    doc = kpi.build(dept, DAY, transcripts_dir=tmp_path / 'missing')
    assert doc['missions']['inbox_watch'] == {}
    assert doc['dept_tokens_today'] == {}
    assert 'model_calls_today' not in doc['top_kpis_flat']
    assert {'board', 'runs', 'mission_map', 'transcripts', f'dispatch:{DAY}'} <= set(doc['sources_missing'])
    assert doc['top_kpis_flat']['mission_kpi_sources_missing'] == len(doc['sources_missing'])
    assert next(a for a in doc['attention'] if a['id'] == 'kpi-sources-missing')['priority'] == 'low'


@pytest.mark.parametrize('kind', ['bad-transcript', 'bad-board', 'bad-runs', 'bad-manifest', 'bad-ledger'])
def test_malformed_sources_are_missing(evidence, kind):
    dept, sessions = evidence
    paths = {'bad-transcript': sessions / 'main.jsonl', 'bad-board': dept / 'monitoring/board-issues.json',
             'bad-runs': dept / 'monitoring/runs.json', 'bad-manifest': dept / 'dept.yaml',
             'bad-ledger': dept / 'outputs' / DAY / 'dispatch.json'}
    path = paths[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[broken')
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    expected = {'bad-transcript': 'transcripts', 'bad-board': 'board', 'bad-runs': 'runs',
                'bad-manifest': 'dept.yaml', 'bad-ledger': f'dispatch:{DAY}'}[kind]
    assert expected in doc['sources_missing']
    if kind == 'bad-transcript':
        assert doc['dept_tokens_today'] == {}
        assert 'total_tokens_approx' not in doc['missions']['inbox_watch']


def test_unreadable_source_is_missing(evidence, monkeypatch):
    dept, sessions = evidence
    original = kpi.open_text

    def open_file(path, *args, **kwargs):
        if path.name == 'main.jsonl':
            raise PermissionError('fixture permission denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(kpi, 'open_text', open_file)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert 'transcripts' in doc['sources_missing']
    assert doc['dept_tokens_today'] == {}


def test_unknown_model_is_not_priced_as_zero(evidence):
    dept, sessions = evidence
    transcript(sessions / 'main.jsonl', [message(model='unpriced-model')])
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['dept_tokens_today']['total_tokens'] == 100
    assert 'cost_usd_estimate' not in doc['dept_tokens_today']
    assert 'cost_usd_estimate_approx' not in doc['missions']['inbox_watch']


def test_pricing_unavailable_is_tokens_only(evidence, monkeypatch):
    dept, sessions = evidence
    monkeypatch.setattr(kpi, 'pricing_table', lambda: None)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['dept_tokens_today']['total_tokens'] == 100
    assert 'cost_usd_estimate' not in doc['dept_tokens_today']


def test_optional_reference_metrics(evidence):
    dept, sessions = evidence
    mapping = dept / 'config/mission_kpi_map.yaml'
    mapping.parent.mkdir()
    mapping.write_text(yaml.safe_dump({'inbox_watch': {'titles': ['Health'], 'jobs': ['watch']},
                                       'other': {'titles': ['Health'], 'jobs': []}}, sort_keys=False))
    issues = [dict(title='Health check', createdAt='2026-09-06T12:00:00Z', state='CLOSED',
                   stateReason='NOT_PLANNED' if i < 4 else 'COMPLETED') for i in range(10)]
    issues += [dict(title='Health too early', createdAt='2026-09-05T12:00:00Z', state='OPEN'),
               dict(title='Health too late', createdAt='2026-10-04T12:00:00Z', state='OPEN')]
    board, runs = dept / 'board.json', dept / 'runs.json'
    write_json(board, issues)
    write_json(runs, [dict(job='watch', date=DAY, cost_usd=c, turns=t, is_error=False)
                      for c, t in [(2.5, 1), (3.5, 5)]] +
                     [dict(job='watch', date='2026-09-05', cost_usd=99, turns=5, is_error=True)])
    doc = kpi.build(dept, DAY, transcripts_dir=sessions, board_json=board, runs_json=runs)
    data = doc['missions']['inbox_watch']
    assert (data['cards_created'], data['cards_done'], data['cards_dropped'], data['drop_rate']) == (10, 6, 4, 0.4)
    assert (data['runs'], data['failures'], data['cost_usd_total'], data['cost_usd_last'], data['cost_usd_max']) == (2, 1, 6, 3.5, 3.5)
    assert doc['missions']['other']['cards_created'] == 0  # Ordered first-match rule.
    assert not doc['sources_missing']
    assert next(a for a in doc['attention'] if a['id'] == 'kpi-inbox-watch-low-yield')['priority'] == 'medium'


def test_kpi_cli(evidence):
    dept, sessions = evidence
    out = dept / 'kpi.json'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/lib/mission_kpis.py'), '--dept-dir', str(dept),
                             '--day', DAY, '--transcripts-dir', str(sessions), '--out', str(out)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())['dept_tokens_today']['total_tokens'] == 100
    assert out.stat().st_uid == os.getuid()


@pytest.mark.parametrize('completed', ['not-a-timestamp', '2026-10-03T08:00:00Z', '2026-10-04T09:00:00Z'])
def test_invalid_or_future_completion_is_not_completion_evidence(evidence, completed):
    dept, sessions = evidence
    ledger(dept, DAY, {'inbox_watch': {'dispatched_at': '2026-10-03T08:08:00Z', 'completed_at': completed}})
    data = kpi.build(dept, DAY, transcripts_dir=sessions)['missions']['inbox_watch']
    assert data['runs_completed'] == 0
    assert data['runs_incomplete'] == data['runs_unattributed'] == 1
    assert data['total_tokens_approx'] == 0


def test_invalid_map_is_missing_and_does_not_fabricate_optional_zeroes(evidence):
    dept, sessions = evidence
    path = dept / 'config/mission_kpi_map.yaml'
    path.parent.mkdir()
    path.write_text('inbox_watch: {titles: ["["], jobs: [watch]}\n')
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert 'mission_map' in doc['sources_missing']
    assert 'cards_created' not in doc['missions']['inbox_watch']


def test_overlapping_windows_count_each_message_once_per_mission(evidence):
    dept, sessions = evidence
    # Both surviving records cover the same call; the summed mission volume is a union.
    ledger(dept, '2026-10-02', {'inbox_watch': {'dispatched_at': '2026-10-02T09:00:00Z',
                                             'completed_at': '2026-10-03T09:00:00Z'}})
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['missions']['inbox_watch']['runs_dispatched'] == 2
    assert doc['missions']['inbox_watch']['total_tokens_approx'] == 100


@pytest.fixture(autouse=True)
def nonroot_child_fixtures(monkeypatch):
    # Offline CI may be uid 0; no real privilege transitions are attempted.
    monkeypatch.setattr(sidecar.os, 'geteuid', lambda: 1000)


def read_sidecar(dept):
    return yaml.safe_load((dept / 'outputs' / DAY / '4/management-kpis.yaml').read_text())


@pytest.mark.parametrize('fixture,shape', [('export-schema.yaml', 'schema'),
                                         ('export-wrapped.yaml', 'wrapped:export'),
                                         ('export-extra.yaml', 'nonconforming')])
def test_real_export_shapes_write_sidecar_and_preserve_bytes(evidence, fixture, shape):
    dept, sessions = evidence
    path = export_file(dept)
    path.write_bytes((FIXTURES / fixture).read_bytes())
    before = path.read_bytes()
    (path.parent / 'summary.md').write_text('Something went wrong today.\n')
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    assert path.read_bytes() == before
    doc = read_sidecar(dept)
    assert doc['dept'] == 'tony' and doc['date'] == DAY
    assert doc['export_shape'] == shape and doc['export_present'] is True
    assert doc['links'] == {'day_report': f'outputs/{DAY}/4/summary.md'}
    assert doc['top_kpis']['model_calls_today'] == 1
    assert all(type(n) in (int, float) for n in doc['top_kpis'].values())
    assert dt.datetime.fromisoformat(doc['generated_at'].replace('Z', '+00:00')).utcoffset() == dt.timedelta(0)
    assert any(a['id'] == 'kpi-export-nonconforming' and a['priority'] == 'low'
               for a in doc['attention']) is (shape != 'schema')
    if fixture == 'export-extra.yaml':
        assert 'extra root keys: schema_version, summary' in doc['export_shape_note']
        assert 'MODEL PRIVATE CONTENT' not in doc['export_shape_note']
    out = path.with_name('management-kpis.yaml')
    assert out.stat().st_mode & 0o777 == 0o644
    assert out.stat().st_uid == os.getuid()
    assert not list(path.parent.glob('.management-kpis.*'))


@pytest.mark.parametrize('contents', [None, '', ' \n'])
def test_day_report_missing(evidence, contents):
    dept, sessions = evidence
    path = export_file(dept)
    if contents is not None:
        path.with_name('summary.md').write_text(contents)
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    doc = read_sidecar(dept)
    assert doc['links'] == {}
    assert 'day_report' in doc['sources_missing']
    assert [a['priority'] for a in doc['attention'] if a['id'] == 'kpi-day-report-missing'] == ['low']


@pytest.mark.parametrize('raw', [b'[broken', b'x' * (1024 * 1024 + 1), b'!!python/object:hostile {}',
                                 b'dept: tony\ndept: ben\n', b'\xff'],
                         ids=['bad-yaml', 'oversized', 'unsafe-tag', 'duplicate-key', 'bad-utf8'])
def test_unparseable_and_oversized_still_write(evidence, raw):
    dept, sessions = evidence
    path = export_file(dept)
    path.write_bytes(raw)
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    assert read_sidecar(dept)['export_shape'] == 'unparseable'
    assert path.read_bytes() == raw


def test_rewritten_sidecar_refreshes_time_and_late_activity(evidence):
    dept, sessions = evidence
    path = export_file(dept)
    before = path.read_bytes()
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    first = read_sidecar(dept)
    transcript(sessions / 'main.jsonl', [message(), message('later')])
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    second = read_sidecar(dept)
    assert second['generated_at'] > first['generated_at']
    assert second['dept_tokens_today']['model_calls'] == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize('component', ['management-export.yaml', 'summary.md', 'day', 'outputs', 'dept'])
def test_child_symlink_refuses_before_any_file_read_or_write(evidence, tmp_path, monkeypatch, component):
    dept, _ = evidence
    path = export_file(dept)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'target'
    target.write_bytes(b'SECRET MODEL CONTENT')
    if component in ('management-export.yaml', 'summary.md'):
        link = path.with_name(component)
        if link.exists():
            link.unlink()
        link.symlink_to(target)
        argument = dept
    else:
        link = {'day': dept / 'outputs' / DAY, 'outputs': dept / 'outputs', 'dept': dept}[component]
        link.rename(link.with_name(link.name + '-original'))
        link.symlink_to(outside, target_is_directory=True)
        argument = dept
    reads = []
    original = sidecar.read_file
    def guarded_read(fd, name):
        reads.append(name)
        return original(fd, name)
    monkeypatch.setattr(sidecar, 'read_file', guarded_read)
    monkeypatch.setattr(sidecar, 'build', lambda *a, **k: pytest.fail('KPI sources must not be read'))
    status = sidecar.child(argument, DAY)
    assert status.startswith('skipped:symlink-')
    assert reads == ([] if component == 'dept' else ['dept.yaml'])
    assert target.read_bytes() == b'SECRET MODEL CONTENT'
    assert sorted(p.name for p in outside.iterdir()) == ['target']
    assert not list(dept.rglob('management-kpis.yaml'))


@pytest.mark.parametrize('status,layers,expected', [('paused', [4], 'skipped:not-live'),
                                                   ('live', [1, 2, 3], 'skipped:no-l4')])
def test_ineligible_skips_without_sidecar_or_alarm(evidence, tmp_path, status, layers, expected):
    dept, _ = evidence
    (dept / 'dept.yaml').write_text(yaml.safe_dump({'status': status, 'layers': {'subscribed': layers}}))
    assert sidecar.child(dept, DAY) == expected
    assert not (dept / 'outputs' / DAY / '4/management-kpis.yaml').exists()
    emitted = []
    result = fleet_check.run_loop(dept.parent, tmp_path / 'state', DAY, True, env={},
        owner_check=lambda p, o: ('agent-tony', None) if p == dept else (None, 'fixture'),
        child_runner=lambda c, **k: sidecar.child(dept, DAY),
        emit_runner=lambda *a, **k: emitted.append(a))
    assert result == 0 and emitted == []


def test_nested_live_case_insensitive(evidence):
    dept, sessions = evidence
    manifest_doc = yaml.safe_load((dept / 'dept.yaml').read_text())
    del manifest_doc['status']
    manifest_doc['department'] = {'status': 'LiVe'}
    (dept / 'dept.yaml').write_text(yaml.safe_dump(manifest_doc))
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'export_missing'
    assert read_sidecar(dept)['export_shape'] == 'absent'


@pytest.mark.parametrize('mode,uid,user,reason', [(0o120777, 1000, 'agent-tony', 'symlink-dept'),
    (0o40755, 0, 'root', 'uid-below-1000'), (0o40755, 999, 'agent-tony', 'uid-below-1000'),
    (0o40755, 1000, 'wrong-owner', 'owner-name-mismatch')])
def test_root_owner_refusals_injected(mode, uid, user, reason):
    from types import SimpleNamespace
    def lookup(n):
        assert n >= 1000
        return SimpleNamespace(pw_name=user, pw_uid=n)
    assert fleet_check.owner_for(Path('/agents/tony'), {},
        lstat=lambda p: SimpleNamespace(st_mode=mode, st_uid=uid), lookup=lookup) == (None, reason)


def test_owner_override_never_allows_uid_zero():
    from types import SimpleNamespace
    for uid in (0, 1000):
        result = fleet_check.owner_for(Path('/agents/tony'), {'tony': 'legacy'},
            lstat=lambda p: SimpleNamespace(st_mode=0o40755, st_uid=uid),
            lookup=lambda n: SimpleNamespace(pw_name='legacy', pw_uid=n))
        assert result == (('legacy', None) if uid == 1000 else (None, 'uid-below-1000'))


@pytest.fixture
def fleet(tmp_path):
    agents = tmp_path / 'agents'
    for slug in ('ben', 'tony'):
        manifest(agents / slug)
    path = export_file(agents / 'tony')
    sessions = tmp_path / 'sessions'
    transcript(sessions / 'tony/main.jsonl', [message()])
    outside = tmp_path / 'outside'
    manifest(outside)
    (agents / 'maya').symlink_to(outside, target_is_directory=True)
    state = tmp_path / 'state'
    env = {'PYTHON_BIN': sys.executable, 'TRANSCRIPTS_ROOT': str(sessions)}
    return agents, state, env, path


def run_fleet(fleet, *, runner=None, emitter=None, dry_run=False, due=True):
    from types import SimpleNamespace
    agents, state, env, _ = fleet
    def owner_check(dept, overrides):
        # Real lstat; inject only uid/pwd because this workstation has uid 501.
        def fixture_stat(path):
            value = os.lstat(path)
            return SimpleNamespace(st_mode=value.st_mode, st_uid=1000)
        return fleet_check.owner_for(dept, overrides, lstat=fixture_stat,
            lookup=lambda n: SimpleNamespace(pw_name='agent-' + dept.name, pw_uid=n))
    def child_runner(command, **kwargs):
        dept = Path(command[command.index('--dept-dir') + 1])
        return sidecar.child(dept, DAY, dry_run='--dry-run' in command,
            transcripts_dir=Path(env['TRANSCRIPTS_ROOT']) / dept.name)
    return fleet_check.run_loop(agents, state, DAY, due, env=env,
        dry_run=dry_run, owner_check=owner_check, child_runner=runner or child_runner,
        emit_runner=emitter or (lambda *a, **k: None))


def test_root_does_not_read_or_follow_dept_files(fleet, monkeypatch):
    agents, _, _, path = fleet
    original_stat = os.stat
    original_open = Path.open
    def no_dept_stat(p, *a, **k):
        if str(p).startswith(str(agents) + '/'):
            pytest.fail('root followed department path')
        return original_stat(p, *a, **k)
    def no_dept_read(p, *a, **k):
        if str(p).startswith(str(agents) + '/'):
            pytest.fail('root opened department file')
        return original_open(p, *a, **k)
    monkeypatch.setattr(os, 'stat', no_dept_stat)
    monkeypatch.setattr(Path, 'open', no_dept_read)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 120
        return 'written'
    assert run_fleet(fleet, runner=runner) == 0
    assert len(commands) == 2
    for command in commands:
        slug = Path(command[command.index('--dept-dir') + 1]).name
        assert command[:4] == ['runuser', '-u', 'agent-' + slug, '--']
        assert command[4:7] == [sys.executable, '-I', str(ROOT / 'scripts/lib/management_kpis.py')]


def test_missing_export_alarm_once_retry_after_failed_emit(fleet):
    agents, state, _, _ = fleet
    calls = []
    def emit(command, **kwargs):
        calls.append(command)
        assert kwargs['timeout'] == 60 and kwargs['check'] is True
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command)
    assert run_fleet(fleet, emitter=emit) == 1
    assert not list(state.glob('*.sent'))
    assert read_sidecar(agents / 'ben')['export_present'] is False
    assert run_fleet(fleet, emitter=emit) == 0
    assert run_fleet(fleet, emitter=emit) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert f'task=fleet-export-missing-ben-{DAY}' in calls[0]
    assert f'title=Missing daily management export: ben {DAY}' in calls[0]
    assert len(list(state.glob('*.sent'))) == 1


def test_alarm_contains_only_root_slug_and_day(fleet):
    agents, _, _, _ = fleet
    (agents / 'ben/dept.yaml').write_text('status: live\nlayers: {subscribed: [4]}\ndepartment: {name: EVIL-CONTENT}\n')
    calls = []
    assert run_fleet(fleet, emitter=lambda cmd, **k: calls.append(cmd)) == 0
    assert 'EVIL-CONTENT' not in ' '.join(calls[0])


def test_dry_run_no_write_and_does_not_emit(fleet):
    agents, state, _, _ = fleet
    before = {p: p.read_bytes() for p in agents.rglob('*') if p.is_file()}
    assert run_fleet(fleet, dry_run=True, emitter=lambda *a, **k: pytest.fail('unexpected emit')) == 0
    assert not state.exists()
    assert before == {p: p.read_bytes() for p in agents.rglob('*') if p.is_file()}


@pytest.mark.parametrize('now,date,alarm', [('2026-10-03T21:50:00+02:00', DAY, False),
    ('2026-10-03T23:29:59+02:00', DAY, False), ('2026-10-03T23:30:00+02:00', DAY, True),
    ('2026-10-04T08:10:00+02:00', DAY, True)])
def test_paris_day_deadline(now, date, alarm):
    assert fleet_check.select_day(dt.datetime.fromisoformat(now)) == (date, alarm)


def test_pending_export_no_alarm(fleet):
    assert run_fleet(fleet, due=False, emitter=lambda *a, **k: pytest.fail('early alarm')) == 0


def test_real_child_timeout_loop_continues_next_department(fleet, capsys):
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return fleet_check.run_child([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.1)
        return 'written'
    assert run_fleet(fleet, runner=runner) == 1
    output = capsys.readouterr().out
    assert 'ben ' + DAY + ': error:child-timeout' in output
    assert 'tony ' + DAY + ': written' in output
    assert len(commands) == 2


@pytest.mark.parametrize('code,raw', [(0, b'{"status":"written","evil":1}\n'),
    (0, b'{"status":"written","status":"export_missing"}\n'),
    (1, b'{"status":"export_missing"}\n'), (0, b'[]\n'), (0, b'x' * 4097),
    (0, b'{"status":"written"}\nmore\n'), (0, b'{"status":"error:evil content"}\n')])
def test_untrusted_reply_rejected(code, raw):
    assert fleet_check.parse_reply(code, raw).startswith('error:')


def test_bounded_real_child_output_and_framework_cli(evidence):
    dept, _ = evidence
    result = fleet_check.run_child([sys.executable, '-c', 'print("x" * 100000)'], timeout=5)
    assert result == 'error:oversized-reply'
    # This workstation's PyYAML is in user-site, disabled by -I. Add only its
    # installed dependency directory in this offline bootstrap (production uses system PyYAML).
    bootstrap = ('import sys,runpy; sys.path.append(' + repr(str(Path(yaml.__file__).parent.parent)) + '); '
                 'script=sys.argv.pop(1); runpy.run_path(script,run_name="__main__")')
    result = fleet_check.run_child([sys.executable, '-I', '-c', bootstrap,
                                   str(ROOT / 'scripts/lib/management_kpis.py'),
                                   '--dept-dir', str(dept), '--day', DAY, '--dry-run'], timeout=10)
    assert result in ('export_missing', 'skipped:uid-zero')  # Actual host UID, no shim.


def test_timer_service_contract():
    timer = (ROOT / 'deploy/templates/fleet-export-check.timer').read_text()
    for hour in ('21:50', '23:30', '08:10'):
        assert f'OnCalendar=*-*-* {hour}:00 Europe/Paris' in timer
    assert 'Persistent=true' in timer
    service = (ROOT / 'deploy/templates/fleet-export-check.service').read_text()
    assert 'User=root' in service and 'TimeoutStartSec=20min' in service
    assert 'ExecStart=/opt/bubble-ops-loop/scripts/fleet-export-check.sh' in service
    assert 'ConditionPathExists' not in service and 'EnvironmentFile' not in service


@pytest.mark.parametrize('uid,user,reason', [(0, 'root', 'uid-below-1000'),
    (999, 'agent-ben', 'uid-below-1000'), (1000, 'wrong-user', 'owner-name-mismatch')])
def test_root_refusals_log_and_never_dispatch_or_alarm(fleet, uid, user, reason, capsys):
    from types import SimpleNamespace
    agents, state, env, _ = fleet
    def owner_check(dept, overrides):
        return fleet_check.owner_for(dept, overrides,
            lstat=lambda p: SimpleNamespace(st_mode=0o40755, st_uid=uid),
            lookup=lambda n: SimpleNamespace(pw_name=user, pw_uid=n))
    result = fleet_check.run_loop(agents, state, DAY, True, env=env,
        owner_check=owner_check, child_runner=lambda *a, **k: pytest.fail('refused owner dispatched'),
        emit_runner=lambda *a, **k: pytest.fail('refused owner alarmed'))
    assert result == 0
    assert reason in capsys.readouterr().out
    assert not list(state.glob('*.sent'))


def test_long_whitespace_prefix_day_report_is_nonempty(evidence):
    dept, sessions = evidence
    path = export_file(dept)
    path.with_name('summary.md').write_bytes(b' ' * (1024 * 1024 + 10) + b'A late paragraph.')
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    assert 'day_report' in read_sidecar(dept)['links']


def test_missing_required_root_note_and_attention_item_schema(evidence):
    from scripts.lib.management_export_shape import validate
    dept, sessions = evidence
    doc = valid_export()
    del doc['open_gates']
    export_file(dept, doc)
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    out = read_sidecar(dept)
    assert out['export_shape'] == 'nonconforming'
    assert out['export_shape_note'] == 'missing required root keys: open_gates'
    schema = yaml.safe_load((ROOT / 'schemas-draft/management-export.schema.yaml').read_text())
    for item in out['attention']:
        validate(item, schema['properties']['needs_management_attention']['items'])


def test_symlink_kpi_source_not_followed(evidence, tmp_path):
    dept, sessions = evidence
    target = tmp_path / 'target-ledger'
    target.write_text(json.dumps({'secret': {'dispatched_at': DAY + 'T08:00:00Z'}}))
    source = dept / 'outputs' / DAY / 'dispatch.json'
    source.unlink()
    source.symlink_to(target)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert f'dispatch:{DAY}' in doc['sources_missing']
    assert 'secret' not in doc['missions']
