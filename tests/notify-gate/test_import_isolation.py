"""The optional fixture must never resolve to scripts/lib/notify.py."""
import sys
from types import ModuleType

import pytest


def test_loader_ignores_cached_fleet_notify(tmp_path, monkeypatch, gate_notify_loader):
    fleet_notify = ModuleType("notify")
    monkeypatch.setitem(sys.modules, "notify", fleet_notify)
    impl = tmp_path / "notify.py"
    impl.write_text("def notify_gate():\n    return 'fixture'\n")
    loaded = gate_notify_loader(impl)
    assert loaded.notify_gate() == "fixture"
    assert sys.modules["notify"] is fleet_notify


def test_loader_does_not_hide_missing_dependencies(tmp_path, gate_notify_loader):
    impl = tmp_path / "notify.py"
    impl.write_text("import nonexistent_gate_test_dependency\n")
    with pytest.raises(ModuleNotFoundError, match="nonexistent_gate_test_dependency"):
        gate_notify_loader(impl)
