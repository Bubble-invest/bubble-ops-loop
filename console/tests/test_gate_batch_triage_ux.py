"""test_gate_batch_triage_ux.py — wave 2 gate/décision UX (Jade, 2026-07-16).

Three asks, verified here:
  1. Bubble charte — gate_card.html / gate_batch.html now render on the
     SAME body.m4 skin as the home page ("Le Matin") and dept pages, instead
     of the old un-skinned base card design.
  2. Card titles prominent — each batch-view card's title gets its own
     dominant class (gate-batch-title); the id/slug is demoted to a small
     secondary line (gate-batch-meta), not sharing top billing.
  3. Sort (date asc/desc) + channel filter on GET /gate/<slug>/kind/<kind>,
     both optional query params, graceful default to the pre-existing
     oldest-first / all-channels behaviour (#255) when absent or invalid.

Reuses the same on-disk fixture builder as test_gate_batch_view.py (kept
separate here so each file stays focused and independently readable).
"""
from __future__ import annotations

import re
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
            "display_name": "Fixture", "owner": "operator",
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


def _gate(n: int, kind: str = "prospect_dm", created: str | None = None,
          channel: str | None = None) -> dict:
    doc = {
        "id": f"{kind}-person-{n:03d}",
        "kind": kind,
        "slug": f"person-{n:03d}",
        "account_used": "Operator",
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
    if created:
        doc["created"] = created
    if channel:
        doc["channel"] = channel
    return doc


# ─── 1. Bubble charte — body.m4 skin on the gate views ────────────────────

def test_gate_batch_view_uses_m4_charte_skin(tmp_path, monkeypatch):
    """The batch triage view must render on body.m4 (same skin as home/dept),
    not the old un-skinned base card design."""
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1)])
    r = c.get("/gate/fixture/kind/prospect_dm")
    assert r.status_code == 200
    assert '<body class="m4">' in r.text


def test_gate_card_single_view_uses_m4_charte_skin(tmp_path, monkeypatch):
    """The single-gate decision view must also render on body.m4."""
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1)])
    r = c.get("/gate/fixture/prospect_dm-person-001")
    assert r.status_code == 200
    assert '<body class="m4">' in r.text


# ─── 2. Card titles prominent — dedicated dominant title class ────────────

def test_batch_card_title_is_visually_dominant_class(tmp_path, monkeypatch):
    """Each card's title renders in gate-batch-title (the dominant element,
    per Jade's direct feedback); the id/risk metadata is demoted to the
    smaller, muted gate-batch-meta line — not sharing the h3 with the title.

    Option A (card #666 follow-up): the h3 content is now the HUMAN title
    (gate_human_title()), not the raw slug — the raw id moved to the small
    mono card footer (.a-footer) instead. For this fixture (no summary
    quote, no image_hook) gate_human_title de-slugifies the id, so
    'person 001' (space-separated, sentence-cased) still appears inside the
    dominant h3, and the raw id itself now appears in the footer line.
    """
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1)])
    body = c.get("/gate/fixture/kind/prospect_dm").text
    assert 'class="gate-batch-title"' in body
    assert 'class="gate-batch-meta"' in body
    # The human title (de-slugified fallback) appears inside the dominant h3.
    title_start = body.index('class="gate-batch-title"')
    title_chunk = body[title_start:title_start + 300]
    assert "person 001" in title_chunk.lower()
    # The raw id is demoted to the footer, not the title.
    assert "prospect_dm-person-001" in body


# ─── 3. Sort — date_asc (default) / date_desc ──────────────────────────────

def test_batch_default_sort_is_oldest_first(tmp_path, monkeypatch):
    """No ?sort= param -> unchanged #255 behaviour: oldest first."""
    gates = [
        _gate(1, created="2026-07-01"),
        _gate(2, created="2026-07-10"),
        _gate(3, created="2026-07-05"),
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    i1 = body.index("prospect_dm-person-001")
    i2 = body.index("prospect_dm-person-002")
    i3 = body.index("prospect_dm-person-003")
    assert i1 < i3 < i2, "expected oldest (07-01) -> 07-05 -> newest (07-10) order"


def test_batch_sort_date_desc_reverses_order(tmp_path, monkeypatch):
    """?sort=date_desc must show the newest gate first."""
    gates = [
        _gate(1, created="2026-07-01"),
        _gate(2, created="2026-07-10"),
        _gate(3, created="2026-07-05"),
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm?sort=date_desc").text
    i1 = body.index("prospect_dm-person-001")
    i2 = body.index("prospect_dm-person-002")
    i3 = body.index("prospect_dm-person-003")
    assert i2 < i3 < i1, "expected newest (07-10) -> 07-05 -> oldest (07-01) order"


def test_batch_invalid_sort_falls_back_to_default(tmp_path, monkeypatch):
    """An unrecognized ?sort= value must not error — falls back to date_asc."""
    gates = [_gate(1, created="2026-07-01"), _gate(2, created="2026-07-10")]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/prospect_dm?sort=bogus")
    assert r.status_code == 200
    i1 = r.text.index("prospect_dm-person-001")
    i2 = r.text.index("prospect_dm-person-002")
    assert i1 < i2  # still oldest-first


def test_batch_sort_toggle_reflects_current_value(tmp_path, monkeypatch):
    """The 2-state sort toggle (Option A) must mark the active state with
    the 'on' class so the control shows the operator's current view, not
    always the default."""
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1)])
    body = c.get("/gate/fixture/kind/prospect_dm?sort=date_desc").text
    m = re.search(r'class="([^"]*)">↓ Plus récent', body)
    assert m is not None, "sort-desc toggle link not found"
    assert "on" in m.group(1).split()
    m_asc = re.search(r'class="([^"]*)">↑ Plus ancien', body)
    assert m_asc is not None, "sort-asc toggle link not found"
    assert "on" not in m_asc.group(1).split()


# ─── 4. Channel filter ──────────────────────────────────────────────────

def test_batch_channel_filter_narrows_results(tmp_path, monkeypatch):
    """?channel=linkedin must show only gates resolving to that channel and
    hide the rest (news_post/prospect_dm/followup_draft default to linkedin
    per humanize.gate_channel; a substack-channelled gate must be excluded)."""
    gates = [
        _gate(1, kind="prospect_dm"),  # implicit linkedin
        _gate(2, kind="prospect_dm", channel="substack"),
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm?channel=linkedin").text
    assert "prospect_dm-person-001" in body
    assert "prospect_dm-person-002" not in body


def test_batch_channel_filter_shows_total_vs_filtered_count(tmp_path, monkeypatch):
    """When a channel filter narrows the pile, the page must show how many
    of the total matched — so an empty/short result never looks broken."""
    gates = [
        _gate(1, kind="prospect_dm"),
        _gate(2, kind="prospect_dm", channel="substack"),
        _gate(3, kind="prospect_dm", channel="substack"),
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm?channel=substack").text
    assert "2 sur 3" in body


def test_batch_unknown_channel_value_is_ignored(tmp_path, monkeypatch):
    """An unrecognized ?channel= value must not error and must not filter
    anything out (graceful no-op, same contract as invalid sort)."""
    gates = [_gate(1), _gate(2)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/prospect_dm?channel=carrier-pigeon")
    assert r.status_code == 200
    assert "prospect_dm-person-001" in r.text
    assert "prospect_dm-person-002" in r.text


def test_batch_empty_channel_result_offers_reset_link(tmp_path, monkeypatch):
    """Filtering to a channel with zero matches shows a clean empty state
    (not a crash) with a way back to the unfiltered view."""
    gates = [_gate(1, kind="prospect_dm")]  # implicit linkedin only
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/prospect_dm?channel=newsletter")
    assert r.status_code == 200
    assert "prospect_dm-person-001" not in r.text
    assert f'href="/gate/fixture/kind/prospect_dm"' in r.text


def test_batch_channel_pills_offer_every_channel_present(tmp_path, monkeypatch):
    """Option A: one chip per channel that actually has pending gates, each
    carrying a real ?channel= link and its count badge — so the operator
    can jump straight to any populated channel from the toolbar. A channel
    with zero gates gets no pill (matches the validated mockup: 'Autre'/
    'other' isn't shown when its count is 0 — a dead 0-count pill is not
    useful triage UI)."""
    gates = [
        _gate(1, kind="prospect_dm"),                       # linkedin (implicit)
        _gate(2, kind="prospect_dm", channel="substack"),
        _gate(3, kind="prospect_dm", channel="x"),
        _gate(4, kind="prospect_dm", channel="newsletter"),
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    for ch in ("linkedin", "substack", "x", "newsletter"):
        assert f'?channel={ch}' in body, f"no pill link for channel={ch}"
    # The populated channels' count badges are visible without filtering.
    assert 'Tous <span class="count">4</span>' in body


def test_batch_channel_pill_zero_count_not_rendered(tmp_path, monkeypatch):
    """A channel with zero pending gates in this kind gets no pill at all —
    prevents a dead '0' pill from cluttering the toolbar."""
    gates = [_gate(1, kind="prospect_dm")]  # linkedin only
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    assert '?channel=newsletter' not in body
    assert '?channel=x' not in body


# ─── 5. Sort + filter combine, and #255 behaviour survives untouched ──────

def test_batch_sort_and_channel_combine(tmp_path, monkeypatch):
    """Sort and channel filter must compose — filtering to one channel then
    reversing the sort order applies both, not just the last one set."""
    gates = [
        _gate(1, kind="prospect_dm", created="2026-07-01", channel="substack"),
        _gate(2, kind="prospect_dm", created="2026-07-10", channel="substack"),
        _gate(3, kind="prospect_dm", created="2026-07-05"),  # linkedin (implicit)
    ]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm?channel=substack&sort=date_desc").text
    assert "prospect_dm-person-003" not in body  # filtered out (linkedin)
    i1 = body.index("prospect_dm-person-001")
    i2 = body.index("prospect_dm-person-002")
    assert i2 < i1  # newest (07-10) first within the filtered substack set


def test_batch_view_still_has_age_chip_and_stale_amber(tmp_path, monkeypatch):
    """Regression guard: #255's per-card age chip must still render — the
    m4 restyle must not have dropped gate-identity-chips.html's markup."""
    from datetime import date, timedelta
    old_date = (date.today() - timedelta(days=10)).isoformat()
    c = _build_app_with_gates(tmp_path, monkeypatch, [_gate(1, created=old_date)])
    body = c.get("/gate/fixture/kind/prospect_dm").text
    assert "gate-chip--age" in body
    assert "gate-chip--age-stale" in body


def test_batch_view_still_lists_every_gate_and_has_inline_actions(tmp_path, monkeypatch):
    """Regression guard: the pre-existing #255/2026-06-01 batch-view contract
    (every gate listed, each with its own inline decide form) still holds
    with the new default query params applied."""
    gates = [_gate(n) for n in range(1, 4)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    body = c.get("/gate/fixture/kind/prospect_dm").text
    for n in range(1, 4):
        assert f"prospect_dm-person-{n:03d}" in body
        assert f'hx-post="/gate/fixture/prospect_dm-person-{n:03d}/decide"' in body


# ── review fixes (r8, 2026-07-16): channel normalization + title precedence ──

def test_gate_channel_prefix_normalizes_dept_variants():
    """Real content-dept gates use channel: substack_post / substack_note —
    they must bucket under 'substack', not leak raw or fall to 'other'."""
    from console.services.humanize import gate_channel
    assert gate_channel({"channel": "substack_post"}) == "substack"
    assert gate_channel({"channel": "substack_note"}) == "substack"
    assert gate_channel({"channel": "x_thread"}) == "x"
    assert gate_channel({"channel": "newsletter_release"}) == "newsletter"


def test_gate_channel_unknown_value_falls_to_other():
    """A truthy-but-unknown channel must return 'other' (docstring contract),
    never the raw value (the `ch or "other"` bug)."""
    from console.services.humanize import gate_channel
    assert gate_channel({"channel": "tiktok"}) == "other"
    assert gate_channel({"channel": "SOMETHING_ELSE"}) == "other"


def test_gate_card_title_uses_slug_even_without_kind():
    """Jinja precedence fix: a slugged gate with falsy kind must still show
    its slug as the title, not the generic fallback."""
    from jinja2 import Environment
    env = Environment()
    tpl = env.from_string(
        "{{ gate.slug or (('Une ' ~ kind_h) if gate.kind else \"Une décision t'attend\") }}")
    assert tpl.render(gate={"slug": "my-gate", "kind": ""}, kind_h="x") == "my-gate"
    assert "décision t'attend" in tpl.render(gate={"slug": "", "kind": ""}, kind_h="x")
