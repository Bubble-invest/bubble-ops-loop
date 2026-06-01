"""
test_dept_detail.py — /dept/<slug> per-dept view.

Notion v5 line 1015: "/dept/<slug> -> vue par dept (layer state live +
recent outputs + queue depths)".
"""


def test_dept_detail_renders_state_and_queue(client):
    """/dept/fixture must show its 4 subscribed layers + queue counts."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text.lower()
    # subscribed layers + visible gate id
    assert "layer" in body
    assert "echo-1" in body


def test_dept_detail_404_for_unknown_slug(client):
    """/dept/nonexistent must 404 cleanly, not 500."""
    r = client.get("/dept/zzz-does-not-exist")
    assert r.status_code == 404


# ─── Decision grouping on dept page (mirror home behaviour) ───────────────
# Why: home.py groups gates by kind via _group_gates_by_kind() + renders
# decision_group_card.html when count>=2. The dept detail page rendered
# one card per gate, which was inconsistent and noisy (9 stale echo gates
# = 9 cards instead of "9 décisions à prendre"). Move the grouping helper
# to a shared module so both routes use the same code path.


def _multi_kind_gates():
    return [
        {"id": f"echo-{i}", "kind": "echo_action", "risk_level": "low",
         "current_mode": "manual_required"}
        for i in range(3)
    ] + [
        {"id": "social-1", "kind": "social_post", "risk_level": "medium",
         "current_mode": "manual_required"},
    ]


def test_dept_detail_groups_gates_by_kind(client, monkeypatch):
    """When a dept has multiple gates of the same kind, they must be
    grouped into a single card showing the count — same as home page."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "list_pending_gates",
        lambda slug: _multi_kind_gates() if slug == "fixture" else [],
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    # Group card for the 3 echo gates: count badge "3", phrase "3 décisions"
    assert "3 décisions" in body or "3 echos" in body.lower(), (
        "Expected grouped card mentioning '3 décisions' for the 3 echo_action gates, "
        "found body without grouping markers."
    )
    # The single social_post gate keeps a single card (count==1)
    assert "social" in body.lower()


def test_dept_detail_group_card_links_to_gate_when_count_one(client, monkeypatch):
    """For a kind with only 1 gate, the card still works (link to the gate)."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "list_pending_gates",
        lambda slug: [{"id": "lonely-1", "kind": "lonely_kind",
                       "risk_level": "low", "current_mode": "manual_required"}]
                      if slug == "fixture" else [],
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    # Singleton still appears
    assert "lonely-1" in r.text


def test_dept_detail_no_gates_shows_empty_state(client, monkeypatch):
    """No gates → friendly empty-state, no broken iteration on gate_groups."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "list_pending_gates", lambda slug: [],
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text.lower()
    # Existing empty-state phrase from the template
    assert "aucune décision en attente" in body


# ─── Retire CTA on dept page (UX gap — backend ready, no entry point) ─────
# Why: POST /agents/<slug>/retire exists + retire_dept_fragment.html exists,
# but no UI element triggers them. Operators can only retire via curl. Add
# a danger-zone CTA on live depts, hidden on éclore/ancien.


def test_dept_detail_live_dept_has_retire_cta(client):
    """A Live dept must expose a 'retire' CTA on its detail page so the
    operator has a non-curl way to decommission a colleague."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text.lower()
    # The CTA copy can vary; look for any of the canonical labels OR
    # the POST target path that confirms the form is wired up.
    has_label = any(token in body for token in [
        "à la retraite", "mettre à la retraite", "retirer",
        "retire ce collègue", "se retire",
    ])
    has_form_target = '/agents/fixture/retire' in body
    assert has_label and has_form_target, (
        f"Expected a retire CTA (label) and a form/htmx target /agents/fixture/retire "
        f"on /dept/fixture. Label found: {has_label}. Target found: {has_form_target}."
    )


def test_dept_detail_eclore_dept_does_not_show_retire_cta(client):
    """An éclore (not yet Live) dept must NOT expose retire — there's
    nothing to retire yet. Cancellation is a different workflow."""
    r = client.get("/dept/miranda")
    assert r.status_code == 200
    body = r.text.lower()
    assert "/agents/miranda/retire" not in body, (
        "Retire CTA should not appear on éclore depts (status != Live)."
    )
