from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VPS = ROOT / "deploy" / "vps"
INSTALLER = ROOT / "scripts" / "install-operator-intents-mirror-linux.sh"
SYNC = VPS / "operator-intents-mirror-sync.sh.template"
VERIFY = VPS / "verify-operator-intents-isolation.sh"
SERVICE = ROOT / "deploy" / "templates" / "operator-intents-mirror.service"
TIMER = ROOT / "deploy" / "templates" / "operator-intents-mirror.timer"
VALIDATOR = ROOT / "tools" / "readonly_intents_mirror.py"
CLOUD_UNIT = ROOT / "deploy" / "templates" / "cloud-wiki-compile@.service"
CLOUD_INSTALLER = ROOT / "scripts" / "install-cloud-wiki-compile.sh"


def test_linux_installer_renders_complete_root_sync_bundle(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(INSTALLER), "--render-dir", str(tmp_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "render-only" in result.stderr
    assert (tmp_path / "operator-intents-mirror-sync").is_file()
    assert (tmp_path / "verify-operator-intents-isolation").is_file()
    assert (tmp_path / "operator-intents-mirror.service").is_file()
    assert (tmp_path / "operator-intents-mirror.timer").is_file()


def test_linux_sync_uses_dedicated_readonly_ssh_transport_and_immutable_paths() -> None:
    source = SYNC.read_text(encoding="utf-8")

    assert "git@github.com:Bubble-invest/bubble-operator-intents.git" in source
    assert "KEY='/etc/bubble/secrets/operator-intents-readonly-deploy-key'" in source
    assert "BASE='/opt/bubble-operator-intents-data'" in source
    assert "MIRROR='/opt/bubble-operator-intents'" in source
    assert "GIT_TERMINAL_PROMPT=0" in source
    assert "IdentitiesOnly=yes" in source
    assert "StrictHostKeyChecking=yes" in source
    assert "git push" not in source
    assert "GITHUB_TOKEN" not in source
    assert "GH_TOKEN" not in source
    assert "chown -R root:root" in source
    assert "chmod 0555" in source
    assert "chmod 0444" in source


def test_linux_systemd_bundle_runs_as_root_and_refreshes_every_15_minutes() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")

    assert "User=root" in service
    assert "ExecStart=/usr/local/sbin/operator-intents-mirror-sync" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/opt/bubble-operator-intents-data" in service
    assert "ReadWritePaths=/opt/bubble-operator-intents-data /opt" not in service
    assert "OnBootSec=1min" in timer
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer


def test_validator_cli_fails_closed_for_missing_mirror(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = subprocess.run(
        ["python3", str(VALIDATOR), str(missing)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert f"mirror is not a stable symlink: {missing}" in result.stderr


def test_cloud_wiki_service_validates_mirror_before_launcher() -> None:
    unit = CLOUD_UNIT.read_text(encoding="utf-8")
    preflight = "ExecStartPre=/usr/bin/python3 /home/claude/scripts/readonly_intents_mirror.py --mode %i"
    launcher = "ExecStart=/home/claude/scripts/cloud-wiki-compile.sh %i"

    assert preflight in unit
    assert unit.index(preflight) < unit.index(launcher)
    installer = CLOUD_INSTALLER.read_text(encoding="utf-8")
    assert '"$MIRROR_VALIDATOR_DST"' in installer
    assert "readonly_intents_mirror.py" in installer


def test_linux_activation_requires_explicit_readonly_attestation() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "--activate" in source
    assert "--deploy-key-readonly-attested" in source
    assert "--agent-user" in source
    assert "credential/UID preflight failed before activation" in source
    assert "live state unchanged" in source
