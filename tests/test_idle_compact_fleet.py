"""Offline fleet safety and installer transactions; all host commands mocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/idle-compact'))
import idle_compact as compact
spec = importlib.util.spec_from_file_location('compact_install', ROOT / 'scripts/idle-compact/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
import test_idle_compact as pilot_tests
NOW = pilot_tests.NOW


class FleetSafetyTests(unittest.TestCase):
    # Reuse fixtures without collecting the pilot test methods a second time.
    setUp = pilot_tests.PilotTests.setUp
    assistant = pilot_tests.PilotTests.assistant
    user = pilot_tests.PilotTests.user
    write_rows = pilot_tests.PilotTests.write_rows
    save = pilot_tests.PilotTests.save
    check = pilot_tests.PilotTests.check
    assert_skip = pilot_tests.PilotTests.assert_skip
    def test_default_55_minutes_and_boundary(self):
        self.assertEqual(self.config.idle_min, 55)
        self.ledger_rows[0]['ts'] = compact.iso(NOW - 54 * 60)
        self.save()
        self.assert_skip('human_recent')
        self.ledger_rows[0]['ts'] = compact.iso(NOW - 55 * 60)
        self.save()
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_recent_attached_client_and_invalid_timestamp(self):
        original = self.runner
        for result in (str(int(NOW - 30)), 'missing', str(int(NOW + 1))):
            def runner(args):
                return result if args[1] == 'list-clients' else original(args)
            self.runner = runner
            outcome = compact.check(self.config, runner=runner, clock=lambda: NOW)
            self.assertEqual(outcome[0], 'SKIP')
            self.assertEqual(original.sends, [])
        self.runner = original

    def test_attached_quiet_client_allowed(self):
        original = self.runner
        def runner(args):
            return str(int(NOW - 7200)) if args[1] == 'list-clients' else original(args)
        self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW)[0], 'COMPACT_SENT')

    def test_client_types_during_second_capture(self):
        original = self.runner
        captures = []
        original.on_capture = lambda: captures.append(True)
        def runner(args):
            if args[1] == 'list-clients':
                return str(int(NOW - 1)) if len(captures) == 2 else ''
            return original(args)
        self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'attached_client_recent'))
        self.assertEqual(original.sends, [])

    def test_only_one_exact_claude_pane(self):
        original = self.runner
        for panes in ('%1 claude\n%2 claude\n', '%1 hermes\n', '%1 bash\n', ''):
            def runner(args):
                return panes if args[1] == 'list-panes' else original(args)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'no_dedicated_claude_pane'))
        self.assertEqual(original.sends, [])

    def test_tmux_binary_override_and_exact_session_lookup(self):
        self.config.tmux_bin = '/custom/bin/tmux'
        self.assertEqual(self.check()[0], 'COMPACT_SENT')
        self.assertTrue(all(call[0] == '/custom/bin/tmux' for call in self.runner.calls))
        self.assertTrue(all(call[call.index('-t') + 1] == '=ops-loop-rnd'
                            for call in self.runner.calls if call[1] in ('list-clients', 'list-panes')))

    def test_meeting_marker_and_declaration(self):
        marker = self.transcripts / 'meeting.active'
        self.config.meeting_marker = marker
        marker.touch()
        self.assert_skip('meeting_poll_declared')
        marker.unlink()
        entry = self.assistant()
        entry['message']['content'] = [{'type': 'tool_use', 'name': 'CronCreate',
                                       'input': {'prompt': 'MEETING POLL: read room'}}]
        self.entries = [entry]
        self.save()
        self.assert_skip('meeting_poll_declared')

    def test_old_cron_declaration_remains_blocked(self):
        entry = self.assistant(age=15000)
        entry['message']['content'] = [{'type': 'tool_use', 'name': 'CronCreate',
                                       'input': {'prompt': 'MEETING POLL: read room'}}]
        self.entries = [entry]
        self.save()
        self.assert_skip('meeting_poll_declared')

    def test_meeting_declaration_in_older_transcript_blocks(self):
        old = self.transcripts / 'previous.jsonl'
        self.write_rows(old, [self.user('MEETING POLL: read room', age=7000)])
        os.utime(old, (NOW - 500, NOW - 500))
        self.assert_skip('meeting_poll_declared')

    def test_meeting_or_harness_switch_during_capture(self):
        selector = self.transcripts / 'harness'
        selector.write_text('claude')
        self.config.harness_selector = selector
        self.runner.on_capture = lambda: selector.write_text('hermes')
        self.assert_skip('hermes_harness_skipped')

    def test_non_rick_machine_wake_is_not_human(self):
        self.entries = [self.user('Resume your OODA loop now'), self.user("Resume Maya's OODA loop now"), self.assistant()]
        self.save()
        self.assertEqual(self.check()[0], 'COMPACT_SENT')


class HostCommands:
    def __init__(self):
        self.calls = []
        self.loaded = False
        self.enabled = set()
        self.active = set()
        self.fail = None
        self.fail_once = False

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if self.fail and self.fail(args):
            if self.fail_once:
                self.fail = None
            raise subprocess.CalledProcessError(1, args)
        code, output = 0, ''
        if args[0] == 'launchctl':
            if args[1] == 'print':
                code = 0 if self.loaded else 1
            elif args[1] == 'bootstrap':
                self.loaded = True
            elif args[1] == 'bootout':
                self.loaded = False
        elif args[0] == 'systemctl':
            verb, unit = args[1], args[-1]
            if verb == 'is-enabled':
                code, output = (0, 'enabled') if unit in self.enabled else (1, 'disabled')
            elif verb == 'is-active':
                code = 0 if unit in self.active else 1
            elif verb == 'enable':
                self.enabled.add(unit)
                if '--now' in args:
                    self.active.add(unit)
            elif verb == 'disable':
                self.enabled.discard(unit)
            elif verb == 'start':
                self.active.add(unit)
            elif verb == 'stop':
                self.active.discard(unit)
        return SimpleNamespace(returncode=code, stdout=output)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.args = SimpleNamespace(platform='mac', slug='rnd', all=False, home=root / 'home & stuff',
                                    dept_dir=root / 'dept & work', transcript_dir=None, ledger=None,
                                    framework_root=ROOT, tmux_session='ops-loop-rnd', tmux_bin='tmux',
                                    dry_run=False, meeting_marker=None, selector_dir=root / 'selectors',
                                    config_dir=root / 'configs', unit_dir=root / 'units', agents_root=root / 'agents')
        self.args.dept_dir.mkdir()
        self.cmd = HostCommands()
        self.patch = patch.object(installer, 'command', self.cmd)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.uid_patch = patch.object(installer.os, 'geteuid', return_value=501)
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def mac_files(self):
        return (self.args.home / 'Library/LaunchAgents/com.bubble.idle-compact-rnd.plist',
                self.args.home / '.local/state/idle-compact/configs/rnd.json')

    def test_mac_render_escaping_defaults_and_idempotence(self):
        installer.install_mac(self.args)
        plist, config = self.mac_files()
        definition = plistlib.loads(plist.read_bytes())
        data = json.loads(config.read_text())
        self.assertEqual(definition['Label'], 'com.bubble.idle-compact-rnd')
        self.assertEqual(definition['StartInterval'], 300)
        self.assertNotIn('KeepAlive', definition)
        self.assertEqual(definition['ProgramArguments'][0], '/usr/bin/python3')
        self.assertEqual(data['runtime']['idle_min'], 55)
        self.assertEqual(data['runtime']['transcript_dir'], str(self.args.home / '.claude/projects' /
                         __import__('re').sub(r'[^A-Za-z0-9]', '-', str(self.args.dept_dir))))
        before = len(self.cmd.calls)
        installer.install_mac(self.args)
        self.assertEqual([call[0:2] for call in self.cmd.calls[before:]], [('plutil', '-lint'), ('launchctl', 'print')])

    def test_mac_overrides_and_installed_dry_run(self):
        self.args.transcript_dir = self.args.home / 'my transcripts'
        self.args.ledger = self.args.home / 'ledger.jsonl'
        self.args.dry_run = True
        installer.install_mac(self.args)
        data = json.loads(self.mac_files()[1].read_text())['runtime']
        self.assertEqual(data['transcript_dir'], str(self.args.transcript_dir))
        self.assertEqual(data['ledger'], str(self.args.ledger))
        self.assertTrue(data['dry_run'])

    def test_mac_bootstrap_failure_restores_loaded_definition_and_config(self):
        installer.install_mac(self.args)
        files = self.mac_files()
        previous = {path: path.read_bytes() for path in files}
        self.args.tmux_session = 'changed'
        self.cmd.fail = lambda args: args[:2] == ('launchctl', 'bootstrap')
        self.cmd.fail_once = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_mac(self.args)
        self.assertTrue(self.cmd.loaded)
        self.assertEqual(previous, {path: path.read_bytes() for path in files})

    def test_mac_first_install_failure_removes_new_files(self):
        self.cmd.fail = lambda args: args[:2] == ('launchctl', 'bootstrap')
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_mac(self.args)
        self.assertTrue(all(not path.exists() for path in self.mac_files()))

    def test_mac_partial_bootstrap_failure_removes_new_loaded_job(self):
        def fail(args):
            if args[:2] == ('launchctl', 'bootstrap'):
                self.cmd.loaded = True
                return True
            return False
        self.cmd.fail = fail
        self.cmd.fail_once = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_mac(self.args)
        self.assertFalse(self.cmd.loaded)
        self.assertTrue(all(not p.exists() for p in self.mac_files()))

    def test_mac_lint_failure_never_replaces_existing_job(self):
        installer.install_mac(self.args)
        previous = self.mac_files()[0].read_bytes()
        self.cmd.fail = lambda args: args[0] == 'plutil'
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_mac(self.args)
        self.assertEqual(self.mac_files()[0].read_bytes(), previous)
        self.assertTrue(self.cmd.loaded)

    def test_hermes_and_morty_skip_without_host_commands(self):
        self.args.selector_dir.mkdir()
        (self.args.selector_dir / 'harness-rnd').write_text('hermes')
        installer.install_mac(self.args)
        self.assertEqual(self.cmd.calls, [])
        self.args.slug = 'morty'
        installer.install_mac(self.args)
        self.assertEqual(self.cmd.calls, [])
        self.assertIn('hermes_harness', self.output.getvalue())

    def vps_setup(self):
        self.args.platform = 'vps'
        self.args.framework_root = Path('/opt/bubble-ops-loop')
        self.args.config_dir = Path('/etc/bubble-idle-compact')
        self.args.agents_root = Path('/srv/agents')
        self.args.dept_dir = None
        self.args.tmux_session = None
        self.uid_patch.stop()
        patcher = patch.object(installer.os, 'geteuid', return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        account = SimpleNamespace(pw_dir='/home/agent-rnd', pw_uid=1001, pw_gid=1001)
        pwd_patch = patch.object(installer.pwd, 'getpwnam', return_value=account)
        pwd_patch.start()
        self.addCleanup(pwd_patch.stop)
        # Redirect every production path to this test's local tree. No /etc or /home writes.
        old_read = Path.read_bytes
        old_exists = Path.exists
        old_write = installer.publish
        old_original = installer.original
        mapped_root = self.args.unit_dir.parent / 'vps_fs'
        mapped_root.mkdir()
        def mapped(path):
            if str(path).startswith(('/etc/', '/home/agent-', '/opt/bubble-ops-loop')):
                return mapped_root / str(path).lstrip('/')
            return path
        def read(path):
            if str(path).startswith('/opt/bubble-ops-loop/'):
                return old_read(ROOT / path.relative_to('/opt/bubble-ops-loop'))
            return old_read(mapped(path))
        patches = [patch.object(Path, 'read_bytes', read),
                   patch.object(Path, 'exists', lambda p: old_exists(mapped(p))),
                   patch.object(installer, 'publish', lambda p, data, mode=0o644: old_write(mapped(p), data, mode)),
                   patch.object(installer, 'original', lambda paths: old_original([mapped(p) for p in paths])),
                   patch.object(installer.os, 'chown'), patch.object(installer.os, 'chmod')]
        # Save-map must retain production keys for change detection and restore.
        patches[3] = patch.object(installer, 'original', lambda paths: {p: old_read(mapped(p)) if old_exists(mapped(p)) else None for p in paths})
        old_restore = installer.restore
        patches.append(patch.object(installer, 'restore', lambda saved: old_restore({mapped(p): v for p, v in saved.items()})))
        old_mkdir = Path.mkdir
        patches.append(patch.object(Path, 'mkdir', lambda p, *a, **kw: old_mkdir(mapped(p), *a, **kw)))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return mapped_root

    def test_vps_render_idempotence_and_run_as(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        service = (self.args.unit_dir / 'idle-compact@.service').read_text()
        self.assertIn('User=agent-%i', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertIn('PrivateTmp=false', service)
        config = json.loads((mapped / 'etc/bubble-idle-compact/rnd.json').read_text())
        self.assertEqual(config['runtime']['ledger'], '/home/agent-rnd/.claude/channels/telegram-rnd/delivery-ledger.jsonl')
        self.assertEqual(config['runtime']['transcript_dir'], '/home/agent-rnd/.claude/projects/-srv-agents-rnd')
        self.assertIn('idle-compact@rnd.timer', self.cmd.enabled)
        count = len(self.cmd.calls)
        installer.install_vps(self.args)
        self.assertTrue(all(call[1] in ('is-active', 'is-enabled') for call in self.cmd.calls[count:]))

    def test_vps_enable_failure_rolls_back_and_disables_new_timer(self):
        mapped = self.vps_setup()
        self.cmd.fail = lambda args: args[:3] == ('systemctl', 'enable', '--now')
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_vps(self.args)
        self.assertFalse((self.args.unit_dir / 'idle-compact@.service').exists())
        self.assertFalse((mapped / 'etc/bubble-idle-compact/rnd.json').exists())
        self.assertNotIn('idle-compact@rnd.timer', self.cmd.enabled)
        self.assertIn(('systemctl', 'disable', 'idle-compact@rnd.timer'), self.cmd.calls)

    def test_vps_partial_enable_failure_restores_timer_state(self):
        self.vps_setup()
        def fail(args):
            if args[:3] == ('systemctl', 'enable', '--now'):
                self.cmd.enabled.add(args[-1])
                self.cmd.active.add(args[-1])
                return True
            return False
        self.cmd.fail = fail
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_vps(self.args)
        self.assertFalse(self.cmd.enabled)
        self.assertFalse(self.cmd.active)

    def test_vps_update_failure_preserves_previously_enabled_timer(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        config = mapped / 'etc/bubble-idle-compact/rnd.json'
        previous = config.read_bytes()
        self.args.tmux_session = 'new-session'
        self.cmd.fail = lambda args: args[:2] == ('systemctl', 'daemon-reload')
        self.cmd.fail_once = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_vps(self.args)
        self.assertEqual(config.read_bytes(), previous)
        self.assertIn('idle-compact@rnd.timer', self.cmd.enabled)
        self.assertIn('idle-compact@rnd.timer', self.cmd.active)

    def test_vps_hermes_skip(self):
        self.vps_setup()
        self.args.selector_dir.mkdir()
        (self.args.selector_dir / 'rnd').write_text('hermes')
        installer.install_vps(self.args)
        self.assertEqual(self.cmd.calls, [])

    def test_vps_all_skips_hermes_and_enables_each_claude(self):
        self.vps_setup()
        self.args.all = True
        self.args.slug = None
        self.args.selector_dir.mkdir()
        (self.args.selector_dir / 'maya').write_text('hermes')
        self.args.agents_root = Path('/srv/agents')
        old_iterdir = Path.iterdir
        def iterdir(path):
            if path == Path('/srv/agents'):
                return iter([SimpleNamespace(name=slug, is_dir=lambda: True) for slug in ('rnd', 'maya', 'morty')])
            return old_iterdir(path)
        with patch.object(Path, 'iterdir', iterdir):
            installer.install_vps(self.args)
        self.assertEqual(self.cmd.enabled, {'idle-compact@rnd.timer'})
        self.assertIn('SKIP slug=maya reason=hermes_harness', self.output.getvalue())
        self.assertIn('SKIP slug=morty reason=hermes_harness', self.output.getvalue())

    def test_mac_bootout_failure_preserves_job(self):
        installer.install_mac(self.args)
        previous = {p: p.read_bytes() for p in self.mac_files()}
        self.args.tmux_session = 'changed'
        self.cmd.fail = lambda args: args[:2] == ('launchctl', 'bootout')
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_mac(self.args)
        self.assertEqual(previous, {p: p.read_bytes() for p in self.mac_files()})
        self.assertTrue(self.cmd.loaded)


class StatusTests(unittest.TestCase):
    def test_status_reports_decisions_compaction_and_disabled_timer(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            (root / 'log').write_text(compact.iso(NOW - 90000) + ' decision=SKIP reason=human_recent idle_minutes=2 context_tokens=210000\n' +
                                     compact.iso(NOW) + ' decision=SKIP reason=already_compacted_this_idle_period idle_minutes=60 context_tokens=210000\n')
            (root / 'state').write_text(json.dumps({'status': 'sent', 'last_compact_at': compact.iso(NOW - 90000)}))
            configs = root / 'configs'
            configs.mkdir()
            (configs / 'rnd.json').write_text(json.dumps(dict(slug='rnd', platform='vps', service='idle-compact@rnd.timer',
                                                             runtime=dict(state=str(root / 'state'), log=str(root / 'log')))))
            def runner(args, **kwargs):
                return SimpleNamespace(returncode=1 if args[1] == 'is-enabled' else 0, stdout='loaded\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                compact.fleet_status(configs, runner=runner)
            data = json.loads(output.getvalue())
            self.assertTrue(data['timer_installed'])
            self.assertFalse(data['timer_enabled'])
            self.assertTrue(data['agent_active'])
            self.assertTrue(data['agent_installed'])
            self.assertEqual(data['last_decision']['time'], compact.iso(NOW))
            self.assertEqual(data['last_compaction_time'], compact.iso(NOW - 90000))
            self.assertEqual(data['high_context_since'], compact.iso(NOW - 90000))

    def test_pending_send_preserves_prior_compaction_time_from_log(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            (root / 'log').write_text(compact.iso(NOW - 90000) + ' decision=COMPACT_SENT reason=all_conditions_met idle_minutes=60 context_tokens=210000\n')
            (root / 'state').write_text(json.dumps({'status': 'pending', 'last_compact_at': compact.iso(NOW)}))
            configs = root / 'configs'
            configs.mkdir()
            (configs / 'rnd.json').write_text(json.dumps(dict(slug='rnd', platform='vps', service='idle-compact@rnd.timer',
                                                             runtime=dict(state=str(root / 'state'), log=str(root / 'log')))))
            def runner(args, **kwargs):
                return SimpleNamespace(returncode=0, stdout='loaded\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                compact.fleet_status(configs, runner=runner)
            data = json.loads(output.getvalue())
            self.assertEqual(data['send_status'], 'pending')
            self.assertEqual(data['last_compaction_time'], compact.iso(NOW - 90000))

    def test_status_includes_unconfigured_agents(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp, contextlib.redirect_stdout(output):
            compact.fleet_status(Path(tmp), known_slugs=['maya'])
        data = json.loads(output.getvalue())
        self.assertEqual(data['slug'], 'maya')
        self.assertFalse(data['config_installed'])
        self.assertIsNone(data['timer_installed'])


if __name__ == '__main__':
    unittest.main()
