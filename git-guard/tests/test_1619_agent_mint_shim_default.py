"""#1619 step 2: an `agent-<slug>` OS user's guard mints through the sudo shim.

One default, no per-dept config: resolve_broker_binary() returns the shim when
(and only when) the current OS user is `agent-*` and the shim is installed.
"""
from __future__ import annotations

import types

import pytest

from src import guard as guard_module
from src.guard import Guard, resolve_broker_binary
from src.policy_loader import load_policy
from tests.conftest import commit_staged, stage_files


def _as_user(monkeypatch, name):
    monkeypatch.setattr(
        guard_module.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_name=name)
    )


def _shim(monkeypatch, tmp_path, executable=True):
    shim = tmp_path / "bubble-broker-mint-settings.sh"
    shim.write_text("#!/bin/sh\n")
    shim.chmod(0o755 if executable else 0o644)
    monkeypatch.setattr(guard_module, "DEFAULT_AGENT_MINT_SHIM", str(shim))
    return str(shim)


def test_agent_user_gets_the_shim_by_default(monkeypatch, tmp_path):
    shim = _shim(monkeypatch, tmp_path)
    _as_user(monkeypatch, "agent-tony")
    assert resolve_broker_binary() == shim


def test_agent_user_shim_beats_a_broker_on_path(monkeypatch, tmp_path):
    """The in-process broker (agent-readable App key) must NOT win on PATH."""
    shim = _shim(monkeypatch, tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    broker = bindir / "bubble-token-broker"
    broker.write_text("#!/bin/sh\n")
    broker.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    _as_user(monkeypatch, "agent-ben")
    assert resolve_broker_binary() == shim


@pytest.mark.parametrize("broker", [
    guard_module.DEFAULT_BROKER_ABS_PATH,
    guard_module.DEFAULT_BROKER_NAME,
    "/tmp/other/bin/bubble-token-broker",
])
def test_agent_explicit_in_process_broker_uses_shim(
    monkeypatch, tmp_path, capsys, broker
):
    shim = _shim(monkeypatch, tmp_path)
    _as_user(monkeypatch, "agent-tony")
    assert resolve_broker_binary(broker) == shim
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"NOTE: --broker {broker} ignored for agent user; "
        "minting via the sudo shim (#1619)\n"
    )


def test_agent_explicit_shim_stays_verbatim(monkeypatch, tmp_path, capsys):
    shim = _shim(monkeypatch, tmp_path)
    _as_user(monkeypatch, "agent-tony")
    assert resolve_broker_binary(shim) == shim
    assert capsys.readouterr().err == ""


def test_agent_explicit_other_stub_stays_verbatim(monkeypatch, tmp_path, capsys):
    _shim(monkeypatch, tmp_path)
    _as_user(monkeypatch, "agent-maya")
    assert resolve_broker_binary("/tmp/x/fake-broker") == "/tmp/x/fake-broker"
    assert capsys.readouterr().err == ""


def test_non_agent_explicit_default_broker_stays_verbatim(
    monkeypatch, tmp_path, capsys
):
    _shim(monkeypatch, tmp_path)
    _as_user(monkeypatch, "claude")
    broker = guard_module.DEFAULT_BROKER_ABS_PATH
    assert resolve_broker_binary(broker) == broker
    assert capsys.readouterr().err == ""


def test_agent_explicit_default_broker_without_shim_stays_verbatim(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(guard_module, "DEFAULT_AGENT_MINT_SHIM", str(tmp_path / "missing"))
    _as_user(monkeypatch, "agent-tony")
    broker = guard_module.DEFAULT_BROKER_ABS_PATH
    assert resolve_broker_binary(broker) == broker
    assert capsys.readouterr().err == ""


def test_non_agent_users_keep_the_old_resolution(monkeypatch, tmp_path):
    _shim(monkeypatch, tmp_path)
    for user in ("claude", "root", "joris", "agentx", "bubble-console"):
        _as_user(monkeypatch, user)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr(guard_module, "DEFAULT_BROKER_ABS_PATH", str(tmp_path / "nope"))
        assert resolve_broker_binary() == guard_module.DEFAULT_BROKER_NAME, user


def test_agent_without_installed_shim_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(guard_module, "DEFAULT_AGENT_MINT_SHIM", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(guard_module, "DEFAULT_BROKER_ABS_PATH", str(tmp_path / "nope"))
    _as_user(monkeypatch, "agent-tony")
    assert resolve_broker_binary() == guard_module.DEFAULT_BROKER_NAME


def test_agent_shim_not_executable_is_ignored(monkeypatch, tmp_path):
    _shim(monkeypatch, tmp_path, executable=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(guard_module, "DEFAULT_BROKER_ABS_PATH", str(tmp_path / "nope"))
    _as_user(monkeypatch, "agent-tony")
    assert resolve_broker_binary() == guard_module.DEFAULT_BROKER_NAME


def test_unknown_uid_does_not_crash(monkeypatch, tmp_path):
    _shim(monkeypatch, tmp_path)

    def boom(uid):
        raise KeyError(uid)

    monkeypatch.setattr(guard_module.pwd, "getpwuid", boom)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(guard_module, "DEFAULT_BROKER_ABS_PATH", str(tmp_path / "nope"))
    assert resolve_broker_binary() == guard_module.DEFAULT_BROKER_NAME


def test_guard_default_ctor_mints_through_the_shim(
    monkeypatch, tmp_path, fixture_policy_yaml, temp_git_repo, mock_git_push
):
    """End to end: Guard() with no broker_cmd, running as agent-fixture, execs
    the shim for the write mint (never the bare broker)."""
    stub = tmp_path / "bubble-broker-mint-settings.sh"
    log = tmp_path / "calls.log"
    stub.write_text(
        '#!/bin/sh\necho "$@" >> %s\necho ghs_FAKESHIMTOKEN\n' % log
    )
    stub.chmod(0o755)
    monkeypatch.setattr(guard_module, "DEFAULT_AGENT_MINT_SHIM", str(stub))
    _as_user(monkeypatch, "agent-fixture")

    stage_files(temp_git_repo, ["outputs/x.md"])
    commit_staged(temp_git_repo)
    g = Guard(policy=load_policy(fixture_policy_yaml), audit_log_path=tmp_path / "audit.jsonl")
    assert g.broker_cmd == [str(stub)]
    g.push(repo_dir=temp_git_repo, dept="fixture", action="runtime_write_own",
           repo="bubble-ops-fixture")
    calls = log.read_text() if log.exists() else ""
    assert "--action runtime_write_own" in calls, calls
