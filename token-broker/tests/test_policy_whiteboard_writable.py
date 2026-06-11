"""whiteboard.yaml runtime-write allow-list regression (Tony 2026-06-09).

Every L4 debrief writes root-level whiteboard.yaml (cockpit KPI cards). It was
absent from the policy templates' allowed_paths, so the all-or-nothing push was
DENIED ("1 path failed policy: whiteboard.yaml") and the cards stranded
local-only. These tests load the ACTUAL policy templates and assert
whiteboard.yaml now enforces as runtime_write_own (allowed) while staying
non-structural.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.policy import Policy, is_structural_for_repo

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "deploy" / "policies"


def _render(template_name: str, tmp_path: Path, slug: str = "ben") -> Path:
    raw = (_TEMPLATES_DIR / template_name).read_text().replace("<DEPT_SLUG>", slug)
    out = tmp_path / f"{slug}-{template_name}".replace(".template", "")
    out.write_text(raw)
    return out


@pytest.mark.parametrize(
    "template_name",
    ["ops-leaf-policy.template.yaml", "management-policy.template.yaml"],
)
def test_whiteboard_yaml_is_runtime_writable(template_name, tmp_path):
    policy_path = _render(template_name, tmp_path)
    p = Policy.from_yaml(policy_path)
    allowed, reasons = p.enforce(
        actor=p.actor,
        repo="bubble-ops-ben",
        action="runtime_write_own",
        paths=["whiteboard.yaml"],
    )
    assert allowed, f"whiteboard.yaml must be runtime-writable ({template_name}): {reasons}"


def test_whiteboard_yaml_is_not_structural():
    # belt-and-suspenders: the allow-list only helps if it is also non-structural
    assert is_structural_for_repo("whiteboard.yaml", "bubble-ops-ben") is False
    assert is_structural_for_repo("whiteboard.yaml", "bubble-ops-loop") is False
