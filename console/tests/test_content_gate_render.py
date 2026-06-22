"""
test_content_gate_render.py — Visual render of Miranda's content / social_post
gate cards in gate_card.html and gate_batch.html.

Board issue #193: Miranda generates rich YAML gate cards (summary, draft_body,
attachments) but the cockpit was rendering them as raw text. This test verifies
the new content-specific render layer:

  1. Channel badge (e.g. "LINKEDIN") renders in gate_card and gate_batch.
  2. Pillar tag renders in both views.
  3. Structured summary dict (hook, theme, compliance) renders as labelled rows.
  4. draft_body renders as formatted post body (not as YAML blob).
  5. hashtags render inline.
  6. Attachment PNG (preview image) renders inline via the existing attachment
     route — same /gate/<slug>/attachment pattern used by Ben's chart-attach.
  7. Graceful: plain-string summary (not a dict) still renders correctly.
  8. Graceful: no-op when content fields are absent.
  9. Non-content gates (trade_proposal) are NOT affected — they still render
     their summary and draft_body via the existing generic blocks.
 10. gate_batch view also renders the content proposal block.
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest


# ── Minimal 1x1 PNG bytes ─────────────────────────────────────────────────────
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd40000"
    "000049454e44ae426082"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_gate(repo_root: Path, gate_id: str, fields: dict) -> None:
    """Write a gate YAML into queues/gates/ of the given dept repo."""
    gates_dir = repo_root / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(fields, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_attachment_png(repo_root: Path, date: str, fname: str) -> str:
    """Drop a preview PNG and return the relative path."""
    attdir = repo_root / "outputs" / date / "attachments"
    attdir.mkdir(parents=True, exist_ok=True)
    (attdir / fname).write_bytes(_PNG_BYTES)
    return f"outputs/{date}/attachments/{fname}"


def _base_content_gate(gate_id: str, **extra) -> dict:
    """Return a minimal social_post gate dict; extra kwargs override fields."""
    doc = {
        "id": gate_id,
        "kind": "social_post",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "channel": "linkedin",
        "pillar": "thought_leadership",
        "actions": ["approve", "reject", "modify", "defer"],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def fixture_repo(fixture_root: Path) -> Path:
    """Return the 'fixture' dept repo root (the live dept in the test root)."""
    return fixture_root / "bubble-ops-fixture"


# ── 1. Channel badge ──────────────────────────────────────────────────────────

def test_gate_card_renders_channel_badge(client, fixture_repo):
    """gate_card.html must render the channel name as a badge for social_post gates."""
    _write_gate(fixture_repo, "content-ch-1", _base_content_gate(
        "content-ch-1",
        draft_body="Mon post LinkedIn.",
    ))
    r = client.get("/gate/fixture/content-ch-1")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text


def test_gate_batch_renders_channel_badge(client, fixture_repo):
    """gate_batch.html must render the channel name for social_post gates."""
    _write_gate(fixture_repo, "content-ch-2", _base_content_gate(
        "content-ch-2",
        draft_body="Mon post LinkedIn.",
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text


# ── 2. Pillar tag ─────────────────────────────────────────────────────────────

def test_gate_card_renders_pillar_tag(client, fixture_repo):
    """gate_card.html must render the pillar tag for social_post gates."""
    _write_gate(fixture_repo, "content-pil-1", _base_content_gate(
        "content-pil-1",
        pillar="thought_leadership",
        draft_body="Post sur l'IA.",
    ))
    r = client.get("/gate/fixture/content-pil-1")
    assert r.status_code == 200, r.text
    # pillar is title-cased and underscore-replaced in the template
    assert "Thought Leadership" in r.text


def test_gate_batch_renders_pillar_tag(client, fixture_repo):
    """gate_batch.html must render the pillar tag for social_post gates."""
    _write_gate(fixture_repo, "content-pil-2", _base_content_gate(
        "content-pil-2",
        pillar="market_insight",
        draft_body="Post sur les marchés.",
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "Market Insight" in r.text


# ── 3. Structured summary dict ────────────────────────────────────────────────

def test_gate_card_renders_structured_summary(client, fixture_repo):
    """gate_card.html must render hook, theme, compliance rows for dict summary."""
    _write_gate(fixture_repo, "content-sum-1", _base_content_gate(
        "content-sum-1",
        summary={
            "hook": "IA transforme votre bureau.",
            "theme": "Agentic workflows",
            "pillar": "thought_leadership",
            "compliance": ["Pas de promesse de rendement", "Ton professionnel"],
        },
        draft_body="Corps du post.",
    ))
    r = client.get("/gate/fixture/content-sum-1")
    assert r.status_code == 200, r.text
    # Jinja2 autoescape renders apostrophes as &#39; — use apostrophe-free strings
    # or the escaped form; here we use apostrophe-free values to stay portable.
    assert "IA transforme votre bureau." in r.text
    assert "Agentic workflows" in r.text
    assert "Pas de promesse de rendement" in r.text
    # Section labels present
    assert "Accroche" in r.text
    assert "Conformité" in r.text


def test_gate_batch_renders_structured_summary(client, fixture_repo):
    """gate_batch.html must render structured summary for social_post gates."""
    _write_gate(fixture_repo, "content-sum-2", _base_content_gate(
        "content-sum-2",
        summary={
            "hook": "Hook de test batch.",
            "theme": "Innovation",
            "compliance": ["Règle A"],
        },
        draft_body="Corps du post batch.",
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "Hook de test batch." in r.text
    assert "Innovation" in r.text


# ── 4. draft_body formatted ───────────────────────────────────────────────────

def test_gate_card_renders_draft_body_formatted(client, fixture_repo):
    """gate_card.html must render draft_body with white-space:pre-wrap for line breaks."""
    draft = "Ligne 1.\n\nLigne 2.\n\nLigne 3."
    _write_gate(fixture_repo, "content-body-1", _base_content_gate(
        "content-body-1",
        draft_body=draft,
    ))
    r = client.get("/gate/fixture/content-body-1")
    assert r.status_code == 200, r.text
    assert "Texte du post" in r.text
    assert "Ligne 1." in r.text
    assert "Ligne 3." in r.text
    assert "pre-wrap" in r.text


def test_gate_batch_renders_draft_body_formatted(client, fixture_repo):
    """gate_batch.html must render draft_body for social_post gates."""
    draft = "Post batch ligne 1.\n\nPost batch ligne 2."
    _write_gate(fixture_repo, "content-body-2", _base_content_gate(
        "content-body-2",
        draft_body=draft,
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "Post batch ligne 1." in r.text


# ── 5. Hashtags ───────────────────────────────────────────────────────────────

def test_gate_card_renders_hashtags(client, fixture_repo):
    """gate_card.html must render hashtags for social_post gates."""
    _write_gate(fixture_repo, "content-htag-1", _base_content_gate(
        "content-htag-1",
        draft_body="Post avec hashtags.",
        hashtags=["IA", "Bubble", "Agentic"],
    ))
    r = client.get("/gate/fixture/content-htag-1")
    assert r.status_code == 200, r.text
    assert "#IA" in r.text or "IA" in r.text
    assert "#Bubble" in r.text or "Bubble" in r.text


def test_gate_batch_renders_hashtags(client, fixture_repo):
    """gate_batch.html must render hashtags for social_post gates."""
    _write_gate(fixture_repo, "content-htag-2", _base_content_gate(
        "content-htag-2",
        draft_body="Post batch avec hashtags.",
        hashtags=["Batch", "Test"],
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "Batch" in r.text


# ── 6. Attachment PNG renders inline ─────────────────────────────────────────

def test_gate_card_renders_preview_png_inline(client, fixture_repo):
    """gate_card.html must render <img> for social_post gate with PNG attachment."""
    rel = _write_attachment_png(fixture_repo, "2026-06-20", "post-preview.png")
    _write_gate(fixture_repo, "content-att-1", _base_content_gate(
        "content-att-1",
        draft_body="Post avec image.",
        attachments=[{"path": rel, "caption": "Aperçu visuel"}],
    ))
    r = client.get("/gate/fixture/content-att-1")
    assert r.status_code == 200, r.text
    assert "/gate/fixture/attachment?path=" in r.text
    assert rel in r.text
    assert "<img" in r.text.lower()


def test_gate_batch_renders_preview_png_inline(client, fixture_repo):
    """gate_batch.html must render <img> for social_post gate with PNG attachment."""
    rel = _write_attachment_png(fixture_repo, "2026-06-20", "batch-preview.png")
    _write_gate(fixture_repo, "content-att-2", _base_content_gate(
        "content-att-2",
        draft_body="Post batch avec image.",
        attachments=[{"path": rel, "caption": "Aperçu batch"}],
    ))
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text
    assert "/gate/fixture/attachment?path=" in r.text
    assert "<img" in r.text.lower()


# ── 7. Plain-string summary (graceful fallback) ───────────────────────────────

def test_gate_card_renders_plain_string_summary(client, fixture_repo):
    """gate_card.html must render plain-string summary gracefully for content gates."""
    _write_gate(fixture_repo, "content-plain-1", _base_content_gate(
        "content-plain-1",
        summary="Résumé simple en texte brut.",
        draft_body="Corps du post.",
    ))
    r = client.get("/gate/fixture/content-plain-1")
    assert r.status_code == 200, r.text
    assert "Résumé simple en texte brut." in r.text


# ── 8. Graceful no-op when content fields absent ─────────────────────────────

def test_gate_card_graceful_when_no_content_fields(client, fixture_repo):
    """gate_card.html must not error for minimal social_post gate (no extra fields)."""
    _write_gate(fixture_repo, "content-minimal-1", {
        "id": "content-minimal-1",
        "kind": "social_post",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "actions": ["approve", "reject", "modify", "defer"],
    })
    r = client.get("/gate/fixture/content-minimal-1")
    assert r.status_code == 200, r.text


def test_gate_batch_graceful_when_no_content_fields(client, fixture_repo):
    """gate_batch.html must not error for minimal social_post gate."""
    _write_gate(fixture_repo, "content-minimal-2", {
        "id": "content-minimal-2",
        "kind": "social_post",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "actions": ["approve", "reject", "modify", "defer"],
    })
    r = client.get("/gate/fixture/kind/social_post")
    assert r.status_code == 200, r.text


# ── 9. Non-content gates unaffected ──────────────────────────────────────────

def test_gate_card_trade_proposal_unaffected(client, fixture_repo):
    """trade_proposal gates must still render summary + draft_body via existing blocks."""
    _write_gate(fixture_repo, "trade-unaffected-1", {
        "id": "trade-unaffected-1",
        "kind": "trade_proposal",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "ticker": "AAPL",
        "side": "buy",
        "proposed_qty": 10,
        "summary": "Thèse trade classique.",
        "draft_body": "Approuver cet ordre.",
        "actions": ["approve", "reject", "modify", "defer"],
    })
    r = client.get("/gate/fixture/trade-unaffected-1")
    assert r.status_code == 200, r.text
    # Generic summary block (not content block) must render
    assert "Thèse trade classique." in r.text
    assert "Approuver cet ordre." in r.text
    # Must NOT show content-specific labels
    assert "Texte du post" not in r.text


def test_gate_card_content_publish_kind_also_renders(client, fixture_repo):
    """content_publish kind (alias) must also get the content render block."""
    _write_gate(fixture_repo, "content-pub-1", {
        "id": "content-pub-1",
        "kind": "content_publish",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "channel": "linkedin",
        "draft_body": "Post à publier via content_publish.",
        "actions": ["approve", "reject", "modify", "defer"],
    })
    r = client.get("/gate/fixture/content-pub-1")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text
    assert "Texte du post" in r.text
    assert "Post à publier via content_publish." in r.text


# ── 11. Maya — news_post (LinkedIn post via post_body) — #193 extended ─────────

def test_gate_card_renders_maya_news_post(client, fixture_repo):
    """Maya's news_post gate (post_body, no channel field) gets the content
    render: implicit LINKEDIN badge, formatted body, why_it_matters context."""
    _write_gate(fixture_repo, "news_post-maya-1", {
        "id": "news_post-maya-1",
        "kind": "news_post",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "account_used": "Operator",
        "post_body": "Un agent IA qui code: une demo.\n\nCent agents: une usine.",
        "why_it_matters": "La bascule agent unique vers maillage est notre these.",
        "summary": "Relais news du jour : Factory 2.0.",
        "actions": ["approve", "reject", "modify"],
    })
    r = client.get("/gate/fixture/news_post-maya-1")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text                          # implicit channel
    assert "Texte du post" in r.text
    assert "Cent agents: une usine." in r.text             # post_body rendered
    assert "Pourquoi c'est pertinent" in r.text          # why_it_matters context
    assert "Compte" in r.text                            # account_used


def test_gate_batch_renders_maya_news_post(client, fixture_repo):
    """gate_batch must render Maya's news_post content block too."""
    _write_gate(fixture_repo, "news_post-maya-2", {
        "id": "news_post-maya-2",
        "kind": "news_post",
        "source_layer": 2, "target_layer": 3,
        "risk_level": "low", "requires_human": True,
        "current_mode": "manual_required",
        "post_body": "Corps du post Maya batch.",
        "actions": ["approve", "reject", "modify"],
    })
    r = client.get("/gate/fixture/kind/news_post")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text
    assert "Corps du post Maya batch." in r.text


# ── 12. Maya — prospect_dm (cold message via draft_message) ───────────────────

def test_gate_card_renders_maya_prospect_dm(client, fixture_repo):
    """Maya's prospect_dm gate (draft_message + recipient/angle) renders the
    message body + recipient context; label says 'Texte du message'."""
    _write_gate(fixture_repo, "prospect_dm-maya-1", {
        "id": "prospect_dm-maya-1",
        "kind": "prospect_dm",
        "source_layer": 2, "target_layer": 3,
        "risk_level": "medium", "requires_human": True,
        "current_mode": "manual_required",
        "channel": "linkedin",
        "account_used": "Maya",
        "recipient": "Isabelle Portal Correia",
        "recipient_role": "Recruteuse independante (Lyon)",
        "angle": "relationship-first / networking (zero vente)",
        "fit_note": "Influenceuse, relation long terme, ROI tri faible.",
        "draft_message": "Bonjour Isabelle, votre commentaire m'a parle.",
        "actions": ["approve", "reject", "modify"],
    })
    r = client.get("/gate/fixture/prospect_dm-maya-1")
    assert r.status_code == 200, r.text
    assert "LINKEDIN" in r.text
    assert "Texte du message" in r.text                  # DM-specific label
    assert "Bonjour Isabelle" in r.text                  # draft_message rendered
    assert "Destinataire" in r.text
    assert "Isabelle Portal Correia" in r.text
    assert "Angle" in r.text
    assert "Note de pertinence" in r.text


def test_maya_dm_summary_not_double_rendered_in_batch(client, fixture_repo):
    """A Maya content-kind gate must render its summary once (in the proposal
    block), not also in the generic thesis <details>."""
    _write_gate(fixture_repo, "prospect_dm-maya-2", {
        "id": "prospect_dm-maya-2",
        "kind": "prospect_dm",
        "source_layer": 2, "target_layer": 3,
        "risk_level": "low", "requires_human": True,
        "current_mode": "manual_required",
        "recipient": "Test Lead",
        "draft_message": "Bonjour, message de test.",
        "summary": "DM relation-first unique-marker-xyz.",
        "actions": ["approve", "reject", "modify"],
    })
    r = client.get("/gate/fixture/kind/prospect_dm")
    assert r.status_code == 200, r.text
    # summary string must appear AT MOST once (the generic thesis <details>
    # block is skipped for content kinds).
    assert r.text.count("unique-marker-xyz") <= 1
    assert "Bonjour, message de test." in r.text


# ── 13. Attachment preview images are size-capped (not full-bleed) ─────────────

def test_attachment_preview_image_is_size_capped(client, fixture_repo):
    """A tall post-screenshot attachment must render as a capped preview
    thumbnail (max-width/max-height), not full card width — #193 follow-up."""
    date = "2026-06-21"
    relpath = _write_attachment_png(fixture_repo, date, "post-preview.png")
    _write_gate(fixture_repo, "content-att-cap-1", _base_content_gate(
        "content-att-cap-1",
        draft_body="Post avec preview image.",
        attachments=[{"path": relpath, "caption": "Apercu du post"}],
    ))
    r = client.get("/gate/fixture/content-att-cap-1")
    assert r.status_code == 200, r.text
    # The attachment <img> must carry a max-height cap so portrait screenshots
    # don't blow up the card.
    assert "max-height:460px" in r.text
    assert "post-preview.png" in r.text or "attachment?path=" in r.text
