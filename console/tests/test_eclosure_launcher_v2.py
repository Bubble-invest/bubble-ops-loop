"""Integration contract for cockpit éclosure's canonical unit hand-off."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("slug", ["tony", "maya", "ben", "miranda", "eliot"])
def test_all_department_levels_delegate_to_same_platform_renderer(
    slug, tmp_path: Path, monkeypatch
):
    from console.services import eclosure_launcher

    platform = tmp_path / "platform"
    agents = tmp_path / "agents"
    tenant = tmp_path / "tenant.yaml"
    renderer = platform / "scripts" / "render-agent-units.py"
    dept = agents / slug / "dept.yaml"
    renderer.parent.mkdir(parents=True)
    renderer.write_text("", encoding="utf-8")
    dept.parent.mkdir(parents=True)
    dept.write_text(f"department:\n  slug: {slug}\n", encoding="utf-8")
    tenant.write_text("tenant_name: test\n", encoding="utf-8")
    monkeypatch.setattr(eclosure_launcher, "PLATFORM_ROOT", platform)
    monkeypatch.setattr(eclosure_launcher, "AGENTS_PARENT", agents)
    monkeypatch.setattr(eclosure_launcher, "TENANT_YAML_PATH", tenant)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(eclosure_launcher.subprocess, "run", fake_run)
    unit, helper, dropin = eclosure_launcher._render_systemd_bundle(slug, tmp_path / "out")
    assert calls == [[
        str(renderer),
        "--tenant", str(tenant),
        "--dept", str(dept),
        "--output-dir", str(tmp_path / "out"),
        "--verify",
    ]]
    assert unit.name == "bubble-agent@.service"
    assert helper.name == "bubble-agent-prepare"
    assert dropin.as_posix().endswith(f"bubble-agent@{slug}.service.d/{slug}.conf")


def test_install_uses_only_canonical_bundle_destinations(tmp_path: Path, monkeypatch):
    from console.services import eclosure_launcher

    bundle = (
        tmp_path / "bubble-agent@.service",
        tmp_path / "bubble-agent-prepare",
        tmp_path / "maya.conf",
    )
    monkeypatch.setattr(eclosure_launcher, "_render_systemd_bundle", lambda *a: bundle)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(eclosure_launcher.subprocess, "run", fake_run)
    eclosure_launcher.install_systemd_unit("maya")
    joined = "\n".join(" ".join(cmd) for cmd in calls)
    assert "/etc/systemd/system/bubble-agent@.service" in joined
    assert "/etc/systemd/system/bubble-agent@maya.service.d/maya.conf" in joined
    assert "/usr/local/libexec/bubble-agent-prepare" in joined
    assert "ops-loop-maya.service" not in joined
    assert "claude-agent-morty.service" not in joined
