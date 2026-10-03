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
    assert total['cost_usd_estimate'] == 0.0
    assert data['cost_usd_estimate_approx'] == 0.0
    assert doc['top_kpis_flat']['tokens_today_total_mtok'] == 0.0
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
    assert doc['sources_missing'] == ['dispatch', 'transcripts']
    assert doc['sources_not_configured'] == ['board', 'mission_map', 'runs']
    assert doc['top_kpis_flat']['dispatch_days_present'] == 0
    assert doc['top_kpis_flat']['dispatch_days_absent'] == 28
    assert len(doc['dispatch_days_absent_list']) == 28
    assert doc['top_kpis_flat']['mission_kpi_sources_missing'] == len(doc['sources_missing'])
    assert next(a for a in doc['attention'] if a['id'] == 'kpi-sources-missing')['priority'] == 'low'


@pytest.mark.parametrize('kind', ['bad-transcript', 'bad-board', 'bad-runs', 'bad-map', 'bad-manifest', 'bad-ledger'])
def test_malformed_sources_are_missing(evidence, kind):
    dept, sessions = evidence
    paths = {'bad-transcript': sessions / 'main.jsonl', 'bad-board': dept / 'monitoring/board-issues.json',
             'bad-runs': dept / 'monitoring/runs.json', 'bad-manifest': dept / 'dept.yaml',
             'bad-map': dept / 'config/mission_kpi_map.yaml',
             'bad-ledger': dept / 'outputs' / DAY / 'dispatch.json'}
    path = paths[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[broken')
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    expected = {'bad-transcript': 'transcripts', 'bad-board': 'board', 'bad-runs': 'runs',
                'bad-map': 'mission_map',
                'bad-manifest': 'dept.yaml', 'bad-ledger': 'dispatch'}[kind]
    assert expected in doc['sources_missing']
    assert expected not in doc['sources_not_configured']
    assert any(a['id'] == 'kpi-sources-missing' for a in doc['attention'])
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


def test_sparse_dispatch_days_and_unconfigured_sources_do_not_raise_attention(evidence):
    dept, sessions = evidence
    absent = sorted(path.parent.name for path in (dept / 'outputs').glob('*/dispatch.json')
                    if path.parent.name != DAY)
    for date in absent:
        (dept / 'outputs' / date / 'dispatch.json').unlink()
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['top_kpis_flat']['dispatch_days_present'] == 1
    assert doc['top_kpis_flat']['dispatch_days_absent'] == 27
    assert doc['dispatch_days_absent_list'] == absent
    assert doc['sources_missing'] == []
    assert doc['top_kpis_flat']['mission_kpi_sources_missing'] == 0
    assert doc['sources_not_configured'] == ['board', 'mission_map', 'runs']
    assert not any(a['id'] == 'kpi-sources-missing' for a in doc['attention'])
    # The on-disk document exposes both new body keys and numeric counts.
    export = export_file(dept)
    export.with_name('summary.md').write_text('Daily report.\n')
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    out = read_sidecar(dept)
    assert out['dispatch_days_absent_list'] == absent
    assert out['sources_not_configured'] == doc['sources_not_configured']
    assert out['sources_missing'] == []
    assert out['top_kpis']['dispatch_days_present'] == 1
    assert out['top_kpis']['dispatch_days_absent'] == 27
    assert out['attention'] == []


def test_empty_readable_dispatch_is_present(evidence):
    dept, sessions = evidence
    for path in (dept / 'outputs').glob('*/dispatch.json'):
        path.unlink()
    ledger(dept, DAY, {})
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['top_kpis_flat']['dispatch_days_present'] == 1
    assert doc['top_kpis_flat']['dispatch_days_absent'] == 27
    assert 'dispatch' not in doc['sources_missing']


@pytest.mark.parametrize('source', ['board', 'runs', 'mission_map', 'dispatch'])
def test_existing_unreadable_sources_still_raise_attention(evidence, monkeypatch, source):
    dept, sessions = evidence
    paths = {'board': dept / 'monitoring/board-issues.json',
             'runs': dept / 'monitoring/runs.json',
             'mission_map': dept / 'config/mission_kpi_map.yaml',
             'dispatch': dept / 'outputs' / DAY / 'dispatch.json'}
    target = paths[source]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{}' if source in ('mission_map', 'dispatch') else '[]')
    original = kpi.open_text

    def open_file(path):
        if path == target:
            raise PermissionError('fixture permission denied')
        return original(path)

    monkeypatch.setattr(kpi, 'open_text', open_file)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['sources_missing'] == [source]
    assert source not in doc['sources_not_configured']
    assert next(a for a in doc['attention'] if a['id'] == 'kpi-sources-missing')['priority'] == 'low'
    if source == 'dispatch':
        assert doc['top_kpis_flat']['dispatch_days_present'] == 27
        assert doc['dispatch_days_absent_list'] == [DAY]


def test_token_and_dollar_rounding_in_written_sidecar(evidence):
    dept, sessions = evidence
    transcript(sessions / 'main.jsonl', [message(input_tokens=16_465_677, output_tokens=1_234_567)])
    export = export_file(dept)
    export.with_name('summary.md').write_text('Daily report.\n')
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    doc = read_sidecar(dept)
    assert doc['top_kpis']['tokens_today_total_mtok'] == 17.7
    assert doc['top_kpis']['tokens_today_output_mtok'] == 1.2
    assert doc['dept_tokens_today']['total_tokens'] == 17_700_244
    assert doc['dept_tokens_today']['cost_usd_estimate'] == 67.92
    assert doc['missions']['inbox_watch']['cost_usd_estimate_approx'] == 67.92
    for values in (doc['top_kpis'], doc['dept_tokens_today'], *doc['missions'].values()):
        for key, value in values.items():
            if key.endswith('_mtok'):
                assert value == round(value, 1)
            elif 'cost_usd' in key:
                assert value == round(value, 2)
            elif key.endswith(('tokens', 'tokens_approx')):
                assert type(value) is int


def test_heavy_token_summary_uses_millions_and_model_calls(evidence):
    dept, sessions = evidence
    calls = [message('large', input_tokens=34_750_337)]
    calls.extend(message(f'call-{i}', input_tokens=1) for i in range(317))
    transcript(sessions / 'main.jsonl', calls)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['dept_tokens_today']['total_tokens'] == 34_750_654
    flag = next(a for a in doc['attention'] if a['id'] == 'kpi-dept-token-use')
    assert flag['priority'] == 'medium'
    assert flag['summary'] == f'Department used 34.8 million tokens across 318 model calls on {DAY}'
    assert doc['top_kpis_flat']['tokens_today_total_mtok'] == 34.8


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
    monkeypatch.setattr(sidecar.os, 'geteuid', lambda: 996)


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


@pytest.mark.parametrize('component', ['management-export.yaml', 'summary.md', 'management-kpis.yaml', 'day', 'outputs', '4', 'dept'])
def test_child_symlink_refuses_before_any_file_read_or_write(evidence, tmp_path, monkeypatch, component):
    dept, _ = evidence
    path = export_file(dept)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'target'
    target.write_bytes(b'SECRET MODEL CONTENT')
    if component in ('management-export.yaml', 'summary.md', 'management-kpis.yaml'):
        link = path.with_name(component)
        if link.exists():
            link.unlink()
        link.symlink_to(target)
        argument = dept
    else:
        link = {'day': dept / 'outputs' / DAY, 'outputs': dept / 'outputs', '4': path.parent, 'dept': dept}[component]
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
    assert status.startswith('error:symlink-')
    assert reads == ([] if component == 'dept' else ['dept.yaml'])
    assert target.read_bytes() == b'SECRET MODEL CONTENT'
    assert sorted(p.name for p in outside.iterdir()) == ['target']
    assert list(dept.rglob('management-kpis.yaml')) == ([link] if component == 'management-kpis.yaml' else [])


@pytest.mark.parametrize('component', ['outputs', 'day', '4', 'management-export.yaml',
                                     'summary.md', 'management-kpis.yaml'])
@pytest.mark.parametrize('kind', ['symlink', 'nonregular'])
def test_output_refusal_cli_exits_nonzero(evidence, tmp_path, monkeypatch, capsys, component, kind):
    dept, _ = evidence
    export = export_file(dept)
    path = {'outputs': dept / 'outputs', 'day': dept / 'outputs' / DAY,
            '4': export.parent}.get(component, export.with_name(component))
    if path.exists():
        path.rename(path.with_name(path.name + '-original'))
    if kind == 'symlink':
        path.symlink_to(tmp_path / 'absent-target')
    else:
        os.mkfifo(path)
    monkeypatch.setattr(sidecar, 'build', lambda *a, **k: pytest.fail('unexpected KPI read'))
    monkeypatch.setattr(sys, 'argv', ['management_kpis.py', '--dept-dir', str(dept), '--day', DAY])
    assert sidecar.main() == 1
    raw = capsys.readouterr().out.encode()
    status = 'error:' + kind + '-' + path.name
    assert fleet_check.parse_reply(1, raw) == status
    assert fleet_check.parse_reply(0, raw) == 'error:child-exit'
    assert not list(dept.rglob('.management-kpis.*'))


def test_child_refuses_uid_zero(evidence, monkeypatch):
    dept, _ = evidence
    monkeypatch.setattr(sidecar.os, 'geteuid', lambda: 0)
    assert sidecar.child(dept, DAY) == 'error:uid-zero'


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
    assert result == fleet_check.NOTHING_PROCESSED and emitted == []


def test_nested_live_case_insensitive(evidence):
    dept, sessions = evidence
    manifest_doc = yaml.safe_load((dept / 'dept.yaml').read_text())
    del manifest_doc['status']
    manifest_doc['department'] = {'status': 'LiVe'}
    (dept / 'dept.yaml').write_text(yaml.safe_dump(manifest_doc))
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'export_missing'
    assert read_sidecar(dept)['export_shape'] == 'absent'


@pytest.mark.parametrize('slug,uid', [('ben', 994), ('tony', 996), ('maya', 999),
                                     ('claudette', 995), ('morty', 993)])
def test_system_department_owner_accepted(slug, uid):
    from types import SimpleNamespace
    assert fleet_check.owner_for(Path('/agents') / slug, {},
        lstat=lambda p: SimpleNamespace(st_mode=0o40755, st_uid=uid),
        lookup=lambda n: SimpleNamespace(pw_name='agent-' + slug, pw_uid=n)) == ('agent-' + slug, None)


@pytest.mark.parametrize('mode,uid,user,pw_uid,reason', [
    (0o120777, 996, 'agent-tony', 996, 'symlink-dept'),
    (0o40755, 0, 'agent-tony', 0, 'uid-zero'),
    (0o40755, 996, 'wrong-owner', 996, 'owner-name-mismatch'),
    (0o40755, 996, 'agent-tony', 994, 'owner-uid-mismatch')])
def test_root_owner_refusals_injected(mode, uid, user, pw_uid, reason):
    from types import SimpleNamespace
    def lookup(n):
        assert n != 0
        return SimpleNamespace(pw_name=user, pw_uid=pw_uid)
    assert fleet_check.owner_for(Path('/agents/tony'), {},
        lstat=lambda p: SimpleNamespace(st_mode=mode, st_uid=uid), lookup=lookup) == (None, reason)


@pytest.mark.parametrize('uid,expected', [(0, (None, 'uid-zero')), (996, ('legacy', None))])
def test_owner_override_never_allows_uid_zero(uid, expected):
    from types import SimpleNamespace
    assert fleet_check.owner_for(Path('/agents/tony'), {'tony': 'legacy'},
        lstat=lambda p: SimpleNamespace(st_mode=0o40755, st_uid=uid),
        lookup=lambda n: SimpleNamespace(pw_name='legacy', pw_uid=n)) == expected


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
            return SimpleNamespace(st_mode=value.st_mode, st_uid=996)
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
        assert command[4:8] == [sys.executable, '-I', '-B', str(ROOT / 'scripts/lib/management_kpis.py')]


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


@pytest.mark.parametrize('status', ['skipped:not-live', 'skipped:no-l4', 'skipped:invalid-manifest',
                                  'error:child-failed'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_nothing_processed_is_visible(fleet, capsys, status, dry_run):
    assert run_fleet(fleet, runner=lambda *a, **k: status, dry_run=dry_run,
                     emitter=lambda *a, **k: pytest.fail('unexpected alarm')) == fleet_check.NOTHING_PROCESSED
    assert capsys.readouterr().err.count('nothing processed') == 1


def test_empty_fleet_is_visible(tmp_path, capsys):
    agents = tmp_path / 'agents'
    agents.mkdir()
    assert fleet_check.run_loop(agents, tmp_path / 'state', DAY, False, env={}) == fleet_check.NOTHING_PROCESSED
    assert capsys.readouterr().err.count('nothing processed') == 1


@pytest.mark.parametrize('processed', ['written', 'export_missing'])
@pytest.mark.parametrize('other,expected', [('skipped:no-l4', 0), ('error:child-failed', 1)])
def test_one_processed_department_keeps_exit_semantics(fleet, capsys, processed, other, expected):
    statuses = iter([other, processed])
    assert run_fleet(fleet, runner=lambda *a, **k: next(statuses), due=False) == expected
    assert 'nothing processed' not in capsys.readouterr().err


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
    (0, b'{"status":"written"}\nmore\n'), (0, b'{"status":"error:evil content"}\n'),
    (0, b'{"status":"skipped:symlink-outputs"}\n'), (0, b'{"status":"skipped:nonregular-summary.md"}\n'),
    (0, b'{"status":"error:symlink-outputs"}\n'), (1, b'{"status":"skipped:no-l4"}\n')])
def test_untrusted_reply_rejected(code, raw):
    assert fleet_check.parse_reply(code, raw).startswith('error:')


@pytest.mark.parametrize('status,code', [('written', 0), ('error:symlink-outputs', 1)])
def test_reaped_child_group_is_not_killed(monkeypatch, status, code):
    monkeypatch.setattr(fleet_check.os, 'killpg', lambda *a: pytest.fail('reaped process group killed'))
    command = [sys.executable, '-c', f'print({json.dumps({"status": status})!r}); raise SystemExit({code})']
    assert fleet_check.run_child(command, timeout=5) == status


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
    assert result in ('export_missing', 'error:uid-zero')  # Actual host UID, no shim.


def test_timer_service_contract():
    timer = (ROOT / 'deploy/templates/fleet-export-check.timer').read_text()
    for hour in ('21:50', '23:30', '08:10'):
        assert f'OnCalendar=*-*-* {hour}:00 Europe/Paris' in timer
    assert 'Persistent=true' in timer
    service = (ROOT / 'deploy/templates/fleet-export-check.service').read_text()
    assert 'User=root' in service and 'TimeoutStartSec=20min' in service
    assert 'ExecStart=/opt/bubble-ops-loop/scripts/fleet-export-check.sh' in service
    assert 'ConditionPathExists' not in service and 'EnvironmentFile' not in service


@pytest.mark.parametrize('uid,user,reason', [(0, 'root', 'uid-zero'),
    (994, 'wrong-user', 'owner-name-mismatch')])
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
    assert result == fleet_check.NOTHING_PROCESSED
    output = capsys.readouterr()
    assert reason in output.out
    assert output.err.count('nothing processed') == 1
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
    assert doc['sources_missing'] == ['dispatch']
    assert 'secret' not in doc['missions']


def test_symlinked_transcript_entry_is_skipped_not_fatal(tmp_path):
    """Real case (accountant, 2026-10-03): one symlinked subagent .jsonl made
    the whole transcript scan report "missing". It must be skipped: totals
    come from the regular files, and the link is never followed."""
    import json as _json
    root = tmp_path / "transcripts"
    (root / "s" / "subagents").mkdir(parents=True)
    rec = {"type": "assistant", "timestamp": "2026-10-03T10:00:00Z",
           "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 1, "output_tokens": 2,
                       "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}}
    (root / "main.jsonl").write_text(_json.dumps(rec) + "\n")
    outside = tmp_path / "outside.jsonl"
    other = dict(rec, message=dict(rec["message"], id="m2"))
    outside.write_text(_json.dumps(other) + "\n")
    (root / "s" / "subagents" / "agent-x.jsonl").symlink_to(outside)
    missing = set()
    calls = kpi.read_calls(root, missing)
    assert missing == set()
    assert [c["total_tokens"] for c in calls] == [10]


@pytest.fixture
def mac_evidence(tmp_path, monkeypatch):
    """Mac schema with one leased mission, one start-less success, one unseen mission."""
    from scripts.due_missions import command_complete, parser
    from scripts.lib.loop_backup import claim_due_missions

    dept = tmp_path / 'rick'
    dept.mkdir()
    entries = [dict(id='daily_scan', cadence='daily', status='live', layer=1,
                    due={'policy': 'calendar_period', 'timezone': 'Europe/Paris'},
                    mission_file='missions/daily_scan.md'),
               dict(id='board', cadence='continuous', status='live', layer=1,
                    due={'policy': 'every_tick'}, mission_file='missions/board.md'),
               dict(id='unseen', cadence='daily', status='live', layer=1,
                    due={'policy': 'calendar_period', 'timezone': 'Europe/Paris'},
                    mission_file='missions/unseen.md')]
    data = dict(status='live', layers={'subscribed': [1, 4]}, recurring_missions=entries,
                loop={'due_dispatch': {'mission_ids': [m['id'] for m in entries],
                                      'watermark': 'monitoring/due.json', 'pending_lease_seconds': 21600}})
    (dept / 'dept.yaml').write_text(yaml.safe_dump(data))
    for entry in entries:
        path = dept / entry['mission_file']
        path.parent.mkdir(exist_ok=True)
        path.write_text('# synthetic mission\n')
    for layer in (1, 4):
        path = dept / f'layers/{layer}/PROMPT.md'
        path.parent.mkdir(parents=True)
        path.write_text('# synthetic layer\n')
    started = dt.datetime(2026, 10, 3, 8, 0, tzinfo=dt.timezone.utc)
    # The sidecar's autouse fixture simulates a nonroot UID; writes must use
    # the real fixture owner, then restore the sidecar's isolated-child UID.
    with monkeypatch.context() as owner:
        owner.setattr(os, 'geteuid', os.getuid)
        claim_due_missions(dept / 'monitoring/due.json', data, started, 21600)
        for mission_id, period in (('daily_scan', DAY), ('board', 'continuous')):
            args = parser().parse_args(['complete', '--dept-dir', str(dept), '--mission', mission_id,
                                        '--period', period, '--now-epoch', str(int(started.timestamp()) + 600)])
            assert command_complete(args) == 0
    sessions = tmp_path / 'sessions'
    transcript(sessions / 'main.jsonl', [message(stamp=DAY + 'T08:05:00Z')])
    return dept, sessions


def test_1711_mac_reader_successes_and_short_history(mac_evidence):
    dept, sessions = mac_evidence
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['top_kpis_flat']['dispatch_days_present'] == 1
    assert doc['top_kpis_flat']['dispatch_days_absent'] == len(doc['dispatch_days_absent_list']) == 27
    assert 'dispatch' not in doc['sources_missing']
    scan = doc['missions']['daily_scan']
    assert scan['runs_recorded'] == scan['runs_dispatched'] == scan['runs_completed'] == 1
    assert scan['runs_unattributed'] == 0
    assert scan['total_tokens_approx'] == 100
    board = doc['missions']['board']
    assert board['runs_completed'] == board['runs_recorded'] == board['runs_unattributed'] == 1
    assert board['runs_dispatched'] == board['days_dispatched'] == 0
    assert board['total_tokens_approx'] == 0
    for data in doc['missions'].values():
        assert data['history_days_present'] == 1
        assert data['history_days_absent'] == 27
    assert doc['missions']['unseen'] == {'history_days_present': 1, 'history_days_absent': 27}


def test_1711_mac_sidecar_contains_real_mission_evidence(mac_evidence, nonroot_child_fixtures):
    dept, sessions = mac_evidence
    export_file(dept)
    assert sidecar.child(dept, DAY, transcripts_dir=sessions) == 'written'
    doc = read_sidecar(dept)
    assert doc['missions']['daily_scan']['runs_completed'] == 1
    assert doc['missions']['daily_scan']['tokens_date'] == DAY
    assert doc['top_kpis']['dispatch_days_present'] == 1
    assert all(isinstance(value, (int, float)) for value in doc['top_kpis'].values())


@pytest.mark.parametrize('mac', [False, True])
def test_1711_shared_windows_split_once_and_mark_estimates(evidence, mac):
    dept, sessions = evidence
    manifest(dept, ('morning_sync', 'news_relay', 'solo'))
    records = {mission: dict(dispatched_at=DAY + 'T07:00:00Z', completed_at=DAY + 'T07:30:00Z')
               for mission in ('morning_sync', 'news_relay')}
    records['solo'] = dict(dispatched_at=DAY + 'T09:00:00Z', completed_at=DAY + 'T09:30:00Z')
    if mac:
        ledger(dept, DAY, {})
        write_json(dept / 'outputs' / DAY / 'due-dispatch.json',
                   [dict(mission_id=mission, period=DAY, **record) for mission, record in records.items()])
    else:
        ledger(dept, DAY, records)
    transcript(sessions / 'main.jsonl', [
        message('shared', DAY + 'T07:10:00Z', input_tokens=11, output_tokens=5,
                cache_read_input_tokens=21, cache_creation_input_tokens=31),
        message('solo', DAY + 'T09:10:00Z'), message('outside', DAY + 'T10:00:00Z')])
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    missions = doc['missions']
    assert missions['morning_sync']['total_tokens_approx'] == 34
    assert missions['news_relay']['total_tokens_approx'] == 34
    for mission, peer in (('morning_sync', 'news_relay'), ('news_relay', 'morning_sync')):
        assert missions[mission]['attribution'] == 'shared_window'
        assert missions[mission]['shared_with'] == [peer]
        assert missions[mission]['tokens_date'] == DAY
    assert missions['solo']['total_tokens_approx'] == 100
    assert 'attribution' not in missions['solo'] and 'shared_with' not in missions['solo']
    for key in (*kpi.TOKEN_FIELDS, 'total_tokens'):
        assert sum(data[key + '_approx'] for data in missions.values()) <= doc['dept_tokens_today'][key]
    assert doc['dept_tokens_today']['total_tokens'] == 268


def test_1711_partial_three_way_overlap_and_prior_day_calls(evidence):
    dept, sessions = evidence
    manifest(dept, ('first', 'second', 'third'))
    ledger(dept, DAY, {
        'first': dict(dispatched_at=DAY + 'T07:00:00Z', completed_at=DAY + 'T08:00:00Z'),
        'second': dict(dispatched_at=DAY + 'T07:30:00Z', completed_at=DAY + 'T08:30:00Z'),
        'third': dict(dispatched_at=DAY + 'T07:45:00Z', completed_at=DAY + 'T08:00:00Z')})
    ledger(dept, '2026-10-02', {'first': dict(dispatched_at='2026-10-02T07:00:00Z',
                                             completed_at='2026-10-02T08:00:00Z')})
    transcript(sessions / 'main.jsonl', [
        message('prior', '2026-10-02T07:15:00Z', input_tokens=999),
        message('first', DAY + 'T07:15:00Z', input_tokens=30),
        message('two', DAY + 'T07:40:00Z', input_tokens=20),
        message('three', DAY + 'T07:50:00Z', input_tokens=9),
        message('last', DAY + 'T08:15:00Z', input_tokens=40)])
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert [doc['missions'][m]['total_tokens_approx'] for m in ('first', 'second', 'third')] == [43, 53, 3]
    assert doc['missions']['first']['runs_dispatched'] == 2
    assert sum(m['total_tokens_approx'] for m in doc['missions'].values()) == doc['dept_tokens_today']['total_tokens'] == 99
    assert doc['missions']['first']['shared_with'] == ['second', 'third']
    assert all(m['attribution'] == 'shared_window' for m in doc['missions'].values())


def test_1711_shared_marker_without_transcript_calls_in_overlap(evidence):
    dept, sessions = evidence
    manifest(dept, ('first', 'second'))
    ledger(dept, DAY, {mission: dict(dispatched_at=DAY + 'T07:00:00Z', completed_at=DAY + 'T07:30:00Z')
                       for mission in ('first', 'second')})
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert all(m['attribution'] == 'shared_window' for m in doc['missions'].values())
    assert all(m['total_tokens_approx'] == 0 for m in doc['missions'].values())


@pytest.mark.parametrize('value', ['broken json', '{}', '[null]', '[{"mission_id": "board"}]'])
def test_1711_malformed_mac_ledger_is_visible(evidence, value):
    dept, sessions = evidence
    path = dept / 'outputs' / DAY / 'due-dispatch.json'
    path.write_text(value)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert 'dispatch' in doc['sources_missing']
    assert 'kpi-sources-missing' in {a['id'] for a in doc['attention']}


def test_1711_mac_without_ledger_reports_unknown_history(mac_evidence):
    dept, sessions = mac_evidence
    (dept / 'outputs' / DAY / 'due-dispatch.json').unlink()
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert 'dispatch' in doc['sources_missing']
    assert all(data == {'history_days_present': 0, 'history_days_absent': 28}
               for data in doc['missions'].values())


def test_1711_mac_ledger_symlink_is_not_followed(evidence, tmp_path):
    dept, sessions = evidence
    target = tmp_path / 'external.json'
    write_json(target, [dict(mission_id='external', period=DAY, completed_at=DAY + 'T08:10:00Z')])
    (dept / 'outputs' / DAY / 'due-dispatch.json').symlink_to(target)
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert 'dispatch' in doc['sources_missing']
    assert 'external' not in doc['missions']
def test_root_wrapper_prevents_bytecode_even_in_isolated_python(tmp_path):
    """Exercise -I (which ignores PYTHON* env) with a real repo-local import."""
    framework = tmp_path / "framework"
    scripts = framework / "scripts"
    lib = scripts / "lib"
    lib.mkdir(parents=True)
    wrapper = scripts / "fleet-export-check.sh"
    wrapper.write_text((ROOT / "scripts/fleet-export-check.sh").read_text())
    (lib / "dispatch_helpers.py").write_text("value = 1709\n")
    (lib / "fleet_export_check.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "import dispatch_helpers\n"
        "assert sys.dont_write_bytecode\n"
        "print(dispatch_helpers.value)\n"
    )
    env = dict(os.environ, PYTHON_BIN=sys.executable, PYTHONDONTWRITEBYTECODE="0")
    result = subprocess.run(["bash", str(wrapper)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1709"
    assert not list(framework.rglob("__pycache__"))
