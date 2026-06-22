"""test_dept_host_hybrid.py — Hybrid local/VPS agent: DeptSummary.host.

A dept declares its runtime host in onboarding/STATE.yaml (`host: vps|local`,
absent → vps). The registry surfaces it as DeptSummary.host so the cockpit can
treat a local dept (e.g. Miranda on {{OPERATOR_2}}'s Mac) host-aware (no VPS heartbeat
expected) while still showing its gates/state from its repo.

Note: each test builds its OWN isolated dept root and patches settings.disk_*
INSIDE the test body (not via a shared fixture) so there is zero interleaving
with other tests' app/client fixtures that also touch the read-mode settings.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console import settings
from console.services import dept_registry


def _write_dept(root: Path, slug: str, *, host: str | None) -> None:
    repo = root / f"bubble-ops-{slug}"
    (repo / "onboarding").mkdir(parents=True)
    state = {
        "schema_version": 1,
        "slug": slug,
        "display_name": slug.capitalize(),
        "owner": "operator2",
        "created_at": "2026-06-11T20:00:00Z",
        "status": "Live",
        "validated_steps": ["mandate", "missions", "layers",
                            "skills_tools", "gates_kpis", "dry_run"],
        "last_updated_at": "2026-06-11T20:00:00Z",
        "commits": [],
    }
    if host is not None:
        state["host"] = host
    (repo / "onboarding" / "STATE.yaml").write_text(yaml.safe_dump(state))


def _host_of(root: Path, slug: str, monkeypatch) -> str:
    # Patch the two functions the registry calls, right here, right before the
    # call — immune to any other test's fixture teardown ordering.
    monkeypatch.setattr(settings, "disk_mode", lambda: True)
    monkeypatch.setattr(settings, "disk_root", lambda: root)
    for d in dept_registry.list_departments():
        if d.slug == slug:
            return d.host
    raise AssertionError(f"dept {slug!r} not discovered under {root}")


def test_local_dept_reports_host_local(tmp_path, monkeypatch):
    root = tmp_path / "depts"
    root.mkdir()
    _write_dept(root, "hybridlocal", host="local")
    assert _host_of(root, "hybridlocal", monkeypatch) == "local"


def test_explicit_vps_dept_reports_host_vps(tmp_path, monkeypatch):
    root = tmp_path / "depts"
    root.mkdir()
    _write_dept(root, "hybridvps", host="vps")
    assert _host_of(root, "hybridvps", monkeypatch) == "vps"


def test_absent_host_defaults_to_vps(tmp_path, monkeypatch):
    root = tmp_path / "depts"
    root.mkdir()
    _write_dept(root, "hybridnone", host=None)
    assert _host_of(root, "hybridnone", monkeypatch) == "vps"
