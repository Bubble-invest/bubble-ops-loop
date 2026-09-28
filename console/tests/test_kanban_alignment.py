import yaml



def test_kanban_landing_uses_manifests_and_cache(client, fixture_root, monkeypatch, tmp_path):
    from console.services import mission_alignment as service
    from console.routes import kanban

    intents = tmp_path / "intents"
    intents.mkdir()
    (intents / "growth.md").write_text("Growth")
    monkeypatch.setenv("OPERATOR_INTENTS_DIR", str(intents))
    monkeypatch.setattr(service, "_cache", None)
    monkeypatch.setattr(kanban, "_fetch_issues", lambda: ([], None))
    dept = fixture_root / "bubble-ops-alignment"
    dept.mkdir()
    path = dept / "dept.yaml"
    path.write_text(yaml.safe_dump({"recurring_missions": [
        {"id": "mapped", "business_unit": "fund", "serves_intents": ["growth"]},
        {"id": "legacy"},
        {"id": "unknown", "business_unit": "steering", "serves_intents": ["gone"]},
    ]}))
    response = client.get("/kanban")
    assert response.status_code == 200
    body = response.text
    assert "Chantiers Bubble — carte des opérations" in body
    assert 'href="https://claude.ai/artifact/YB7yT9rUh2eC7SEcudhRXH"' in body
    assert 'target="_blank" rel="noopener noreferrer"' in body
    assert body.index('id="operations-map-title"') < body.index('id="kanban-hero-title"')
    assert "1 missions alignées" in body
    assert "<strong>alignment</strong> : 1 alignées · 1 non alignées" in body
    assert "1 avec intentions inconnues" in body
    snapshot = service.alignment_summary()
    path.write_text("[broken")
    assert service.alignment_summary() is snapshot
    monkeypatch.setattr(service, "_cache_time", 0)
    updated = service.alignment_summary()
    assert next(d for d in updated["departments"] if d["dept"] == "alignment")["errors"]


def test_landing_missing_catalog_is_explicit(client, monkeypatch, tmp_path):
    from console.services import mission_alignment as service
    from console.routes import kanban

    monkeypatch.setenv("OPERATOR_INTENTS_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(service, "_cache", None)
    monkeypatch.setattr(kanban, "_fetch_issues", lambda: ([], "board unavailable"))
    response = client.get("/kanban")
    assert response.status_code == 200
    assert "Catalogue des intentions indisponible" in response.text
    assert "Chantiers Bubble" in response.text
