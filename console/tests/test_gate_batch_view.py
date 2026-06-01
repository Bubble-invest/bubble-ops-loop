"""test_gate_batch_view.py — batch list view of all pending gates of one kind.

Context — Joris (2026-06-01): triaging 35 prospect_dm gates one-by-one was
broken in two ways:
  1. After deciding a gate, the single-card page only offered "← retour",
     no way to advance to the next → operator stranded on gate #1.
  2. No way to SEE all pending decisions at once — the grouped "Voir les 35"
     card deep-linked to gate[0] only (stub: "until a per-kind view exists").

The batch view fixes both: GET /gate/<slug>/kind/<kind> lists EVERY pending
gate of that kind, each with its own inline action form. Deciding one swaps
ONLY that card (hx-swap=outerHTML on the card) into a small "done" stub, so
the operator flows through all N without leaving the page.

TDD: written BEFORE the route + template exist. RED -> GREEN.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient


def _build_app_with_gates(tmp_path: Path, monkeypatch, gates: list[dict]) -> TestClient:
    root = tmp_path / "depts"
    dept = root / "bubble-ops-fixture"
    (dept / "queues" / "gates").mkdir(parents=True)
    (dept / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops", "mandate": "MVP"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (dept / "onboarding").mkdir()
    (dept / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "fixture",
            "display_name": "Fixture", "owner": "joris",
            "created_at": "2026-05-15T10:00:00Z", "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (dept / "outputs").mkdir()
    for g in gates:
        (dept / "queues" / "gates" / f"{g['id']}.yaml").write_text(
            yaml.safe_dump(g, sort_keys=False, allow_unicode=True),
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


def _gate(n: int, kind: str = "prospect_dm") -> dict:
    return {
        "id": f"{kind}-person-{n:03d}",
        "kind": kind,
        "slug": f"person-{n:03d}",
        "account_used": "Joris",
        "chosen_variant": "V2",
        "chosen_angle": f"Angle pour la personne {n}.",
        "chosen_reason": "Raison du choix.",
        "alternatives": [{"variant": "V1", "angle": "Autre angle."}],
        "draft_body": f"Bonjour personne {n}, ...",
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "gate_policy_id": kind,
        "actions": ["approve", "reject", "modify", "defer"],
        "summary": f"DM pour la personne {n}.",
    }


def test_batch_view_lists_all_gates_of_kind(tmp_path, monkeypatch):
    """The batch route renders EVERY pending gate of the kind, not just one."""
    gates = [_gate(n) for n in range(1, 6)]  # 5 prospect_dm
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/prospect_dm")
    assert r.status_code == 200, r.text
    body = r.text
    # All 5 gate ids must appear.
    for n in range(1, 6):
        assert f"prospect_dm-person-{n:03d}" in body, (
            f"gate {n} missing from batch view — it must list ALL pending "
            f"gates of the kind, not deep-link to one."
        )


def test_batch_view_each_gate_has_inline_actions(tmp_path, monkeypatch):
    """Each gate in the batch must carry its OWN inline decision form posting
    to the existing per-gate decide endpoint, so the operator acts in place."""
    gates = [_gate(n) for n in (1, 2)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    # Each gate's form posts to its own /decide endpoint.
    assert 'hx-post="/gate/fixture/prospect_dm-person-001/decide"' in body
    assert 'hx-post="/gate/fixture/prospect_dm-person-002/decide"' in body
    # All four actions present per gate.
    for action in ("approve", "reject", "modify", "defer"):
        assert f'value="{action}"' in body


def test_batch_view_card_swaps_itself_on_decision(tmp_path, monkeypatch):
    """Deciding a gate must swap ONLY that card (outerHTML on a per-gate
    target) so it disappears from the flow and the operator advances. We
    assert each form targets its own card element and uses outerHTML."""
    gates = [_gate(1)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    # The form targets its own card (id keyed by gate id) and swaps outerHTML.
    assert 'id="gatecard-prospect_dm-person-001"' in body
    assert 'hx-target="#gatecard-prospect_dm-person-001"' in body
    assert 'hx-swap="outerHTML"' in body


def test_batch_view_shows_count_and_draft(tmp_path, monkeypatch):
    """Operator-facing essentials: the count of pending decisions and each
    draft body (so they can triage without opening each one)."""
    gates = [_gate(n) for n in range(1, 4)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    assert "3" in body  # count somewhere
    assert "Bonjour personne 1" in body  # draft rendered inline


def test_batch_view_unknown_dept_404(tmp_path, monkeypatch):
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1)])
    assert c.get("/gate/nope/kind/prospect_dm").status_code == 404


def test_batch_view_empty_kind_renders_empty_state(tmp_path, monkeypatch):
    """A kind with no pending gates renders a clean empty state, not a crash."""
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1, kind="prospect_dm")])
    r = c.get("/gate/fixture/kind/warming_comment")
    assert r.status_code == 200
    # No prospect_dm gate leaks into a different kind's view.
    assert "prospect_dm-person-001" not in r.text


def test_decision_ok_partial_offers_inline_dismiss(tmp_path, monkeypatch):
    """When a decision is made FROM the batch view, the returned partial must
    be a compact 'done' stub (not the full single-card retour flow). We assert
    the decide endpoint still returns 200 and names the action — the batch
    template handles the in-place swap via hx-swap on the card."""
    gates = [_gate(1)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.post("/gate/fixture/prospect_dm-person-001/decide",
               data={"action": "defer", "comment": ""})
    assert r.status_code == 200, r.text
    assert "defer" in r.text
