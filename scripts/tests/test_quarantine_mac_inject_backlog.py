from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "quarantine-mac-inject-backlog.py"
SPEC = importlib.util.spec_from_file_location("quarantine_inject", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def fixture(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    body = b"unknown-one\nunknown-two\n"
    inject = state / "inject"
    inject.write_bytes(body)
    inject.chmod(0o644)
    return state, inject, body, hashlib.sha256(body).hexdigest()


def test_dry_run_reports_metadata_and_changes_nothing(tmp_path):
    state, inject, body, digest = fixture(tmp_path)
    result = mod.quarantine(state, "content", digest, 2, False)
    assert result == {"bytes": len(body), "lines": 2, "sha256": digest, "applied": False}
    assert inject.read_bytes() == body
    assert not (state / ".held-maintenance-inject").exists()


def test_apply_moves_exact_bytes_and_creates_private_empty_queue(tmp_path):
    state, inject, body, digest = fixture(tmp_path)
    result = mod.quarantine(state, "content", digest, 2, True)
    held = state / ".held-maintenance-inject" / result["held_name"]
    assert result["applied"] is True
    assert held.read_bytes() == body
    assert hashlib.sha256(held.read_bytes()).hexdigest() == digest
    assert inject.read_bytes() == b""
    assert stat.S_IMODE(inject.stat().st_mode) == 0o600
    assert stat.S_IMODE(held.parent.stat().st_mode) == 0o700


def test_changed_plan_symlink_hardlink_and_peer_writable_inputs_fail(tmp_path):
    state, inject, body, digest = fixture(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        mod.quarantine(state, "content", "0" * 64, 2, False)
    outside = tmp_path / "outside"
    outside.write_bytes(body)
    inject.unlink()
    inject.symlink_to(outside)
    with pytest.raises(OSError):
        mod.quarantine(state, "content", digest, 2, False)
    inject.unlink()
    os.link(outside, inject)
    with pytest.raises(ValueError, match="regular file"):
        mod.quarantine(state, "content", digest, 2, False)
    inject.unlink()
    inject.write_bytes(body)
    inject.chmod(0o666)
    with pytest.raises(ValueError, match="regular file"):
        mod.quarantine(state, "content", digest, 2, False)


def test_symlink_parent_is_rejected_without_touching_target(tmp_path):
    real, _inject, body, digest = fixture(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        mod.quarantine(alias, "content", digest, 2, True)
    assert (real / "inject").read_bytes() == body


def test_post_rename_competing_queue_is_never_overwritten(tmp_path, monkeypatch):
    state, inject, body, digest = fixture(tmp_path)
    real_open = mod.os.open

    def racing_open(path, flags, *args, **kwargs):
        if path == "inject" and flags & os.O_EXCL:
            inject.write_bytes(b"concurrent-new-line\n")
            raise FileExistsError("synthetic writer won race")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(mod.os, "open", racing_open)
    with pytest.raises(FileExistsError, match="synthetic writer"):
        mod.quarantine(state, "content", digest, 2, True)
    assert inject.read_bytes() == b"concurrent-new-line\n"
    held = list((state / ".held-maintenance-inject").iterdir())
    assert len(held) == 1
    assert held[0].read_bytes() == body
