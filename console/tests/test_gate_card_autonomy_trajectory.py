"""
test_gate_card_autonomy_trajectory.py — QA-Letter Blocker B.

Notion v5 lines 920-924 mandate 3 lines visible on EVERY gate surface:

    Current mode: Manual required
    Future autonomy: Possible, but not active
    Shadow autonomy: Enabled

The current gate_card.html shows only `current_mode` (humanized).
`future_eligible_modes` and the shadow-autonomy state are absent.
{{OPERATOR}} validates Maya gates from his phone — he needs the full trajectory
to evaluate "where this gate type is going".

Gate-level shadow autonomy state does NOT live on the gate itself in v3
(it's computed at dept-level by Layer 4 under
`management-export.autonomy_readiness.action_classes[*]`). For v1 of
this UX, the card renders a static placeholder explaining that Layer 4
will wire it on. Future work: link real state from the management export.
"""
from __future__ import annotations


def _fetch_gate_card(client) -> str:
    """The fixture seeds gate echo-1 under dept 'fixture' with
    current_mode=manual_required and no future_eligible_modes; we'll
    also check the gate-notif-test-002 shape via direct template render."""
    r = client.get("/gate/fixture/echo-1")
    assert r.status_code == 200, r.text
    return r.text


def test_gate_card_renders_current_mode_line(client):
    """The 'niveau actuel' line must humanize current_mode in French prose."""
    body = _fetch_gate_card(client).lower()
    # manual_required → "tu valides chaque fois"
    assert "tu valides chaque fois" in body, (
        "humanized current_mode line missing from gate card"
    )


def test_gate_card_renders_future_autonomy_line(client):
    """The 'pourrait apprendre' line must humanize future_eligible_modes."""
    body = _fetch_gate_card(client).lower()
    # echo-1 fixture has no future_eligible_modes → empty-case copy.
    # Jinja's default autoescape converts ' → &#39; so match either form.
    assert (
        "pas de palier d'autonomie supérieur prévu" in body
        or "pas de palier d&#39;autonomie supérieur prévu" in body
    ), (
        "humanized future_eligible_modes line missing from gate card "
        "(empty case)"
    )


def test_gate_card_renders_shadow_autonomy_placeholder(client):
    """The 'phase d'observation' line must show the static placeholder."""
    body = _fetch_gate_card(client).lower()
    assert "pas encore activée pour cette équipe" in body, (
        "shadow autonomy placeholder line missing from gate card"
    )


def test_gate_card_autonomy_block_wraps_three_lines(client):
    """The 3 lines must live under a dedicated .gate-autonomie block so the
    mobile CSS can target them as one unit."""
    body = _fetch_gate_card(client)
    assert "gate-autonomie" in body, (
        ".gate-autonomie wrapper class missing — mobile styling will leak"
    )


def test_gate_card_with_future_modes_renders_humanized_future(tmp_path,
                                                                monkeypatch):
    """With future_eligible_modes=[auto_if_policy_passed] the future line
    must render 'elle gère seule si la règle est OK' (NOT raw slug)."""
    # Build a fresh fixture root carrying the explicit future_eligible_modes
    import yaml
    from fastapi.testclient import TestClient

    root = tmp_path / "depts"
    root.mkdir()
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
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "queues" / "gates").mkdir(parents=True)
    (live / "queues" / "gates" / "echo-fut.yaml").write_text(
        yaml.safe_dump({
            "id": "echo-fut", "kind": "echo_action", "source_layer": 2,
            "target_layer": 3, "risk_level": "low", "requires_human": True,
            "current_mode": "manual_required",
            "future_eligible_modes": ["auto_if_policy_passed"],
            "gate_policy_id": "echo_action",
            "actions": ["approve", "reject", "modify", "defer"],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "outputs").mkdir()

    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    app = create_app()
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/gate/fixture/echo-fut")
    assert r.status_code == 200, r.text
    body = r.text.lower()
    # humanized future for auto_if_policy_passed
    assert "elle gère seule si la règle est ok" in body, (
        "humanized future_eligible_modes [auto_if_policy_passed] missing"
    )
    # NO raw slugs leaking into the operator-prose area. The technical
    # `<pre>` panel intentionally renders raw YAML — strip it before
    # asserting no slug leaks through the human-facing prose.
    import re as _re
    prose = _re.sub(r"<pre[\s\S]*?</pre>", "", body, flags=_re.IGNORECASE)
    assert "auto_if_policy_passed" not in prose, (
        "raw autonomy slug leaked into gate card prose"
    )
