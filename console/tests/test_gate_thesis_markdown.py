"""test_gate_thesis_markdown.py — gate thesis rendered as markdown, not a
raw-text wall (card #523-A).

Ben's trade-proposal gate cards render the thesis ("Thèse / raisonnement")
from `gate.summary` using `white-space:pre-line` — newlines are preserved but
markdown syntax (**bold**, ## headings, - bullets) shows up as literal
characters instead of formatted HTML.

Fix (mirrors #507's whiteboard-notes pipeline): console/routes/gate.py now
sanitizes `gate.summary` (when it is a plain string) via
markdown_render.render_markdown_safe before it ever reaches the templates,
attaching the result as `gate.thesis_rendered` (a markupsafe.Markup, already
nh3-sanitized). gate_batch.html and gate_card.html render that Markup at
every thesis site instead of the raw string, and drop `pre-line` (markdown's
own paragraphs/lists/headings now carry the structure).

TDD note: written against the existing route/template contract exercised by
test_gate_batch_view.py's `_build_app_with_gates` helper and test_gate_card.py's
fixture-based `client`.
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


def _trade_gate(gate_id: str, summary: str) -> dict:
    return {
        "id": gate_id,
        "kind": "trade_proposal",
        "ticker": "ACME",
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "gate_policy_id": "trade_proposal",
        "actions": ["approve", "reject", "modify", "defer"],
        "summary": summary,
    }


MARKDOWN_THESIS = (
    "## Thèse ACME\n"
    "\n"
    "**Conviction forte** sur ce titre pour les raisons suivantes :\n"
    "\n"
    "- Croissance du chiffre d'affaires en accélération\n"
    "- Valorisation attractive vs pairs\n"
    "- Momentum technique positif\n"
    "\n"
    "Voir le graphique ci-dessous pour le contexte de prix.\n"
)

PLAIN_PROSE_THESIS = (
    "Ce titre présente un profil intéressant sur le plan fondamental.\n"
    "\n"
    "La direction a communiqué des objectifs ambitieux pour l'année prochaine, "
    "et le marché semble sous-évaluer ce potentiel.\n"
    "\n"
    "Le risque principal reste la concurrence accrue sur ce segment.\n"
)

SCRIPT_THESIS = "Thèse légitime <script>alert('xss')</script> avec du texte après."


# ─── /gate/<slug>/<id> — single gate card ───────────────────────────────────

def test_gate_card_renders_markdown_thesis_as_html(tmp_path, monkeypatch):
    """A thesis with markdown syntax renders as actual formatted HTML tags,
    not literal '**'/'##'/'-' characters."""
    gates = [_trade_gate("trade-1", MARKDOWN_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/trade-1")
    assert r.status_code == 200, r.text
    body = r.text
    assert "<h2>" in body and "Thèse ACME" in body
    assert "<strong>Conviction forte</strong>" in body
    assert "<li>" in body and "Valorisation attractive vs pairs" in body
    # The literal markdown syntax must NOT leak into the gate-thesis div —
    # the raw gate YAML is deliberately dumped verbatim elsewhere on the page
    # (the "Voir le détail technique" <pre> block), so we scope the assertion
    # to the rendered thesis element itself, not the whole page body.
    thesis_html = body.split('class="gate-thesis markdown-body"', 1)[1]
    thesis_html = thesis_html.split("</div>", 1)[0]
    assert "## Thèse ACME" not in thesis_html
    assert "**Conviction forte**" not in thesis_html
    assert "- Croissance du chiffre d'affaires" not in thesis_html


def test_gate_card_plain_prose_thesis_still_renders(tmp_path, monkeypatch):
    """A plain-prose, multi-paragraph thesis (no markdown syntax) still
    renders fine — paragraphs preserved."""
    gates = [_trade_gate("trade-2", PLAIN_PROSE_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/trade-2")
    assert r.status_code == 200, r.text
    body = r.text
    assert "<p>" in body
    assert "profil intéressant sur le plan fondamental" in body
    assert "objectifs ambitieux pour l'année prochaine" in body
    assert "concurrence accrue sur ce segment" in body


def test_gate_card_thesis_script_tag_is_stripped(tmp_path, monkeypatch):
    """A thesis containing <script> must never reach the page as an
    executable tag — nh3 sanitization is non-negotiable."""
    gates = [_trade_gate("trade-3", SCRIPT_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/trade-3")
    assert r.status_code == 200, r.text
    body = r.text
    assert "<script>alert" not in body
    assert "Thèse légitime" in body
    assert "avec du texte après" in body


# ─── /gate/<slug>/kind/<kind> — batch view ──────────────────────────────────

def test_gate_batch_renders_markdown_thesis_as_html(tmp_path, monkeypatch):
    """Same markdown->HTML treatment applies in the batch triage view."""
    gates = [_trade_gate("trade-batch-1", MARKDOWN_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/trade_proposal")
    assert r.status_code == 200, r.text
    body = r.text
    assert "<h2>" in body
    assert "<strong>Conviction forte</strong>" in body
    assert "<li>" in body
    assert "## Thèse ACME" not in body
    assert "**Conviction forte**" not in body


def test_gate_batch_plain_prose_thesis_still_renders(tmp_path, monkeypatch):
    gates = [_trade_gate("trade-batch-2", PLAIN_PROSE_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/trade_proposal")
    assert r.status_code == 200, r.text
    assert "profil intéressant sur le plan fondamental" in r.text
    assert "<p>" in r.text


def test_gate_batch_thesis_script_tag_is_stripped(tmp_path, monkeypatch):
    gates = [_trade_gate("trade-batch-3", SCRIPT_THESIS)]
    c = _build_app_with_gates(tmp_path, monkeypatch, gates)
    r = c.get("/gate/fixture/kind/trade_proposal")
    assert r.status_code == 200, r.text
    assert "<script>alert" not in r.text
    assert "Thèse légitime" in r.text


def test_gate_batch_content_kind_plain_string_summary_renders_markdown(tmp_path, monkeypatch):
    """The second thesis render site in gate_batch.html — content-kind gates
    (social_post etc.) with a plain-string summary (not the structured dict
    shape) — must also go through the sanitized markdown pipeline."""
    gate = {
        "id": "content-1",
        "kind": "social_post",
        "channel": "linkedin",
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "gate_policy_id": "social_post",
        "actions": ["approve", "reject", "modify", "defer"],
        "summary": "**Résumé du post** avec du gras.",
        "draft_body": "Corps du post ici.",
    }
    c = _build_app_with_gates(tmp_path, monkeypatch, [gate])
    r = c.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "<strong>Résumé du post</strong>" in r.text
    assert "**Résumé du post**" not in r.text


def test_gate_card_structured_dict_summary_unaffected(tmp_path, monkeypatch):
    """A structured-dict summary (Miranda's hook/theme/compliance shape) must
    keep rendering field-by-field, unaffected by the thesis_rendered pipeline
    (which only applies to plain-string summaries)."""
    gate = {
        "id": "content-dict-1",
        "kind": "social_post",
        "channel": "linkedin",
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "gate_policy_id": "social_post",
        "actions": ["approve", "reject", "modify", "defer"],
        "summary": {"hook": "Accroche du post", "theme": "IA agentique"},
        "draft_body": "Corps du post ici.",
    }
    c = _build_app_with_gates(tmp_path, monkeypatch, [gate])
    r = c.get("/gate/fixture/content-dict-1")
    assert r.status_code == 200, r.text
    assert "Accroche du post" in r.text
    assert "IA agentique" in r.text
