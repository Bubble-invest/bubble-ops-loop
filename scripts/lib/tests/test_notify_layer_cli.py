"""test_notify_layer_cli.py — board #521, cause 2 (artifact gate false-drop).

``tools/notify_layer.py`` gates an L1/L4 "fired" send on a real artifact
existing. Before board #521 the gate ONLY checked ``--summary`` — so a tick
that wrote the real brief (e.g. morning_brief.md) but not a separate
summary.md was silently dropped ("NOT sending L{N} fired — no real summary
artifact"), even though there was real content to deliver. Observed 4x live
in Tony's transcripts vs 110x sent.

This module loads ``tools/notify_layer.py`` via ``importlib`` (it's a script,
not a package member — same pattern as token-broker's policy tests) and
exercises the gate helpers directly, plus an end-to-end ``main()`` run with a
stubbed ``loop_notify`` so no live HTTP happens.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
NOTIFY_LAYER_PY = ROOT / "tools" / "notify_layer.py"


def _load_notify_layer_module():
    """Fresh import each call so module-level ROOT-relative state (cwd via
    argparse) doesn't leak between tests."""
    spec = importlib.util.spec_from_file_location("notify_layer_cli", str(NOTIFY_LAYER_PY))
    m = importlib.util.module_from_spec(spec)
    sys.modules["notify_layer_cli"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def nl():
    return _load_notify_layer_module()


# ─── _has_real_artifact ─────────────────────────────────────────────────────


def test_has_real_artifact_true_for_nonempty_file(tmp_path, nl):
    p = tmp_path / "summary.md"
    p.write_text("hello")
    assert nl._has_real_artifact(str(p)) is True


def test_has_real_artifact_false_for_missing(tmp_path, nl):
    assert nl._has_real_artifact(str(tmp_path / "nope.md")) is False


def test_has_real_artifact_false_for_empty_file(tmp_path, nl):
    p = tmp_path / "empty.md"
    p.write_text("")
    assert nl._has_real_artifact(str(p)) is False


def test_has_real_artifact_false_for_none(nl):
    assert nl._has_real_artifact(None) is False


# ─── _brief_artifact_present (the board #521 gate fix) ─────────────────────


def test_brief_artifact_present_true_when_configured_and_written(tmp_path, nl):
    layer_dir = tmp_path / "1"
    layer_dir.mkdir()
    summary = layer_dir / "summary.md"  # deliberately never written for real
    brief = layer_dir / "morning_brief.md"
    brief.write_text("real brief content")
    cfg = {"brief_artifacts": {"1": "morning_brief.md"}}
    assert nl._brief_artifact_present(str(summary), "1", cfg) is True


def test_brief_artifact_present_false_when_not_configured(tmp_path, nl):
    layer_dir = tmp_path / "1"
    layer_dir.mkdir()
    summary = layer_dir / "summary.md"
    (layer_dir / "morning_brief.md").write_text("content")
    assert nl._brief_artifact_present(str(summary), "1", {}) is False


def test_brief_artifact_present_false_when_configured_but_not_written(tmp_path, nl):
    layer_dir = tmp_path / "1"
    layer_dir.mkdir()
    summary = layer_dir / "summary.md"
    cfg = {"brief_artifacts": {"1": "morning_brief.md"}}
    assert nl._brief_artifact_present(str(summary), "1", cfg) is False


def test_brief_artifact_present_false_when_no_summary_path(nl):
    assert nl._brief_artifact_present(None, "1", {"brief_artifacts": {"1": "x.md"}}) is False


# ─── main() end-to-end — the gate no longer drops when the brief exists ───


class _FakeReceipt:
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error


@pytest.fixture
def stub_loop_notify(monkeypatch):
    """Stub scripts/lib/loop_notify's send functions so main() never hits
    live HTTP; records calls for assertions."""
    calls = {"fired": [], "batched": [], "logged": []}

    import types
    fake_mod = types.ModuleType("loop_notify")

    def notify_layer_fired(dept, layer, summary_path, config=None, test=False, notify_log_path=None):
        calls["fired"].append(
            {"dept": dept, "layer": layer, "summary_path": summary_path, "test": test}
        )
        return _FakeReceipt(success=True)

    def notify_layers_batched(dept, counts, config=None, notify_log_path=None):
        calls["batched"].append({"dept": dept, "counts": counts})
        return _FakeReceipt(success=True)

    def log_notify_event(event, notify_log_path=None):
        calls["logged"].append(event)

    def _configured_brief_filename(config, layer):
        layer_str = str(layer).lstrip("L").lstrip("l")
        mapping = (config or {}).get("brief_artifacts") or {}
        for key in (layer, layer_str, f"L{layer_str}"):
            if key in mapping:
                return mapping[key]
        return None

    fake_mod.notify_layer_fired = notify_layer_fired
    fake_mod.notify_layers_batched = notify_layers_batched
    fake_mod.log_notify_event = log_notify_event
    fake_mod._configured_brief_filename = _configured_brief_filename
    monkeypatch.setitem(sys.modules, "loop_notify", fake_mod)
    return calls


def test_gate_sends_when_brief_exists_but_summary_missing(tmp_path, monkeypatch, stub_loop_notify, nl):
    """THE board #521 regression test: summary.md was never written this
    tick, but the configured brief WAS — the gate must not drop the send."""
    layer_dir = tmp_path / "outputs" / "2026-07-04" / "1"
    layer_dir.mkdir(parents=True)
    summary_path = layer_dir / "summary.md"  # never created
    (layer_dir / "morning_brief.md").write_text("today's real brief")

    monkeypatch.setattr(nl, "ROOT", tmp_path)
    monkeypatch.setattr(nl, "_cfg", lambda: {"brief_artifacts": {"1": "morning_brief.md"}})
    monkeypatch.setattr(nl, "_dept", lambda: "tony")
    monkeypatch.setattr(
        sys, "argv", ["notify_layer.py", "fired", "--layer", "1", "--summary", str(summary_path)]
    )

    nl.main()

    assert len(stub_loop_notify["fired"]) == 1, "gate incorrectly dropped the send"
    assert stub_loop_notify["fired"][0]["dept"] == "tony"


def test_gate_still_drops_when_neither_summary_nor_brief_exist(tmp_path, monkeypatch, stub_loop_notify, nl, capsys):
    layer_dir = tmp_path / "outputs" / "2026-07-04" / "1"
    layer_dir.mkdir(parents=True)
    summary_path = layer_dir / "summary.md"

    monkeypatch.setattr(nl, "ROOT", tmp_path)
    monkeypatch.setattr(nl, "_cfg", lambda: {"brief_artifacts": {"1": "morning_brief.md"}})
    monkeypatch.setattr(nl, "_dept", lambda: "tony")
    monkeypatch.setattr(
        sys, "argv", ["notify_layer.py", "fired", "--layer", "1", "--summary", str(summary_path)]
    )

    nl.main()

    assert stub_loop_notify["fired"] == []
    out = capsys.readouterr().out
    assert "NOT sending" in out
    # The drop itself must be LOGGED (observability), not just printed.
    assert len(stub_loop_notify["logged"]) == 1
    assert stub_loop_notify["logged"][0]["success"] is False


def test_gate_sends_when_summary_exists_and_no_brief_configured(tmp_path, monkeypatch, stub_loop_notify, nl):
    """Back-compat: a dept with no brief_artifacts config keeps the original
    summary-only gate behavior — no regression."""
    layer_dir = tmp_path / "outputs" / "2026-07-04" / "1"
    layer_dir.mkdir(parents=True)
    summary_path = layer_dir / "summary.md"
    summary_path.write_text("summary content")

    monkeypatch.setattr(nl, "ROOT", tmp_path)
    monkeypatch.setattr(nl, "_cfg", lambda: {})
    monkeypatch.setattr(nl, "_dept", lambda: "maya")
    monkeypatch.setattr(
        sys, "argv", ["notify_layer.py", "fired", "--layer", "1", "--summary", str(summary_path)]
    )

    nl.main()

    assert len(stub_loop_notify["fired"]) == 1


def test_test_flag_bypasses_gate_entirely(tmp_path, monkeypatch, stub_loop_notify, nl):
    monkeypatch.setattr(nl, "ROOT", tmp_path)
    monkeypatch.setattr(nl, "_cfg", lambda: {})
    monkeypatch.setattr(nl, "_dept", lambda: "tony")
    monkeypatch.setattr(
        sys, "argv", ["notify_layer.py", "fired", "--layer", "4", "--test"]
    )

    nl.main()

    assert len(stub_loop_notify["fired"]) == 1
    assert stub_loop_notify["fired"][0]["test"] is True


def test_batched_command_still_works(tmp_path, monkeypatch, stub_loop_notify, nl):
    monkeypatch.setattr(nl, "ROOT", tmp_path)
    monkeypatch.setattr(nl, "_cfg", lambda: {})
    monkeypatch.setattr(nl, "_dept", lambda: "maya")
    monkeypatch.setattr(sys, "argv", ["notify_layer.py", "batched", "--counts", "2=3,3=1"])

    nl.main()

    assert stub_loop_notify["batched"] == [{"dept": "maya", "counts": {"2": 3, "3": 1}}]


def test_default_notify_log_path_uses_outputs_dir(tmp_path, monkeypatch, nl):
    monkeypatch.delenv("BUBBLE_NOTIFY_LOG_PATH", raising=False)
    monkeypatch.setattr(nl, "ROOT", tmp_path)
    assert nl._default_notify_log_path() == str(tmp_path / "outputs" / "notify.log")


def test_default_notify_log_path_env_override(tmp_path, monkeypatch, nl):
    monkeypatch.setenv("BUBBLE_NOTIFY_LOG_PATH", str(tmp_path / "custom.log"))
    assert nl._default_notify_log_path() == str(tmp_path / "custom.log")
