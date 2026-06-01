"""CLI end-to-end tests — invoke the bubble-token-broker CLI in-process.

Critical invariants:
  - token printed to stdout ONLY (never written to disk by default)
  - audit log goes to stderr or --audit-log path, never contains the token
  - --mock-github bypass works (no live API call needed for offline tests)
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout


def _write_pem_to_tmp(tmp_path, pem_bytes):
    p = tmp_path / "test-app.pem"
    p.write_bytes(pem_bytes)
    return p


def test_cli_mint_writes_token_to_stdout_only(tmp_path, mock_pem, ops_policy_yaml):
    """The mint command must print only the token value to stdout.

    Use --mock-github to skip the real GitHub call. The PEM path is passed
    directly (not via SOPS in tests), but in production it's a SOPS-encrypted
    blob decrypted to memory only."""
    from src.cli import main

    pem_path = _write_pem_to_tmp(tmp_path, mock_pem)
    audit_log = tmp_path / "audit.jsonl"

    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_write_own",
                "--repo", "bubble-ops-fixture",
                "--app-id", "3782718",
                "--installation-id", "134075326",
                "--pem-path", str(pem_path),
                "--no-sops",  # treat the pem-path as plain (test-only)
                "--policy", str(ops_policy_yaml),
                "--audit-log", str(audit_log),
                "--mock-github",  # don't hit real GitHub
            ]
        )

    assert exit_code == 0
    stdout_value = stdout.getvalue().strip()
    # stdout must be exactly the token value (one line, no extras)
    assert stdout_value.startswith("ghs_"), f"stdout = {stdout_value!r}"
    assert "\n" not in stdout_value, "stdout must contain only the token, no extra lines"
    # Token must NOT appear in audit log
    assert stdout_value not in audit_log.read_text()


def test_cli_audit_log_contains_metadata_not_secret(tmp_path, mock_pem, ops_policy_yaml):
    from src.cli import main

    pem_path = _write_pem_to_tmp(tmp_path, mock_pem)
    audit_log = tmp_path / "audit.jsonl"

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_read",
                "--repo", "bubble-ops-fixture",
                "--app-id", "3782718",
                "--installation-id", "134075326",
                "--pem-path", str(pem_path),
                "--no-sops",
                "--policy", str(ops_policy_yaml),
                "--audit-log", str(audit_log),
                "--mock-github",
            ]
        )
    assert rc == 0
    row = json.loads(audit_log.read_text().strip().split("\n")[0])
    assert row["dept"] == "fixture"
    assert row["action"] == "runtime_read"
    assert row["status"] == "issued"
    # No `token`-named field, and no leaked value
    assert "token" not in row
    for v in row.values():
        if isinstance(v, str):
            assert not v.startswith("ghs_")


def test_cli_check_does_not_call_github(tmp_path, mock_pem, ops_policy_yaml):
    """`check` does policy-only enforcement, never mints, never calls GitHub."""
    from src.cli import main

    pem_path = _write_pem_to_tmp(tmp_path, mock_pem)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = main(
            [
                "check",
                "--dept", "fixture",
                "--action", "runtime_write_own",
                "--repo", "bubble-ops-fixture",
                "--paths", "outputs/2026-05-20/1/summary.md",
                "--policy", str(ops_policy_yaml),
                # No --pem-path required for check
            ]
        )
    assert rc == 0
    # check exits 0 on allowed, non-zero on denied; stdout summarizes
    assert "ALLOWED" in stdout.getvalue() or "allowed" in stdout.getvalue().lower()


def test_cli_check_denies_dept_yaml_path(tmp_path, mock_pem, ops_policy_yaml):
    from src.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = main(
            [
                "check",
                "--dept", "fixture",
                "--action", "runtime_write_own",
                "--repo", "bubble-ops-fixture",
                "--paths", "dept.yaml",
                "--policy", str(ops_policy_yaml),
            ]
        )
    assert rc != 0
    combined = stdout.getvalue() + stderr.getvalue()
    assert "DENIED" in combined or "denied" in combined.lower()


def test_cli_help_shows_examples(tmp_path):
    """`--help` must print usage with at least 3 example invocations."""
    from src.cli import main

    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout):
            main(["--help"])
    except SystemExit:
        pass  # argparse exits 0 on --help
    text = stdout.getvalue()
    # At least 3 examples per acceptance criteria
    example_count = text.lower().count("example")
    assert example_count >= 1, "Expected at least one 'Examples' header"
    # Heuristic: count `bubble-token-broker mint` / `bubble-token-broker check` occurrences
    invocation_count = text.count("bubble-token-broker")
    assert invocation_count >= 3, f"Need ≥3 example invocations, saw {invocation_count}"


def test_cli_does_not_leak_pem_to_disk(tmp_path, mock_pem, ops_policy_yaml):
    """The CLI must not write a copy of the PEM anywhere under tmp_path
    (other than the input PEM file)."""
    from src.cli import main

    pem_path = _write_pem_to_tmp(tmp_path, mock_pem)
    audit_log = tmp_path / "audit.jsonl"

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_write_own",
                "--repo", "bubble-ops-fixture",
                "--app-id", "3782718",
                "--installation-id", "134075326",
                "--pem-path", str(pem_path),
                "--no-sops",
                "--policy", str(ops_policy_yaml),
                "--audit-log", str(audit_log),
                "--mock-github",
            ]
        )
    # Confirm no other file under tmp_path contains the PEM
    pem_text = mock_pem.decode()
    pem_marker = pem_text.split("\n")[1]  # the first base64 line
    for root, _, files in os.walk(tmp_path):
        for f in files:
            p = os.path.join(root, f)
            if p == str(pem_path):
                continue
            try:
                content = open(p, "r", errors="ignore").read()
            except OSError:
                continue
            assert pem_marker not in content, f"PEM leaked into {p}"


def test_cli_mint_denies_when_policy_forbids(tmp_path, mock_pem, ops_policy_yaml):
    """If the action+path is forbidden by policy, mint must NOT hit GitHub and
    must return non-zero with an audit row marked 'failed'."""
    from src.cli import main

    pem_path = _write_pem_to_tmp(tmp_path, mock_pem)
    audit_log = tmp_path / "audit.jsonl"

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = main(
            [
                "mint",
                "--dept", "fixture",
                "--action", "runtime_write_own",
                "--repo", "bubble-ops-fixture",
                "--paths", "dept.yaml",  # forbidden under runtime_write_own
                "--app-id", "3782718",
                "--installation-id", "134075326",
                "--pem-path", str(pem_path),
                "--no-sops",
                "--policy", str(ops_policy_yaml),
                "--audit-log", str(audit_log),
                "--mock-github",
            ]
        )
    assert rc != 0
    row = json.loads(audit_log.read_text().strip().split("\n")[0])
    assert row["status"] == "failed"
    assert "error" in row
