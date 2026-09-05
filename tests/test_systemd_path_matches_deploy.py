"""#1120 workdir invariant after the #1119 canonical-renderer hand-off."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_RENDERER = ROOT.parent / "bubble-vps-platform" / "lib" / "agent_unit_renderer.py"


def test_deploy_reads_workdir_from_the_rendered_canonical_dropin():
    deploy = (ROOT / "scripts" / "deploy-to-morty.sh").read_text(encoding="utf-8")
    assert "WORKDIR=\"$(sed -n 's/^WorkingDirectory=//p' \"$stage/$dropin\")\"" in deploy
    assert "test -d $WORKDIR" in deploy
    assert "chown -R ${OS_USER}:${OS_GROUP} $WORKDIR" in deploy


def test_canonical_renderer_preserves_1120_default_path_convention():
    source = PLATFORM_RENDERER.read_text(encoding="utf-8")
    assert "agent_workdir(slug, os_user)" in source
    assert "LEGACY_OS_USER" in source
    assert "WorkingDirectory={workdir}" in source
