"""Synthetic fixtures only. No real tmux or launchctl calls."""

import ast
import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/idle-compact"))
import idle_compact as pilot


NOW = 1791028800.0
EMPTY_PANE = ("Done.\n────────────────\n❯\u00a0\n────────────────\n"
              "  bubble-rnd-workspace | main | Opus 5.5 (1M context) | ctx 54% left | 5h 7% | 7d 18%\n")


class TmuxStub:
    def __init__(self):
        self.pane = EMPTY_PANE
        self.calls = []
        self.on_capture = None
        self.fail_send = None

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "list-clients":
            return ""
        if args[1] == "list-panes":
            return "%1 claude\n"
        if args[1] == "capture-pane":
            if self.on_capture:
                self.on_capture()
            return self.pane
        if args[1] == "send-keys":
            if self.fail_send == args[-1]:
                raise subprocess.CalledProcessError(1, args, output="private content")
            return ""
        raise AssertionError("Unexpected command")

    @property
    def sends(self):
        return [call for call in self.calls if call[1] == "send-keys"]


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.transcripts = root / "transcripts"
        self.transcripts.mkdir()
        self.transcript = self.transcripts / "current.jsonl"
        self.config = pilot.Config("ops-loop-rnd", self.transcripts, root / "ledger.jsonl",
                                   root / "state" / "rnd.json", root / "logs" / "rnd.log",
                                   recheck_delay=0)
        self.runner = TmuxStub()
        self.now = NOW
        self.ledger_rows = [{"kind": "text", "ts": pilot.iso(NOW - 7200),
                             "user_id": "6532205130"}]
        self.entries = [self.assistant()]
        self.save()

    def assistant(self, tokens=210000, stop="end_turn", age=600):
        return {"type": "assistant", "timestamp": pilot.iso(NOW - age),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Ready"}],
                            "stop_reason": stop, "usage": {"input_tokens": 2,
                            "cache_read_input_tokens": tokens - 288,
                            "cache_creation_input_tokens": 286}}}

    def user(self, content, age=1800, **extra):
        return dict({"type": "user", "timestamp": pilot.iso(NOW - age),
                     "message": {"role": "user", "content": content}}, **extra)

    def write_rows(self, path, entries):
        path.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")

    def save(self, mtime=NOW - 300):
        self.write_rows(self.config.ledger, self.ledger_rows)
        self.write_rows(self.transcript, self.entries)
        os.utime(self.transcript, (mtime, mtime))

    def check(self):
        # Any accidental use of the real subprocess runner fails the test.
        with patch.object(pilot.subprocess, "run", side_effect=AssertionError("Real subprocess forbidden")):
            before = len(self.config.log.read_text().splitlines()) if self.config.log.exists() else 0
            outcome = pilot.check(self.config, runner=self.runner, clock=lambda: self.now)
            self.assertEqual(len(self.config.log.read_text().splitlines()), before + 1)
            return outcome

    def assert_skip(self, reason):
        self.assertEqual(self.check(), ("SKIP", reason))
        self.assertEqual(self.runner.sends, [])

    def test_all_met_then_same_idle_period_no_send(self):
        self.assertEqual(self.check()[0], "COMPACT_SENT")
        expected = [["tmux", "send-keys", "-t", "%1", "-l", "/compact"],
                    ["tmux", "send-keys", "-t", "%1", "Enter"]]
        self.assertEqual(self.runner.sends, expected)
        self.assertEqual(self.check(), ("SKIP", "compact_cooldown"))
        self.assertEqual(self.runner.sends, expected)
        state = json.loads(self.config.state.read_text())
        self.assertEqual(state["human_activity_at"], pilot.iso(NOW - 7200))
        self.assertEqual(state["last_compact_at"], pilot.iso(NOW))
        self.assertEqual(state["status"], "sent")

    def test_human_ledger_recent_any_kind(self):
        # Real ledgers also carry "caption" and "message" rows (rnd ledger, 2026-10-02).
        for kind in ("text", "voice", "callback", "photo", "document", "caption", "message", "future_kind"):
            with self.subTest(kind=kind):
                self.ledger_rows.append({"kind": kind, "ts": pilot.iso(NOW - 1800),
                                         "user_id": 7470271615})
                self.save()
                self.assert_skip("human_recent")

    def test_context_150k(self):
        self.entries = [self.assistant(tokens=150000)]
        self.save()
        self.assert_skip("context_small")

    def test_esc_to_interrupt(self):
        self.runner.pane = "working (ESC TO INTERRUPT)\n" + EMPTY_PANE
        self.assert_skip("pane_busy")

    def test_sample_style_busy_spinner_without_interrupt(self):
        self.runner.pane = "✶ Sautéing… (37s · ↓ 2.4k tokens)\n" + EMPTY_PANE
        self.assert_skip("pane_working_indicator")

    def test_typed_draft(self):
        self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯ draft PRIVATE_SENTINEL")
        self.assert_skip("draft_present")
        self.assertNotIn("PRIVATE_SENTINEL", self.config.log.read_text())

    def test_multiline_draft(self):
        self.runner.pane = EMPTY_PANE.replace("❯\u00a0\n", "❯\u00a0\n   continuation\n")
        self.assert_skip("draft_or_ambiguous_prompt")

    def test_tool_use_stop_reason(self):
        self.entries = [self.assistant(stop="tool_use")]
        self.save()
        self.assert_skip("turn_not_finished")

    def test_terminal_human_recent_string_or_text_blocks(self):
        for content in ("Please help", [{"type": "text", "text": "Please help"}]):
            with self.subTest(content_type=type(content).__name__):
                self.entries = [self.user(content), self.assistant()]
                self.save()
                self.assert_skip("human_recent")

    def test_human_max_across_transcript_files_and_unsorted_ledger(self):
        older = self.transcripts / "previous.jsonl"
        self.write_rows(older, [self.user("Please help", age=1800)])
        os.utime(older, (NOW - 500, NOW - 500))
        self.ledger_rows.insert(0, {"kind": "voice", "ts": pilot.iso(NOW - 1900),
                                    "user_id": "7470271615"})
        self.save()
        self.assert_skip("human_recent")

    def test_wakes_injections_wrappers_and_tool_results_not_human(self):
        ignored = [
            self.user("Resume Rick's OODA loop now"),
            self.user("tick DUE_MISSIONS=[x]"),
            self.user('<channel source="bubble-inject">machine</channel>'),
            self.user('<channel source="ops-loop-boot-rearm">boot</channel>'),
            self.user('<channel source="plugin:telegram:telegram">human via ledger</channel>'),
            self.user("machine text", source="bubble-inject"),
            self.user("machine text", source="ops-loop-boot-rearm"),
            self.user([{ "type": "text", "text": "<local-command-caveat>command"}]),
            self.user("<local-command-stdout>output"),
            self.user("<command-name>/compact</command-name>"),
            self.user([{ "type": "tool_result", "content": "not human"}]),
            self.user("summary", isCompactSummary=True),
        ]
        nested = self.user("injected")
        nested["message"]["source"] = "bubble-inject"
        ignored.append(nested)
        self.entries = ignored + [self.assistant()]
        self.ledger_rows.append({"kind": "text", "ts": pilot.iso(NOW - 30), "user_id": "system"})
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_malformed_lines_tolerated_fail_closed(self):
        for path in (self.transcript, self.config.ledger):
            with self.subTest(path=path.name):
                self.save()
                with path.open("a") as handle:
                    handle.write('{"incomplete":\n')
                self.assert_skip("malformed_jsonl")

    def test_odd_ledger_kind_shapes_still_count_as_human_activity(self):
        # The kind field is irrelevant: a row from the human at a recent time blocks.
        for kind in ([], {}, None, 42):
            with self.subTest(kind=kind):
                self.ledger_rows = [{"kind": kind, "ts": pilot.iso(NOW - 1800), "user_id": "6532205130"}]
                self.save()
                self.assert_skip("human_recent")

    def test_deeply_nested_json_skips_and_logs(self):
        self.config.ledger.write_text('[' * 2000 + '0' + ']' * 2000 + '\n')
        self.assert_skip("io_parse_or_tmux_error")

    def test_dry_run_never_sends_or_records_compaction(self):
        self.config.dry_run = True
        self.assertEqual(self.check(), ("WOULD COMPACT", "all_conditions_met"))
        self.assertEqual(self.check()[0], "WOULD COMPACT")
        self.assertFalse(self.config.state.exists())
        self.assertEqual(self.runner.sends, [])

    def test_newest_file_by_mtime_not_filename(self):
        old = self.transcripts / "zzzz.jsonl"
        self.write_rows(old, [self.assistant(tokens=150000)])
        os.utime(old, (NOW - 500, NOW - 500))
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_compaction_summary_and_boundary_reset_context(self):
        for row in (self.user("summary", age=500, isCompactSummary=True),
                    {"type": "system", "subtype": "compact_boundary", "timestamp": pilot.iso(NOW - 500)}):
            with self.subTest(kind=row["type"]):
                self.entries = [self.assistant(), row]
                self.save()
                self.assert_skip("context_small")

    def test_assistant_after_summary_uses_current_context(self):
        self.entries = [self.user("summary", age=900, isCompactSummary=True), self.assistant()]
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_transcript_not_quiet(self):
        self.save(mtime=NOW - 30)
        self.assert_skip("transcript_not_quiet")

    def test_trailing_turn_duration_and_metadata_ignored(self):
        self.entries += [{"type": "system", "subtype": "turn_duration", "timestamp": pilot.iso(NOW - 500)},
                         {"type": "file-history-snapshot"}, {"type": "last-prompt"}]
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_trailing_meaningful_records_block(self):
        for row in (self.user("<channel>wake</channel>"), {"type": "attachment"},
                    {"type": "queue-operation"}, {"type": "unknown"},
                    self.user([{"type": "tool_result", "content": "done"}])):
            with self.subTest(kind=row["type"]):
                self.entries = [self.assistant(), row]
                self.save()
                self.assert_skip("turn_not_finished")

    def test_no_repeat_even_after_cooldown_with_large_context(self):
        self.assertEqual(self.check()[0], "COMPACT_SENT")
        self.now += 3600
        self.assertEqual(self.check(), ("SKIP", "context_not_regrown_since_send"))
        self.assertEqual(len(self.runner.sends), 2)

    def test_autonomous_regrowth_for_three_days_at_each_wake_cadence(self):
        for cadence in (1800, 3600, 10800):
            with self.subTest(cadence=cadence):
                self.config.state.unlink(missing_ok=True)
                self.runner.calls.clear()
                self.now = NOW
                self.entries = [self.assistant()]
                self.save()
                self.assertEqual(self.check()[0], "COMPACT_SENT")
                last_send = self.now
                expected_sends = 1
                for elapsed in range(cadence, 3 * 86400 + 1, cadence):
                    self.now = NOW + elapsed
                    # A completed compact followed by an autonomous wake and new turn.
                    self.entries += [
                        {"type": "system", "subtype": "compact_boundary", "timestamp": pilot.iso(last_send + 1)},
                        self.user("Resume Rick's OODA loop", age=-elapsed + 300),
                        self.assistant(age=-elapsed + 180),
                    ]
                    self.save(mtime=self.now - 180)
                    eligible = self.now - last_send >= self.config.idle_min * 60
                    self.assertEqual(self.check()[0], "COMPACT_SENT" if eligible else "SKIP")
                    if eligible:
                        last_send = self.now
                        expected_sends += 1
                    # Another poll of the same quiet stretch never sends.
                    self.assertEqual(self.check()[0], "SKIP")
                    self.assertEqual(len(self.runner.sends), expected_sends * 2)
                    self.assertEqual(json.loads(self.config.state.read_text())["human_activity_at"], pilot.iso(NOW - 7200))
                # No new turn: even a day later and still high, there is no resend.
                self.now += 86400
                self.assertEqual(self.check(), ("SKIP", "context_not_regrown_since_send"))
                self.assertEqual(len(self.runner.sends), expected_sends * 2)

    def test_new_high_turn_without_reduction_does_not_repeat(self):
        self.check()
        self.now += 3600
        self.entries += [self.assistant(tokens=230000, age=-3400)]
        self.save(mtime=self.now - 180)
        self.assertEqual(self.check(), ("SKIP", "context_not_regrown_since_send"))
        self.assertEqual(len(self.runner.sends), 2)

    def test_lower_usage_then_regrowth_without_boundary_allows_repeat(self):
        self.check()
        self.now += 3600
        self.entries += [self.assistant(tokens=50000, age=-10), self.assistant(tokens=230000, age=-3400)]
        self.save(mtime=self.now - 180)
        self.assertEqual(self.check()[0], "COMPACT_SENT")
        self.now += 3600
        self.assertEqual(self.check(), ("SKIP", "context_not_regrown_since_send"))

    def test_permissions_only_footer_cannot_prove_claude_repl(self):
        self.runner.pane = "Done.\n────────────────\n❯ \n────────────────\n⏵⏵ bypass permissions on\n"
        self.assert_skip("unrecognized_pane_footer")

    def test_cooldown_expiry_small_context_still_skips(self):
        self.check()
        self.runner.calls.clear()
        self.now += 3600
        self.entries = [self.assistant(tokens=100000)]
        self.save()
        self.assert_skip("context_small")

    def test_new_human_after_compaction_still_must_be_idle(self):
        self.check()
        self.runner.calls.clear()
        self.now += 1800
        self.ledger_rows.append({"kind": "text", "ts": pilot.iso(NOW + 60), "user_id": "6532205130"})
        self.save()
        self.assert_skip("human_recent")
        self.now = NOW + 3660
        self.assertEqual(self.check(), ("SKIP", "context_not_regrown_since_send"))
        self.entries += [{"type": "system", "subtype": "compact_boundary", "timestamp": pilot.iso(NOW + 10)},
                         self.assistant(age=-3400)]
        self.save(mtime=self.now - 180)
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_missing_or_invalid_usage_does_not_reuse_old_assistant(self):
        for invalid in (None, {}, {"input_tokens": -1, "cache_read_input_tokens": 210000,
                                  "cache_creation_input_tokens": 0}):
            with self.subTest(usage=invalid):
                latest = self.assistant(age=500)
                latest["message"]["usage"] = invalid
                self.entries = [self.assistant(), latest]
                self.save()
                outcome = self.check()
                self.assertEqual(outcome[0], "SKIP")
                self.assertEqual(self.runner.sends, [])

    def test_any_kind_old_human_rows_do_not_block(self):
        for kind in ("caption", "message"):
            self.ledger_rows.append({"kind": kind, "ts": pilot.iso(NOW - 7300), "user_id": "6532205130"})
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_bad_row_in_older_transcript_counts_as_activity_at_its_mtime(self):
        older = self.transcripts / "previous.jsonl"
        older.write_text('{"type": "user", "message": {"content": [{"type": "image"}]}}\n', encoding="utf-8")
        os.utime(older, (NOW - 1200, NOW - 1200))
        self.save()
        self.assert_skip("human_recent")
        os.utime(older, (NOW - 7000, NOW - 7000))
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_transcripts_older_than_a_day_are_ignored(self):
        older = self.transcripts / "ancient.jsonl"
        older.write_text("not json at all\n", encoding="utf-8")
        os.utime(older, (NOW - 3 * 86400, NOW - 3 * 86400))
        self.save()
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_task_notifications_and_system_notices_not_human(self):
        for text in ("<task-notification> <task-id>a1</task-id> done",
                     "<system-reminder> something </system-reminder>",
                     "[SYSTEM NOTIFICATION - NOT USER INPUT] background event"):
            with self.subTest(text=text[:20]):
                self.entries = [self.user(text, age=900), self.assistant()]
                self.save()
                self.assertEqual(self.check()[0], "COMPACT_SENT")
                self.config.state.unlink()

    def test_dim_prompt_suggestion_is_not_a_draft(self):
        # Real capture (tmux -e) of Rick's idle prompt with Claude Code's grey suggestion.
        self.runner.pane = EMPTY_PANE.replace(
            "❯\u00a0", "\x1b[39m❯\u00a0\x1b[2mfix it and switch it live\x1b[0m")
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_normal_intensity_text_after_suggestion_is_a_draft(self):
        self.runner.pane = EMPTY_PANE.replace(
            "❯\u00a0", "\x1b[39m❯\u00a0hello\x1b[2m world\x1b[0m")
        self.assert_skip("draft_present")

    def test_styled_pane_elsewhere_is_fine_but_odd_escapes_are_not(self):
        self.runner.pane = "\x1b[31m" + EMPTY_PANE.replace("Done.", "\x1b[1mDone.\x1b[0m")
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_future_or_naive_activity_timestamp(self):
        for ts in (pilot.iso(NOW + 30), "2026-10-03T10:00:00", "bad"):
            with self.subTest(ts=ts):
                self.ledger_rows[0]["ts"] = ts
                self.save()
                self.assert_skip("invalid_or_future_timestamp")

    def test_missing_sources_unknown_human_and_corrupt_state(self):
        self.config.ledger.unlink()
        self.assert_skip("io_parse_or_tmux_error")
        self.ledger_rows = []
        self.save()
        self.assert_skip("no_known_human_activity")
        self.config.state.write_text("{}")
        self.assert_skip("invalid_state")

    def test_empty_unboxed_or_ambiguous_pane(self):
        for pane in ("", "❯ ", EMPTY_PANE + "────────────────\n", "\x1b]0;title\x07" + EMPTY_PANE):
            with self.subTest(pane_shape=repr(pane[:5])):
                self.runner.pane = pane
                self.assertEqual(self.check()[0], "SKIP")
                self.assertEqual(self.runner.sends, [])

    def test_whitespace_prompt_and_old_draft_in_scrollback(self):
        self.runner.pane = "❯ old request\n" + EMPTY_PANE.replace("❯\u00a0", " ❯ \t\u00a0")
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_stale_prompt_followed_by_shell_or_unknown_footer_skips(self):
        for suffix in ("bash-3.2$ ", "joris@mac % ", "unknown layout", "❯ shell command"):
            with self.subTest(suffix=suffix):
                self.runner.pane = EMPTY_PANE + suffix
                self.assertEqual(self.check()[0], "SKIP")
                self.assertEqual(self.runner.sends, [])

    def test_sample_permissions_footer_accepted(self):
        self.runner.pane += "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_missing_status_footer_skips(self):
        self.runner.pane = "────────────────\n❯ \n────────────────\n"
        self.assert_skip("unrecognized_pane_footer")

    def test_truncated_status_line_accepted(self):
        # Board #1707: real 80-column pane after the 2026-10-03 restart.
        box = "Done.\n────────────────\n❯\u00a0\n────────────────\n"
        mode = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ⧉ wiki-compile-audit · ← fo…\n"
        for status in (
            "  bubble-rnd-workspace | main | Opus 5.5 (1M context) | ctx 57% left | 5h 6% …\n",
            "  bubble-rnd-workspace | main | Opus 5.5 (1M context) | ctx 57% left | 5h…\n",
            "  bubble-rnd-workspace | main | Opus 5.5 (1M context) | ctx 57% left …\n",
            "  bubble-rnd-workspace | main | Opus 5.5 (1M context) | ctx 57% left | 5h 6% | 7d 21%\n",
        ):
            with self.subTest(status=status.strip()[-14:]):
                self.runner.pane = box + status + mode
                self.assertEqual(self.check()[0], "COMPACT_SENT")
                self.config.state.unlink()

    def test_status_truncated_before_ctx_anchor_skips(self):
        box = "Done.\n────────────────\n❯\u00a0\n────────────────\n"
        for status in ("  bubble-rnd-workspace | main | Opus 5.5 (1M con…\n",
                       "  joris@mac ~ % ls …\n",
                       "  bubble-rnd-workspace | main | ctx 57% left | 5h 6% | rm -rf x\n",
                       "  x | ctx 5% left | rm -rf / …\n",
                       "  x | ctx 5% left | $(rm -rf ~) …\n",
                       "  x | ctx 5% left | 5h 6% | foo …\n",
                       "  x | ctx 5% left | 5h … extra\n"):
            with self.subTest(status=status.strip()[-14:]):
                self.runner.pane = box + status
                self.assert_skip("unrecognized_pane_footer")

    def test_real_footer_variants_accepted(self):
        # Both permission mode variants require the model/context status line.
        for footer in ("  repo | main | Opus 5.5 (1M context) | ctx 49% left | 5h 8% | 7d 18%\n"
                       "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n",
                       "  repo | main | Sonnet 4.6 | ctx 49% left\n"
                       "  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n"):
            with self.subTest(footer=footer[:30]):
                self.runner.pane = "Done.\n────────────────\n❯\u00a0\n────────────────\n          \n" + footer
                self.assertEqual(self.check()[0], "COMPACT_SENT")
                self.config.state.unlink()

    def test_dim_busy_hint_still_blocks(self):
        self.runner.pane = "\x1b[2m✶ Working… (esc to interrupt)\x1b[0m\n" + EMPTY_PANE
        self.assertEqual(self.check()[0], "SKIP")
        self.assertEqual(self.runner.sends, [])

    def test_dim_span_with_combined_reset_does_not_hide_draft(self):
        for end in ("\x1b[0;39m", "\x1b[22;39m", "\x1b[00m", "\x1b[22m", "\x1b[m"):
            with self.subTest(end=repr(end)):
                self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯\u00a0\x1b[2msug" + end + "hello")
                self.assert_skip("draft_present")

    def test_dim_not_cleared_by_color_only_code(self):
        # \x1b[39m changes colour, not intensity: text stays dim (suggestion) -> empty.
        self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯\u00a0\x1b[2msug\x1b[39mgestion\x1b[0m")
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_paste_placeholders_count_as_draft_even_dim(self):
        for ph in ("[Pasted text #1 +12 lines]", "[Image #1]"):
            with self.subTest(ph=ph):
                self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯\u00a0\x1b[2m" + ph + "\x1b[0m")
                self.assert_skip("draft_present")

    def test_extended_colour_arguments_are_not_dim(self):
        for seq in ("\x1b[38;2;215;119;87m", "\x1b[38;5;2m", "\x1b[48;2;2;2;2m", "\x1b[1;38;5;2;48;5;2m"):
            with self.subTest(seq=repr(seq)):
                self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯\u00a0" + seq + "hello\x1b[0m")
                self.assert_skip("draft_present")

    def test_dim_then_truecolor_keeps_dim(self):
        self.runner.pane = EMPTY_PANE.replace(
            "❯\u00a0", "❯\u00a0\x1b[2m\x1b[38;2;10;10;10msuggestion\x1b[0m")
        self.assertEqual(self.check()[0], "COMPACT_SENT")

    def test_ledger_changes_during_capture(self):
        def mutate():
            self.ledger_rows.append({"kind": "text", "ts": pilot.iso(NOW - 1), "user_id": "6532205130"})
            self.write_rows(self.config.ledger, self.ledger_rows)
        self.runner.on_capture = mutate
        self.assert_skip("inputs_changed_before_send")

    def test_second_capture_sees_draft(self):
        captures = []
        def mutate():
            captures.append(True)
            if len(captures) == 2:
                self.runner.pane = EMPTY_PANE.replace("❯\u00a0", "❯ just typed")
        self.runner.on_capture = mutate
        self.assert_skip("draft_present")

    def test_partial_send_failure_stays_pending_and_no_retry(self):
        self.runner.fail_send = "Enter"
        self.assertEqual(self.check(), ("SKIP", "io_parse_or_tmux_error"))
        self.assertEqual(json.loads(self.config.state.read_text())["status"], "pending")
        sends = list(self.runner.sends)
        self.assertEqual(self.check(), ("SKIP", "previous_send_uncertain_check_state"))
        self.assertEqual(self.runner.sends, sends)
        self.assertNotIn("private content", self.config.log.read_text())

    def test_tmux_timeout(self):
        def timeout(args):
            raise subprocess.TimeoutExpired(args, 5, output="private content")
        self.runner.on_capture = lambda: timeout([])
        self.assert_skip("io_parse_or_tmux_error")

    def test_lock_prevents_overlap(self):
        self.config.state.parent.mkdir(parents=True)
        with self.config.state.with_name("rnd.json.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_skip("another_check_running")

    def test_unwritable_log_never_calls_tmux(self):
        self.config.log = self.transcripts
        with patch("builtins.print"):
            self.assertEqual(pilot.check(self.config, runner=self.runner, clock=lambda: NOW),
                             ("SKIP", "log_unavailable"))
        self.assertEqual(self.runner.calls, [])

    def test_python39_syntax_and_template(self):
        source = Path(pilot.__file__).read_text()
        ast.parse(source, feature_version=(3, 9))
        template = Path(__file__).resolve().parents[1] / "deploy/templates/com.bubble.idle-compact.plist.template"
        with template.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["StartInterval"], 300)
        self.assertIn("--config", plist["ProgramArguments"])
        self.assertEqual(plist["ProgramArguments"][1], "@FRAMEWORK_ROOT@/scripts/idle-compact/idle_compact.py")


if __name__ == "__main__":
    unittest.main()
