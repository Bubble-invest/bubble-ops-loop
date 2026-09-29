"""Board #1616: transient token/GitHub failures get bounded fresh-token retries."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_push_retries_mint_and_repository_not_found_with_fresh_tokens(
    tmp_path, monkeypatch, capsys
):
    minted = iter([None, "ghs_fake_attempt_2", "ghs_fake_attempt_3"])
    mint_calls = []
    sleeps = []
    pushes = []

    def fake_mint(repo_name, repo_dir=None):
        mint_calls.append((repo_name, repo_dir))
        return next(minted)

    def fake_run(cmd, **_kwargs):
        if "status" in cmd and "--porcelain" in cmd:
            return _completed(cmd, stdout=" M queues/management/directive-x.yaml\n")
        if "push" in cmd:
            pushes.append(cmd)
            if len(pushes) == 1:
                return _completed(cmd, 128, stderr="remote: Repository not found.")
        return _completed(cmd)

    monkeypatch.setattr(dd, "_mint_token", fake_mint)
    monkeypatch.setattr(dd, "_run", fake_run)
    monkeypatch.setattr(dd, "_sleep_before_transport_retry", sleeps.append)

    ok, detail = dd._push_repo(
        tmp_path,
        "bubble-ops-maya",
        "deliver test directive",
        False,
        paths=["queues/management/directive-x.yaml"],
    )

    assert (ok, detail) == (True, "pushed")
    assert len(mint_calls) == 3
    assert len(pushes) == 2
    assert sleeps == [1, 2]
    output = capsys.readouterr().out
    assert "mint-empty" in output
    assert "repository-not-found" in output
    assert "ghs_fake_attempt" not in output


def test_push_stops_after_bounded_mint_failures(tmp_path, monkeypatch):
    mint_calls = []
    sleeps = []

    def fake_run(cmd, **_kwargs):
        if "status" in cmd and "--porcelain" in cmd:
            return _completed(cmd, stdout=" M queues/management/directive-x.yaml\n")
        return _completed(cmd)

    monkeypatch.setattr(dd, "_run", fake_run)
    monkeypatch.setattr(
        dd,
        "_mint_token",
        lambda *_args: mint_calls.append("called") or None,
    )
    monkeypatch.setattr(dd, "_sleep_before_transport_retry", sleeps.append)

    ok, detail = dd._push_repo(
        tmp_path,
        "bubble-ops-maya",
        "deliver test directive",
        False,
        paths=["queues/management/directive-x.yaml"],
    )

    assert ok is False
    assert "could not mint token" in detail
    assert len(mint_calls) == dd._TRANSPORT_MAX_RETRIES + 1
    assert sleeps == [1, 2, 3]


def test_push_does_not_retry_non_transient_rejection(tmp_path, monkeypatch):
    sleeps = []
    mint_calls = []

    def fake_run(cmd, **_kwargs):
        if "status" in cmd and "--porcelain" in cmd:
            return _completed(cmd, stdout=" M queues/management/directive-x.yaml\n")
        if "push" in cmd:
            return _completed(cmd, 1, stderr="rejected: non-fast-forward")
        return _completed(cmd)

    monkeypatch.setattr(dd, "_run", fake_run)
    monkeypatch.setattr(
        dd,
        "_mint_token",
        lambda *_args: mint_calls.append("called") or "ghs_fake",
    )
    monkeypatch.setattr(dd, "_sleep_before_transport_retry", sleeps.append)

    ok, detail = dd._push_repo(
        tmp_path,
        "bubble-ops-maya",
        "deliver test directive",
        False,
        paths=["queues/management/directive-x.yaml"],
    )

    assert ok is False
    assert "transport-error" in detail
    assert len(mint_calls) == 1
    assert sleeps == []


def test_clone_retries_403_with_fresh_token_and_cleans_partial_destination(
    tmp_path, monkeypatch, capsys
):
    destination = tmp_path / "private" / "bubble-ops-maya"
    tokens = iter(["ghs_fake_clone_1", "ghs_fake_clone_2"])
    mint_calls = []
    sleeps = []
    clone_calls = []

    def fake_mint(repo_name, repo_dir=None):
        mint_calls.append((repo_name, repo_dir))
        return next(tokens)

    def fake_run(cmd, **_kwargs):
        assert "clone" in cmd
        clone_calls.append(cmd)
        if len(clone_calls) == 1:
            destination.mkdir(parents=True)
            (destination / "partial").write_text("incomplete")
            return _completed(cmd, 128, stderr="fatal: unable to access: 403")
        assert not destination.exists(), "partial clone must be removed before retry"
        destination.mkdir(parents=True)
        (destination / ".git").mkdir()
        return _completed(cmd)

    monkeypatch.setattr(dd, "_mint_token", fake_mint)
    monkeypatch.setattr(dd, "_run", fake_run)
    monkeypatch.setattr(dd, "_sleep_before_transport_retry", sleeps.append)

    assert dd._clone_remote_repo(destination, "bubble-ops-maya") == (True, "cloned")
    assert len(mint_calls) == 2
    assert len(clone_calls) == 2
    assert sleeps == [1]
    output = capsys.readouterr().out
    assert "http-403" in output
    assert "ghs_fake_clone" not in output


def test_retry_backoff_is_exponential_jittered_and_bounded(monkeypatch):
    sleeps = []
    jitter_windows = []

    def upper_jitter(low, high):
        jitter_windows.append((low, high))
        return high

    monkeypatch.setattr(dd.random, "uniform", upper_jitter)
    monkeypatch.setattr(dd.time, "sleep", sleeps.append)

    for retry_number in (1, 2, 3):
        dd._sleep_before_transport_retry(retry_number)

    assert sleeps == [5.0, 15.0, 45.0]
    assert jitter_windows == [(0.0, 2.5), (0.0, 7.5), (0.0, 22.5)]
