"""
test_mark_activated.py — UX-5 task 6.

`state_yaml.mark_activated(state_yaml_path, pr_number, pr_url,
systemd_unit_path)` writes:
  - status -> "Live"
  - activated_at -> ISO8601 UTC now
  - activation_pr -> {number, url}
  - systemd_unit -> path
Idempotent: a second call is a no-op (same values).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from lib import state_yaml  # noqa: E402


def _seed(path: Path) -> None:
    state_yaml.init_state(
        path=path, slug="miranda", display_name="Miranda", owner="operator",
        created_at="2026-05-19T10:00:00Z",
    )
    # Pretend all 6 steps are validated.
    doc = state_yaml.load_state(path)
    doc["validated_steps"] = list(state_yaml.ALL_STEPS)
    doc["status"] = "Ready to activate"
    state_yaml.save_state(path, doc)


def test_mark_activated_sets_status_to_live(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=42,
        pr_url="https://github.com/vdk888/bubble-ops-miranda/pull/42",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["status"] == "Live"


def test_mark_activated_records_pr_info(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=99,
        pr_url="https://github.com/vdk888/bubble-ops-miranda/pull/99",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["activation_pr"]["number"] == 99
    assert "/pull/99" in doc["activation_pr"]["url"]


def test_mark_activated_records_systemd_unit_path(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=1, pr_url="https://x/pull/1",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["systemd_unit"] == "/etc/systemd/system/ops-loop-miranda.service"


def test_mark_activated_sets_iso8601_activated_at(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=1, pr_url="https://x/pull/1",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    # ISO8601 UTC with Z suffix.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", doc["activated_at"],
    ), f"bad activated_at: {doc['activated_at']!r}"


def test_mark_activated_is_idempotent(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=42, pr_url="https://x/pull/42",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc1 = yaml.safe_load(p.read_text(encoding="utf-8"))
    # Second call: must be a no-op (preserve original activated_at).
    state_yaml.mark_activated(
        p, pr_number=42, pr_url="https://x/pull/42",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc2 = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc1["activated_at"] == doc2["activated_at"]
    assert doc1["activation_pr"] == doc2["activation_pr"]
    assert doc1["systemd_unit"] == doc2["systemd_unit"]
    assert doc1["status"] == doc2["status"] == "Live"


def test_mark_activated_round_trip_preserves_other_fields(tmp_path):
    p = tmp_path / "STATE.yaml"
    _seed(p)
    state_yaml.mark_activated(
        p, pr_number=42, pr_url="https://x/pull/42",
        systemd_unit_path="/etc/systemd/system/ops-loop-miranda.service",
    )
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    # Pre-existing fields intact.
    assert doc["slug"] == "miranda"
    assert doc["display_name"] == "Miranda"
    assert doc["owner"] == "operator"
    assert doc["schema_version"] == 1
