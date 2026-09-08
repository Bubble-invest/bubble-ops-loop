"""Exercise the floor's Python import from its real style of working directory."""
import os
from pathlib import Path
import subprocess
import sys


def test_framework_helpers_win_over_department_scripts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    department_scripts = tmp_path / "scripts"
    department_scripts.mkdir()
    (department_scripts / "__init__.py").write_text(
        'raise RuntimeError("department scripts package was imported")\n'
    )
    code = """
import os, pathlib, sys
root = pathlib.Path(os.environ['BUBBLE_OPS_LOOP_ROOT']).resolve()
sys.path.insert(0, str(root))
from scripts.lib import loop_backup
assert pathlib.Path(loop_backup.__file__).resolve() == root / 'scripts/lib/loop_backup.py'
from scripts.lib.loop_backup import latest_heartbeat_epoch, backup_decision, format_event, append_event
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path,
        env={**os.environ, "BUBBLE_OPS_LOOP_ROOT": str(root)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
