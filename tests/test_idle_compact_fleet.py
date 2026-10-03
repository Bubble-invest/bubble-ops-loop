"""Offline fleet safety and installer transactions; all host commands mocked."""
import contextlib
import importlib.util
import io
import json
import os
import stat
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
        for panes in ('%1 claude\n%2 claude\n', '%1 hermes\n', '%1 bash\n'):
            def runner(args):
                return panes if args[1] == 'list-panes' else original(args)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'no_dedicated_claude_pane'))
        self.assertEqual(original.sends, [])

    def test_claude_command_and_live_version_strings_require_model_and_gauge(self):
        original = self.runner
        for command in ('claude', '2.1.288', '2.1.283'):
            def runner(args):
                return '%1 ' + command + '\n' if args[1] == 'list-panes' else original(args)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW)[0], 'COMPACT_SENT')
            self.config.state.unlink()
        original.calls.clear()
        for command in ('zsh', 'bash', 'sh', 'login', 'python', 'vim', 'ssh', 'node', '2.1', '2.1.288-beta', 'hermes'):
            def runner(args):
                return '%1 ' + command + '\n' if args[1] == 'list-panes' else original(args)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'no_dedicated_claude_pane'))
        self.assertEqual(original.sends, [])
        for footer in ('x | ctx 54% left', 'repo | Opus 5.5', '⏵⏵ bypass permissions on'):
            original.pane = 'Done.\n────────────────\n❯ \n────────────────\n' + footer
            def runner(args):
                return '%1 2.1.288\n' if args[1] == 'list-panes' else original(args)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'unrecognized_pane_footer'))

    def test_no_tmux_session_has_explicit_content_free_log(self):
        for error in ("can't find session: private", 'no server running on /private/socket',
                      'error connecting to /private/socket (No such file or directory)'):
            def runner(args):
                raise subprocess.CalledProcessError(1, args, stderr=error)
            self.assertEqual(compact.check(self.config, runner=runner, clock=lambda: NOW), ('SKIP', 'no_tmux_session'))
            self.assertIn('decision=SKIP reason=no_tmux_session', self.config.log.read_text())
            self.assertNotIn('private', self.config.log.read_text())
        self.assertEqual(compact.check(self.config, runner=lambda args: '', clock=lambda: NOW), ('SKIP', 'no_tmux_session'))

    def test_generated_wakes_custom_patterns_and_real_humans(self):
        self.config.machine_wake_patterns = [r'^FLEET HEARTBEAT\b']
        for text in ("Resume Jean-Luc's OODA loop", "Resume Élodie's OODA loop", 'DUE_MISSIONS=[]',
                     'MEETING POLL: room', '[session-rotate, automated maintenance - not a human message] handoff',
                     '<task-notification>done</task-notification>', '<channel source="bubble-inject">wake</channel>',
                     'FLEET HEARTBEAT: wake'):
            row = self.user(text, age=15000)  # Wake text is automation, never a meeting declaration.
            self.assertFalse(compact.human_text(row, self.config.machine_wake_patterns))
            self.entries = [row, self.assistant()]
            self.save()
            self.assertEqual(self.check()[0], 'COMPACT_SENT')
            self.config.state.unlink()
        self.runner.calls.clear()
        self.entries = [self.user([{'type': 'text', 'text': '<task-notification>done'},
                                  {'type': 'text', 'text': 'Please help with this'}]), self.assistant()]
        self.save()
        self.assert_skip('human_recent')
        # Human ledger activity is authoritative, even if it quotes a generated prompt.
        self.entries = [self.user("Resume Rick's OODA loop"), self.assistant()]
        self.ledger_rows.append({'ts': compact.iso(NOW - 30), 'user_id': '6532205130'})
        self.save()
        self.assert_skip('human_recent')

    def test_invalid_wake_configuration_fails_closed(self):
        for patterns in ('wake', ['('], [''], ['.*'], [None]):
            self.config.machine_wake_patterns = patterns
            self.assert_skip('invalid_machine_wake_patterns')

    def test_split_channel_envelopes_and_outside_human_text(self):
        parts = [{'type': 'text', 'text': '<channel source="bubble-inject">'},
                 {'type': 'text', 'text': 'machine wake</channel>'}]
        self.assertFalse(compact.human_text(self.user(parts)))
        parts += [{'type': 'text', 'text': 'Please help with this'}]
        self.assertTrue(compact.human_text(self.user(parts)))

    def test_wake_patterns_load_from_agent_config_file(self):
        path = self.transcripts / 'config.json'
        runtime = dict(tmux_session=self.config.tmux_session, transcript_dir=str(self.transcripts),
                       ledger=str(self.config.ledger), state=str(self.config.state), log=str(self.config.log),
                       machine_wake_patterns=[r'^CUSTOM WAKE\b'])
        path.write_text(json.dumps({'runtime': runtime}))
        with patch.object(sys, 'argv', ['idle_compact.py', '--config', str(path)]), patch.object(compact, 'check') as check:
            compact.main()
        config = check.call_args.args[0]
        self.assertFalse(compact.human_text(self.user('CUSTOM WAKE: tick'), config.machine_wake_patterns))
        self.assertTrue(compact.human_text(self.user('Please help'), config.machine_wake_patterns))

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
        self.assertIn('idle_minutes=120.00 context_tokens=210000', self.config.log.read_text())
        marker.unlink()
        entry = self.assistant()
        entry['message']['content'] = [{'type': 'tool_use', 'name': 'CronCreate',
                                       'input': {'prompt': 'MEETING POLL: read room'}}]
        self.entries = [entry]
        self.save()
        self.assert_skip('meeting_poll_declared')

    def cron(self, name='CronCreate', inputs=None, age=600, call_id='create-1'):
        entry = self.assistant(age=age)
        entry['message']['content'] = [{'type': 'tool_use', 'id': call_id, 'name': name,
                                       'input': inputs if inputs is not None else
                                       {'prompt': ' \n MEETING POLL: read room', 'recurring': True}}]
        return entry

    def cron_result(self, content='Created job with ID: meeting-1', call_id='create-1'):
        return self.user([{'type': 'tool_result', 'tool_use_id': call_id, 'content': content}])

    def test_meeting_words_in_unrelated_content_never_declare(self):
        for content in (
                [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'cat <<EOF\nMEETING POLL\nEOF'}}],
                [{'type': 'text', 'text': 'MEETING POLL'}]):
            entry = self.assistant()
            entry['message']['content'] = content
            self.entries = [entry, self.cron_result('MEETING POLL: skill body'), self.assistant()]
            self.save()
            self.assertEqual(self.check()[0], 'COMPACT_SENT')
            self.config.state.unlink()
        # Recent user text remains human activity; it cannot declare a meeting.
        self.runner.calls.clear()
        self.entries = [self.user('The document contains MEETING POLL'), self.assistant()]
        self.save()
        self.assert_skip('human_recent')
        self.entries = [self.user('MEETING POLL: wake'), self.assistant()]
        self.save()
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_real_meeting_logs_idle_and_context(self):
        self.config.dry_run = True
        self.entries = [self.cron(), self.assistant()]
        self.save()
        self.assert_skip('meeting_poll_declared')
        self.assertIn('idle_minutes=120.00 context_tokens=210000', self.config.log.read_text())

    def test_only_assistant_cron_prompt_prefix_declares(self):
        entries = [self.cron(inputs={'prompt': 'A document about MEETING POLL', 'recurring': True}),
                   self.cron(inputs={'prompt': 'Resume your OODA loop', 'description': 'MEETING POLL'}),
                   self.user([{'type': 'tool_use', 'name': 'CronCreate', 'input': {'prompt': 'MEETING POLL'}}]),
                   self.cron()]
        entries[-1]['message']['role'] = 'user'
        for entry in entries:
            # Invalid user tool_use content still fails closed, but not as a meeting.
            self.entries = [entry, self.assistant()]
            self.save()
            outcome = self.check()
            self.assertNotEqual(outcome[1], 'meeting_poll_declared')
            if entry['type'] == 'assistant':
                self.assertEqual(outcome[0], 'COMPACT_SENT')
                self.config.state.unlink()

    def test_cron_delete_lifts_correlated_meeting(self):
        for result in ('Created job with ID: meeting-1', 'Created cron job meeting-1 (recurring)',
                       {'id': 'meeting-1'}, '{"job_id": "meeting-1"}',
                       [{'type': 'text', 'text': '{"id": "meeting-1"}'}]):
            self.entries = [self.cron(), self.cron_result(result),
                            self.cron('CronDelete', {'id': 'meeting-1'}, call_id='delete-1'), self.assistant()]
            self.save()
            self.assertEqual(self.check()[0], 'COMPACT_SENT')
            self.config.state.unlink()

    def test_unrelated_or_quoted_delete_cannot_lift_meeting(self):
        for deletion in (self.cron('CronDelete', {'id': 'other'}),
                         self.user('CronDelete meeting-1', age=7200),
                         self.cron('Bash', {'command': 'CronDelete meeting-1'})):
            self.entries = [self.cron(), self.cron_result(), deletion, self.assistant()]
            self.save()
            self.assert_skip('meeting_poll_declared')
        # An uncorrelated result cannot supply the id, nor can an earlier delete close a later create.
        self.entries = [self.cron('CronDelete', {'id': 'meeting-1'}), self.cron(),
                        self.cron_result(call_id='other'), self.assistant()]
        self.save()
        self.assert_skip('meeting_poll_declared')

    def test_multiple_meetings_require_matching_deletions(self):
        self.entries = [self.cron(), self.cron_result(), self.cron(call_id='create-2'),
                        self.cron_result({'id': 'meeting-2'}, call_id='create-2'),
                        self.cron('CronDelete', {'id': 'meeting-1'}), self.assistant()]
        self.save()
        self.assert_skip('meeting_poll_declared')

    def test_cron_declaration_expires_at_three_hours(self):
        for age in (10799, 10800, 15000):
            self.entries = [self.cron(age=age), self.assistant()]
            self.save()
            if age < 10800:
                self.assert_skip('meeting_poll_declared')
            else:
                self.assertEqual(self.check()[0], 'COMPACT_SENT')
                self.config.state.unlink()

    def test_meeting_declaration_in_older_session_does_not_block(self):
        old = self.transcripts / 'previous.jsonl'
        self.write_rows(old, [self.cron(age=7000)])
        os.utime(old, (NOW - 500, NOW - 500))
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_session_id_switch_expires_declaration(self):
        creation, last = self.cron(), self.assistant()
        creation['sessionId'], last['sessionId'] = 'old-session', 'new-session'
        self.entries = [creation, last]
        self.save()
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_quoted_wake_keywords_and_sources_are_human(self):
        self.config.machine_wake_patterns = [r'FLEET HEARTBEAT\b']
        for text in ('Explain DUE_MISSIONS=[]', 'Document MEETING POLL',
                     'Quote Resume your OODA loop', 'Explain FLEET HEARTBEAT: wake',
                     'Explain [session-rotate, automated maintenance]',
                     'Quote <channel source="bubble-inject">wake</channel>'):
            self.assertTrue(compact.human_text(self.user(text), self.config.machine_wake_patterns))
            self.entries = [self.user(text), self.assistant()]
            self.save()
            self.assert_skip('human_recent')
        self.assertTrue(compact.human_text(self.user('help', source='document-about-bubble-inject')))

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


class DtachSafetyTests(unittest.TestCase):
    assistant = pilot_tests.PilotTests.assistant
    user = pilot_tests.PilotTests.user
    write_rows = pilot_tests.PilotTests.write_rows
    save = pilot_tests.PilotTests.save

    def setUp(self):
        pilot_tests.PilotTests.setUp(self)
        self.config.transport = 'dtach'
        self.config.slug = 'rnd'
        self.config.dtach_socket = self.transcripts.parent / 'dtach.sock'
        self.config.dtach_socket.touch()
        socket_path = self.config.dtach_socket
        original_lstat = Path.lstat
        def lstat(path):
            info = original_lstat(path)
            if path == socket_path:
                return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid(),
                                       st_dev=info.st_dev, st_ino=info.st_ino)
            return info
        socket_stat = patch.object(Path, 'lstat', new=lstat)
        socket_stat.start()
        self.addCleanup(socket_stat.stop)
        self.processes = {
            101: dict(parent=1, name='dtach', argv=['/usr/bin/dtach', '-N', str(self.config.dtach_socket),
                      '/bin/sh', '-c', 'exec claude'], cgroup='0::/system.slice/bubble-agent@rnd.service\n'),
            102: dict(parent=101, name='claude', argv=['claude', '--dangerously-skip-permissions'],
                      cgroup='0::/system.slice/bubble-agent@rnd.service\n')}
        self.events = []
        self.on_sleep = None
        self.fail_payload = None
        proc = patch.object(compact, 'own_processes', side_effect=lambda: self.processes)
        proc.start()
        self.addCleanup(proc.stop)

    def sleep(self, seconds):
        self.events.append(('sleep', seconds))
        if self.on_sleep:
            self.on_sleep()

    def send(self, args, **kwargs):
        self.assertEqual(args, [self.config.dtach_bin, '-p', str(self.config.dtach_socket)])
        self.assertEqual(kwargs['timeout'], 5)
        self.assertTrue(kwargs['check'])
        self.assertTrue(kwargs['capture_output'])
        self.assertNotIn('shell', kwargs)
        payload = kwargs['input']
        self.assertIn(payload, (b'/compact', b'\r'))
        saved = json.loads(self.config.state.read_text())
        self.assertEqual(saved['status'], 'pending')
        self.events.append(('send', payload))
        if payload == self.fail_payload:
            raise subprocess.TimeoutExpired(args, 5, output='PRIVATE_SENTINEL')
        return SimpleNamespace(stdout=b'')

    def check(self):
        with patch.object(compact.subprocess, 'run', side_effect=self.send), \
             patch.object(compact.time, 'sleep', side_effect=self.sleep):
            before = len(self.config.log.read_text().splitlines()) if self.config.log.exists() else 0
            result = compact.check(self.config, runner=lambda _: self.fail('tmux used'), clock=lambda: self.now)
            self.assertEqual(len(self.config.log.read_text().splitlines()), before + 1)
            return result

    def assert_skip(self, reason):
        self.assertEqual(self.check(), ('SKIP', reason))
        self.assertFalse(any(event[0] == 'send' for event in self.events))

    def boundary(self, when):
        return {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': compact.iso(when)}

    def test_two_step_fixed_send_order_delay_and_state(self):
        self.config.dtach_bin = '/custom/dtach'
        self.assertEqual(self.check(), ('COMPACT_SENT', 'all_conditions_met'))
        self.assertEqual(self.events, [('sleep', 0), ('send', b'/compact'), ('sleep', 1.5), ('send', b'\r')])
        saved = json.loads(self.config.state.read_text())
        self.assertEqual(saved['status'], 'sent')
        self.assertEqual(saved['transport'], 'dtach')
        self.assertEqual(saved['transcript_path'], str(self.transcript))
        self.assertEqual(saved['transcript_size'], self.transcript.stat().st_size)
        self.events.clear()
        self.assert_skip('compact_cooldown')

    def test_quiet_by_mtime_and_row_timestamp(self):
        self.save(mtime=NOW - 119)
        self.assert_skip('transcript_not_quiet')
        self.entries = [self.assistant(age=119)]
        self.save()
        self.assert_skip('transcript_row_not_quiet')
        self.entries = [self.assistant(age=120)]
        self.save(mtime=NOW - 120)
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_only_completed_assistant_last_row(self):
        for row in (self.assistant(stop='tool_use'), self.assistant(stop=None),
                    self.user('<channel>wake</channel>'),
                    self.user([{'type': 'tool_result', 'content': 'done'}]),
                    {'type': 'unknown'}):
            self.entries = [self.assistant(), row]
            self.save()
            self.assert_skip('turn_not_finished')
        row = self.assistant()
        row['message']['role'] = 'user'
        self.entries = [row]
        self.save()
        self.assert_skip('turn_not_finished')
        row['message']['role'] = 'assistant'
        row['message']['content'] = [{'type': 'tool_use', 'name': 'Bash'}]
        self.save()
        self.assert_skip('turn_not_finished')

    def test_partial_json_and_complete_json_without_newline_fail_closed(self):
        for path in (self.transcript, self.config.ledger):
            self.save()
            path.write_bytes(path.read_bytes().rstrip(b'\n'))
            self.assert_skip('partial_transcript_write')
            self.save()
            with path.open('a') as handle:
                handle.write('{"incomplete":')
            self.assert_skip('partial_transcript_write')

    def test_newest_session_mtime_and_metadata(self):
        old = self.transcripts / 'zzzz.jsonl'
        self.write_rows(old, [self.assistant(tokens=100000)])
        os.utime(old, (NOW - 1000, NOW - 1000))
        self.entries += [{'type': 'last-prompt'}, {'type': 'system', 'subtype': 'turn_duration'}]
        self.save()
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_subagent_writing_in_each_layout_blocks(self):
        for relative in ('current/subagents/agent-a.jsonl', 'current/subagents/nested/agent-b.jsonl',
                         'agent-a.jsonl', 'subagents/agent-a.jsonl'):
            path = self.transcripts / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('background\n')
            os.utime(path, (NOW - 1, NOW - 1))
            self.assert_skip('subagent_not_quiet')
            os.utime(path, (NOW - 120, NOW - 120))
            self.config.dry_run = True
            self.assertEqual(self.check()[0], 'WOULD COMPACT')
            path.unlink()

    def test_unrelated_session_subagent_does_not_block(self):
        path = self.transcripts / 'other/subagents/agent-a.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('working\n')
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_human_context_meeting_and_harness_gates_are_shared(self):
        self.ledger_rows.append({'ts': compact.iso(NOW - 1), 'user_id': '6532205130'})
        self.save()
        self.assert_skip('human_recent')
        self.ledger_rows.pop()
        self.entries = [self.assistant(tokens=199999)]
        self.save()
        self.assert_skip('context_small')
        self.entries = [self.assistant()]
        self.save()
        marker = self.transcripts / 'meeting'
        self.config.meeting_marker = marker
        marker.touch()
        self.assert_skip('meeting_poll_declared')
        marker.unlink()
        entry = FleetSafetyTests.cron(self)
        self.entries = [entry, self.assistant()]
        self.save()
        self.assert_skip('meeting_poll_declared')
        selector = self.transcripts / 'harness'
        selector.write_text('hermes')
        self.config.harness_selector = selector
        self.assert_skip('hermes_harness_skipped')

    def test_socket_missing_not_socket_foreign_owned_and_symlink(self):
        path = self.config.dtach_socket
        self.config.dtach_socket = path.with_name('missing')
        self.assert_skip('dtach_socket_missing')
        self.config.dtach_socket.touch()
        self.assert_skip('dtach_not_socket')
        self.config.dtach_socket.unlink()
        self.config.dtach_socket.symlink_to(path)
        self.assert_skip('dtach_not_socket')
        self.config.dtach_socket = path
        original = Path.lstat
        with patch.object(Path, 'lstat', new=lambda p: SimpleNamespace(st_mode=original(p).st_mode,
                   st_uid=os.getuid() + 1) if p == path else original(p)):
            self.assert_skip('dtach_socket_foreign_owner')

    def test_dead_wrong_socket_wrong_unit_or_ambiguous_master(self):
        original = self.processes.copy()
        self.processes = {}
        self.assert_skip('dtach_master_not_alive')
        self.processes = original
        self.processes[101]['argv'][2] += '-other'
        self.assert_skip('dtach_master_not_alive')
        self.processes[101]['argv'][2] = str(self.config.dtach_socket)
        self.processes[101]['cgroup'] = '0::/system.slice/bubble-agent@maya.service\n'
        self.assert_skip('dtach_master_not_alive')
        self.processes[101]['cgroup'] = '0::/system.slice/bubble-agent@rnd.service\n'
        self.processes[103] = self.processes[101].copy()
        self.assert_skip('dtach_master_not_alive')

    def test_claude_child_must_be_alive_and_owned_by_master(self):
        child = self.processes.pop(102)
        self.assert_skip('dtach_claude_child_not_alive')
        self.processes[102] = child
        child['parent'] = 42
        self.assert_skip('dtach_claude_child_not_alive')
        child.update(parent=101, name='sh', argv=['/bin/sh', '-c', 'exec claude'])
        self.assert_skip('dtach_claude_child_not_alive')

    def test_main_or_ledger_fingerprint_change_before_send(self):
        for path in (self.transcript, self.config.ledger):
            self.save()
            self.on_sleep = lambda: path.write_text(path.read_text() + '\n')
            self.assert_skip('inputs_changed_before_send')
            self.assertFalse(self.config.state.exists())

    def test_subagent_appears_or_changes_during_recheck(self):
        path = self.transcripts / 'current/subagents/agent-a.jsonl'
        path.parent.mkdir(parents=True)
        self.on_sleep = lambda: path.write_text('working\n')
        self.assert_skip('inputs_changed_before_send')
        os.utime(path, (NOW - 300, NOW - 300))
        self.assert_skip('inputs_changed_before_send')

    def test_socket_or_master_replaced_before_send(self):
        def replace_master():
            self.processes[201] = self.processes.pop(101)
            self.processes[102]['parent'] = 201
        self.on_sleep = replace_master
        self.assert_skip('dtach_changed_before_send')

    def test_meeting_starts_during_recheck(self):
        marker = self.transcripts / 'meeting'
        self.config.meeting_marker = marker
        self.on_sleep = marker.touch
        self.assert_skip('meeting_poll_declared')

    def test_final_fingerprint_after_reservation(self):
        original = compact.write_state
        def mutate(path, saved):
            original(path, saved)
            with self.transcript.open('a') as handle:
                handle.write('\n')
        with patch.object(compact, 'write_state', side_effect=mutate):
            self.assert_skip('inputs_changed_before_send')
        self.assertEqual(json.loads(self.config.state.read_text())['status'], 'pending')

    def test_partial_send_timeout_stays_pending_and_never_retries(self):
        self.fail_payload = b'\r'
        self.assertEqual(self.check(), ('SKIP', 'io_parse_or_tmux_error'))
        self.assertEqual(json.loads(self.config.state.read_text())['status'], 'pending')
        self.events.clear()
        self.assert_skip('previous_send_uncertain_check_state')
        self.assertNotIn('PRIVATE_SENTINEL', self.config.log.read_text())

    def test_dry_run_never_writes_compaction_state(self):
        self.config.dry_run = True
        self.assertEqual(self.check(), ('WOULD COMPACT', 'all_conditions_met'))
        self.assertFalse(self.config.state.exists())
        self.assertFalse(any(event[0] == 'send' for event in self.events))

    def test_unconfirmed_logs_once_and_does_not_retry(self):
        self.check()
        self.events.clear()
        self.now += 599
        self.assert_skip('compact_cooldown')
        self.now += 1
        self.assertEqual(self.check(), ('COMPACT_UNCONFIRMED', 'compact_boundary_timeout'))
        self.assertTrue(json.loads(self.config.state.read_text())['unconfirmed_logged'])
        self.now += 300
        self.assert_skip('compact_cooldown')
        self.now = NOW + 3600
        self.assert_skip('context_not_regrown_since_send')
        self.assertEqual(self.config.log.read_text().count('decision=COMPACT_UNCONFIRMED'), 1)

    def test_custom_confirmation_deadline(self):
        self.config.confirm_min = 2
        self.check()
        self.now += 120
        self.assertEqual(self.check()[0], 'COMPACT_UNCONFIRMED')

    def test_boundary_confirms_reserved_transcript_after_session_rotation(self):
        self.check()
        self.entries.append(self.boundary(NOW + 65))
        self.save(mtime=NOW + 65)
        new = self.transcripts / 'new-session.jsonl'
        self.write_rows(new, [self.assistant(tokens=100000, age=-70)])
        os.utime(new, (NOW + 70, NOW + 70))
        self.now += 900
        self.check()
        saved = json.loads(self.config.state.read_text())
        self.assertEqual(saved['compact_confirmed_at'], compact.iso(NOW + 65))
        self.assertNotIn('COMPACT_UNCONFIRMED', self.config.log.read_text())

    def test_missing_malformed_replaced_or_late_boundary_is_unconfirmed(self):
        for case in ('missing', 'malformed', 'replaced', 'late', 'summary'):
            self.config.state.unlink(missing_ok=True)
            self.now = NOW
            self.entries = [self.assistant()]
            self.save()
            self.check()
            if case == 'missing':
                self.transcript.unlink()
            elif case == 'malformed':
                with self.transcript.open('a') as handle:
                    handle.write('not json\n')
            elif case == 'replaced':
                replacement = self.transcript.with_suffix('.tmp')
                self.write_rows(replacement, self.entries + [self.boundary(NOW + 65)])
                replacement.replace(self.transcript)
            else:
                self.entries.append(self.boundary(NOW + 601) if case == 'late' else
                                    self.user('summary', age=-65, isCompactSummary=True))
                self.save(mtime=NOW + 65)
            self.now = NOW + 900
            self.assertEqual(self.check()[0], 'COMPACT_UNCONFIRMED')

    def test_repeat_uses_shared_reduction_regrowth_and_cooldown_rules(self):
        self.check()
        self.events.clear()
        self.entries += [self.boundary(NOW + 65), self.assistant(age=-3000)]
        self.now = NOW + 3200
        self.save(mtime=self.now - 180)
        self.assert_skip('compact_cooldown')
        self.now = NOW + 3600
        self.assertEqual(self.check()[0], 'COMPACT_SENT')
        self.events.clear()
        self.now += 3600
        self.check()  # Record timeout for the second send, whose boundary is absent.
        self.assert_skip('context_not_regrown_since_send')
        self.entries += [self.assistant(tokens=50000, age=-(self.now - NOW) + 300),
                         self.assistant(age=-(self.now - NOW) + 180)]
        self.save(mtime=self.now - 180)
        self.assertEqual(self.check()[0], 'COMPACT_SENT')

    def test_payload_guard(self):
        with self.assertRaises(compact.Unsafe), patch.object(compact.subprocess, 'run') as run:
            compact.write_dtach(self.config, b'/compact\n')
        run.assert_not_called()

    def test_corrupt_confirmation_metadata_fails_closed(self):
        self.check()
        saved = json.loads(self.config.state.read_text())
        self.events.clear()
        for key, invalid in (('transcript_size', -1), ('transcript_identity', []),
                             ('transcript_path', None), ('unconfirmed_logged', 'true')):
            self.config.state.write_text(json.dumps(dict(saved, **{key: invalid})))
            self.assert_skip('invalid_state')

    def test_socket_inode_changes_before_send(self):
        original = Path.lstat
        changed = []
        def lstat(path):
            info = original(path)
            if path == self.config.dtach_socket and changed:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid, st_dev=info.st_dev,
                                       st_ino=info.st_ino + 1)
            return info
        self.on_sleep = lambda: changed.append(True)
        with patch.object(Path, 'lstat', new=lstat):
            self.assert_skip('dtach_changed_before_send')

    def test_inputs_change_during_inspection(self):
        original = compact.rows
        def mutate(path, **kwargs):
            yield from original(path, **kwargs)
            if path == self.transcript:
                with path.open('a') as handle:
                    handle.write('\n')
        with patch.object(compact, 'rows', side_effect=mutate):
            self.assert_skip('inputs_changed_during_read')

    def test_dry_run_preserves_existing_sent_state_even_after_timeout(self):
        self.check()
        saved = self.config.state.read_bytes()
        self.config.dry_run = True
        self.now += 900
        self.events.clear()
        self.assert_skip('compact_cooldown')
        self.assertEqual(self.config.state.read_bytes(), saved)


class ProcTests(unittest.TestCase):
    def test_proc_reader_filters_foreign_uids_zombies_and_parses_cmdline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, uid, state in ((101, os.getuid(), 'S'), (102, os.getuid() + 1, 'S'),
                                    (103, os.getuid(), 'Z')):
                path = root / str(pid)
                path.mkdir()
                (path / 'status').write_text('Name:\tclaude\nState:\t' + state + ' (state)\nPPid:\t1\nUid:\t' +
                                           '\t'.join([str(uid)] * 4) + '\n')
                (path / 'cmdline').write_bytes(b'/usr/bin/claude\0--continue\0')
                (path / 'cgroup').write_text('0::/system.slice/bubble-agent@rnd.service\n')
            (root / '104').mkdir()  # Exited between enumeration and reading.
            (root / 'self').mkdir()
            result = compact.own_processes(root)
            self.assertEqual(set(result), {101})
            self.assertEqual(result[101]['argv'], ['/usr/bin/claude', '--continue'])
            self.assertEqual(result[101]['parent'], 1)

    def test_malformed_proc_data_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '101').mkdir()
            (root / '101/status').write_text('Name:\tclaude\n')
            with self.assertRaisesRegex(compact.Unsafe, 'invalid_dtach_process_record'):
                compact.own_processes(root)


class DtachCliTests(unittest.TestCase):
    def test_cli_dtach_defaults_socket_and_allows_no_tmux_session(self):
        argv = ['idle_compact.py', '--transport', 'dtach', '--slug', 'maya', '--transcript-dir', '/t',
                '--ledger', '/l', '--state', '/s', '--log', '/log', '--dtach-bin', '/custom/dtach',
                '--confirm-min', '3', '--dry-run']
        with patch.object(sys, 'argv', argv), patch.object(compact, 'check') as check:
            compact.main()
        config = check.call_args.args[0]
        self.assertEqual(config.tmux_session, '')
        self.assertEqual(config.transport, 'dtach')
        self.assertEqual(config.dtach_socket, Path('/run/bubble-agent-maya/dtach.sock'))
        self.assertEqual(config.dtach_bin, '/custom/dtach')
        self.assertEqual(config.confirm_min, 3)
        self.assertTrue(config.dry_run)

    def test_config_transport_socket_and_uid_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'runtime': dict(transport='dtach', transcript_dir='/t', ledger='/l',
                            state='/s', log='/log', dtach_socket='/custom/dtach.sock')}))
            with patch.object(sys, 'argv', ['idle_compact.py', '--config', str(path)]), \
                 patch.object(compact.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='agent-maya')), \
                 patch.object(compact, 'check') as check:
                compact.main()
            config = check.call_args.args[0]
            self.assertEqual(config.slug, 'maya')
            self.assertEqual(config.dtach_socket, Path('/custom/dtach.sock'))

    def test_invalid_transport_config_or_relative_socket_fails_before_check(self):
        argv = ['idle_compact.py', '--transport', 'dtach', '--slug', 'maya', '--transcript-dir', '/t',
                '--ledger', '/l', '--state', '/s', '--log', '/log']
        for extra in (['--dtach-socket', 'relative'], ['--confirm-min', 'nan'], ['--confirm-min', '0']):
            with patch.object(sys, 'argv', argv + extra), patch.object(compact, 'check') as check, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                compact.main()
            check.assert_not_called()


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
                if '--now' in args:
                    self.active.discard(unit)
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

    def test_mac_restore_failure_still_rebootstraps_and_reports_both_errors(self):
        installer.install_mac(self.args)
        self.args.tmux_session = 'changed'
        self.cmd.fail = lambda args: args[:2] == ('launchctl', 'bootstrap')
        self.cmd.fail_once = True
        with patch.object(installer, 'restore', side_effect=OSError('private payload')):
            with self.assertRaises(installer.RollbackFailure) as caught:
                installer.install_mac(self.args)
        self.assertTrue(self.cmd.loaded)
        self.assertIn('original=CalledProcessError', str(caught.exception))
        self.assertIn('restore:OSError', str(caught.exception))
        self.assertNotIn('private payload', str(caught.exception))

    def test_mac_restore_and_rebootstrap_failures_are_both_reported(self):
        installer.install_mac(self.args)
        self.args.tmux_session = 'changed'
        self.cmd.fail = lambda args: args[:2] == ('launchctl', 'bootstrap')
        with patch.object(installer, 'restore', side_effect=OSError()):
            with self.assertRaises(installer.RollbackFailure) as caught:
                installer.install_mac(self.args)
        self.assertIn('restore:OSError,rebootstrap:CalledProcessError', str(caught.exception))
        self.assertEqual(sum(call[:2] == ('launchctl', 'bootstrap') for call in self.cmd.calls), 3)

    def test_mac_uninstall_retains_state_logs_and_is_idempotent(self):
        installer.install_mac(self.args)
        config = json.loads(self.mac_files()[1].read_text())['runtime']
        retained = [Path(config[key]) for key in ('state', 'log')]
        for path in retained:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('retained')
        self.args.config_dir = self.mac_files()[1].parent
        installer.uninstall_mac(self.args)
        self.assertFalse(self.cmd.loaded)
        self.assertTrue(all(not p.exists() for p in self.mac_files()))
        self.assertTrue(all(p.read_text() == 'retained' for p in retained))
        installer.uninstall_mac(self.args)

    def test_mac_reinstall_preserves_custom_wake_patterns(self):
        installer.install_mac(self.args)
        config = self.mac_files()[1]
        data = json.loads(config.read_text())
        data['runtime']['machine_wake_patterns'] = ['^CUSTOM WAKE']
        config.write_text(json.dumps(data))
        installer.install_mac(self.args)
        self.assertEqual(json.loads(config.read_text())['runtime']['machine_wake_patterns'], ['^CUSTOM WAKE'])

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
        old_read_text = Path.read_text
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
        patches.append(patch.object(Path, 'read_text', lambda p, *a, **kw: old_read_text(mapped(p), *a, **kw)))
        # Save-map must retain production keys for change detection and restore.
        patches[3] = patch.object(installer, 'original', lambda paths: {p: old_read(mapped(p)) if old_exists(mapped(p)) else None for p in paths})
        old_restore = installer.restore
        patches.append(patch.object(installer, 'restore', lambda saved: old_restore({mapped(p): v for p, v in saved.items()})))
        old_mkdir = Path.mkdir
        patches.append(patch.object(Path, 'mkdir', lambda p, *a, **kw: old_mkdir(mapped(p), *a, **kw)))
        old_unlink, old_glob = Path.unlink, Path.glob
        patches.append(patch.object(Path, 'unlink', lambda p, *a, **kw: old_unlink(mapped(p), *a, **kw)))
        patches.append(patch.object(Path, 'glob', lambda p, *a, **kw: old_glob(mapped(p), *a, **kw)))
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
        self.assertIn('ReadOnlyPaths=-/run/bubble-agent-%i', service)
        self.assertIn('ProtectHome=read-only', service)
        self.assertIn('ReadWritePaths=/home/agent-%i/.local/state/idle-compact', service)
        config = json.loads((mapped / 'etc/bubble-idle-compact/rnd.json').read_text())
        self.assertEqual(config['runtime']['ledger'], '/home/agent-rnd/.claude/channels/telegram-rnd/delivery-ledger.jsonl')
        self.assertEqual(config['runtime']['transcript_dir'], '/home/agent-rnd/.claude/projects/-srv-agents-rnd')
        self.assertEqual(config['runtime']['transport'], 'dtach')
        self.assertEqual(config['runtime']['slug'], 'rnd')
        self.assertEqual(config['runtime']['dtach_socket'], '/run/bubble-agent-rnd/dtach.sock')
        self.assertEqual(config['runtime']['dtach_bin'], '/usr/bin/dtach')
        self.assertEqual(config['runtime']['confirm_min'], 10)
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

    def test_vps_observe_and_dtach_overrides_render(self):
        mapped = self.vps_setup()
        self.args.dry_run = True
        self.args.dtach_socket = Path('/run/custom/dtach.sock')
        self.args.dtach_bin = '/custom/dtach'
        self.args.confirm_min = 4
        installer.install_vps(self.args)
        data = json.loads((mapped / 'etc/bubble-idle-compact/rnd.json').read_text())['runtime']
        self.assertTrue(data['dry_run'])
        self.assertEqual(data['transport'], 'dtach')
        self.assertEqual(data['dtach_socket'], '/run/custom/dtach.sock')
        self.assertEqual(data['dtach_bin'], '/custom/dtach')
        self.assertEqual(data['confirm_min'], 4)
        self.assertIn('idle-compact@rnd.timer', self.cmd.enabled)
        self.args.dry_run = False
        installer.install_vps(self.args)
        self.assertFalse(json.loads((mapped / 'etc/bubble-idle-compact/rnd.json').read_text())['runtime']['dry_run'])

    def test_vps_all_upgrades_transport_preserves_rules_and_forces_observe(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        config = mapped / 'etc/bubble-idle-compact/rnd.json'
        data = json.loads(config.read_text())
        data['runtime'].update(transport='tmux', transcript_dir='/custom/transcripts',
                               machine_wake_patterns=['^CUSTOM WAKE'], idle_min=80)
        del data['runtime']['dtach_socket']
        config.write_text(json.dumps(data))
        self.args.all, self.args.slug, self.args.dry_run = True, None, True
        original = Path.iterdir
        with patch.object(Path, 'iterdir', new=lambda p: iter([SimpleNamespace(name='rnd', is_dir=lambda: True)])
                          if p == self.args.agents_root else original(p)):
            installer.install_vps(self.args)
        runtime = json.loads(config.read_text())['runtime']
        self.assertEqual(runtime['transport'], 'dtach')
        self.assertEqual(runtime['dtach_socket'], '/run/bubble-agent-rnd/dtach.sock')
        self.assertEqual(runtime['transcript_dir'], '/custom/transcripts')
        self.assertEqual(runtime['machine_wake_patterns'], ['^CUSTOM WAKE'])
        self.assertEqual(runtime['idle_min'], 80)
        self.assertTrue(runtime['dry_run'])

    def test_vps_restore_failure_still_attempts_reload_and_timer_recovery(self):
        self.vps_setup()
        installer.install_vps(self.args)
        self.args.tmux_session = 'changed'
        self.cmd.fail = lambda args: args[:2] == ('systemctl', 'daemon-reload')
        with patch.object(installer, 'restore', side_effect=OSError()):
            with self.assertRaises(installer.RollbackFailure) as caught:
                installer.install_vps(self.args)
        self.assertIn('restore:OSError,daemon_reload:CalledProcessError', str(caught.exception))
        self.assertEqual(self.cmd.calls[-2:], [('systemctl', 'enable', 'idle-compact@rnd.timer'),
                                             ('systemctl', 'start', 'idle-compact@rnd.timer')])

    def test_vps_uninstall_removes_units_and_retains_state_and_logs(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        # Production home is mapped by vps_setup; state/logs stay outside definition removal.
        state = mapped / 'home/agent-rnd/.local/state/idle-compact/rnd.json'
        log = state.with_suffix('.log')
        for path in (state, log):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('retained')
        installer.uninstall_vps(self.args)
        self.assertFalse(self.cmd.enabled)
        self.assertFalse(self.cmd.active)
        self.assertFalse((mapped / 'etc/bubble-idle-compact/rnd.json').exists())
        self.assertFalse((self.args.unit_dir / 'idle-compact@.service').exists())
        self.assertFalse((self.args.unit_dir / 'idle-compact@.timer').exists())
        self.assertTrue(all(path.read_text() == 'retained' for path in (state, log)))

    def test_vps_uninstall_keeps_shared_units_for_other_agents(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        (mapped / 'etc/bubble-idle-compact/maya.json').write_text('{}')
        installer.uninstall_vps(self.args)
        self.assertTrue((self.args.unit_dir / 'idle-compact@.timer').exists())
        self.assertTrue((mapped / 'etc/bubble-idle-compact/maya.json').exists())

    def test_vps_uninstall_all_includes_configs_and_skips_uninstalled_agents(self):
        mapped = self.vps_setup()
        installer.install_vps(self.args)
        self.args.all = True
        self.args.slug = None
        old_iterdir, old_exists = Path.iterdir, Path.exists
        with patch.object(Path, 'iterdir', lambda p: iter([SimpleNamespace(name='maya', is_dir=lambda: True)])
                          if p == self.args.agents_root else old_iterdir(p)), \
             patch.object(Path, 'exists', lambda p: True if p == self.args.agents_root else old_exists(p)):
            installer.uninstall_vps(self.args)
        self.assertFalse(self.cmd.enabled)
        self.assertFalse((mapped / 'etc/bubble-idle-compact/rnd.json').exists())
        self.assertIn('SKIP slug=maya reason=not_installed', self.output.getvalue())

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
    def test_both_status_entrypoints_only_report_configured_agents(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            configs = root / 'configs'
            configs.mkdir()
            launchagents = root / 'Library/LaunchAgents'
            launchagents.mkdir(parents=True)
            for slug in ('rnd', 'backup-rnd', 'wake-rnd', 'main'):
                (launchagents / ('com.bubble.ops-loop-' + slug + '.plist')).touch()
            (configs / 'rnd.json').write_text(json.dumps(dict(slug='rnd', platform='mac', home=str(root),
                service='gui/501/com.bubble.idle-compact-rnd', unit_path=str(root / 'missing.plist'),
                runtime=dict(transport='tmux', state=str(root / 'state'), log=str(root / 'log')))))
            for main, argv in ((installer.main, ['install.py', 'mac', '--status', '--home', str(root),
                                                '--config-dir', str(configs)]),
                               (compact.main, ['idle_compact.py', '--status', '--config-dir', str(configs)])):
                output = io.StringIO()
                with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output), \
                     patch.object(compact.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
                    main()
                rows = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([row['slug'] for row in rows], ['rnd'])
                self.assertEqual(rows[0]['transport'], 'tmux')

    def test_status_reports_dtach_confirmation_and_transport(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            (root / 'state').write_text(json.dumps(dict(status='sent', last_compact_at=compact.iso(NOW),
                                                       compact_confirmed_at=compact.iso(NOW + 65))))
            configs = root / 'configs'
            configs.mkdir()
            (configs / 'rnd.json').write_text(json.dumps(dict(slug='rnd', platform='vps', service='idle-compact@rnd.timer',
                runtime=dict(transport='dtach', state=str(root / 'state'), log=str(root / 'log')))))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                compact.fleet_status(configs, runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout='loaded\n'))
            row = json.loads(output.getvalue())
            self.assertEqual(row['transport'], 'dtach')
            self.assertEqual(row['compact_confirmed_at'], compact.iso(NOW + 65))

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
            self.assertEqual(data['transport'], 'tmux')  # Backwards-compatible configs.

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

    def test_status_excludes_unconfigured_and_phantom_agents(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp, contextlib.redirect_stdout(output):
            compact.fleet_status(Path(tmp), known_slugs=['maya', 'backup-rnd', 'wake-rnd', 'main'])
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
