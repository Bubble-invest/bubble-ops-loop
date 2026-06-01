"""The broker's token value MUST NEVER appear in the audit JSONL.

Per Notion v4 line 720-723:
  "À ne jamais stocker: ... GITHUB_TOKEN dans logs, token dans outputs/,
   token dans queue items, token dans exceptions"

The guard's audit.py reuses the broker's pattern: forbidden_fields drop +
ghs_* prefix raises ValueError.
"""

from __future__ import annotations

import json
import pytest

from src.audit import GuardAudit
from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def test_token_value_never_in_audit_jsonl_after_push(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
    tmp_path,
):
    """Run a real allowed push, then grep audit for 'ghs_'."""
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    audit = tmp_path / "audit.jsonl"
    policy = load_policy(fixture_policy_yaml)
    g = Guard(
        policy=policy, broker_cmd=[str(mock_broker_binary)], audit_log_path=audit
    )
    g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    content = audit.read_text()
    assert "ghs_" not in content, f"audit leaked a token: {content}"
    assert "ghs" not in content, f"audit leaked the prefix 'ghs': {content}"


def test_audit_drops_forbidden_field_names():
    """Even if a caller passes token=..., audit must strip it pre-serialize."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as fh:
        path = fh.name
    a = GuardAudit(log_path=path)
    a.log(
        ts="2026-05-20T16:00:00Z",
        actor="ops-loop-fixture",
        dept="fixture",
        repo="bubble-ops-fixture",
        action="runtime_write_own",
        status="pushed",
        paths_count=1,
        token_ttl_minutes=60,
        # These should be DROPPED, not written:
        token="ghs_SHOULDNEVERAPPEAR1234567890",
        access_token="ghs_NORTHIS",
        pem=b"-----BEGIN PRIVATE KEY-----",
    )
    content = open(path).read()
    assert "ghs_" not in content
    assert "SHOULDNEVERAPPEAR" not in content
    assert "PRIVATE KEY" not in content


def test_audit_raises_when_value_starts_with_ghs():
    """Defense-in-depth: any value starting with ghs_ raises ValueError."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as fh:
        path = fh.name
    a = GuardAudit(log_path=path)
    with pytest.raises(ValueError, match="ghs_"):
        a.log(
            ts="2026-05-20T16:00:00Z",
            actor="ops-loop-fixture",
            dept="fixture",
            repo="bubble-ops-fixture",
            action="runtime_write_own",
            status="pushed",
            paths_count=1,
            token_ttl_minutes=60,
            # Field name not in FORBIDDEN_FIELDS, but value is a token shape:
            note="ghs_SNEAKY_TOKEN_LEAK_VIA_NOTE_FIELD",
        )
