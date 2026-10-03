"""Hermetic fleet costs regressions (#1712); no live profiles or network."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from console.services import cost_tracker as tracker
from console.services.cost_io import atomic_json

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('hermes_usage_export', REPO / 'scripts/export-hermes-usage.py')
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / 'projects'
    root.mkdir()
    monkeypatch.setattr(tracker, 'PROJECTS_DIR', root)
    monkeypatch.setattr(tracker, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(tracker, 'CACHE_FILE', tmp_path / 'cache/usage.sqlite3')
    tracker._report_cache.update(report=None, built_at=0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(tracker, 'datetime', Clock)
    yield root
    tracker._report_cache.update(report=None, built_at=0)


def write_lines(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def message(ts='2026-10-03T08:00:00Z', *, mid='msg-1', uuid='uuid-1', session='session-1', tokens=100, output=20):
    return {'type': 'assistant', 'timestamp': ts, 'uuid': uuid, 'sessionId': session,
            'requestId': 'req-1', 'message': {'id': mid, 'model': 'claude-sonnet-4-6',
            'content': 'private session text', 'usage': {'input_tokens': tokens,
            'output_tokens': output, 'cache_read_input_tokens': 30,
            'cache_creation_input_tokens': 40}}}


def hermes_row(**overrides):
    return {'session_id': 'h1', 'started_at': '2026-10-03T08:00:00Z', 'model': 'gpt-unpriced',
            'input_tokens': 100, 'output_tokens': 20, 'cache_read_tokens': 30,
            'cache_write_tokens': 40, 'reasoning_tokens': 10,
            'actual_cost_usd': None, 'estimated_cost_usd': None, **overrides}


def hermes_export(projects, *rows, slug='morty'):
    path = projects / f'_vps-{slug}-hermes/hermes-usage.json'
    atomic_json(path, {'rows': list(rows)})
    return path


def test_morty_own_actual_cost_precedes_estimate_and_no_cache_bill_invented(projects):
    hermes_export(projects, hermes_row(actual_cost_usd=0.75, estimated_cost_usd=1.25))
    rep = tracker.build_report(refresh=True)
    bucket = rep['agents']['morty']['today']
    assert bucket['tokens'] == 190  # reasoning subset excluded
    assert bucket['tokens_by_class']['reasoning'] == 10
    assert bucket['cost'] == bucket['cost_usd_estimate'] == 0.75
    assert bucket['cost_usd_estimate_priced_only'] == 0.75
    assert bucket['cache_cost'] is None
    assert bucket['runs'] == 1
    assert rep['agents']['morty']['week']['tokens'] == 190
    assert any('cumulative' in n for n in rep['notes'])


@pytest.mark.parametrize('value', [0.0, 0.123])
def test_hermes_estimate_and_valid_zero_cost(projects, value):
    hermes_export(projects, hermes_row(estimated_cost_usd=value))
    summary = tracker.fleet_summary('2026-10-03')
    assert summary['agents']['morty']['cost_usd_estimate'] == value
    assert summary['agents']['morty']['cost_usd_estimate_priced_only'] == value
    assert summary['totals']['cost_usd_estimate_priced_only'] == value


def test_hermes_matched_model_uses_existing_prices(projects):
    hermes_export(projects, hermes_row(model='claude-sonnet-4-6', input_tokens=1_000_000,
                  output_tokens=0, cache_read_tokens=1_000_000, cache_write_tokens=0))
    bucket = tracker.build_report(refresh=True)['agents']['morty']['today']
    assert bucket['cost'] == 3.0
    assert bucket['cache_cost'] == 0.3
    assert bucket['cost_usd_estimate'] == 3.3


def test_unpriced_hermes_propagates_null_to_fleet_total(projects):
    hermes_export(projects, hermes_row())
    write_lines(projects / '_vps-ellie/work/s.jsonl', message())
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    assert summary['agents']['morty']['cost_usd_estimate'] is None
    assert summary['totals']['cost_usd_estimate'] is None
    assert summary['totals']['tokens'] == 380
    assert any('Unpriced Hermes' in n for n in summary['notes'])
    assert summary['agents']['morty']['cost_usd_estimate_priced_only'] == 0.0
    assert summary['totals']['cost_usd_estimate_priced_only'] == summary['agents']['ellie']['cost_usd_estimate']
    assert any('missing costs for agents: morty.' in n for n in summary['notes'])


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': 0, 'output_tokens': 0},
                                  {'input_tokens': 1000, 'output_tokens': 2000,
                                   'cache_read_input_tokens': 3000,
                                   'cache_creation_input_tokens': 4000, 'reasoning_tokens': 5000}])
def test_synthetic_records_ignored_entirely(projects, usage):
    path = projects / '_vps-accountant/work/s.jsonl'
    priced = message()
    write_lines(path, priced)
    baseline = tracker.fleet_summary('2026-10-03', refresh=True)
    synthetic = message(ts=None, mid='local-notice', session='local-only')
    synthetic['message']['model'] = '<synthetic>'
    if usage is None:
        synthetic['message'].pop('usage')
    else:
        synthetic['message']['usage'] = usage
    write_lines(path, synthetic, priced)
    assert tracker.fleet_summary('2026-10-03', refresh=True) == baseline
    parsed = tracker.parse_session(path)
    assert parsed['n_turns'] == 1
    assert '<synthetic>' not in parsed['model_usage']


def test_synthetic_records_in_old_parse_cache_ignored(projects):
    path = projects / '_vps-miranda/work/s.jsonl'
    write_lines(path, message())
    baseline = tracker.fleet_summary('2026-10-03', refresh=True)
    # Obsolete JSON cache is never loaded; incompatible SQLite file entries
    # are reparsed from source (including filtering synthetic records).
    legacy = tracker.CACHE_FILE.with_suffix('.json')
    legacy.write_text(json.dumps({str(path): {'version': 2, 'records': [
        {'model': '<synthetic>', 'usage': dict.fromkeys(tracker.TOKEN_CLASSES, 1000)}]}}))
    with sqlite3.connect(tracker.CACHE_FILE) as db:
        db.execute('UPDATE files SET version=2')
    assert tracker.fleet_summary('2026-10-03', refresh=False) == baseline
    with sqlite3.connect(tracker.CACHE_FILE) as db:
        assert db.execute('SELECT version FROM files').fetchone() == (4,)


@pytest.mark.parametrize('source', ['claude', 'hermes'])
def test_zero_token_unknown_model_ignored_for_pricing(projects, source):
    if source == 'claude':
        row = message()
        row['message'].update(model='future-unpriced', usage={})
        write_lines(projects / '_vps-ben/work/s.jsonl', row)
    else:
        hermes_export(projects, hermes_row(model='future-unpriced', input_tokens=0,
                      output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                      reasoning_tokens=0), slug='ben')
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    for bucket in (summary['agents']['ben'], summary['totals']):
        assert bucket['tokens'] == 0
        assert bucket['cost_usd_estimate'] == 0.0
        assert bucket['cost_usd_estimate_priced_only'] == 0.0
        assert bucket['cache_cost'] == 0.0
    assert not any('Unpriced' in n or 'missing costs' in n for n in summary['notes'])


@pytest.mark.parametrize('source', ['claude', 'hermes'])
@pytest.mark.parametrize('token_class', tracker.TOKEN_CLASSES)
def test_unknown_model_with_tokens_keeps_priced_lower_bound(projects, source, token_class):
    known = message()
    known['message']['usage'] = {'input_tokens': 1_000_000,
                                'cache_read_input_tokens': 1_000_000}
    other = message(mid='other-agent')
    other['message'].update(model='claude-haiku-4-5', usage={'input_tokens': 1_000_000})
    write_lines(projects / '_vps-ellie/work/s.jsonl', other)
    write_lines(projects / '_vps-ben/work/s.jsonl', known)
    claude_keys = dict(zip(tracker.TOKEN_CLASSES, ('input_tokens', 'output_tokens',
                       'cache_read_input_tokens', 'cache_creation_input_tokens', 'reasoning_tokens')))
    if source == 'claude':
        unknown = message(mid='unknown')
        unknown['message'].update(model='future-unpriced', usage={claude_keys[token_class]: 10})
        write_lines(projects / '_vps-ben/work/s.jsonl', known, unknown)
    else:
        hermes_keys = {**claude_keys, 'cache_read': 'cache_read_tokens', 'cache_create': 'cache_write_tokens'}
        usage = dict.fromkeys(hermes_keys.values(), 0)
        usage[hermes_keys[token_class]] = 10
        hermes_export(projects, hermes_row(model='future-unpriced', **usage), slug='ben')
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    assert summary['agents']['ben']['cost_usd_estimate'] is None
    assert summary['totals']['cost_usd_estimate'] is None
    assert summary['agents']['ben']['cost_usd_estimate_priced_only'] == 3.3
    assert summary['totals']['cost_usd_estimate_priced_only'] == 4.3
    assert summary['agents']['ben']['tokens_by_class'][token_class] >= 10
    assert any(f'Unpriced {source.title()} model for ben: future-unpriced;' in n for n in summary['notes'])
    assert any('missing costs for agents: ben.' in n for n in summary['notes'])


@pytest.mark.parametrize('contents', [None, 'broken json', '[]', '{"rows":{}}', '{"rows":[null,3,{}]}'])
def test_missing_or_corrupt_export_does_not_crash(projects, contents):
    path = projects / '_vps-morty-hermes/hermes-usage.json'
    path.parent.mkdir()
    if contents is not None:
        path.write_text(contents)
    write_lines(projects / '_vps-ellie/work/s.jsonl', message())
    report = tracker.build_report(refresh=True)
    assert 'ellie' in report['agents']
    assert 'morty' not in report['agents']
    assert report['notes']


def test_hermes_jsonls_are_not_billed_again(projects):
    hermes_export(projects, hermes_row(estimated_cost_usd=0.2))
    write_lines(projects / '_vps-morty-hermes/morty/duplicate.jsonl', message())
    assert tracker.build_report(refresh=True)['totals']['today']['tokens'] == 190


def test_tonio_alias_does_not_change_vps_tony(projects):
    write_lines(projects / '_mac-joris/-Users-joris-claude-workspaces-Tony_CEO/s.jsonl', message())
    write_lines(projects / '_vps-tony/work/s.jsonl', message(mid='msg-tony', session='session-tony'))
    # Actual mangled Claude paths replace underscores with hyphens.
    assert tracker.classify('_mac-joris/-Users-joris-claude-workspaces-Tony-CEO') == 'tonio'
    assert tracker.classify('_vps-tony/work') == 'tony'
    assert tracker.classify('-home-claude-agents-bubble-ops-tony') == 'tony'
    assert set(tracker.build_report(refresh=True)['agents']) == {'tonio', 'tony'}


@pytest.mark.parametrize('day,before,after', [
    ('2026-07-02', '2026-07-01T21:59:59Z', '2026-07-01T22:00:00Z'),
    ('2026-01-02', '2026-01-01T22:59:59Z', '2026-01-01T23:00:00Z'),
    ('2026-03-29', '2026-03-28T22:59:59Z', '2026-03-28T23:00:00Z'),
    ('2026-10-25', '2026-10-24T21:59:59Z', '2026-10-24T22:00:00Z'),
])
def test_paris_boundaries_include_dst(projects, day, before, after):
    path = projects / '_vps-ben/work/s.jsonl'
    write_lines(path, message(before, mid='before'), message(after, mid='after', tokens=200))
    parsed = tracker.parse_session_for_day(path, day)
    assert parsed['model_usage']['claude-sonnet-4-6']['input'] == 200
    assert tracker.build_report(refresh=True, day=day)['totals']['day']['tokens'] == 290


def test_today_only_includes_todays_messages_not_touched_history(projects):
    path = projects / '_vps-accountant/work/s.jsonl'
    write_lines(path, message('2026-10-01T08:00:00Z', mid='old', tokens=1_000_000),
                message(tokens=100))
    report = tracker.build_report(refresh=True)
    assert report['today_date'] == '2026-10-03'
    assert report['timezone'] == 'Europe/Paris'
    assert report['agents']['accountant']['today']['tokens'] == 190
    assert report['agents']['accountant']['week']['tokens'] == 1_000_280
    # Changing mtime to well before the window cannot hide recent message usage.
    os.utime(path, (1, 1))
    assert tracker.build_report(refresh=True)['totals']['today']['tokens'] == 190


def test_week_is_seven_paris_calendar_days_not_file_mtimes(projects):
    write_lines(projects / '_vps-ben/work/s.jsonl',
                message('2026-09-26T21:59:59Z', mid='excluded'),
                message('2026-09-26T22:00:00Z', mid='included'))
    assert tracker.build_report(refresh=True)['totals']['week']['tokens'] == 190


def test_dedupe_resumed_content_blocks_subagents_and_symlinks(projects):
    root = projects / '_vps-rick/work'
    write_lines(root / 'original.jsonl', message(output=5), message(output=20, uuid='uuid-2'))
    write_lines(root / 'resumed.jsonl', message())
    write_lines(root / 'subagents/agent-1.jsonl', message(), message(mid='subcall', session='subsession'))
    (root / 'linked-file.jsonl').symlink_to(root / 'original.jsonl')
    (root / 'linked-directory').symlink_to(root / 'subagents', target_is_directory=True)
    (projects / '_vps-copy').symlink_to(projects / '_vps-rick', target_is_directory=True)
    rep = tracker.build_report(refresh=True)
    assert rep['totals']['today']['tokens'] == 380
    assert rep['totals']['today']['runs'] == 2
    assert 'copy' not in rep['agents']
    assert tracker.parse_session(root / 'original.jsonl')['n_turns'] == 1
    assert tracker.build_report(refresh=False, day='2026-10-03')['totals']['day']['tokens'] == 380


def test_distinct_ids_equal_usage_are_not_deduplicated(projects):
    write_lines(projects / '_vps-ben/work/s.jsonl', message(mid='a'), message(mid='b'))
    assert tracker.build_report(refresh=True)['totals']['today']['tokens'] == 380


def test_uuid_fallback_deduplicates_copies(projects):
    row = message(mid=None)
    write_lines(projects / '_vps-ben/work/a.jsonl', row)
    write_lines(projects / '_vps-ben/work/b.jsonl', row)
    assert tracker.build_report(refresh=True)['totals']['today']['tokens'] == 190


def test_missing_timestamps_are_excluded_and_noted(projects):
    row = message(ts=None)
    write_lines(projects / '_vps-ben/work/s.jsonl', row)
    report = tracker.build_report(refresh=True)
    assert report['totals']['today']['tokens'] == 0
    assert any('timestamp' in n for n in report['notes'])


def test_summary_fields_expected_agents_and_no_content(projects):
    for name, timestamp in [('claudette', '2026-10-03T08:00:00Z'), ('ellie', '2026-10-02T08:00:00Z')]:
        write_lines(projects / f'_vps-{name}/work/s.jsonl', message(timestamp, mid=name))
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    assert summary['day'] == '2026-10-03'
    assert summary['generated_at'] == '2026-10-03T12:00:00+00:00'
    assert summary['timezone'] == 'Europe/Paris'
    assert summary['expected_agents'] == ['claudette', 'ellie']
    assert summary['expected_agents_without_data'] == ['ellie']
    assert summary['agents']['claudette']['source'] == 'claude'
    assert set(summary['agents']['claudette']) == {
        'tokens', 'tokens_by_class', 'cost_usd_estimate', 'cost_usd_estimate_priced_only',
        'cache_cost', 'runs', 'source'}
    assert summary['totals']['tokens'] == 190
    assert 'private session text' not in json.dumps(summary)
    assert 'session-1' not in json.dumps(summary)


def make_db(path, *, models=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sessions (id TEXT, started_at REAL, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, content TEXT)')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   ('h1', datetime(2026, 10, 3, 8, tzinfo=timezone.utc).timestamp(), 'gpt-unpriced',
                    100, 20, 30, 40, 10, 0.8, 0.5, 'secret job prompt'))
        if models:
            db.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL)')
            for model in ('model-a', 'model-b'):
                db.execute('INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?)',
                           ('h1', model, 50, 10, 15, 20, 5, 0.4, 0.25))


def test_exporter_models_replace_session_totals_read_only_no_prompts(tmp_path, projects):
    db = tmp_path / 'home/agent-morty/.hermes/profiles/morty/state.db'
    make_db(db)
    before = db.read_bytes()
    assert exporter.export_all(tmp_path / 'home', projects) == 0
    path = projects / '_vps-morty-hermes/hermes-usage.json'
    data = json.loads(path.read_text())
    assert len(data['rows']) == 2
    assert sum(row['input_tokens'] for row in data['rows']) == 100
    assert db.read_bytes() == before
    assert 'secret job prompt' not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    assert summary['agents']['morty']['tokens'] == 190
    assert summary['agents']['morty']['cost_usd_estimate'] == 0.5
    assert summary['agents']['morty']['runs'] == 1


def test_exporter_fallback_without_model_table(tmp_path):
    db = tmp_path / 'state.db'
    make_db(db, models=False)
    data = exporter.read_usage(db)
    assert len(data['rows']) == 1
    assert data['rows'][0]['actual_cost_usd'] == 0.5
    assert any('session_model_usage' in n for n in data['notes'])


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'no_tables', 'missing_required_columns'])
def test_exporter_bad_database_produces_no_data(tmp_path, kind):
    db = tmp_path / 'state.db'
    if kind == 'corrupt':
        db.write_text('not sqlite')
    elif kind != 'missing':
        with sqlite3.connect(db) as con:
            if kind == 'missing_required_columns':
                con.execute('CREATE TABLE sessions (id TEXT, started_at REAL)')
    with pytest.raises(exporter.UsageReadError):
        exporter.read_usage(db)


def test_exporter_optional_columns_are_null(tmp_path):
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE sessions (session_id TEXT, started_at TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER)')
        con.execute('INSERT INTO sessions VALUES ("s", "2026-10-03T08:00:00Z", "unknown", 10, 20)')
    row = exporter.read_usage(db)['rows'][0]
    assert row['cache_write_tokens'] is None
    assert row['actual_cost_usd'] is None
    assert row['session_id'] == 's'


def test_exporter_reports_destination_symlink_failure(tmp_path):
    db = tmp_path / 'home/agent-morty/.hermes/profiles/morty/state.db'
    make_db(db)
    outside = tmp_path / 'outside'
    outside.mkdir()
    projects = tmp_path / 'projects'
    projects.mkdir()
    (projects / '_vps-morty-hermes').symlink_to(outside, target_is_directory=True)
    assert exporter.export_all(tmp_path / 'home', projects) == 1
    assert list(outside.iterdir()) == []


def test_atomic_summary_mode_and_symlink_refusal(tmp_path):
    path = tmp_path / 'costs/2026-10-03.json'
    atomic_json(path, {'day': '2026-10-03'})
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    atomic_json(path, {'day': 'replacement'})
    assert json.loads(path.read_text()) == {'day': 'replacement'}
    link = path.parent / 'latest.json'
    link.symlink_to(path)
    with pytest.raises(ValueError):
        atomic_json(link, {})
    assert json.loads(path.read_text()) == {'day': 'replacement'}
    assert len(list(path.parent.iterdir())) == 2


def test_cli_publishes_identical_day_and_latest_without_prompts(tmp_path):
    projects = tmp_path / 'projects'
    write_lines(projects / '_vps-ben/work/s.jsonl', message())
    out = tmp_path / 'costs/2026-10-03.json'
    latest = out.parent / 'latest.json'
    env = dict(os.environ, BUBBLE_COST_PROJECTS_DIR=str(projects), HOME=str(tmp_path),
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(REPO / 'console/services/cost_tracker.py'),
                            '--fleet-summary', '--day', '2026-10-03', '--out', str(out),
                            '--latest', str(latest)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == latest.read_bytes()
    assert json.loads(out.read_text())['agents']['ben']['tokens'] == 190
    assert 'private session text' not in out.read_text()


def test_costs_template_renders_tokens_notes_and_null_costs(projects):
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    hermes_export(projects, hermes_row())
    report = tracker.build_report(refresh=True)
    env = Environment(loader=ChoiceLoader([
        DictLoader({'base.html': '{% block content %}{% endblock %}'}),
        FileSystemLoader(REPO / 'console/templates'),
    ]), autoescape=True)
    html = env.get_template('costs.html').render(report=report, agent_budgets={}, dept_budgets={})
    assert 'morty' in html
    assert '190 / 190' in html
    assert 'Europe/Paris' in html
    assert 'cumulative' in html
    assert '$0.00' not in html.split('<table class="costs-table"', 1)[1]  # no invented model price


def test_old_hermes_session_agent_remains_visible_with_start_day_limit(projects):
    hermes_export(projects, hermes_row(started_at='2026-01-01T08:00:00Z', actual_cost_usd=3.0))
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    assert summary['agents']['morty']['tokens'] == 0
    assert summary['agents']['morty']['runs'] == 0
    assert summary['expected_agents'] == []  # no evidence of usage in the last 7 days
    assert any('long-lived' in n for n in summary['notes'])


def test_legacy_tonio_envelope_override_remains_effective(monkeypatch):
    from console import settings
    monkeypatch.setenv('OPERATING_ENVELOPE_JSON', '{"tony (local)":230}')
    assert settings._load_operating_envelope()['tonio'] == 230
    monkeypatch.setenv('OPERATING_ENVELOPE_JSON', '{"tony (local)":230,"tonio":250}')
    assert settings._load_operating_envelope()['tonio'] == 250


def test_many_tiny_calls_are_rounded_after_aggregation(projects):
    rows = [message(mid=f'm{i}', tokens=1, output=0) for i in range(100)]
    for row in rows:
        row['message']['usage']['cache_read_input_tokens'] = 0
        row['message']['usage']['cache_creation_input_tokens'] = 0
    write_lines(projects / '_vps-ben/work/s.jsonl', *rows)
    assert tracker.build_report(refresh=True)['totals']['today']['cost'] == 0.0003


def test_multimodel_session_bill_counted_once_without_inventing_model_prices(tmp_path, projects):
    db = tmp_path / 'home/agent-morty/.hermes/profiles/morty/state.db'
    make_db(db)
    with sqlite3.connect(db) as con:
        con.execute('UPDATE session_model_usage SET actual_cost_usd=NULL, estimated_cost_usd=NULL')
    exporter.export_all(tmp_path / 'home', projects)
    rows = json.loads((projects / '_vps-morty-hermes/hermes-usage.json').read_text())['rows']
    assert len(rows) == 2
    assert all(row['actual_cost_usd'] is None for row in rows)
    rep = tracker.build_report(refresh=True)
    assert rep['totals']['today']['cost_usd_estimate'] == 0.5
    assert rep['totals']['today']['tokens'] == 190
    assert all(bm['cost'] is None for bm in rep['agents']['morty']['today']['by_model'].values())


def test_all_requested_agents_share_one_readable_summary(projects, tmp_path):
    for slug in ('claudette', 'ellie', 'tony'):
        write_lines(projects / f'_vps-{slug}/work/s.jsonl', message(mid=slug, session=slug))
    write_lines(projects / '_mac-joris/-Users-joris-claude-workspaces-Tony-CEO/s.jsonl',
                message(mid='tonio', session='tonio'))
    hermes_export(projects, hermes_row(estimated_cost_usd=0.25))
    summary = tracker.fleet_summary('2026-10-03', refresh=True)
    atomic_json(tmp_path / 'costs/latest.json', summary)
    assert set(summary['agents']) == {'morty', 'claudette', 'ellie', 'tonio', 'tony'}
    assert summary['totals']['tokens'] == 950
    assert summary['totals']['runs'] == 5
    assert summary['agents']['morty']['source'] == 'hermes'
    assert summary['expected_agents_without_data'] == []


def test_resumed_copy_without_request_metadata_is_same_message(projects):
    original = message()
    resumed = message(uuid='resumed')
    resumed.pop('requestId')
    write_lines(projects / '_vps-ben/work/a.jsonl', original)
    write_lines(projects / '_vps-ben/work/b.jsonl', resumed)
    assert tracker.build_report(refresh=True)['totals']['today']['tokens'] == 190


@pytest.mark.parametrize('failure', ['corrupt', 'locked'])
def test_failed_database_refresh_preserves_good_export(tmp_path, projects, monkeypatch, caplog, failure):
    db = tmp_path / 'home/agent-morty/.hermes/profiles/morty/state.db'
    make_db(db)
    assert exporter.export_all(tmp_path / 'home', projects) == 0
    output = projects / '_vps-morty-hermes/hermes-usage.json'
    before = output.read_bytes(), output.stat().st_mtime_ns
    if failure == 'corrupt':
        db.write_text('corrupt')
    else:
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(exporter.sqlite3, 'connect', locked)
    assert exporter.export_all(tmp_path / 'home', projects) == 1
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before
    assert 'previous export preserved' in caplog.text


def test_cache_warm_days_do_not_parse_or_write_and_deleted_files_shrink(projects, monkeypatch):
    a = projects / '_vps-ben/work/a.jsonl'
    b = projects / '_vps-ben/work/b.jsonl'
    write_lines(a, message())
    write_lines(b, message(mid='b', session='b', ts='2026-10-02T08:00:00Z'))
    tracker.build_report(day='2026-10-03')
    before = tracker.CACHE_FILE.read_bytes(), tracker.CACHE_FILE.stat().st_mtime_ns
    def no_parse(*args):
        raise AssertionError('unchanged files must not be parsed')
    monkeypatch.setattr(tracker, '_session_records', no_parse)
    assert tracker.build_report(day='2026-10-02')['totals']['day']['tokens'] == 190
    assert tracker.build_report(refresh=True, day='2026-10-03')['totals']['day']['tokens'] == 190
    assert (tracker.CACHE_FILE.read_bytes(), tracker.CACHE_FILE.stat().st_mtime_ns) == before
    a.unlink()
    report = tracker.build_report(day='2026-10-03')
    assert report['totals']['day']['tokens'] == 0
    with sqlite3.connect(tracker.CACHE_FILE) as db:
        assert db.execute('SELECT path FROM files').fetchall() == [(str(b),)]
        assert db.execute('SELECT count(*) FROM records').fetchone() == (1,)
        assert db.execute('PRAGMA freelist_count').fetchone() == (0,)


def test_cache_publication_rolls_back_on_interrupted_scan(projects, monkeypatch):
    path = projects / '_vps-ben/work/a.jsonl'
    write_lines(path, message())
    tracker.build_report(day='2026-10-03')
    before = tracker.CACHE_FILE.read_bytes()
    write_lines(path, message(tokens=500))
    def interrupted(path, meta):
        yield {'identity': 'partial', 'session_id': 'partial', 'timestamp': '2026-10-03T08:00:00Z',
               'model': 'claude-sonnet-4-6', 'usage': dict.fromkeys(tracker.TOKEN_CLASSES, 5)}
        raise RuntimeError('interrupted')
    monkeypatch.setattr(tracker, '_session_records', interrupted)
    with pytest.raises(RuntimeError, match='interrupted'):
        tracker.build_report(day='2026-10-03')
    assert tracker.CACHE_FILE.read_bytes() == before


def test_idless_content_copies_collapse_but_different_time_or_usage_survive(projects):
    original = message(mid=None, uuid=None)
    write_lines(projects / '_vps-ben/work/a.jsonl', original,
                message(mid=None, uuid=None, ts='2026-10-03T08:00:01Z'),
                message(mid=None, uuid=None, tokens=101))
    write_lines(projects / '_vps-rick/work/subagents/copy.jsonl', original)
    assert tracker.build_report(day='2026-10-03')['totals']['day']['tokens'] == 571


def test_sqlite_matches_straightforward_uncached_reference(projects, monkeypatch):
    """Independent line reader/global dict reference, including different-day
    stream snapshots: maxima must be resolved before filtering by Paris day.
    """
    import hashlib
    from collections import defaultdict
    from zoneinfo import ZoneInfo
    root = projects / '_vps-ben/work'
    rows = [
        message('2026-03-28T22:59:59Z', mid='before-midnight', tokens=10),
        message('2026-03-28T23:00:00Z', mid='midnight', tokens=20, output=1),
        message('2026-03-29T00:59:59Z', mid='spring-before', tokens=30),
        message('2026-03-29T01:00:00Z', mid='spring-after', tokens=40),
        message('2026-10-24T21:59:59Z', mid='autumn-midnight-before', tokens=50),
        message('2026-10-24T22:00:00Z', mid='autumn-midnight', tokens=60),
        message('2026-10-25T00:59:59Z', mid='autumn-before', tokens=70),
        message('2026-10-25T01:00:00Z', mid='autumn-after', tokens=80),
        message('2026-10-03T08:00:00Z', mid=None, uuid=None),
    ]
    haiku = message('2026-03-29T04:00:00Z', mid='haiku', tokens=17, output=7)
    haiku['message']['model'] = 'claude-haiku-4-5'
    rows.append(haiku)
    write_lines(root / 'a.jsonl', *rows)
    write_lines(root / 'b-resumed.jsonl', *rows,
                message('2026-03-29T23:30:00Z', mid='midnight', output=46, tokens=25))
    write_lines(root / 'subagents/agent.jsonl', *rows,
                message('2026-10-25T01:30:00Z', mid='child', session='child', tokens=91))
    write_lines(projects / '_vps-rick/work/compacted.jsonl', *rows)
    reference = {}
    for path in sorted(projects.rglob('*.jsonl')):
        label = path.relative_to(projects).parts[0][len('_vps-'):]
        for line in path.read_text().splitlines():
            d = json.loads(line)
            msg = d['message']
            keys = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                    'cache_creation_input_tokens', 'reasoning_tokens')
            usage = tuple(msg['usage'].get(k, 0) for k in keys)
            identity = ('message', msg['id']) if msg.get('id') else ('uuid', d['uuid']) if d.get('uuid') else (
                'content', hashlib.blake2b(json.dumps([d['timestamp'], msg['model'], list(usage),
                    msg.get('role', 'assistant')], separators=(',', ':'), sort_keys=True).encode(), digest_size=16).digest())
            if identity not in reference:
                date = datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Paris')).date().isoformat()
                reference[identity] = [date, label, d['sessionId'], msg['model'], usage]
            else:
                reference[identity][4] = tuple(max(a, b) for a, b in zip(reference[identity][4], usage))
    # Freeze today's date inside the autumn fixture to check default week/today,
    # distinct runs across days and pricing as well as arbitrary backfill days.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 12, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(tracker, 'datetime', Clock)
    for day in ['2026-03-28', '2026-03-29', '2026-03-30', '2026-10-03', '2026-10-24', '2026-10-25']:
        report = tracker.build_report(day=day)
        for span, dates in [('day', {day}), ('today', {'2026-10-25'}),
                            ('week', {f'2026-10-{n}' for n in range(19, 26)})]:
            totals = [0] * 5
            by_agent = defaultdict(lambda: {'usage': [0] * 5, 'sessions': set(), 'real': 0., 'cache': 0., 'full': 0., 'models': defaultdict(lambda: [0, 0.])})
            for date, label, session, model, usage in reference.values():
                if date not in dates:
                    continue
                bucket = by_agent[label]
                bucket['sessions'].add(session)
                prices = (1, 5, .1, 1.25) if 'haiku' in model else (3, 15, .3, 3.75)
                real = (usage[0] * prices[0] + usage[1] * prices[1]) / 1e6
                cache = (usage[2] * prices[2] + usage[3] * prices[3]) / 1e6
                short = 'haiku' if 'haiku' in model else 'sonnet'
                bucket['models'][short][0] += sum(usage[:4])
                bucket['models'][short][1] += real + cache
                bucket['real'] += real
                bucket['cache'] += cache
                bucket['full'] += real + cache
                for n, amount in enumerate(usage):
                    totals[n] += amount
                    bucket['usage'][n] += amount
            actual = report['totals'][span]
            assert actual['tokens_by_class'] == dict(zip(tracker.TOKEN_CLASSES, totals))
            assert actual['tokens'] == sum(totals[:4])
            assert actual['runs'] == sum(len(b['sessions']) for b in by_agent.values())
            assert actual['cost'] == round(sum(round(b['real'], 4) for b in by_agent.values()), 4)
            assert actual['cache_cost'] == round(sum(round(b['cache'], 4) for b in by_agent.values()), 4)
            assert actual['cost_usd_estimate'] == round(sum(round(b['full'], 4) for b in by_agent.values()), 4)
            for label, expected in by_agent.items():
                got = report['agents'][label][span]
                assert got['tokens_by_class'] == dict(zip(tracker.TOKEN_CLASSES, expected['usage']))
                assert got['runs'] == len(expected['sessions'])
                for model, (tokens, cost) in expected['models'].items():
                    assert got['by_model'][model] == {'tokens': tokens, 'cost': round(cost, 4)}


def test_unreadable_projects_cli_preserves_latest(tmp_path):
    # A file in place of the directory is unreadable even when tests run as root.
    projects = tmp_path / 'projects'
    projects.write_text('not a directory')
    latest = tmp_path / 'latest.json'
    latest.write_text('{"last_good":true}\n')
    before = latest.read_bytes(), latest.stat().st_mtime_ns
    env = {**os.environ, 'BUBBLE_COST_PROJECTS_DIR': str(projects),
           'BUBBLE_COST_CACHE_DIR': str(tmp_path / 'cache')}
    result = subprocess.run([sys.executable, str(REPO / 'console/services/cost_tracker.py'),
        '--fleet-summary', '--day', '2026-10-03', '--latest', str(latest)],
        env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'unreadable' in result.stderr
    assert (latest.read_bytes(), latest.stat().st_mtime_ns) == before


def test_tonio_is_separate_from_tony_department_rollup():
    report = {'agents': {'tony': {'week': {'cost': 3}},
                         'tonio': {'week': {'cost': 7}},
                         'rick': {'week': {'cost': 11}}}}
    assert tracker.spent_by_dept(report) == {'tony': 3, 'tonio': 7, 'rnd': 11}


def test_cost_services_hardening_matches_write_paths():
    for name in ['hermes-usage-export', 'fleet-cost-summary']:
        unit = (REPO / 'deploy/templates' / f'{name}.service').read_text()
        for directive in ['NoNewPrivileges=true', 'PrivateTmp=true', 'ProtectSystem=strict']:
            assert directive in unit
        assert 'ProtectHome=' not in unit
        assert 'ReadWritePaths=/home/claude/.claude/projects' in unit
        if name == 'fleet-cost-summary':
            assert 'BUBBLE_COST_CACHE_DIR=/var/lib/bubble-fleet/costs/cache' in unit
            assert ' /var/lib/bubble-fleet/costs' in unit


def test_incomplete_discovery_still_prunes_confirmed_deleted_sources(projects, monkeypatch):
    path = projects / '_vps-ben/work/s.jsonl'
    write_lines(path, message())
    tracker.build_report(day='2026-10-03')
    path.unlink()
    broken = projects / '_vps-rick/broken'
    broken.mkdir(parents=True)
    original = Path.iterdir
    def unreadable(self):
        if self == broken:
            raise PermissionError('fixture unreadable directory')
        return original(self)
    monkeypatch.setattr(Path, 'iterdir', unreadable)
    report = tracker.build_report(day='2026-10-03')
    assert report['unreadable_transcripts'] == 1
    assert report['totals']['day']['tokens'] == 0
    with sqlite3.connect(tracker.CACHE_FILE) as db:
        assert db.execute('SELECT count(*) FROM files').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM records').fetchone() == (0,)
