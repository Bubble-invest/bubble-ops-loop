"""CLI env resolution + error-path tests — push coverage of cli.py.

These tests exercise the env-var fallbacks the broker uses when CLI flags
aren't passed (production install on Morty relies on EnvironmentFile=).
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock

import pytest


def test_cli_uses_env_app_id_and_installation_id(
    monkeypatch, tmp_path, mock_pem, ops_policy_yaml, mock_github_token_response
):
    """When --app-id / --installation-id are NOT passed, fall back to env."""
    from src import broker as broker_module
    from src.cli import main

    pem_path = tmp_path / "test-app.pem"
    pem_path.write_bytes(mock_pem)

    monkeypatch.setenv("GITHUB_APP_ID", "9999999")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID_FIXTURE", "11111111")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(pem_path))
    monkeypatch.setenv("BUBBLE_TOKEN_BROKER_POLICY", str(ops_policy_yaml))

    # Mock real requests.post so we don't hit GitHub
    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.json.return_value = mock_github_token_response
    fake_resp.raise_for_status = MagicMock()
    monkeypatch.setattr(broker_module.requests, "post", MagicMock(return_value=fake_resp))

    audit_log = tmp_path / "audit.jsonl"
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        rc = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_read",
                "--repo", "bubble-ops-fixture",
                "--no-sops",  # tests pass a plaintext PEM
                "--audit-log", str(audit_log),
            ]
        )
    assert rc == 0
    # Audit confirms the action class was honored
    row = json.loads(audit_log.read_text().strip())
    assert row["status"] == "issued"


def test_cli_falls_back_to_bubble_ops_fixture_install_id(
    monkeypatch, tmp_path, mock_pem, ops_policy_yaml, mock_github_token_response
):
    """If dept-specific installation env var isn't set, fall back to canonical name."""
    from src import broker as broker_module
    from src.cli import main

    pem_path = tmp_path / "test-app.pem"
    pem_path.write_bytes(mock_pem)

    # Only the canonical fallback name is set
    monkeypatch.setenv("GITHUB_APP_ID", "9999999")
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID_FIXTURE", raising=False)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE", "134075326")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(pem_path))
    monkeypatch.setenv("BUBBLE_TOKEN_BROKER_POLICY", str(ops_policy_yaml))

    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.json.return_value = mock_github_token_response
    fake_resp.raise_for_status = MagicMock()
    monkeypatch.setattr(broker_module.requests, "post", MagicMock(return_value=fake_resp))

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_read",
                "--repo", "bubble-ops-fixture",
                "--no-sops",
                "--audit-log", str(tmp_path / "audit.jsonl"),
            ]
        )
    assert rc == 0


def test_cli_missing_app_id_exits(monkeypatch, tmp_path, mock_pem, ops_policy_yaml):
    """No --app-id and no GITHUB_APP_ID env → SystemExit."""
    from src.cli import main

    pem_path = tmp_path / "test-app.pem"
    pem_path.write_bytes(mock_pem)

    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(pem_path))

    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "mint",
                    "--dept", "fixture",
                    "--action", "runtime_read",
                    "--repo", "bubble-ops-fixture",
                    "--installation-id", "1",
                    "--pem-path", str(pem_path),
                    "--no-sops",
                    # No --mock-github -> must resolve app_id from env (missing)
                ]
            )


def test_cli_missing_installation_id_exits(monkeypatch, tmp_path, mock_pem):
    from src.cli import main

    pem_path = tmp_path / "test-app.pem"
    pem_path.write_bytes(mock_pem)

    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID_FIXTURE", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE", raising=False)

    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "mint",
                    "--dept", "fixture",
                    "--action", "runtime_read",
                    "--repo", "bubble-ops-fixture",
                    "--app-id", "1",
                    "--pem-path", str(pem_path),
                    "--no-sops",
                ]
            )


def test_cli_missing_pem_path_exits(monkeypatch):
    from src.cli import main

    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "mint",
                    "--dept", "fixture",
                    "--action", "runtime_read",
                    "--repo", "bubble-ops-fixture",
                    "--app-id", "1",
                    "--installation-id", "2",
                    # No --pem-path, no env
                ]
            )


def test_cli_check_requires_policy(monkeypatch):
    """`check` without a --policy and no env var must exit non-zero."""
    from src.cli import main

    monkeypatch.delenv("BUBBLE_TOKEN_BROKER_POLICY", raising=False)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
        rc = main(
            [
                "check",
                "--dept", "fixture",
                "--action", "runtime_read",
                "--repo", "bubble-ops-fixture",
            ]
        )
    assert rc != 0


def test_cli_real_mint_handles_github_error(
    monkeypatch, tmp_path, mock_pem, ops_policy_yaml
):
    """If the real (non-mock) GitHub call raises, audit logs failed + rc != 0."""
    from src import broker as broker_module
    from src.cli import main

    pem_path = tmp_path / "test-app.pem"
    pem_path.write_bytes(mock_pem)

    bad_resp = MagicMock()
    bad_resp.status_code = 401
    bad_resp.raise_for_status.side_effect = RuntimeError("401 Bad credentials")
    monkeypatch.setattr(broker_module.requests, "post", MagicMock(return_value=bad_resp))

    audit_log = tmp_path / "audit.jsonl"
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_read",
                "--repo", "bubble-ops-fixture",
                "--app-id", "1",
                "--installation-id", "2",
                "--pem-path", str(pem_path),
                "--no-sops",
                "--policy", str(ops_policy_yaml),
                "--audit-log", str(audit_log),
            ]
        )
    assert rc != 0
    row = json.loads(audit_log.read_text().strip().split("\n")[0])
    assert row["status"] == "failed"
    assert "error" in row


def test_cli_no_subcommand_prints_help(monkeypatch):
    from src.cli import main

    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        rc = main([])
    text = out.getvalue()
    assert rc == 0
    assert "bubble-token-broker" in text


def test_audit_rejects_token_lookalike(tmp_path):
    """Audit.log must refuse a value that looks like a leaked installation token."""
    from src.audit import Audit

    a = Audit(log_path=tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        a.log(
            ts="2026-05-20T15:00:00Z",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_read",
            permissions=["contents:read"],
            actor="ops-loop-fixture",
            token_ttl_minutes=60,
            status="issued",
            note="ghs_ACCIDENTALleakIntoNoteField",  # caller bug — must be caught
        )


def test_audit_drops_forbidden_field_names(tmp_path):
    """If caller passes `token=`/`pem=`/`secret=`, they get silently dropped."""
    from src.audit import Audit

    a = Audit(log_path=tmp_path / "audit.jsonl")
    a.log(
        ts="2026-05-20T15:00:00Z",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_read",
        permissions=["contents:read"],
        actor="ops-loop-fixture",
        token_ttl_minutes=60,
        status="issued",
        token="this_must_be_dropped",
        pem=b"BEGIN PRIVATE KEY",
        secret="hunter2",
    )
    content = (tmp_path / "audit.jsonl").read_text()
    assert "this_must_be_dropped" not in content
    assert "hunter2" not in content


def test_audit_missing_required_fields_raises(tmp_path):
    from src.audit import Audit

    a = Audit(log_path=tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        a.log(
            ts="2026-05-20T15:00:00Z",
            # missing dept/repo/etc.
        )
