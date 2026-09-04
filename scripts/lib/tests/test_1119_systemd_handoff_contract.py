"""CI contract for the cross-repo #1119 systemd renderer hand-off."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_ops_loop_has_no_agent_unit_renderer_or_legacy_template():
    assert not (ROOT / "deploy/templates/ops-loop-dept.service.template").exists()
    scaffold = (ROOT / "scripts/lib/scaffold.py").read_text(encoding="utf-8")
    assert "def render_systemd_unit" not in scaffold
    assert "render_systemd_handoff" in scaffold


def test_deploy_wrapper_consumes_platform_renderer_and_verifies():
    source = (ROOT / "scripts/deploy-to-morty.sh").read_text(encoding="utf-8")
    assert "scripts/render-agent-units.py" in source
    assert '--output-dir "$stage" --verify' in source
    assert "bubble-agent@${SLUG}.service" in source
    assert "systemd-analyze verify ${service}" in source


def test_live_control_paths_share_the_canonical_unit_name():
    expected = {
        "scripts/loop-backup.sh": "bubble-agent@${slug}.service",
        "scripts/loop-tick-watchdog.py": 'f"bubble-agent@{slug}.service"',
        "scripts/lib/cancel_eclosion.py": '"bubble-agent@{slug}.service"',
        "scripts/lib/retire_dept.py": '"bubble-agent@{slug}.service"',
    }
    for relative, needle in expected.items():
        assert needle in (ROOT / relative).read_text(encoding="utf-8"), relative
