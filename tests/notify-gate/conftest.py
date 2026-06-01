"""Conftest for notify-gate tests.

Inserts the fixture's tools/notify-gate/ dir on sys.path so tests can
`import notify` from the actual implementation that will land in
/tmp/bubble-ops-fixture/tools/notify-gate/.
"""
import os
import sys

FIXTURE_TOOL_DIR = "/tmp/bubble-ops-fixture/tools/notify-gate"
if os.path.isdir(FIXTURE_TOOL_DIR) and FIXTURE_TOOL_DIR not in sys.path:
    sys.path.insert(0, FIXTURE_TOOL_DIR)
