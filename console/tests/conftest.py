"""
Test fixtures for console/ — bubble-ops-console (UX-3).

Provides:
  - `client`            : FastAPI TestClient with bearer-auth header pre-set
  - `client_noauth`     : TestClient WITHOUT the header (for auth tests)
  - `bearer_token`      : the test bearer token value
  - `fixtures_dir`      : path to console/tests/fixtures/ (on-disk dept data)
  - `temp_dept_repo`    : a temp directory shaped like a bubble-ops-<slug> repo
  - `mock_gh`           : monkeypatched subprocess fake recording all `gh` calls
  - `disk_mode_env`     : sets READ_FROM_DISK so services read fixtures, not GH

These fixtures guarantee NO real GitHub or SSH calls happen during tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Dict, Any

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_BEARER = "test-token-xyz"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def bearer_token() -> str:
    return TEST_BEARER


@pytest.fixture
def temp_dept_repo(tmp_path: Path) -> Path:
    """
    Build a minimal bubble-ops-<slug> on-disk repo with:
      - dept.yaml
      - onboarding/STATE.yaml (mid-onboarding, 3/6 steps validated)
      - queues/gates/sample-gate.yaml (one pending gate)
      - outputs/onboarding/N-mandate/chat.log
      - missions/echo.yaml
    """
    slug = "miranda"
    repo = tmp_path / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml.draft").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops",
                           "mandate": "produce social content"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {
                "social_post": {
                    "current_mode": "manual_required",
                    "eligible_future_modes": ["auto_with_veto_window"],
                }
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "slug": slug,
            "display_name": "Miranda",
            "owner": "operator",
            "created_at": "2026-05-19T10:00:00Z",
            "status": "Drafting",
            "validated_steps": ["mandate", "missions", "layers"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [
                {"step": "mandate", "commit_sha": "a" * 7,
                 "validated_at": "2026-05-19T11:00:00Z"},
                {"step": "missions", "commit_sha": "b" * 7,
                 "validated_at": "2026-05-19T15:00:00Z"},
                {"step": "layers", "commit_sha": "c" * 7,
                 "validated_at": "2026-05-20T10:00:00Z"},
            ],
        }, sort_keys=False),
        encoding="utf-8",
    )
    # Chat log for step 1 (mandate)
    chat_dir = repo / "onboarding" / "1-mandate"
    chat_dir.mkdir(parents=True)
    (chat_dir / "chat.log").write_text(
        "OPERATOR: I want a content dept\n"
        "AGENT: Great — what's the one-sentence mandate?\n"
        "OPERATOR: Produce, plan, audit social content for Bubble.\n"
        "AGENT: Validated. Moving to step 2.\n",
        encoding="utf-8",
    )
    # One pending gate
    gates_dir = repo / "queues" / "gates"
    gates_dir.mkdir(parents=True)
    (gates_dir / "post-draft-1.yaml").write_text(
        yaml.safe_dump({
            "id": "post-draft-1",
            "kind": "social_post",
            "source_layer": 2,
            "target_layer": 3,
            "risk_level": "low",
            "requires_human": True,
            "current_mode": "manual_required",
            "gate_policy_id": "social_post",
            "actions": ["approve", "reject", "modify", "defer"],
            "draft": {"text": "Hello LinkedIn — a thought on agentic AI."},
        }, sort_keys=False),
        encoding="utf-8",
    )
    # One mission
    (repo / "missions").mkdir()
    (repo / "missions" / "echo.yaml").write_text(
        yaml.safe_dump({
            "id": "echo_heartbeat", "layer": 1, "cadence": "every_2h",
        }, sort_keys=False),
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    """
    Build a multi-dept root (the parent dir that contains all bubble-ops-<slug>
    repos in disk mode). Contains 2 depts:
      - 'fixture'   : status=Live, 6/6 steps validated (live dept)
      - 'miranda'   : status=Drafting, 3/6 steps validated (a-eclore dept)
    """
    root = tmp_path / "depts"
    root.mkdir()

    # live fixture dept
    live = root / "bubble-ops-fixture"
    live.mkdir()
    (live / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops",
                           "mandate": "MVP fixture"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {
                "echo_action": {
                    "current_mode": "manual_required",
                    "eligible_future_modes": ["auto_if_policy_passed"],
                }
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "onboarding").mkdir()
    (live / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "fixture", "display_name": "Fixture",
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "queues" / "gates").mkdir(parents=True)
    (live / "queues" / "gates" / "echo-1.yaml").write_text(
        yaml.safe_dump({
            "id": "echo-1", "kind": "echo_action", "source_layer": 2,
            "target_layer": 3, "risk_level": "low", "requires_human": True,
            "current_mode": "manual_required",
            "gate_policy_id": "echo_action",
            "actions": ["approve", "reject", "modify", "defer"],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "outputs").mkdir()

    # a-eclore miranda dept (incomplete)
    miranda = root / "bubble-ops-miranda"
    miranda.mkdir()
    (miranda / "dept.yaml.draft").write_text("department:\n  slug: miranda\n",
                                              encoding="utf-8")
    (miranda / "onboarding").mkdir()
    (miranda / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "miranda", "display_name": "Miranda",
            "owner": "operator", "created_at": "2026-05-19T10:00:00Z",
            "status": "Drafting",
            "validated_steps": ["mandate", "missions", "layers"],
            "last_updated_at": "2026-05-20T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )

    return root


@pytest.fixture
def app(monkeypatch, fixture_root: Path):
    """
    Build the FastAPI app with READ_FROM_DISK=<fixture_root> so all services
    read from local fixtures rather than calling `gh`/SSH.
    """
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(fixture_root))

    # Force a fresh import so env vars are picked up.
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


@pytest.fixture
def client_noauth(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def mock_gh(monkeypatch, tmp_path: Path):
    """
    Replace any subprocess call to `gh` with a recorder. Returns a list of
    (args, env) tuples. The fake exits 0 and writes its argv to a JSONL.
    """
    calls_file = tmp_path / "gh_calls.jsonl"

    import subprocess
    original_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if (isinstance(cmd, list) and cmd and cmd[0].endswith("gh")) or \
           (isinstance(cmd, str) and "gh " in cmd):
            with calls_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"cmd": cmd}) + "\n")

            class _R:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return _R()
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls_file


@pytest.fixture
def mock_bootstrap(monkeypatch, tmp_path: Path):
    """
    Replace any subprocess call to bootstrap-dept.sh with a recorder.
    Streams a fake "OK: created bubble-ops-<slug>" line.
    """
    calls_file = tmp_path / "bootstrap_calls.jsonl"

    import subprocess
    original_popen = subprocess.Popen
    original_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and any(
            "bootstrap-dept" in str(c) for c in cmd
        ):
            with calls_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"cmd": cmd}) + "\n")

            class _R:
                returncode = 0
                stdout = "OK: created bubble-ops-foo on onboarding/foo branch\n"
                stderr = ""
            return _R()
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls_file
