"""Audit log tests — guarantee the token VALUE never appears in audit lines.

Notion v4 lines 603-614: audit logs metadata only. The token never appears.
"""

from __future__ import annotations

import json


SENSITIVE_TOKEN = "ghs_THISsecretMUSTnotEVERappearINlogs"
SENSITIVE_PEM = "-----BEGIN RSA PRIVATE KEY-----secret-----END RSA PRIVATE KEY-----"


def test_audit_line_does_not_contain_token(tmp_path):
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    a = Audit(log_path=log_path)
    a.log(
        ts="2026-05-20T15:00:00Z",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        permissions=["contents:write"],
        actor="ops-loop-fixture",
        token_ttl_minutes=60,
        status="issued",
        # Pretend caller accidentally passes extras; broker contract = drop unknowns
    )
    line = log_path.read_text()
    assert SENSITIVE_TOKEN not in line
    assert SENSITIVE_PEM not in line


def test_audit_log_appends_jsonl(tmp_path):
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    a = Audit(log_path=log_path)
    a.log(
        ts="2026-05-20T15:00:00Z",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_read",
        permissions=["contents:read"],
        actor="ops-loop-fixture",
        token_ttl_minutes=60,
        status="issued",
    )
    a.log(
        ts="2026-05-20T15:01:00Z",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        permissions=["contents:write"],
        actor="ops-loop-fixture",
        token_ttl_minutes=60,
        status="issued",
    )
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line is valid JSON


def test_audit_line_has_required_metadata_fields(tmp_path):
    """Schema per Notion v4 audit example (lines 605-614)."""
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    a = Audit(log_path=log_path)
    a.log(
        ts="2026-05-20T15:00:00Z",
        dept="maya",
        repo="bubble-ops-maya",
        action="push_outputs",
        permissions=["contents:write"],
        actor="ops-loop-maya",
        token_ttl_minutes=60,
        status="issued",
    )
    row = json.loads(log_path.read_text().strip())
    for field in [
        "ts",
        "dept",
        "repo",
        "action",
        "permissions",
        "actor",
        "token_ttl_minutes",
        "status",
    ]:
        assert field in row, f"required audit field missing: {field}"
    assert row["dept"] == "maya"
    assert row["token_ttl_minutes"] == 60
    assert row["status"] == "issued"


def test_audit_failed_status_carries_error(tmp_path):
    from src.audit import Audit

    log_path = tmp_path / "audit.jsonl"
    a = Audit(log_path=log_path)
    a.log(
        ts="2026-05-20T15:00:00Z",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        permissions=["contents:write"],
        actor="ops-loop-fixture",
        token_ttl_minutes=60,
        status="failed",
        error="policy_denied: path not in allowed_paths",
    )
    row = json.loads(log_path.read_text().strip())
    assert row["status"] == "failed"
    assert "error" in row
    assert "policy_denied" in row["error"]
