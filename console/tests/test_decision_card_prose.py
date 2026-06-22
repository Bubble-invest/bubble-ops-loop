"""
test_decision_card_prose.py — Item E2 polish.

The home page décision-card body must read like French prose. No raw gate
id (e.g. 'gate-notif-test-002'), no raw enum slug (e.g. 'research decision').
Risk level translated: low → faible, medium → modéré, high → élevé,
critical → critique.

The gate id can still appear on the detail page (/gate/<dept>/<id>) — but
only as a discreet mono-font footer, not in card-body prose.
"""
from __future__ import annotations


def test_home_card_body_uses_humanized_risk_for_low(client):
    """The fixture's 'echo-1' gate has risk_level='low'. The home card body
    must surface 'faible', not 'low'."""
    r = client.get("/")
    body = r.text
    # 'faible' must appear (humanized risk for low)
    assert "faible" in body, "humanized risk 'faible' missing for low"
    # 'risque low' (raw enum) must NOT appear in card body prose
    assert "risque low" not in body.lower(), \
        "raw enum 'risque low' must not appear in home prose"


def test_home_card_body_does_not_leak_gate_id(client):
    """The home page card body must not show the raw gate id (technical
    identifier the operator doesn't care about). The id remains in the
    href (URL), but not in visible card-body prose."""
    r = client.get("/")
    body = r.text
    # The fixture's single live gate has id 'echo-1'.
    # It must appear in href (link target) but NOT in card body text.
    # We grep the rendered body text by extracting <p> contents.
    import re
    # Find decision-card-body texts
    bodies = re.findall(
        r'<p class="decision-card-body">(.*?)</p>',
        body, flags=re.S,
    )
    assert bodies, "no decision-card-body block found on home"
    for b in bodies:
        assert "echo-1" not in b, \
            f"gate id 'echo-1' leaked into card body prose: {b!r}"


def test_home_card_body_does_not_leak_enum_slug(client):
    """The home page card body must not show the raw enum kind (e.g.
    'echo_action' or 'echo action'). Use humanized French label."""
    r = client.get("/")
    body = r.text
    import re
    bodies = re.findall(
        r'<p class="decision-card-body">(.*?)</p>',
        body, flags=re.S,
    )
    for b in bodies:
        # Raw enum 'echo_action' must not appear in prose
        assert "echo_action" not in b, \
            f"raw enum 'echo_action' leaked into prose: {b!r}"


def test_gate_detail_page_shows_id_as_discreet_footer(client):
    """The /gate/<dept>/<id> page must still display the gate id (for
    operator reference / debugging) — but NOT in the prose copy. It
    appears in the header subtitle as a `slug-ref` mono badge."""
    r = client.get("/gate/fixture/echo-1")
    assert r.status_code == 200
    body = r.text
    # Id still appears (the operator can verify they're on the right gate)
    assert "echo-1" in body
    # Confirm the id is wrapped in slug-ref styling (discreet mono badge)
    # OR appears within a <code>/<pre> block (raw yaml details).
    import re
    has_discreet = bool(re.search(
        r'<[a-z]+\s+[^>]*class="[^"]*slug-ref[^"]*"[^>]*>\s*echo-1\s*<',
        body,
    )) or "<pre" in body
    assert has_discreet, "gate id must be in a discreet slug-ref or pre block"


def test_home_card_uses_humanized_risk_labels_table(tmp_path, monkeypatch):
    """Exercise the full risk-mapping table: low/medium/high/critical →
    faible/modéré/élevé/critique."""
    import yaml
    root = tmp_path / "risk-depts"
    root.mkdir()
    repo = root / "bubble-ops-fixture"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops",
                           "mandate": "fixture"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
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
    (repo / "queues" / "gates").mkdir(parents=True)
    # Four distinct kinds so each renders as its own single-card (no grouping)
    for gid, kind, risk in [
        ("g-low",  "kind_one",   "low"),
        ("g-med",  "kind_two",   "medium"),
        ("g-hi",   "kind_three", "high"),
        ("g-crit", "kind_four",  "critical"),
    ]:
        (repo / "queues" / "gates" / f"{gid}.yaml").write_text(
            yaml.safe_dump({
                "id": gid, "kind": kind, "source_layer": 2,
                "target_layer": 3, "risk_level": risk, "requires_human": True,
                "current_mode": "manual_required",
                "gate_policy_id": kind,
                "actions": ["approve", "reject", "modify", "defer"],
            }, sort_keys=False),
            encoding="utf-8",
        )

    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})

    r = c.get("/")
    body = r.text
    for fr in ("faible", "modéré", "élevé", "critique"):
        assert fr in body, f"humanized risk '{fr}' missing"
