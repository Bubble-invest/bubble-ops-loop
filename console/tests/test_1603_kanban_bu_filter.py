"""Board #1603: read-only business-unit filter on /kanban (?bu=<slug>)."""
import yaml


def _issue(n, dept=None, extra=()):
    labels = [{"name": x} for x in extra]
    if dept:
        labels.append({"name": f"dept:{dept}"})
    return {"number": n, "title": f"Card {n}", "labels": labels, "body": "",
            "updatedAt": "2026-10-01T10:00:00Z", "createdAt": "2026-10-01T10:00:00Z",
            "url": f"https://x/{n}"}


def _setup(client, fixture_root, monkeypatch):
    from console.services import mission_alignment as service
    from console.routes import kanban
    monkeypatch.setattr(service, "_cache", None)
    for slug, unit in (("ben", "fund"), ("tony", ["steering", "pro_clients"])):
        d = fixture_root / f"bubble-ops-{slug}"
        d.mkdir(exist_ok=True)
        (d / "dept.yaml").write_text(yaml.safe_dump({"recurring_missions": [
            {"id": "m1", "business_unit": unit}]}))
    issues = [_issue(1, "ben"), _issue(2, "tony"), _issue(3, "rick"),
              _issue(4, "rick", ["bu:ai_methods"]), _issue(5, "ben", ["bu:steering"])]
    monkeypatch.setattr(kanban, "_fetch_issues", lambda: (issues, None))


def _titles(body):
    return {n for n in range(1, 6) if f"Card {n}<" in body or f"Card {n}\n" in body or f">Card {n}" in body}


def test_default_view_unchanged_and_chips_present(client, fixture_root, monkeypatch):
    _setup(client, fixture_root, monkeypatch)
    body = client.get("/kanban").text
    assert _titles(body) == {1, 2, 3, 4, 5}
    for label in ("Fonds", "Méthodes IA", "Clients pros", "Pilotage"):
        assert label in body
    assert "/kanban?bu=fund" in body


def test_filter_by_dept_mission_unit(client, fixture_root, monkeypatch):
    _setup(client, fixture_root, monkeypatch)
    assert _titles(client.get("/kanban?bu=fund").text) == {1}
    assert _titles(client.get("/kanban?bu=pro_clients").text) == {2}


def test_bu_label_overrides_dept(client, fixture_root, monkeypatch):
    _setup(client, fixture_root, monkeypatch)
    assert _titles(client.get("/kanban?bu=ai_methods").text) == {4}
    # card 5 is dept ben (fund) but labelled bu:steering -> only in Pilotage
    assert _titles(client.get("/kanban?bu=steering").text) == {2, 5}


def test_unknown_bu_ignored(client, fixture_root, monkeypatch):
    _setup(client, fixture_root, monkeypatch)
    r = client.get("/kanban?bu=bogus")
    assert r.status_code == 200
    assert _titles(r.text) == {1, 2, 3, 4, 5}
