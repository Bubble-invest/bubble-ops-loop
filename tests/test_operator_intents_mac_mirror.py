"""Targeted Mac root-mirror installer and isolation tests (#1267)."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "deploy/local"
INSTALLER = LOCAL / "install-operator-intents-mirror.sh"
SYNC = LOCAL / "operator-intents-mirror-sync.sh.template"
PLIST = LOCAL / "com.bubble.operator-intents-mirror.plist.template"
VERIFY = LOCAL / "verify-operator-intents-isolation.sh"
COMPILE = ROOT / "skills/cloud-wiki-compile/scripts/cloud-wiki-compile.sh"


def test_scripts_are_executable_and_bash_valid() -> None:
    for path in (INSTALLER, SYNC, VERIFY):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_plist_is_root_launchdaemon_at_load_every_900_seconds() -> None:
    with PLIST.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["Label"] == "com.bubble.operator-intents-mirror"
    assert data["UserName"] == "root"
    assert data["RunAtLoad"] is True
    assert data["StartInterval"] == 900
    assert data["ProgramArguments"] == [
        "/Library/Application Support/Bubble/bin/operator-intents-mirror-sync"
    ]


def test_default_installer_run_only_renders_and_validates(tmp_path: Path) -> None:
    render = tmp_path / "render"
    result = subprocess.run(
        [str(INSTALLER), "--render-dir", str(render)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "render-only: no live file or launchd state changed" in result.stderr
    assert (render / "operator-intents-mirror-sync").is_file()
    rendered_plist = render / "com.bubble.operator-intents-mirror.plist"
    with rendered_plist.open("rb") as handle:
        assert plistlib.load(handle)["UserName"] == "root"


def test_nonroot_activation_fails_before_live_mutation(tmp_path: Path) -> None:
    if os.getuid() == 0:
        return
    result = subprocess.run(
        [str(INSTALLER), "--render-dir", str(tmp_path / "render"), "--activate"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--activate requires uid 0" in result.stderr


def test_sync_has_exact_read_only_auth_and_no_write_credential_path() -> None:
    source = SYNC.read_text(encoding="utf-8")
    assert "git@github.com:vdk888/bubble-operator-intents.git" in source
    assert "REMOTE_REF='main'" in source
    assert "operator-intents-readonly-deploy-key" in source
    assert "root:wheel:400" in source
    assert "IdentitiesOnly=yes" in source
    assert "refs/heads/$REMOTE_REF" in source
    for forbidden in (
        "GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "credential.helper",
        "https://github.com", "git push", "/Users/joris", "/home/claude",
    ):
        assert forbidden not in source


def test_sync_enforces_immutable_publication_tamper_heal_and_alarm() -> None:
    source = SYNC.read_text(encoding="utf-8")
    for required in (
        "root:wheel", "0555", "0444", ".mirror-commit", ".mirror-manifest",
        ".mirror-synced-at", "vault content contains a symlink",
        "rm -rf -- \"$checkout/.git\"", "tamper was healed",
        "notify_operator", "/bin/launchctl asuser", "/usr/bin/osascript",
        "vault main moved during sync", "remote get-url origin",
    ):
        assert required in source
    assert "root_info = root.lstat()" in source
    assert "stat.S_IMODE(root_info.st_mode) != 0o555" in source
    assert "stable mirror path is missing while releases exist" in source
    assert "preexisting mirror differed from vault main; divergence was healed" in source
    assert "path not in {root / '.mirror-manifest', root / '.mirror-synced-at'}" in source
    assert "synced_tmp=\"$stage/synced-at\"" in source
    assert "trap unexpected_error ERR" in source


def test_activation_is_transactional_and_never_handles_key_bytes() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert "old_sync=0; old_plist=0; was_loaded=0" in source
    assert "restore()" in source
    assert "launchctl bootout" in source
    assert "launchctl bootstrap" in source
    assert "install -o root -g wheel -m 0755" in source
    assert "install -o root -g wheel -m 0644" in source
    assert "plistlib.load" in source
    assert "plutil -lint" in source
    assert '"$LIVE_SYNC" || die' in source
    assert "mirror_ready" not in source
    assert "cat \"$KEY\"" not in source
    assert "cp \"$KEY\"" not in source
    assert "stat -f '%Su:%Lp' \"$ancestor\"" in source
    assert "install -d -o root -g wheel -m 0755 \"$ancestor\"" not in source


def test_shared_consumer_validator_checks_release_itself_and_git_metadata() -> None:
    validator = (ROOT / "tools/readonly_intents_mirror.py").read_text(encoding="utf-8")
    assert "release_info = release.lstat()" in validator
    assert "release_info.st_uid != 0 or release_info.st_gid != 0" in validator
    assert "stat.S_IMODE(release_info.st_mode) != 0o555" in validator
    assert 'path.name == ".git"' in validator


def test_cloud_compile_passes_mirror_separately_and_fails_closed() -> None:
    source = COMPILE.read_text(encoding="utf-8")
    assert 'BUBBLE_OPERATOR_INTENTS_MIRROR:-/opt/bubble-operator-intents' in source
    assert '--wiki "$WIKI_DIR" --intents-root "$INTENTS_ROOT"' in source
    assert 'read-only operator-intents mirror unavailable' in source


def test_isolation_verifier_is_read_only_and_rejects_write_permissions() -> None:
    source = VERIFY.read_text(encoding="utf-8")
    for required in (
        "--agent-user", "test ! -w", "test ! -O", "test ! -r \"$KEY\"",
        ".viewerPermission", "ADMIN|MAINTAIN|WRITE", "READ|NONE",
        "! -perm 0555", "! -perm 0444", "! -user root", "! -group wheel",
    ):
        assert required in source
    commands = [line.strip().split()[0] for line in source.splitlines() if line.strip()]
    assert "chmod" not in commands
    assert "chown" not in commands
    assert "rm" not in commands
    assert "git" not in commands
