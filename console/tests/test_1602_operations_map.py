"""Synthetic, disk-only coverage for Tony's read-only operations grid."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def setup_map(tmp_path, monkeypatch):
    from console import settings
    from console.services import operations_map
    monkeypatch.setattr(settings, 'READ_FROM_DISK', str(tmp_path))
    monkeypatch.setenv('CANONICAL_AGENTS_ROOT', str(tmp_path / 'canonical'))
    for slug in ('tony', 'ben', 'tonio'):
        root = tmp_path / f'bubble-ops-{slug}'
        root.mkdir()
        (root / 'dept.yaml').write_text(yaml.safe_dump({'recurring_missions': [
            {'id': 'daily', 'layer': 1, 'cadence': 'daily', 'time': '07:00'},
            {'id': 'session_handoff', 'layer': 4, 'cadence': 'daily', 'time': '23:00'},
        ]}))
    mapping = {'business_units': {'fund': {'label': 'Fonds', 'lead': 'Ben'}},
               'layers': ['produce'], 'missions': [
                   {'dept': 'ben', 'id': 'daily', 'unit': ['fund', 'methods'], 'layer': 'produce'},
                   {'dept': '*', 'id': 'session_handoff', 'unit': 'support', 'layer': 'steer'},
                   {'dept': 'missing', 'id': 'daily', 'unit': 'fund', 'layer': 'produce'},
               ], 'gaps': [{'unit': 'showcase', 'layer': 'distribute', 'what': '<script>gap</script>'}]}
    path = tmp_path / 'bubble-ops-tony/outputs/operations-map/operations-map.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(mapping))
    return operations_map, tmp_path, path


def all_missions(result):
    return [m for row in result['rows'] for cell in row['cells'] for m in cell['missions']]


def test_grid_multiloop_wildcard_gaps_drift_and_missing(setup_map):
    svc, root, _ = setup_map
    result = svc.load_operations_map(NOW)
    entries = all_missions(result)
    assert sum(m['dept'] == 'ben' and m['id'] == 'daily' for m in entries) == 2
    assert sum(m['id'] == 'session_handoff' for m in entries) == 3
    assert next(m for m in entries if m['dept'] == 'missing')['status'] == 'unknown'
    assert next(m for m in entries if m['dept'] == 'ben' and m['id'] == 'daily')['status'] == 'due'
    assert {m['dept'] for m in result['drift']} == {'tony', 'tonio'}
    assert 'showcase' in result['units']
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / 'templates'),
                      autoescape=select_autoescape())
    html = env.get_template('partials/operations_map.html').render(operations_map=result)
    assert '&lt;script&gt;gap&lt;/script&gt;' in html
    assert '<script>gap</script>' not in html
    assert 'ops-gap' in html and 'dérive' in html
    assert 'Signal inconnu' in html


@pytest.mark.parametrize('evidence,expected', [
    ('ledger', 'completed'), ('marker', 'completed'),
    ('materialized', 'due'), ('layer', 'due'), ('corrupt', 'due'), ('future', 'due'),
])
def test_completion_evidence(setup_map, evidence, expected):
    import json
    svc, root, _ = setup_map
    day = root / 'bubble-ops-ben/outputs/2026-09-28'
    mission_dir = day / 'missions/daily'
    mission_dir.mkdir(parents=True)
    stamp = '2026-09-28T09:00:00Z'
    if evidence in ('ledger', 'future'):
        (day / 'dispatch.json').write_text(json.dumps({'daily': {
            'completed_at': stamp if evidence == 'ledger' else '2026-09-29T09:00:00Z'}}))
    elif evidence == 'corrupt':
        (day / 'dispatch.json').write_text('{broken')
    elif evidence == 'layer':
        (day / '1').mkdir()
        (day / '1/.last-run').write_text(stamp)
    else:
        (mission_dir / ('.last-run' if evidence == 'marker' else '.last-materialized')).write_text(stamp)
    before = sorted(str(p) for p in root.rglob('*'))
    entries = all_missions(svc.load_operations_map(NOW))
    assert next(m for m in entries if m['dept'] == 'ben' and m['id'] == 'daily')['status'] == expected
    assert sorted(str(p) for p in root.rglob('*')) == before


def test_canonical_runtime_and_mirror(setup_map):
    svc, root, _ = setup_map
    runtime = root / 'canonical/ben'
    runtime.mkdir(parents=True)
    (runtime / 'dept.yaml').write_text(yaml.safe_dump({'recurring_missions': [
        {'id': 'daily', 'layer': 2, 'cadence': 'daily', 'time': '23:00'}]}))
    mirror = root / 'canonical/bubble-ops-tonio'
    mirror.mkdir()
    (mirror / 'dept.yaml').write_text(yaml.safe_dump({'recurring_missions': [
        {'id': 'mirrored', 'layer': 3, 'cadence': 'event'}]}))
    day = runtime / 'outputs/2026-09-28'
    day.mkdir(parents=True)
    (day / 'heartbeat.log').write_text('2026-09-28T12:00:00Z\n')
    result = svc.load_operations_map(NOW)
    ben = next(m for m in all_missions(result) if m['dept'] == 'ben')
    assert ben['mission_layer'] == 2 and ben['status'] == 'not-run'
    assert any(m['id'] == 'mirrored' for m in result['drift'])
    assert next(d for d in result['departments'] if d['slug'] == 'ben')['pulse'].alive


@pytest.mark.parametrize('content', ['', '[]', 'broken: [', 'business_units: []'])
def test_missing_or_malformed_map(setup_map, content):
    svc, _, path = setup_map
    path.write_text(content)
    assert not svc.load_operations_map(NOW)['available']
    path.unlink()
    assert not svc.load_operations_map(NOW)['available']


def test_paris_midnight_repeating_and_cron(setup_map):
    import json
    svc, root, _ = setup_map
    runtime = root / 'bubble-ops-ben'
    day = runtime / 'outputs/2026-09-28'
    day.mkdir(parents=True)
    (day / 'dispatch.json').write_text(json.dumps({'daily': {'completed_at': '2026-09-27T22:30:00Z'}}))
    daily = {'id': 'daily', 'layer': 1, 'cadence': 'daily', 'time': '07:00'}
    assert svc._mission_state(runtime, daily, NOW)[0] == 'completed'
    assert svc._mission_state(runtime, {**daily, 'cadence': 'hourly'}, NOW)[0] == 'due'
    assert svc.load_operations_map(datetime(2026, 9, 27, 23, tzinfo=timezone.utc))['date'] == '2026-09-28'
    (day / 'dispatch.json').unlink()
    previous = runtime / 'outputs/2026-09-01'
    previous.mkdir()
    (previous / 'dispatch.json').write_text(json.dumps({'daily': {'completed_at': '2026-09-01T09:00:00Z'}}))
    assert svc._mission_state(runtime, {**daily, 'cadence': 'cron:0 8 1 * *'}, NOW)[0] == 'not-run'


def test_route_tony_only(setup_map, monkeypatch):
    from console import settings
    from console.main import create_app
    from console.routes import dept
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, 'BEARER_TOKEN', 'test-operations')
    monkeypatch.setattr(dept, '_checkout_staleness_cached', lambda slug: None)
    monkeypatch.setattr(dept, '_kanban_snapshot', lambda: None)
    client = TestClient(create_app())
    client.headers['Authorization'] = 'Bearer test-operations'
    response = client.get('/dept/tony')
    assert response.status_code == 200
    assert 'operations-map-heading' in response.text
    assert 'Fonds' in response.text
    response = client.get('/dept/ben')
    assert response.status_code == 200
    assert 'operations-map-heading' not in response.text

    response = client.get('/dept/tony/operations-fragment')
    assert response.status_code == 200
    assert 'hx-trigger="every 60s"' in response.text
    assert 'operations-map-heading' in response.text
    assert TestClient(create_app()).get('/dept/tony/operations-fragment').status_code == 401
