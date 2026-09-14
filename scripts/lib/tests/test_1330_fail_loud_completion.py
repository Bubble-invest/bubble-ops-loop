"""Tests for #1330 — a `complete` (or any due-mission) invocation that ERRORS
must be distinguishable from one that succeeded; a completion is only real
if the marker is actually written.

CONFIRMED LIVE INCIDENT (2026-09-14 16:20Z tick): bare `python3` resolved
non-deterministically between two interpreters on the same Mac; one had no
pyyaml. `due_missions.py complete` crashed with a bare `ModuleNotFoundError`
at IMPORT time (before argparse even ran) — `kanban_board`/`wiki_compile`
silently did NOT complete that tick, and the failure mode looks identical to
one where the command was simply never invoked. Same family as #1235/#1316:
"an error looks like nothing happened" is exactly the ambiguity a canonical
completion predicate must close.

THREE INDEPENDENT HARDENINGS (none of them fix interpreter selection itself —
that remains #1330's own infra/deploy remedy, "pin a known-good interpreter",
out of this module's scope):

  1. `import yaml` is deferred (try/except at module load) so a missing
     pyyaml no longer crashes the module before `main()` can run at all —
     `_require_yaml()` turns it into ONE clear, actionable
     DueMissionConfigError (naming `sys.executable`) instead of a raw
     traceback.
  2. `main()` now catches ANY uncaught exception (not just
     DueMissionConfigError), prints an unambiguous "unexpected <Type>:"
     message, and returns a distinct exit code (3) — no due-mission failure
     can silently look like a normal exit.
  3. `command_complete` reads the watermark back after
     `write_due_success` and raises if the persisted state doesn't actually
     show the completion — "completed" is only printed once the marker is
     CONFIRMED written, not merely attempted.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pytest

import scripts.due_missions as due_missions
from scripts.lib.loop_backup import DueMissionConfigError


def _dept_dir(tmp_path: Path) -> Path:
    dept_dir = tmp_path / "dept"
    (dept_dir / "missions").mkdir(parents=True)
    (dept_dir / "layers" / "1").mkdir(parents=True)
    (dept_dir / "missions" / "m.md").write_text("# m\n")
    (dept_dir / "layers" / "1" / "PROMPT.md").write_text("# layer 1\n")
    (dept_dir / "dept.yaml").write_text(
        "loop:\n"
        "  due_dispatch:\n"
        "    mission_ids: [m]\n"
        "    watermark: monitoring/due-mission-watermarks.json\n"
        "    pending_lease_seconds: 21600\n"
        "layers:\n"
        "  subscribed: [1]\n"
        "recurring_missions:\n"
        "- id: m\n"
        "  layer: 1\n"
        "  status: live\n"
        "  cadence: daily\n"
        "  due: {policy: calendar_period, timezone: Europe/Paris}\n"
        "  mission_file: missions/m.md\n"
    )
    return dept_dir


# ===========================================================================
# 1. Deferred yaml import — a missing pyyaml is a clear error, not a crash
# ===========================================================================

def test_module_imports_cleanly_even_without_yaml(monkeypatch):
    """The module itself must never fail to import just because pyyaml is
    missing — that was the exact #1330 crash (before main() could even run).
    Simulated here via the module's own _YAML_IMPORT_ERROR flag rather than
    actually uninstalling pyyaml."""
    assert due_missions._YAML_IMPORT_ERROR is None  # sanity: pyyaml IS present in test env
    monkeypatch.setattr(due_missions, "_YAML_IMPORT_ERROR", ModuleNotFoundError("No module named 'yaml'"))
    monkeypatch.setattr(due_missions, "yaml", None)

    with pytest.raises(DueMissionConfigError, match="missing dependency 'yaml'"):
        due_missions._require_yaml()


def test_load_manifest_fails_loud_not_with_a_bare_attributeerror(tmp_path, monkeypatch):
    """_load_manifest must surface the SAME clear error, not an AttributeError
    from calling `None.safe_load(...)` once yaml has failed to import."""
    dept_dir = _dept_dir(tmp_path)
    monkeypatch.setattr(due_missions, "_YAML_IMPORT_ERROR", ModuleNotFoundError("No module named 'yaml'"))
    monkeypatch.setattr(due_missions, "yaml", None)

    with pytest.raises(DueMissionConfigError, match="missing dependency 'yaml'"):
        due_missions._load_manifest(dept_dir)


# ===========================================================================
# 2. main() never lets an uncaught exception look like a normal exit
# ===========================================================================

def test_main_catches_any_uncaught_exception_with_a_distinct_exit_code(monkeypatch, capsys):
    def _boom(_args):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(
        due_missions, "parser",
        lambda: type("P", (), {"parse_args": staticmethod(
            lambda: argparse.Namespace(func=_boom)
        )})(),
    )

    rc = due_missions.main()
    err = capsys.readouterr().err

    assert rc == 3, "an unexpected exception must exit non-zero AND distinct from the config-error code (2)"
    assert "unexpected RuntimeError" in err
    assert "simulated unexpected failure" in err


def test_main_still_reports_due_mission_config_error_as_before(monkeypatch, capsys):
    """No regression: the pre-existing DueMissionConfigError path (exit 2)
    is unchanged by the new broader except clause."""
    def _boom(_args):
        raise DueMissionConfigError("bad config")

    monkeypatch.setattr(
        due_missions, "parser",
        lambda: type("P", (), {"parse_args": staticmethod(
            lambda: argparse.Namespace(func=_boom)
        )})(),
    )

    rc = due_missions.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "due-mission error: bad config" in err
    assert "unexpected" not in err


# ===========================================================================
# 3. command_complete verifies the marker actually persisted
# ===========================================================================

def test_command_complete_succeeds_and_persists_normally(tmp_path):
    dept_dir = _dept_dir(tmp_path)
    args = argparse.Namespace(
        dept_dir=str(dept_dir), mission="m", period="2026-09-14",
        now_epoch=int(dt.datetime(2026, 9, 14, 10, 0, tzinfo=dt.timezone.utc).timestamp()),
    )
    assert due_missions.command_complete(args) == 0


def test_command_complete_raises_if_the_write_does_not_actually_persist(tmp_path, monkeypatch):
    """Simulate write_due_success succeeding in-process but the on-disk state
    NOT reflecting it (e.g. a torn/partial write that didn't raise) — the
    read-back verification must catch this and refuse to report success."""
    dept_dir = _dept_dir(tmp_path)

    def _fake_write_due_success(_path, _mission_id, _period, _completed_at):
        return {"version": 1, "missions": {}}  # pretends to succeed, writes nothing

    monkeypatch.setattr(due_missions, "write_due_success", _fake_write_due_success)

    args = argparse.Namespace(
        dept_dir=str(dept_dir), mission="m", period="2026-09-14",
        now_epoch=int(dt.datetime(2026, 9, 14, 10, 0, tzinfo=dt.timezone.utc).timestamp()),
    )
    with pytest.raises(DueMissionConfigError, match="did not persist"):
        due_missions.command_complete(args)
