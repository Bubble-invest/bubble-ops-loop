"""Load the external fixture implementation without shadowing fleet notify."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def gate_notify_loader():
    def load(path):
        spec = importlib.util.spec_from_file_location("fixture_gate_notify", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.fixture
def notify(gate_notify_loader):
    path = Path("/tmp/bubble-ops-fixture/tools/notify-gate/notify.py")
    if not path.is_file():
        pytest.skip(f"external bubble-ops-fixture implementation unavailable: {path}")
    return gate_notify_loader(path)
