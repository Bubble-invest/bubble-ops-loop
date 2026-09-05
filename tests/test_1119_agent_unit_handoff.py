"""#1119: ops-loop consumes, but never reimplements, agent-unit rendering."""

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_unit_generator_is_removed():
    assert not (ROOT / "deploy/templates/ops-loop-dept.service.template").exists()
    scaffold = (ROOT / "scripts/lib/scaffold.py").read_text(encoding="utf-8")
    assert "def render_systemd_unit" not in scaffold
    assert "deploy/AGENT-UNIT.md" not in scaffold  # constructed path, not a second template


def test_transport_wrapper_calls_the_one_platform_renderer():
    script = (ROOT / "scripts/deploy-to-morty.sh").read_text(encoding="utf-8")
    assert "scripts/render-agent-units.py" in script
    assert '"$RENDERER" "${renderer_args[@]}"' in script
    assert '--output-dir "$stage"' in script
    assert "--verify" in script
    assert "s|${DEPT_SLUG}" not in script
    assert '.replace("${DEPT_SLUG}"' not in script
    assert "bubble-agent@${SLUG}.service" in script
    assert 'legacy_service="ops-loop-${SLUG}.service"' in script


def test_transport_preserves_os_user_controls_and_orders_legacy_cutover():
    script = (ROOT / "scripts/deploy-to-morty.sh").read_text(encoding="utf-8")
    for option in ("--os-user", "--os-group", "--workdir"):
        assert option in script
    stop = 'systemctl stop ${legacy_service}'
    disable = 'systemctl disable ${legacy_service}'
    start = 'systemctl enable --now ${service}'
    assert script.index(stop) < script.index(disable) < script.index(start)
    assert "claude-agent-morty.service" not in script


def test_runtime_control_paths_use_canonical_instance_name():
    for relative in (
        "scripts/loop-backup.sh",
        "scripts/loop-tick-watchdog.py",
        "scripts/lib/cancel_eclosion.py",
        "scripts/lib/retire_dept.py",
    ):
        body = (ROOT / relative).read_text(encoding="utf-8")
        assert "bubble-agent@" in body, relative


def test_transport_dry_run_renders_and_verifies_without_contacting_host(tmp_path: Path):
    tenant = tmp_path / "tenant.yaml"
    dept = tmp_path / "dept.yaml"
    tenant.write_text(yaml.safe_dump({
        "tenant_name": "bubble-internal",
        "secrets": {"age_key_path": "/etc/age/key.txt"},
        "agent": {"concierges": []},
    }), encoding="utf-8")
    dept.write_text(yaml.safe_dump({
        "department": {"slug": "maya", "model": "sonnet[1m]"},
    }), encoding="utf-8")
    result = subprocess.run([
        "bash", str(ROOT / "scripts/deploy-to-morty.sh"),
        "--slug=maya", f"--tenant-yaml={tenant}", f"--dept-yaml={dept}",
        f"--platform-root={ROOT.parent / 'bubble-vps-platform'}", "--dry-run",
    ], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scp " in result.stdout
    assert "systemd-analyze verify bubble-agent@maya.service" in result.stdout
    assert "ssh: " not in result.stderr
