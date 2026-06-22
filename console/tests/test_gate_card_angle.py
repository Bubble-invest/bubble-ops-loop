"""test_gate_card_angle.py — draft + angle alternatives rendered on the gate card.

Context — {{OPERATOR}} msg 3434/3436/3441 (2026-05-31): Maya's draft_batch now
emits prospect_dm/warming_comment gates enriched with the actual draft
(`draft_body`), the chosen angle (`chosen_variant`/`chosen_angle`), and
the alternative angles (`alternatives`). The gate card must render these
PROMINENTLY (not only in the raw-yaml expander) so {{OPERATOR}} can triage from
his phone:

  - read the actual message he's approving
  - see which angle Maya picked + why
  - see the other angles + know that typing a variant (e.g. "V2") in the
    comment asks for a redraft

Graceful degradation: gates WITHOUT these fields (e.g. fixture echo) must
render exactly as before — no empty blocks, no crashes.

TDD: written BEFORE the template block exists. RED -> GREEN.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient


def _build_app_with_gate(tmp_path: Path, monkeypatch, gate: dict):
    """Seed a one-dept disk root carrying `gate` and return a TestClient."""
    root = tmp_path / "depts"
    dept = root / "bubble-ops-fixture"
    (dept / "queues" / "gates").mkdir(parents=True)
    (dept / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops",
                           "mandate": "MVP"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (dept / "onboarding").mkdir()
    (dept / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "fixture",
            "display_name": "Fixture", "owner": "operator",
            "created_at": "2026-05-15T10:00:00Z", "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (dept / "outputs").mkdir()
    (dept / "queues" / "gates" / f"{gate['id']}.yaml").write_text(
        yaml.safe_dump(gate, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


_ANGLE_GATE = {
    "id": "prospect_dm-jean-dupont-20260601",
    "kind": "prospect_dm",
    "slug": "jean-dupont",
    "account_used": "Operator",
    "chosen_variant": "V2",
    "chosen_angle": "Angle expertise de gérant à gérant.",
    "chosen_reason": "Profil gérant senior — le peer-to-peer résonne.",
    "alternatives": [
        {"variant": "V1", "angle": "Angle workflow pain direct."},
        {"variant": "V3", "angle": "Angle signal externe récent."},
    ],
    "draft_body": "Bonjour Jean, je me permets de vous écrire car...",
    "risk_level": "low",
    "requires_human": True,
    "current_mode": "manual_required",
    "gate_policy_id": "prospect_dm",
    "actions": ["approve", "reject", "modify", "defer"],
    "summary": "DM Tier 1 pour Jean Dupont — angle V2.",
}


def _prominent_html(full_html: str) -> str:
    """Return the card HTML BEFORE the raw-yaml <details> expander, so we
    test the prominent rendering rather than the yaml dump (which contains
    every field by construction)."""
    marker = "Voir le détail technique"
    idx = full_html.find(marker)
    return full_html[:idx] if idx != -1 else full_html


def test_draft_body_is_rendered(tmp_path, monkeypatch):
    c = _build_app_with_gate(tmp_path, monkeypatch, _ANGLE_GATE)
    body = _prominent_html(c.get("/gate/fixture/prospect_dm-jean-dupont-20260601").text)
    assert "Bonjour Jean, je me permets" in body, "draft_body not rendered prominently"


def test_chosen_angle_is_rendered(tmp_path, monkeypatch):
    c = _build_app_with_gate(tmp_path, monkeypatch, _ANGLE_GATE)
    body = _prominent_html(c.get("/gate/fixture/prospect_dm-jean-dupont-20260601").text)
    assert "V2" in body
    assert "Angle expertise de gérant à gérant" in body, "chosen_angle not prominent"


def test_alternatives_are_rendered(tmp_path, monkeypatch):
    c = _build_app_with_gate(tmp_path, monkeypatch, _ANGLE_GATE)
    body = _prominent_html(c.get("/gate/fixture/prospect_dm-jean-dupont-20260601").text)
    assert "Angle workflow pain direct" in body, "alternative V1 not prominent"
    assert "Angle signal externe récent" in body, "alternative V3 not prominent"


def test_alternatives_hint_mentions_comment(tmp_path, monkeypatch):
    """The card must tell {{OPERATOR}} HOW to pick an alternative (type the variant
    in the comment box)."""
    c = _build_app_with_gate(tmp_path, monkeypatch, _ANGLE_GATE)
    body = _prominent_html(c.get("/gate/fixture/prospect_dm-jean-dupont-20260601").text).lower()
    assert "commentaire" in body and (
        "v1" in body or "variant" in body
    ), "no hint on how to request an alternative angle"


def test_plain_gate_without_angle_fields_still_renders(tmp_path, monkeypatch):
    """A gate WITHOUT draft_body/alternatives (echo-style) must render with
    no empty angle block and no crash."""
    plain = {
        "id": "echo-1", "kind": "echo_action", "source_layer": 2,
        "target_layer": 3, "risk_level": "low", "requires_human": True,
        "current_mode": "manual_required", "gate_policy_id": "echo_action",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    c = _build_app_with_gate(tmp_path, monkeypatch, plain)
    r = c.get("/gate/fixture/echo-1")
    assert r.status_code == 200
    # The angle-block wrapper must be absent for a plain gate.
    assert "gate-draft-block" not in r.text, (
        "angle block leaked onto a gate with no draft_body"
    )
