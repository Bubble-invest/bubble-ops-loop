"""#1119/#1120 contract tests for canonical deployment identity and cutover."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-to-morty.sh"
PLATFORM_ROOT = ROOT.parent / "bubble-vps-platform"


def _inputs(tmp_path: Path, *, os_user: str | None = None) -> tuple[Path, Path]:
    tenant = tmp_path / "tenant.yaml"
    dept = tmp_path / "dept.yaml"
    tenant.write_text(yaml.safe_dump({
        "tenant_name": "bubble-internal",
        "secrets": {"age_key_path": "/etc/age/key.txt"},
        "agent": {"concierges": []},
    }), encoding="utf-8")
    department = {"slug": "eliot", "model": "opus[1m]"}
    if os_user is not None:
        department["os_user"] = os_user
    dept.write_text(yaml.safe_dump({"department": department}), encoding="utf-8")
    return tenant, dept


def _run(tmp_path: Path, *extra: str, os_user: str | None = None, env=None):
    tenant, dept = _inputs(tmp_path, os_user=os_user)
    return subprocess.run([
        "bash", str(SCRIPT),
        "--slug=eliot",
        "--remote=claude@morty",
        f"--tenant-yaml={tenant}",
        f"--dept-yaml={dept}",
        f"--platform-root={PLATFORM_ROOT}",
        *extra,
    ], capture_output=True, text=True, env=env, check=False)


def test_script_exists_and_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_default_identity_preserves_1120_legacy_values(tmp_path: Path):
    result = _run(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resolved User=claude Group=claude WorkingDirectory=/home/claude/agents/eliot" in result.stdout
    assert "chown -R claude:claude /home/claude/agents/eliot" in result.stdout


def test_os_user_override_repoints_identity_and_workdir(tmp_path: Path):
    result = _run(tmp_path, "--os-user=agent-eliot", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resolved User=agent-eliot Group=agent-eliot WorkingDirectory=/srv/agents/eliot" in result.stdout
    assert "chown -R agent-eliot:agent-eliot /srv/agents/eliot" in result.stdout
    assert "bootstrap-os-user.sh --os-user=agent-eliot --workdir=/srv/agents/eliot" in result.stdout


def test_dept_yaml_is_the_identity_source_when_no_cli_override(tmp_path: Path):
    result = _run(tmp_path, "--dry-run", os_user="agent-eliot")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resolved User=agent-eliot Group=agent-eliot WorkingDirectory=/srv/agents/eliot" in result.stdout


@pytest.mark.parametrize("bogus", ["", "root"])
def test_rejects_bogus_os_user(tmp_path: Path, bogus: str):
    result = _run(tmp_path, f"--os-user={bogus}", "--dry-run")
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "os-user" in combined or "os_user" in combined


def test_cutover_stops_and_disables_legacy_before_starting_canonical(tmp_path: Path):
    result = _run(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    stop = "systemctl stop ops-loop-eliot.service"
    disable = "systemctl disable ops-loop-eliot.service"
    start = "systemctl enable --now bubble-agent@eliot.service"
    assert result.stdout.index(stop) < result.stdout.index(disable) < result.stdout.index(start)


def test_real_path_refuses_missing_isolated_user_before_transport(tmp_path: Path):
    ssh_stub = tmp_path / "ssh"
    ssh_stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    result = _run(tmp_path, "--os-user=agent-eliot", env=env)
    assert result.returncode != 0
    assert "does not exist" in (result.stdout + result.stderr)
    assert "bootstrap-os-user.sh" in (result.stdout + result.stderr)


def test_help_documents_identity_and_renderer_inputs():
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for option in ("--tenant-yaml", "--os-user", "--os-group", "--workdir", "--model"):
        assert option in result.stdout
