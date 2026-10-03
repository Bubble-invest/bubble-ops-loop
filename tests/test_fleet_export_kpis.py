"""Offline fixtures for #1708: real ledger shapes, owner writes, independent alarms."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.lib import mission_kpis as kpi
from scripts.lib.enrich_management_export import enrich, load_export

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
    (dept / 'dept.yaml').write_text(yaml.safe_dump({'recurring_missions': [{'id': m} for m in ids]}))


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
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path.name == 'main.jsonl':
            raise PermissionError('fixture permission denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', open_file)
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
                             '--day', DAY, '--transcripts-dir', str(sessions), '--out', str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())['dept_tokens_today']['total_tokens'] == 100
    assert out.stat().st_uid == os.getuid()


def test_enrichment_preserves_model_content_is_atomic_and_idempotent(evidence):
    dept, sessions = evidence
    doc = valid_export()
    doc['top_kpis']['model_calls_today'] = 99
    doc['needs_management_attention'].append(dict(id='kpi-sources-missing', kind='model', priority='high', summary='Original'))
    path = export_file(dept, doc)
    path.chmod(0o640)
    (path.parent / 'summary.md').write_text('Day report: something went wrong.\n')
    kpis = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert enrich(path, kpis, dept, DAY)
    value = load_export(path)
    assert value['top_kpis']['model_calls_today'] == 99
    assert value['top_kpis']['model_kpi'] == doc['top_kpis']['model_kpi']
    assert value['top_kpis']['export_enriched'] == 1
    assert value['needs_management_attention'] == doc['needs_management_attention']
    assert value['links']['day_report'] == f'outputs/{DAY}/4/summary.md'
    assert value['links']['gates'] == doc['links']['gates']
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert not enrich(path, kpis, dept, DAY)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert path.stat().st_mode & 0o777 == 0o640
    assert not list(path.parent.glob('.management-export.yaml.*'))


@pytest.mark.parametrize('contents', [None, '', '  \n'])
def test_day_report_missing_and_string_attention_preserved(evidence, contents):
    dept, sessions = evidence
    path = export_file(dept)
    if contents is not None:
        (path.parent / 'summary.md').write_text(contents)
    kpis = kpi.build(dept, DAY, transcripts_dir=sessions)
    enrich(path, kpis, dept, DAY)
    enrich(path, kpis, dept, DAY)
    value = load_export(path)
    assert 'day_report' not in value['links']
    assert value['needs_management_attention'][0] == 'Keep this free-form model note.'
    assert sum(isinstance(a, dict) and a['id'] == 'kpi-day-report-missing' for a in value['needs_management_attention']) == 1


@pytest.mark.parametrize('invalid', ['bad-yaml', 'missing-root', 'extra-root', 'bad-attention', 'bad-links', 'duplicate-key', 'wrong-day', 'bad-kpi'])
def test_enrichment_refuses_and_does_not_touch_file(evidence, invalid):
    dept, sessions = evidence
    doc = valid_export()
    if invalid == 'missing-root':
        del doc['open_gates']
    if invalid == 'extra-root':
        doc['unexpected'] = 1
    if invalid == 'bad-attention':
        doc['needs_management_attention'] = [dict(id='not_kebab', kind='x', priority='low', summary='s')]
    if invalid == 'bad-links':
        doc['links'] = {'empty': ''}
    if invalid == 'wrong-day':
        doc['date'] = '2026-10-02'
    path = export_file(dept, doc)
    if invalid == 'bad-yaml':
        path.write_text('[broken')
    if invalid == 'duplicate-key':
        path.write_text(path.read_text() + 'dept: ben\n')
    kpis = kpi.build(dept, DAY, transcripts_dir=sessions)
    if invalid == 'bad-kpi':
        kpis['top_kpis_flat'] = {'nan': float('nan')}
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises((ValueError, yaml.YAMLError)):
        enrich(path, kpis, dept, DAY)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_enrichment_preserves_schema_optional_autonomy_and_unquoted_date(evidence):
    dept, sessions = evidence
    doc = valid_export()
    doc['date'] = dt.date.fromisoformat(DAY)
    doc['autonomy_readiness'] = {'window_days': 14, 'action_classes': []}
    path = export_file(dept, doc)
    enrich(path, kpi.build(dept, DAY, transcripts_dir=sessions), dept, DAY)
    assert load_export(path)['autonomy_readiness'] == doc['autonomy_readiness']


def test_enrich_cli_refusal(evidence):
    dept, _ = evidence
    path = export_file(dept)
    path.write_text('[broken')
    inputs = dept / 'kpis.json'
    write_json(inputs, dict(top_kpis_flat={}, attention=[]))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/lib/enrich_management_export.py'), '--export', str(path),
                             '--kpis', str(inputs), '--dept-dir', str(dept), '--day', DAY], capture_output=True, text=True)
    assert result.returncode != 0
    assert path.read_text() == '[broken'


@pytest.fixture
def fleet(tmp_path):
    agents, sessions, state = tmp_path / 'agents', tmp_path / 'transcripts', tmp_path / 'state'
    for slug in ('tony', 'ben'):
        manifest(agents / slug)
    path = export_file(agents / 'tony')
    (path.parent / 'summary.md').write_text('Day report.\n')
    transcript(sessions / 'tony/main.jsonl', [message()])
    # If symlinks are followed this creates an extra missing-export alarm.
    outside = tmp_path / 'outside'
    manifest(outside)
    (agents / 'bubble-ops-maya').symlink_to(outside, target_is_directory=True)
    calls = tmp_path / 'emits.jsonl'
    emitter = tmp_path / 'emit.py'
    emitter.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
                       f'with Path({str(calls)!r}).open("a") as stream:\n'
                       '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n')
    emitter.chmod(0o755)
    env = dict(os.environ, AGENTS_ROOT=str(agents), TRANSCRIPTS_ROOT=str(sessions),
               FLEET_EXPORT_STATE_DIR=str(state), EMIT_BIN=str(emitter), PYTHON_BIN=sys.executable,
               RUNUSER_BIN='/nonexistent-runuser', FLEET_EXPORT_NOW='2026-10-03T23:30:00+02:00')
    # Root CI uses an injected owner-command shim instead of actual privilege changes.
    if os.geteuid() == 0:
        shim = tmp_path / 'runuser'
        shim.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
        shim.chmod(0o755)
        env['RUNUSER_BIN'] = str(shim)
    return agents, path, calls, state, env


def fleet_run(env, *args):
    return subprocess.run(['bash', str(ROOT / 'scripts/fleet-export-check.sh'), *args],
                          env=env, capture_output=True, text=True)


def test_fleet_dry_run_changes_nothing(fleet):
    agents, path, calls, state, env = fleet
    before = {str(p): p.read_bytes() for p in agents.rglob('*') if p.is_file()}
    result = fleet_run(env, '--dry-run', '--day', DAY)
    assert result.returncode == 0, result.stderr
    assert 'DRY RUN enrich tony' in result.stdout
    assert 'DRY RUN alarm ben' in result.stdout
    assert 'bubble-ops-maya' not in result.stdout
    assert not state.exists() and not calls.exists()
    assert before == {str(p): p.read_bytes() for p in agents.rglob('*') if p.is_file()}


def test_fleet_enriches_once_alarms_once_and_skips_symlink(fleet):
    agents, path, calls, state, env = fleet
    first = fleet_run(env, '--day', DAY)
    assert first.returncode == 0, first.stderr
    assert 'Enriched tony' in first.stdout and 'ALARM ben' in first.stdout
    value = load_export(path)
    assert value['top_kpis']['export_enriched'] == 1
    assert value['top_kpis']['model_calls_today'] == 1
    kpis = path.with_name('mission-kpis.json')
    mtimes = path.stat().st_mtime_ns, kpis.stat().st_mtime_ns
    second = fleet_run(env, '--day', DAY)
    assert second.returncode == 0, second.stderr
    assert 'Enriched' not in second.stdout
    assert mtimes == (path.stat().st_mtime_ns, kpis.stat().st_mtime_ns)
    emitted = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(emitted) == 1
    assert f'task=fleet-export-missing-ben-{DAY}' in emitted[0]
    assert f'title=Missing daily management export: ben {DAY}' in emitted[0]
    assert 'budget=5' in emitted[0]
    assert 'bubble-ops-maya' not in first.stdout + second.stdout
    assert all(p.stat().st_uid == agents.stat().st_uid for p in agents.rglob('*') if not p.is_symlink())


@pytest.mark.parametrize('now,date,alarm', [('2026-10-03T21:50:00+02:00', DAY, False),
                                          ('2026-10-03T23:29:59+02:00', DAY, False),
                                          ('2026-10-03T23:30:00+02:00', DAY, True),
                                          ('2026-10-04T08:10:00+02:00', DAY, True)])
def test_fleet_deadline_and_previous_day_selection(fleet, now, date, alarm):
    _, _, _, _, env = fleet
    env['FLEET_EXPORT_NOW'] = now
    result = fleet_run(env, '--dry-run')
    assert result.returncode == 0, result.stderr
    assert f'ben {date}' in result.stdout
    assert ('DRY RUN alarm ben' in result.stdout) is alarm


def test_fleet_emitter_failure_retries_without_receipt(fleet, tmp_path):
    _, _, calls, state, env = fleet
    emitter = env['EMIT_BIN']
    failing = tmp_path / 'fail'
    failing.write_text('#!/bin/sh\nexit 1\n')
    failing.chmod(0o755)
    env['EMIT_BIN'] = str(failing)
    result = fleet_run(env, '--day', DAY)
    assert result.returncode == 1
    assert not list(state.glob('*.sent'))
    env['EMIT_BIN'] = emitter
    assert fleet_run(env, '--day', DAY).returncode == 0
    assert len(calls.read_text().splitlines()) == 1


def test_fleet_bad_export_does_not_hide_other_dept_alarm(fleet):
    _, path, calls, _, env = fleet
    path.write_text('[broken')
    result = fleet_run(env, '--day', DAY)
    assert result.returncode == 1
    assert 'ERROR tony' in result.stderr
    assert len(calls.read_text().splitlines()) == 1
    assert path.read_text() == '[broken'


def test_timer_and_service_contract():
    timer = (ROOT / 'deploy/templates/fleet-export-check.timer').read_text()
    for hour in ('21:50', '23:30', '08:10'):
        assert f'OnCalendar=*-*-* {hour}:00 Europe/Paris' in timer
    service = (ROOT / 'deploy/templates/fleet-export-check.service').read_text()
    assert 'User=root' in service
    assert 'ConditionPathExists' not in service and 'EnvironmentFile' not in service
    assert 'ExecStart=/opt/bubble-ops-loop/scripts/fleet-export-check.sh' in service


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


def test_fleet_root_uses_runuser_with_actual_directory_owner(fleet, tmp_path):
    import pwd
    agents, path, _, _, env = fleet
    owner = pwd.getpwuid(agents.stat().st_uid).pw_name
    shim = tmp_path / 'python-shim'
    shim.write_text(f'#!{sys.executable}\nimport os, sys\n'
                   'if sys.argv[1] == "-":\n'
                   '    os.geteuid = lambda: 0\n'
                   '    sys.argv = sys.argv[1:]\n'
                   '    exec(compile(sys.stdin.read(), "fleet-check", "exec"))\n'
                   'else:\n'
                   '    os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n')
    shim.chmod(0o755)
    log = tmp_path / 'owner-calls.jsonl'
    runuser = tmp_path / 'runuser-shim'
    runuser.write_text(f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n'
                       f'with Path({str(log)!r}).open("a") as f:\n'
                       '    f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                       'os.execv(sys.argv[4], sys.argv[4:])\n')
    runuser.chmod(0o755)
    env.update(PYTHON_BIN=str(shim), RUNUSER_BIN=str(runuser))
    result = fleet_run(env, '--day', DAY)
    assert result.returncode == 0, result.stderr
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(invocations) == 2
    assert all(call[:3] == ['-u', owner, '--'] for call in invocations)
    assert 'mission_kpis.py' in invocations[0][4]
    assert 'enrich_management_export.py' in invocations[1][4]
    assert load_export(path)['top_kpis']['export_enriched'] == 1


@pytest.mark.parametrize('stamp', [0, True, '1'])
def test_existing_enrichment_stamp_is_not_overwritten(evidence, stamp):
    dept, sessions = evidence
    doc = valid_export()
    doc['top_kpis']['export_enriched'] = stamp
    path = export_file(dept, doc)
    original = path.read_bytes()
    with pytest.raises(ValueError, match='conflicts'):
        enrich(path, kpi.build(dept, DAY, transcripts_dir=sessions), dept, DAY)
    assert path.read_bytes() == original


def test_overlapping_windows_count_each_message_once_per_mission(evidence):
    dept, sessions = evidence
    # Both surviving records cover the same call; the summed mission volume is a union.
    ledger(dept, '2026-10-02', {'inbox_watch': {'dispatched_at': '2026-10-02T09:00:00Z',
                                             'completed_at': '2026-10-03T09:00:00Z'}})
    doc = kpi.build(dept, DAY, transcripts_dir=sessions)
    assert doc['missions']['inbox_watch']['runs_dispatched'] == 2
    assert doc['missions']['inbox_watch']['total_tokens_approx'] == 100
