"""Offline Mac runner/LaunchAgent and root mirror-alarm contracts for #1708."""
from __future__ import annotations

import builtins
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

from scripts.lib import fleet_export_check as fleet
from scripts.lib import management_kpis as child

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-10-03'


def manifest(dept, status='live', layers=(4,)):
    dept.mkdir(parents=True, exist_ok=True)
    (dept / 'dept.yaml').write_text(yaml.safe_dump(dict(status=status, layers=dict(subscribed=list(layers)))))


def export(dept, date=DAY):
    path = dept / 'outputs' / date / '4/management-export.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'# retain every byte\ndept: content\nprivate_note: unchanged\n')
    return path


def git_state(dept, *, local='a' * 40, remote=None, age_hours=0, packed=False,
              branch='main'):
    """Offline git metadata fixture: no git process or remote needed."""
    remote = local if remote is None else remote
    git = dept / '.git'
    git.mkdir(exist_ok=True)
    (git / 'HEAD').write_text(f'ref: refs/heads/{branch}\n')
    refs = {f'refs/heads/{branch}': local, f'refs/remotes/origin/{branch}': remote}
    if packed:
        (git / 'packed-refs').write_text('# pack-refs with: peeled fully-peeled sorted\n' +
                                       ''.join(f'{oid} {ref}\n' for ref, oid in refs.items()))
    else:
        for ref, oid in refs.items():
            path = git / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(oid + '\n')
    fetch = git / 'FETCH_HEAD'
    fetch.write_text(local + '\t\tbranch\n')
    stamp = dt.datetime.now(dt.timezone.utc).timestamp() - age_hours * 3600
    os.utime(fetch, (stamp, stamp))
    return git


def snapshot(dept):
    return {str(p.relative_to(dept)): ('link', os.readlink(p)) if p.is_symlink()
            else ('dir',) if p.is_dir() else ('file', p.read_bytes())
            for p in dept.rglob('*')}


@pytest.fixture(autouse=True)
def nonroot(monkeypatch):
    # Simulate the owner for in-process fixtures, including Linux root CI.
    monkeypatch.setattr(child.os, 'geteuid', lambda: 501)


@pytest.fixture
def shell_env(tmp_path):
    """Expose installed PyYAML to isolated test subprocesses; install nothing.

    The workstation has only user-site PyYAML. Production must provide it to
    python3 -I. This shim retains -I, adds only that installed dependency path,
    and simulates a nonroot owner if offline CI itself runs as root.
    """
    bin_dir = tmp_path / 'test bin'
    bin_dir.mkdir()
    bootstrap = (
        'import os,sys,runpy; '
        f'sys.path.append({str(Path(yaml.__file__).parent.parent)!r}); '
        'os.geteuid = (lambda: 501) if os.geteuid() == 0 else os.geteuid; '
        'sys.argv.pop(0); '
        'sys.argv.pop(0) if sys.argv[0] == "-I" else None; '
        'script=sys.argv[0]; '
        'exec(compile(sys.stdin.read(), "<stdin>", "exec")) if script == "-" '
        'else runpy.run_path(script, run_name="__main__")'
    )
    python = bin_dir / 'python3'
    python.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' -I -c ' + shlex.quote(bootstrap) + ' "$@"\n')
    python.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               HOME=str(tmp_path / 'home with & spaces'))
    return env


def run_mac(dept, env, *args):
    return subprocess.run(['/bin/bash', str(ROOT / 'scripts/mac-export-kpis.sh'),
                           '--dept-dir', str(dept), '--transcripts-dir', str(dept.parent / 'sessions with spaces'),
                           *args], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('kind,expected,code', [
    ('present', 'written', 0), ('missing', 'export_missing', 0),
    ('paused', 'skipped:not-live', 0), ('no-l4', 'skipped:no-l4', 0),
    ('invalid', 'skipped:invalid-manifest', 0),
    ('symlink', 'error:symlink-management-export.yaml', 1),
    ('fifo', 'error:nonregular-management-export.yaml', 1)])
def test_mac_runner_status_exit_and_model_bytes(tmp_path, shell_env, kind, expected, code):
    dept = tmp_path / 'department with spaces'
    manifest(dept, status='paused' if kind == 'paused' else 'live', layers=() if kind == 'no-l4' else (4,))
    path = export(dept)
    before = path.read_bytes()
    if kind == 'missing':
        path.unlink()
    elif kind == 'invalid':
        (dept / 'dept.yaml').write_text('[broken')
    elif kind == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(before)
        path.unlink()
        path.symlink_to(outside)
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path)
    result = run_mac(dept, shell_env, '--day', DAY)
    assert result.returncode == code, result.stderr
    assert result.stdout == json.dumps({'status': expected}, separators=(',', ':')) + '\n'
    if kind not in ('missing', 'fifo'):
        assert path.read_bytes() == before
    sidecar = path.with_name('management-kpis.yaml')
    assert sidecar.exists() is (kind in ('present', 'missing'))


def test_mac_also_yesterday_two_sidecars_and_both_exports_unchanged(tmp_path, shell_env):
    dept = tmp_path / 'content with spaces'
    manifest(dept)
    paths = [export(dept, date) for date in ('2026-03-01', '2026-02-28')]
    before = [p.read_bytes() for p in paths]
    result = run_mac(dept, shell_env, '--day', '2026-03-01', '--also-yesterday')
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)['status'] for line in result.stdout.splitlines()] == ['written', 'written']
    assert [p.read_bytes() for p in paths] == before
    for path in paths:
        doc = yaml.safe_load(path.with_name('management-kpis.yaml').read_text())
        assert doc['date'] == path.parent.parent.name
    assert not list(dept.rglob('.management-kpis.*'))


def test_mac_default_day_is_paris_and_dry_run_changes_nothing(tmp_path, shell_env):
    dept = tmp_path / 'content'
    manifest(dept)
    today = dt.datetime.now(ZoneInfo('Europe/Paris')).date()
    path = export(dept, today.isoformat())
    before = snapshot(dept)
    result = run_mac(dept, shell_env, '--also-yesterday', '--dry-run')
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)['status'] for line in result.stdout.splitlines()] == ['written', 'export_missing']
    assert snapshot(dept) == before
    assert path.read_bytes().startswith(b'# retain')


def test_mac_error_still_processes_other_day(tmp_path, shell_env):
    dept = tmp_path / 'content'
    manifest(dept)
    path = export(dept)
    path.unlink()
    path.symlink_to(tmp_path / 'absent')
    result = run_mac(dept, shell_env, '--day', DAY, '--also-yesterday')
    assert result.returncode == 1
    assert [json.loads(line)['status'] for line in result.stdout.splitlines()] == [
        'error:symlink-management-export.yaml', 'export_missing']
    assert (dept / 'outputs/2026-10-02/4/management-kpis.yaml').exists()


@pytest.mark.parametrize('args', [[], ['--dept-dir'], ['--dept-dir', 'foo'],
                                  ['--dept-dir', 'foo', '--transcripts-dir'],
                                  ['--dept-dir', 'foo', '--transcripts-dir', 'bar', '--unknown']])
def test_mac_required_arguments(shell_env, args):
    result = subprocess.run(['/bin/bash', str(ROOT / 'scripts/mac-export-kpis.sh'), *args],
                            env=shell_env, capture_output=True, timeout=30)
    assert result.returncode == 2


def test_macos_publication_path_exercised_on_all_platforms(tmp_path, monkeypatch):
    dept = tmp_path / 'content'
    manifest(dept)
    path = export(dept)
    cwd = os.getcwd()
    dirs = []
    original = child.tempfile.mkstemp
    def mkstemp(*args, **kwargs):
        dirs.append((kwargs['dir'], os.getcwd()))
        return original(*args, **kwargs)
    monkeypatch.setattr(child.sys, 'platform', 'darwin')
    monkeypatch.setattr(child.tempfile, 'mkstemp', mkstemp)
    assert child.child(dept, DAY) == 'written'
    assert dirs == [('.', str(path.parent.resolve()))]
    assert os.getcwd() == cwd
    assert stat.S_IMODE(path.with_name('management-kpis.yaml').stat().st_mode) == 0o644
    assert not list(path.parent.glob('.management-kpis.*'))


@pytest.mark.parametrize('present', [False, True])
def test_check_only_no_directory_or_file_changes(tmp_path, monkeypatch, present):
    dept = tmp_path / 'content'
    manifest(dept)
    git_state(dept)
    if present:
        export(dept).write_bytes(b'x' * (child.CAP + 1))
    before = snapshot(dept)
    monkeypatch.setattr(child, 'build', lambda *a, **k: pytest.fail('check-only tried KPI generation'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('check-only spawned a subprocess'))
    assert child.child(dept, DAY, check_only=True) == ('export_present' if present else 'export_missing')
    assert snapshot(dept) == before


@pytest.mark.parametrize('component', ['dept', 'outputs', DAY, '4', 'management-export.yaml',
                                      'summary.md', 'management-kpis.yaml'])
@pytest.mark.parametrize('kind', ['symlink', 'nonregular'])
def test_check_only_same_filesystem_refusals(tmp_path, component, kind):
    dept = tmp_path / 'content'
    manifest(dept)
    git_state(dept)
    path = export(dept)
    target = {'dept': dept, 'outputs': dept / 'outputs', DAY: dept / 'outputs' / DAY,
              '4': path.parent}.get(component, path.with_name(component))
    if target.exists():
        target.rename(target.with_name(target.name + '-saved'))
    if kind == 'symlink':
        target.symlink_to(tmp_path / 'absent')
    else:
        os.mkfifo(target)
    before = {str(p.relative_to(tmp_path)): os.lstat(p).st_mode for p in tmp_path.rglob('*')}
    expected = ('error:symlink-dept' if kind == 'symlink' else 'error:child-failed') if component == 'dept' else 'error:' + kind + '-' + target.name
    assert child.child(dept, DAY, check_only=True) == expected
    # Every filesystem refusal remains read-only (also when the dept itself is a FIFO).
    assert {str(p.relative_to(tmp_path)): os.lstat(p).st_mode for p in tmp_path.rglob('*')} == before
    assert not list(tmp_path.rglob('.management-kpis.*'))


@pytest.mark.parametrize('manifest_text,expected', [
    ('status: paused\nlayers: {subscribed: [4]}', 'skipped:not-live'),
    ('status: live\nlayers: {subscribed: [1]}', 'skipped:no-l4'),
    ('[broken', 'skipped:invalid-manifest')])
def test_check_only_eligibility(tmp_path, manifest_text, expected):
    dept = tmp_path / 'content'
    manifest(dept)
    (dept / 'dept.yaml').write_text(manifest_text)
    before = snapshot(dept)
    assert child.child(dept, DAY, check_only=True) == expected
    assert snapshot(dept) == before


def test_check_only_cli_reply(tmp_path, shell_env):
    dept = tmp_path / 'content'
    manifest(dept)
    git_state(dept)
    export(dept)
    before = snapshot(dept)
    result = subprocess.run(['python3', '-I', str(ROOT / 'scripts/lib/management_kpis.py'),
        '--dept-dir', str(dept), '--day', DAY, '--check-only'], env=shell_env,
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert fleet.parse_reply(result.returncode, result.stdout, check_only=True) == 'export_present'
    assert snapshot(dept) == before


def install(env, *args):
    return subprocess.run(['/bin/bash', str(ROOT / 'scripts/install-mac-export-kpis.sh'),
        '--slug', 'content', '--dept-dir', str(Path(env['HOME']) / 'content & <dept>'),
        '--transcripts-dir', str(Path(env['HOME']) / 'transcripts & spaces'), *args],
        env=env, capture_output=True, text=True, timeout=30)


def rendered(result):
    assert result.returncode == 0, result.stderr
    xml = result.stdout[:result.stdout.index('</plist>') + len('</plist>')]
    return xml, plistlib.loads(xml.encode())


def test_installer_dry_run_render_and_plutil(tmp_path, shell_env):
    before = snapshot(tmp_path)
    result = install(shell_env, '--framework-root', str(tmp_path / 'framework & spaces'), '--dry-run')
    xml, doc = rendered(result)
    assert snapshot(tmp_path) == before
    assert all('@' + name + '@' not in xml for name in ('SLUG', 'FRAMEWORK_ROOT', 'DEPT_DIR', 'TRANSCRIPTS_DIR', 'HOME'))
    assert doc['StartCalendarInterval'] == [dict(Hour=21, Minute=50), dict(Hour=23, Minute=30), dict(Hour=8, Minute=10)]
    assert 'KeepAlive' not in doc
    assert doc['ProgramArguments'][-3:] == [str(tmp_path / 'framework & spaces'),
        str(Path(shell_env['HOME']) / 'content & <dept>'), str(Path(shell_env['HOME']) / 'transcripts & spaces')]
    assert '--also-yesterday' in doc['ProgramArguments'][2]
    assert doc['EnvironmentVariables']['PATH'].startswith('/opt/homebrew/bin:/usr/local/bin:')
    assert str(Path(shell_env['HOME']) / '.local/bin') in doc['EnvironmentVariables']['PATH']
    assert doc['StandardOutPath'] == doc['StandardErrorPath'] == str(Path(shell_env['HOME']) / 'Library/Logs/export-kpis-content.log')
    assert 'launchctl print gui/' in result.stdout
    assert 'launchctl bootout gui/' in result.stdout
    assert 'launchctl bootstrap gui/' in result.stdout
    if shutil.which('plutil'):
        checked = subprocess.run(['plutil', '-lint', '-'], input=xml.encode(), capture_output=True, timeout=30)
        assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize('hour,extra', [('08', ['--also-yesterday']), ('10', ['--also-yesterday']), ('21', []), ('23', [])])
def test_plist_invocation_morning_and_evening(tmp_path, shell_env, hour, extra):
    framework = tmp_path / 'framework & spaces'
    (framework / 'scripts').mkdir(parents=True)
    runner = framework / 'scripts/mac-export-kpis.sh'
    runner.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    clock = tmp_path / 'clock'
    clock.write_text('#!/bin/bash\nprintf "%s\\n" ' + hour + '\n')
    clock.chmod(0o755)
    _, doc = rendered(install(shell_env, '--framework-root', str(framework), '--dry-run'))
    command = doc['ProgramArguments']
    command[2] = command[2].replace('/bin/date', shlex.quote(str(clock)))
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['--dept-dir', str(Path(shell_env['HOME']) / 'content & <dept>'),
                                         '--transcripts-dir', str(Path(shell_env['HOME']) / 'transcripts & spaces'), *extra]


def mock_launchctl(env, tmp_path, loaded=False, fail=False):
    path = Path(env['PATH'].split(os.pathsep)[0]) / 'launchctl'
    state = tmp_path / 'launch-state.json'
    state.write_text(json.dumps(dict(loaded=loaded, fail=fail, calls=[])))
    code = '''import json,sys
from pathlib import Path
p=Path(sys.argv[1]); doc=json.loads(p.read_text()); command=sys.argv[2:]
doc['calls'].append(command)
result=0
if command[0]=='print': result=0 if doc['loaded'] else 1
elif command[0]=='bootout': doc['loaded']=False
elif command[0]=='bootstrap':
    if doc['fail']: doc['fail']=False; result=1
    else: doc['loaded']=True
p.write_text(json.dumps(doc)); raise SystemExit(result)
'''
    path.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' -I -c ' + shlex.quote(code) + ' ' + shlex.quote(str(state)) + ' "$@"\n')
    path.chmod(0o755)
    return state


@pytest.mark.parametrize('loaded', [False, True])
def test_installer_bootout_only_loaded_and_bootstrap(tmp_path, shell_env, loaded):
    state = mock_launchctl(shell_env, tmp_path, loaded=loaded)
    result = install(shell_env)
    assert result.returncode == 0, result.stderr
    doc = json.loads(state.read_text())
    assert [c[0] for c in doc['calls']] == (['print', 'bootout', 'bootstrap'] if loaded else ['print', 'bootstrap'])
    assert doc['loaded'] is True
    plist = Path(shell_env['HOME']) / 'Library/LaunchAgents/com.bubble.export-kpis-content.plist'
    assert plistlib.loads(plist.read_bytes())['Label'] == 'com.bubble.export-kpis-content'


def test_installer_failed_replacement_restores_loaded_agent(tmp_path, shell_env):
    plist = Path(shell_env['HOME']) / 'Library/LaunchAgents/com.bubble.export-kpis-content.plist'
    plist.parent.mkdir(parents=True)
    old = plistlib.dumps(dict(Label='com.bubble.export-kpis-content', ProgramArguments=['/bin/true']))
    plist.write_bytes(old)
    state = mock_launchctl(shell_env, tmp_path, loaded=True, fail=True)
    result = install(shell_env)
    assert result.returncode != 0
    assert plist.read_bytes() == old
    doc = json.loads(state.read_text())
    assert [c[0] for c in doc['calls']] == ['print', 'bootout', 'bootstrap', 'print', 'bootstrap']
    assert doc['loaded'] is True


@pytest.fixture
def mirrors(tmp_path):
    agents = tmp_path / 'agents'
    agents.mkdir()
    mirror_root = tmp_path / 'mirrors'
    target = mirror_root / 'bubble-ops-content'
    manifest(target)
    git_state(target)
    (agents / 'bubble-ops-content').symlink_to(target, target_is_directory=True)
    state = tmp_path / 'state'
    env = dict(FLEET_EXPORT_MIRROR_ROOT=str(mirror_root), PYTHON_BIN=sys.executable)
    return agents, state, target, env


def fixture_stat(path):
    info = os.lstat(path)
    return SimpleNamespace(st_mode=info.st_mode, st_uid=501)


def fixture_mirror(entry, root, owner):
    return fleet.mirror_for(entry, root, owner, lstat=fixture_stat,
        lookup=lambda n: SimpleNamespace(pw_name='claude', pw_uid=n))


def fixture_real(entry, overrides):
    return fleet.owner_for(entry, overrides, lstat=fixture_stat,
        lookup=lambda n: SimpleNamespace(pw_name='agent-' + entry.name, pw_uid=n))


def run_mirrors(mirrors, report=DAY, *, due=True, morning=True, dry_run=False,
                runner=None, emitter=None, checker=fixture_mirror, mirror_max_age_hours=6):
    agents, state, _, env = mirrors
    def owner_child(command, **kwargs):
        assert kwargs == dict(timeout=120)
        dept = Path(command[command.index('--dept-dir') + 1])
        date = command[command.index('--day') + 1]
        return child.child(dept, date, check_only='--check-only' in command, dry_run='--dry-run' in command,
                           mirror_max_age_hours=float(command[command.index('--mirror-max-age-hours') + 1])
                           if '--check-only' in command else 6)
    return fleet.run_loop(agents, state, report, due, env=env, dry_run=dry_run,
        owner_check=fixture_real, mirror_check=checker, mirror_due=morning,
        mirror_max_age_hours=mirror_max_age_hours,
        child_runner=runner or owner_child, emit_runner=emitter or (lambda *a, **k: None))


@pytest.mark.parametrize('present', [False, True])
def test_mirror_child_alarm_and_no_writes(mirrors, present):
    _, state, target, _ = mirrors
    if present:
        export(target)
    before = snapshot(target)
    emitted = []
    assert run_mirrors(mirrors, emitter=lambda c, **k: emitted.append(c)) == 0
    assert snapshot(target) == before
    assert len(emitted) == int(not present)
    if not present:
        assert 'task=fleet-export-missing-content' in emitted[0]
        assert f'title=Missing daily management export: content since {DAY}' in emitted[0]
        assert json.loads((state / 'fleet-export-missing-content.mirror.json').read_text()) == dict(first_missing_day=DAY, last_alarm=DAY)


def test_mirror_one_card_across_days_recovery_and_later_outage(mirrors):
    _, state, target, _ = mirrors
    (target / 'dept.yaml').write_text('status: live\nlayers: {subscribed: [4]}\nsecret: EVIL-CONTENT\n')
    calls = []
    emitter = lambda c, **k: calls.append(c)
    for date in (DAY, '2026-10-04', '2026-10-05'):
        assert run_mirrors(mirrors, date, emitter=emitter) == 0
    assert len(calls) == 1
    assert 'EVIL-CONTENT' not in ' '.join(calls[0])
    export(target, '2026-10-06')
    assert run_mirrors(mirrors, '2026-10-06', emitter=emitter) == 0
    assert not list(state.glob('*.mirror.json'))
    assert run_mirrors(mirrors, '2026-10-07', emitter=emitter) == 0
    assert len(calls) == 2
    assert 'task=fleet-export-missing-content' in calls[1]
    assert 'title=Missing daily management export: content since 2026-10-07' in calls[1]


def test_mirror_failed_emit_retries_keep_first_day(mirrors):
    _, state, _, _ = mirrors
    calls = []
    def emit(command, **kwargs):
        assert kwargs['timeout'] == 60 and kwargs['check'] is True
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command)
    assert run_mirrors(mirrors, emitter=emit) == 1
    receipt = state / 'fleet-export-missing-content.mirror.json'
    assert json.loads(receipt.read_text()) == dict(first_missing_day=DAY, last_alarm=None)
    assert run_mirrors(mirrors, '2026-10-04', emitter=emit) == 0
    assert run_mirrors(mirrors, '2026-10-05', emitter=emit) == 0
    assert len(calls) == 2
    assert f'title=Missing daily management export: content since {DAY}' in calls[1]
    assert json.loads(receipt.read_text()) == dict(first_missing_day=DAY, last_alarm='2026-10-04')


@pytest.mark.parametrize('mode,uid,user,pw_uid,reason', [
    (0o40755, 0, 'claude', 0, 'uid-zero'),
    (0o40755, 501, 'wrong', 501, 'owner-name-mismatch'),
    (0o40755, 501, 'claude', 502, 'owner-uid-mismatch'),
    (0o120777, 501, 'claude', 501, 'not-directory')])
def test_mirror_owner_refusal(mirrors, mode, uid, user, pw_uid, reason):
    agents, _, target, env = mirrors
    entry = agents / 'bubble-ops-content'
    def lstat(path):
        return SimpleNamespace(st_mode=mode, st_uid=uid) if path == target else os.lstat(path)
    def lookup(n):
        assert n != 0
        return SimpleNamespace(pw_name=user, pw_uid=pw_uid)
    def checker(entry, root, owner):
        result = fleet.mirror_for(entry, root, owner, lstat=lstat, lookup=lookup)
        assert result == (None, None, reason)
        return result
    assert run_mirrors(mirrors, checker=checker,
        runner=lambda *a, **k: pytest.fail('refused owner dispatched')) == fleet.NOTHING_PROCESSED


def test_mirror_owner_configuration_is_separate(mirrors):
    agents, _, target, env = mirrors
    env['FLEET_EXPORT_MIRROR_OWNER'] = 'mirror-user'
    env['FLEET_EXPORT_OWNER_OVERRIDES'] = 'content=wrong'
    def checker(entry, root, owner):
        return fleet.mirror_for(entry, root, owner, lstat=fixture_stat,
            lookup=lambda n: SimpleNamespace(pw_name='mirror-user', pw_uid=n))
    commands = []
    assert run_mirrors(mirrors, checker=checker,
        runner=lambda c, **k: commands.append(c) or 'export_present') == 0
    assert commands[0][:4] == ['runuser', '-u', 'mirror-user', '--']
    assert commands[0][-1] == '--check-only'
    assert commands[0][4:7] == [sys.executable, '-I', str(ROOT / 'scripts/lib/management_kpis.py')]
    assert str(target) in commands[0]


def test_mirror_target_outside_root_ignored_without_target_lstat(mirrors, tmp_path):
    agents, _, _, env = mirrors
    entry = agents / 'bubble-ops-content'
    entry.unlink()
    outside = tmp_path / 'outside'
    manifest(outside)
    entry.symlink_to(outside)
    def checked_stat(path):
        assert path != outside
        return fixture_stat(path)
    def checker(entry, root, owner):
        return fleet.mirror_for(entry, root, owner, lstat=checked_stat)
    assert run_mirrors(mirrors, checker=checker,
        runner=lambda *a, **k: pytest.fail('outside target dispatched')) == fleet.NOTHING_PROCESSED


def test_mirror_relative_link_and_real_dept_precedence(mirrors):
    agents, _, target, _ = mirrors
    entry = agents / 'bubble-ops-content'
    entry.unlink()
    entry.symlink_to(os.path.relpath(target, agents))
    assert fixture_mirror(entry, target.parent, 'claude') == (target, 'claude', None)
    manifest(agents / 'content')
    commands = []
    assert run_mirrors(mirrors, runner=lambda c, **k: commands.append(c) or 'written') == 0
    assert len(commands) == 1
    assert '--check-only' not in commands[0]
    assert commands[0][commands[0].index('--dept-dir') + 1] == str(agents / 'content')


@pytest.mark.parametrize('morning,due', [(False, True), (True, False), (False, False)])
def test_mirror_only_in_morning_alarm_activation(mirrors, morning, due):
    assert run_mirrors(mirrors, morning=morning, due=due,
        checker=lambda *a: pytest.fail('mirror examined outside morning'),
        runner=lambda *a, **k: pytest.fail('mirror dispatched outside morning')) == fleet.NOTHING_PROCESSED


@pytest.mark.parametrize('now,requested,expected', [
    ('2026-10-04T00:05:00+02:00', None, False),
    ('2026-10-04T05:59:59+02:00', None, False),
    ('2026-10-04T06:00:00+02:00', None, True),
    ('2026-10-04T11:59:59+02:00', None, True),
    ('2026-10-04T12:00:00+02:00', DAY, False),
    ('2026-10-04T08:10:00+02:00', None, True),
    ('2026-10-04T21:50:00+02:00', None, False),
    ('2026-10-04T23:30:00+02:00', DAY, False),
    ('2026-10-04T08:10:00+02:00', '2026-10-02', False),
    ('2026-10-04T08:10:00+02:00', '2026-10-04', False),
    ('2026-10-04T06:10:00+00:00', DAY, True)])
def test_main_mirror_morning_wiring(monkeypatch, now, requested, expected):
    monkeypatch.setenv('FLEET_EXPORT_NOW', now)
    monkeypatch.setattr(sys, 'argv', ['fleet_export_check.py'] + (['--day', requested] if requested else []))
    calls = []
    monkeypatch.setattr(fleet, 'run_loop', lambda *a, **k: calls.append(k) or 0)
    assert fleet.main() == 0
    assert calls[0]['mirror_due'] is expected


def test_mirror_dry_run_has_no_state_or_output_writes(mirrors):
    _, state, target, _ = mirrors
    before = snapshot(target)
    assert run_mirrors(mirrors, dry_run=True,
        emitter=lambda *a, **k: pytest.fail('dry-run emitted')) == 0
    assert snapshot(target) == before
    assert not state.exists()


@pytest.mark.parametrize('status', ['export_missing', 'mirror_stale', 'export_present'])
def test_root_never_opens_or_follows_mirror_files(mirrors, monkeypatch, status):
    agents, _, target, _ = mirrors
    original_open = Path.open
    original_builtin_open = builtins.open
    original_os_open = os.open
    original_stat = os.stat
    metadata = []
    def forbidden(path):
        return isinstance(path, (str, os.PathLike)) and (
            str(path).startswith(str(target) + '/') or str(path).startswith(str(agents) + '/'))
    def path_open(path, *args, **kwargs):
        assert not forbidden(path), 'root opened mirror file'
        return original_open(path, *args, **kwargs)
    def builtin_open(path, *args, **kwargs):
        assert not forbidden(path), 'root opened mirror file'
        return original_builtin_open(path, *args, **kwargs)
    def os_open(path, *args, **kwargs):
        assert not forbidden(path), 'root opened mirror file'
        return original_os_open(path, *args, **kwargs)
    def os_stat(path, *args, **kwargs):
        assert not forbidden(path) and path != target, 'root followed mirror path'
        return original_stat(path, *args, **kwargs)
    def checked_lstat(path):
        assert path in (agents / 'bubble-ops-content', agents / 'content', target)
        metadata.append(path)
        return fixture_stat(path)
    def checker(entry, root, owner):
        return fleet.mirror_for(entry, root, owner, lstat=checked_lstat,
            lookup=lambda n: SimpleNamespace(pw_name='claude', pw_uid=n))
    monkeypatch.setattr(Path, 'open', path_open)
    monkeypatch.setattr(builtins, 'open', builtin_open)
    monkeypatch.setattr(os, 'open', os_open)
    monkeypatch.setattr(os, 'stat', os_stat)
    # Inject owner reply: this is the root process, not its owner child.
    assert run_mirrors(mirrors, checker=checker, runner=lambda *a, **k: status) == 0
    assert metadata == [agents / 'bubble-ops-content', agents / 'content', target]


def test_check_only_uid_zero_refused_before_read(tmp_path, monkeypatch):
    dept = tmp_path / 'content'
    manifest(dept)
    monkeypatch.setattr(child.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(child, 'read_file', lambda *a: pytest.fail('root child read a file'))
    assert child.child(dept, DAY, check_only=True) == 'error:uid-zero'


@pytest.mark.parametrize('status', ['skipped:not-live', 'skipped:no-l4',
                                  'skipped:invalid-manifest', 'error:child-failed',
                                  'written', 'untrusted model text'])
@pytest.mark.parametrize('stale', [False, True])
def test_mirror_skip_or_error_preserves_outage_receipt(mirrors, status, stale):
    _, state, target, _ = mirrors
    if stale:
        git_state(target, remote='b' * 40)
    assert run_mirrors(mirrors) == 0
    task = 'fleet-mirror-stale-content' if stale else 'fleet-export-missing-content'
    receipt = state / f'{task}.mirror.json'
    before = receipt.read_bytes()
    assert run_mirrors(mirrors, '2026-10-04', runner=lambda *a, **k: status,
        emitter=lambda *a, **k: pytest.fail('skip/error emitted')) == fleet.NOTHING_PROCESSED
    assert receipt.read_bytes() == before


def test_mirror_dry_run_present_keeps_existing_receipt(mirrors):
    _, state, target, _ = mirrors
    assert run_mirrors(mirrors) == 0
    export(target)
    before = snapshot(state)
    assert run_mirrors(mirrors, dry_run=True) == 0
    assert snapshot(state) == before


def test_mirror_failed_child_keeps_real_processed_exit_semantics(mirrors):
    agents, _, _, _ = mirrors
    manifest(agents / 'ben')
    def runner(command, **kwargs):
        return 'error:child-timeout' if '--check-only' in command else 'written'
    assert run_mirrors(mirrors, runner=runner) == 1


def test_mirror_rejected_real_owner_still_takes_precedence(mirrors):
    agents, state, _, env = mirrors
    manifest(agents / 'content')
    assert fleet.run_loop(agents, state, DAY, True, env=env, mirror_due=True,
        owner_check=lambda *a: (None, 'owner-name-mismatch'), mirror_check=fixture_mirror,
        child_runner=lambda *a, **k: pytest.fail('refused real dept double-processed')) == fleet.NOTHING_PROCESSED


def test_real_pass_rejects_check_only_status(mirrors):
    agents, _, _, _ = mirrors
    manifest(agents / 'ben')
    assert run_mirrors(mirrors, morning=False, runner=lambda *a, **k: 'export_present') == fleet.NOTHING_PROCESSED


@pytest.mark.parametrize('external_alias', [False, True])
def test_mirror_resolves_symlink_chain_with_directory_metadata_only(mirrors, tmp_path, external_alias):
    agents, _, target, _ = mirrors
    alias = (tmp_path if external_alias else target.parent) / 'alias'
    alias.symlink_to(target)
    entry = agents / 'bubble-ops-content'
    entry.unlink()
    entry.symlink_to(alias)
    metadata = []
    def lstat(path):
        metadata.append(path)
        assert path != alias
        return fixture_stat(path)
    def checker(entry, root, owner):
        return fleet.mirror_for(entry, root, owner, lstat=lstat,
            lookup=lambda n: SimpleNamespace(pw_name='claude', pw_uid=n))
    assert run_mirrors(mirrors, checker=checker) == 0
    assert metadata == [entry, agents / 'content', target]


def test_mirror_symlink_cycle_is_refused(mirrors):
    agents, _, target, _ = mirrors
    entry = agents / 'bubble-ops-content'
    entry.unlink()
    alias = target.parent / 'alias'
    alias.symlink_to(entry)
    entry.symlink_to(alias)
    assert fixture_mirror(entry, target.parent, 'claude') == (None, None, 'symlink-loop')
    assert run_mirrors(mirrors,
        runner=lambda *a, **k: pytest.fail('symlink loop dispatched')) == fleet.NOTHING_PROCESSED


def test_runner_prefers_framework_venv_and_fails_closed_without_yaml(tmp_path):
    """Fleet Macs: system python3 -I has no PyYAML; the framework .venv has.
    The runner must pick <framework>/.venv/bin/python3 when present, honour
    PYTHON_BIN, and exit non-zero with a clear status when no usable python
    exists (never run the child with a python that cannot import yaml)."""
    script = (ROOT / 'scripts/mac-export-kpis.sh').read_text()
    assert '"$framework_root/.venv/bin/python3"' in script
    assert script.index('PYTHON_BIN') < script.index('.venv/bin/python3') < script.index('py="python3"')
    fake = tmp_path / 'nopy'
    fake.write_text('#!/bin/bash\nexit 1\n')
    fake.chmod(0o755)
    dept = tmp_path / 'dept'
    dept.mkdir()
    result = subprocess.run(
        ['/bin/bash', str(ROOT / 'scripts/mac-export-kpis.sh'), '--dept-dir', str(dept),
         '--transcripts-dir', str(tmp_path), '--day', '2026-10-03'],
        env=dict(os.environ, PYTHON_BIN=str(fake)), capture_output=True, text=True)
    assert result.returncode == 1
    assert 'error:python-missing-yaml' in result.stdout
    assert list(dept.iterdir()) == []


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('present', [False, True])
def test_current_git_refs_allow_export_decision(tmp_path, monkeypatch, packed, present):
    dept = tmp_path / 'content'
    manifest(dept)
    git_state(dept, packed=packed, branch='feature/export')
    if present:
        export(dept)
    before = snapshot(dept)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('mirror child spawned a process'))
    assert child.child(dept, DAY, check_only=True) == ('export_present' if present else 'export_missing')
    assert snapshot(dept) == before


@pytest.mark.parametrize('kind', [
    'diverged', 'old-fetch', 'no-fetch', 'no-git', 'no-head', 'detached', 'malformed-head',
    'traversal-head', 'no-local', 'no-remote', 'malformed-local', 'malformed-remote',
    'malformed-packed', 'packed-diverged', 'symlink-head', 'symlink-ref-dir',
    'symlink-fetch', 'symlink-git', 'fifo-head', 'fifo-fetch', 'oversized-head',
    'unreadable-head', 'unreadable-ref', 'unreadable-fetch'])
def test_stale_git_metadata_read_only_no_subprocess(tmp_path, monkeypatch, kind):
    dept = tmp_path / 'content'
    manifest(dept)
    git = git_state(dept, remote='b' * 40 if kind in ('diverged', 'packed-diverged') else None,
                    age_hours=7 if kind == 'old-fetch' else 0,
                    packed=kind in ('malformed-packed', 'packed-diverged'))
    path = export(dept)
    # A stale mirror does not inspect export paths, even an unsafe one.
    path.unlink()
    path.symlink_to(tmp_path / 'missing')
    if kind == 'no-git':
        shutil.rmtree(git)
    elif kind.startswith('no-'):
        (git / {'no-head': 'HEAD', 'no-fetch': 'FETCH_HEAD', 'no-local': 'refs/heads/main',
                'no-remote': 'refs/remotes/origin/main'}[kind]).unlink()
    elif kind in ('detached', 'malformed-head', 'traversal-head', 'oversized-head'):
        (git / 'HEAD').write_text({'detached': 'a' * 40 + '\n', 'malformed-head': 'junk\n',
                                 'traversal-head': 'ref: refs/heads/../../outside\n',
                                 'oversized-head': 'x' * (child.CAP + 1)}[kind])
    elif kind in ('malformed-local', 'malformed-remote', 'malformed-packed'):
        (git / {'malformed-local': 'refs/heads/main', 'malformed-remote': 'refs/remotes/origin/main',
                'malformed-packed': 'packed-refs'}[kind]).write_text('bad commit\n')
    elif kind.startswith(('symlink-', 'fifo-')):
        target = {'symlink-head': git / 'HEAD', 'symlink-ref-dir': git / 'refs/heads',
                  'symlink-fetch': git / 'FETCH_HEAD', 'symlink-git': git,
                  'fifo-head': git / 'HEAD', 'fifo-fetch': git / 'FETCH_HEAD'}[kind]
        target.rename(target.with_name(target.name + '-saved'))
        if kind.startswith('symlink-'):
            target.symlink_to(tmp_path / 'outside')
        else:
            os.mkfifo(target)
    elif kind.startswith('unreadable-'):
        original_open = os.open
        denied = {'unreadable-head': 'HEAD', 'unreadable-ref': 'main',
                  'unreadable-fetch': 'FETCH_HEAD'}[kind]
        def unreadable(name, *args, **kwargs):
            if name == denied:
                raise PermissionError('fixture unreadable')
            return original_open(name, *args, **kwargs)
        monkeypatch.setattr(os, 'open', unreadable)
    before = {str(p.relative_to(dept)): os.lstat(p).st_mode for p in dept.rglob('*')}
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('mirror child spawned a process'))
    assert child.child(dept, DAY, check_only=True) == 'mirror_stale'
    assert {str(p.relative_to(dept)): os.lstat(p).st_mode for p in dept.rglob('*')} == before
    assert not list(dept.rglob('management-kpis.yaml'))


def test_mirror_age_threshold_cli_and_parent_forwarding(mirrors, shell_env):
    _, _, target, _ = mirrors
    git_state(target, age_hours=7)
    emitted = []
    assert run_mirrors(mirrors, mirror_max_age_hours=8, emitter=lambda c, **k: emitted.append(c)) == 0
    assert 'task=fleet-export-missing-content' in emitted[0]
    result = subprocess.run(['python3', '-I', str(ROOT / 'scripts/lib/management_kpis.py'),
        '--dept-dir', str(target), '--day', DAY, '--check-only', '--mirror-max-age-hours', '8'],
        env=shell_env, capture_output=True, timeout=30)
    assert result.returncode == 0
    assert fleet.parse_reply(0, result.stdout, check_only=True) == 'export_missing'
    result = subprocess.run(['python3', '-I', str(ROOT / 'scripts/lib/management_kpis.py'),
        '--dept-dir', str(target), '--day', DAY, '--check-only'],
        env=shell_env, capture_output=True, timeout=30)
    assert result.returncode == 0
    assert fleet.parse_reply(0, result.stdout, check_only=True) == 'mirror_stale'


@pytest.mark.parametrize('present', [False, True])
def test_stale_mirror_one_card_across_days_recovery_and_later_outage(mirrors, present):
    _, state, target, _ = mirrors
    git = git_state(target, remote='b' * 40)
    if present:
        for date in (DAY, '2026-10-04', '2026-10-05', '2026-10-06'):
            export(target, date)
    before = snapshot(target)
    emitted = []
    emitter = lambda c, **k: emitted.append(c)
    receipt = state / 'fleet-mirror-stale-content.mirror.json'
    for date in (DAY, '2026-10-04', '2026-10-05'):
        assert run_mirrors(mirrors, date, emitter=emitter) == 0
    assert snapshot(target) == before
    assert len(emitted) == 1
    assert 'task=fleet-mirror-stale-content' in emitted[0]
    assert f'title=VPS mirror of content is not updating since {DAY}' in emitted[0]
    assert 'Mac export status is unknown' in ' '.join(emitted[0])
    assert not (state / 'fleet-export-missing-content.mirror.json').exists()
    assert json.loads(receipt.read_text()) == dict(first_stale_day=DAY, last_alarm=DAY)
    git_state(target)
    assert run_mirrors(mirrors, '2026-10-06', emitter=emitter) == 0
    assert not receipt.exists()
    assert len(emitted) == (1 if present else 2)
    (git / 'FETCH_HEAD').unlink()
    assert run_mirrors(mirrors, '2026-10-07', emitter=emitter) == 0
    assert 'title=VPS mirror of content is not updating since 2026-10-07' in emitted[-1]
    assert json.loads(receipt.read_text()) == dict(first_stale_day='2026-10-07', last_alarm='2026-10-07')


def test_stale_emit_retry_preserves_first_day_and_missing_receipt(mirrors):
    _, state, target, _ = mirrors
    assert run_mirrors(mirrors) == 0
    missing = state / 'fleet-export-missing-content.mirror.json'
    before = missing.read_bytes()
    git_state(target, remote='b' * 40)
    calls = []
    def emit(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command)
    assert run_mirrors(mirrors, '2026-10-04', emitter=emit) == 1
    receipt = state / 'fleet-mirror-stale-content.mirror.json'
    assert json.loads(receipt.read_text()) == dict(first_stale_day='2026-10-04', last_alarm=None)
    assert run_mirrors(mirrors, '2026-10-05', emitter=emit) == 0
    assert run_mirrors(mirrors, '2026-10-06', emitter=emit) == 0
    assert len(calls) == 2
    assert all('task=fleet-mirror-stale-content' in command for command in calls)
    assert 'title=VPS mirror of content is not updating since 2026-10-04' in calls[1]
    assert missing.read_bytes() == before


@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('corrupt', ['json', 'shape', 'date', 'type', 'oversized', 'encoding', 'unreadable',
                                   'symlink', 'fifo'])
def test_invalid_mirror_receipt_is_absent_and_replaced(mirrors, monkeypatch, capsys, stale, corrupt):
    _, state, target, _ = mirrors
    state.mkdir()
    task = 'fleet-mirror-stale-content' if stale else 'fleet-export-missing-content'
    first_key = 'first_stale_day' if stale else 'first_missing_day'
    receipt = state / f'{task}.mirror.json'
    if stale:
        git_state(target, remote='b' * 40)
    data = {'json': b'{broken', 'shape': b'{}',
            'date': json.dumps({first_key: 'wrong date', 'last_alarm': None}).encode(),
            'type': json.dumps({first_key: None, 'last_alarm': None}).encode(),
            'oversized': b' ' * fleet.MAX_REPLY + b'{}', 'encoding': b'\xff',
            'unreadable': b'{}', 'symlink': b'{}', 'fifo': b'{}'}[corrupt]
    receipt.write_bytes(data)
    if corrupt in ('symlink', 'fifo'):
        receipt.unlink()
        if corrupt == 'symlink':
            outside = state / 'outside'
            outside.write_text('unchanged')
            receipt.symlink_to(outside)
        else:
            os.mkfifo(receipt)
    if corrupt == 'unreadable':
        original_open = Path.open
        def unreadable(path, *args, **kwargs):
            if path == receipt:
                raise PermissionError('fixture unreadable')
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', unreadable)
    emitted = []
    assert run_mirrors(mirrors, emitter=lambda c, **k: emitted.append(c)) == 0
    assert len(emitted) == 1 and f'task={task}' in emitted[0]
    assert capsys.readouterr().err.count('ignoring unreadable or invalid mirror receipt') == 1
    monkeypatch.undo()
    assert json.loads(receipt.read_text()) == {first_key: DAY, 'last_alarm': DAY}
    if corrupt == 'symlink':
        assert outside.read_text() == 'unchanged'


@pytest.mark.parametrize('stale', [False, True])
def test_stale_dry_run_never_changes_receipts_or_mirror(mirrors, stale):
    _, state, target, _ = mirrors
    git_state(target, remote='b' * 40)
    assert run_mirrors(mirrors) == 0
    if not stale:
        git_state(target)
        export(target)
    before = snapshot(state), snapshot(target)
    assert run_mirrors(mirrors, dry_run=True,
                       emitter=lambda *a, **k: pytest.fail('dry run emitted')) == 0
    assert (snapshot(state), snapshot(target)) == before


@pytest.mark.parametrize('status', ['export_present', 'mirror_stale'])
def test_mirror_status_parser_requires_mirror_mode(mirrors, status):
    raw = json.dumps({'status': status}).encode() + b'\n'
    assert fleet.parse_reply(0, raw, check_only=True) == status
    assert fleet.parse_reply(0, raw) == 'error:invalid-reply'
    assert fleet.parse_reply(1, raw, check_only=True) == 'error:child-exit'
    agents, _, _, _ = mirrors
    manifest(agents / 'ben')
    assert run_mirrors(mirrors, morning=False, runner=lambda *a, **k: status) == fleet.NOTHING_PROCESSED
    assert fleet.parse_reply(0, b'{"status":"written"}\n', check_only=True) == 'error:invalid-reply'
