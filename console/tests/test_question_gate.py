"""
test_question_gate.py — Miranda's dynamic QUESTION gate cards (board #730).

Beyond publish approvals (social_post / content_publish), Miranda can emit a
`kind: question` gate when she has a mid-draft doubt: a question text + 2-3
options the operator picks from. The cockpit must:

  1. Render the question gate as a DISTINCT card (gate-question-block) with
     the question text and one button per option — in gate_card.html AND
     gate_batch.html — never the approve/reject strip.
  2. Show it in the dept "Décisions qu'on attend de toi" panel with the
     distinct decision-card--question treatment.
  3. Record the selected option as a decision through the SAME path publish
     approvals use: POST /gate/<slug>/<id>/decide with action=choose +
     option=<id> → inbox/decisions/<id>.yaml (action, selected_option,
     selected_option_label).
  4. Hide the gate from the pending pile once chosen (terminal decision).
  5. Refuse an option the gate never declared (400), and refuse `choose`
     on a gate that has no options (400).
  6. Leave publish_proposal-style gates untouched (no question block).
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest


def _write_gate(repo_root: Path, gate_id: str, fields: dict) -> None:
    gates_dir = repo_root / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(fields, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _question_gate(gate_id: str, **extra) -> dict:
    """Minimal kind=question gate matching the schemas-draft/gate-item v3
    shape (#730): question + options[{id,label}] on top of the standard
    gate metadata publish gates already carry."""
    doc = {
        "id": gate_id,
        "kind": "question",
        "source_layer": 2,
        "target_layer": 2,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "future_eligible_modes": [],
        "gate_policy_id": "question",
        "authorization_band_id": "content_question_band",
        "actions": ["choose"],
        "question": "Quel angle pour le post sur les agents IA ?",
        "options": [
            {"id": "v1", "label": "Angle contrarian — bousculer le consensus"},
            {"id": "v2", "label": "Angle pedagogique — vulgariser simplement"},
        ],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def fixture_repo(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. Rendering: gate page ───────────────────────────────────────────────────

def test_gate_card_renders_question_and_option_buttons(client, fixture_repo):
    """gate_card.html: question text + one button per option, distinct block."""
    _write_gate(fixture_repo, "question-1", _question_gate("question-1"))
    r = client.get("/gate/fixture/question-1")
    assert r.status_code == 200, r.text
    assert "Quel angle pour le post sur les agents IA ?" in r.text
    assert "Angle contrarian — bousculer le consensus" in r.text
    assert "Angle pedagogique — vulgariser simplement" in r.text
    # Distinct visual container + choose mechanics
    assert "gate-question-block" in r.text
    assert 'name="option" value="v1"' in r.text
    assert 'name="option" value="v2"' in r.text
    assert 'name="action" value="choose"' in r.text
    # The 4-action approval strip must NOT render on a question gate
    assert 'value="approve"' not in r.text


def test_gate_card_question_context_renders_via_summary(client, fixture_repo):
    """Optional context travels in the standard summary field (markdown)."""
    _write_gate(fixture_repo, "question-ctx-1", _question_gate(
        "question-ctx-1",
        summary="Deux pistes se valent, contexte-unique-abc.",
    ))
    r = client.get("/gate/fixture/question-ctx-1")
    assert r.status_code == 200, r.text
    assert "contexte-unique-abc" in r.text
    assert "gate-question-context" in r.text
    # The generic thesis block must NOT also render it (question excluded);
    # the only other occurrence is the raw-YAML technical <details>.
    assert 'class="gate-thesis' not in r.text


# ── 2. Rendering: batch view ─────────────────────────────────────────────────

def test_gate_batch_renders_question_options(client, fixture_repo):
    """gate_batch.html: question block + option buttons per gate."""
    _write_gate(fixture_repo, "question-b1", _question_gate("question-b1"))
    _write_gate(fixture_repo, "question-b2", _question_gate(
        "question-b2",
        question="Newsletter cette semaine : cas client ou tendance marche ?",
        options=[
            {"id": "cas_client", "label": "Cas client Gefineo"},
            {"id": "tendance", "label": "Tendance marche IA"},
            {"id": "mix", "label": "Un mix des deux"},
        ],
    ))
    r = client.get("/gate/fixture/kind/question")
    assert r.status_code == 200, r.text
    assert "Quel angle pour le post sur les agents IA ?" in r.text
    assert "Cas client Gefineo" in r.text
    assert "Un mix des deux" in r.text
    assert r.text.count("gate-question-block") == 2
    assert 'name="option" value="mix"' in r.text
    # No approval strip on question cards
    assert 'value="approve"' not in r.text


# ── 3. Dept panel ("Décisions qu'on attend de toi") ──────────────────────────

def test_dept_panel_question_card_visually_distinct(client, fixture_repo):
    """A single question renders a decision-card--question card in the SAME
    panel, alongside (but visually distinct from) approval cards."""
    _write_gate(fixture_repo, "question-p1", _question_gate("question-p1"))
    r = client.get("/dept/fixture")
    assert r.status_code == 200, r.text
    assert "decision-card--question" in r.text
    assert "Quel angle pour le post sur les agents IA ?" in r.text
    assert "Répondre →" in r.text
    # The fixture's echo_action gate still renders its generic card
    assert "/gate/fixture/echo-1" in r.text


def test_dept_panel_question_group_links_to_batch(client, fixture_repo):
    """2+ questions group into one distinct card linking to the batch view."""
    _write_gate(fixture_repo, "question-g1", _question_gate("question-g1"))
    _write_gate(fixture_repo, "question-g2", _question_gate("question-g2"))
    r = client.get("/dept/fixture")
    assert r.status_code == 200, r.text
    assert "decision-card--question" in r.text
    assert "/gate/fixture/kind/question" in r.text


# ── 4. Decision capture — same path as publish approvals ─────────────────────

def test_choose_option_writes_decision_file(client, fixture_repo, fixture_root):
    """POST action=choose + option=v2 → inbox/decisions/<id>.yaml carrying
    the selected option, exactly like an approval decision."""
    _write_gate(fixture_repo, "question-d1", _question_gate("question-d1"))
    r = client.post(
        "/gate/fixture/question-d1/decide",
        data={"action": "choose", "option": "v2", "comment": "plus notre ton"},
    )
    assert r.status_code == 200, r.text
    # Terminal decision → back to the dept page (same UX as approve)
    assert r.headers.get("HX-Redirect") == "/dept/fixture"

    decision_path = (
        fixture_root / "bubble-ops-fixture" / "inbox" / "decisions"
        / "question-d1.yaml"
    )
    assert decision_path.exists(), f"decision file not written at {decision_path}"
    doc = yaml.safe_load(decision_path.read_text(encoding="utf-8"))
    assert doc.get("gate_id") == "question-d1"
    assert doc.get("action") == "choose"
    assert doc.get("selected_option") == "v2"
    assert doc.get("selected_option_label") == \
        "Angle pedagogique — vulgariser simplement"
    assert doc.get("comment") == "plus notre ton"
    assert doc.get("decided_by") == "operator"
    assert doc.get("decided_at")


def test_chosen_question_leaves_pending_pile(client, fixture_repo):
    """Once an option is chosen, the question disappears from the dept panel
    (terminal decision — same filter as approve/reject)."""
    _write_gate(fixture_repo, "question-d2", _question_gate(
        "question-d2", question="Question qui doit disparaitre ensuite ?"))
    r = client.post(
        "/gate/fixture/question-d2/decide",
        data={"action": "choose", "option": "v1"},
    )
    assert r.status_code == 200, r.text
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Question qui doit disparaitre ensuite ?" not in r.text


def test_choose_invalid_option_rejected(client, fixture_repo):
    """An option id the gate never declared must 400 (nothing written)."""
    _write_gate(fixture_repo, "question-d3", _question_gate("question-d3"))
    r = client.post(
        "/gate/fixture/question-d3/decide",
        data={"action": "choose", "option": "v99"},
    )
    assert r.status_code == 400


def test_choose_on_gate_without_options_rejected(client, fixture_root):
    """`choose` on a normal gate (fixture echo-1, no options) must 400."""
    r = client.post(
        "/gate/fixture/echo-1/decide",
        data={"action": "choose", "option": "v1"},
    )
    assert r.status_code == 400
    decision_path = (
        fixture_root / "bubble-ops-fixture" / "inbox" / "decisions"
        / "echo-1.yaml"
    )
    assert not decision_path.exists()


# ── 5. Publish gates untouched ───────────────────────────────────────────────

def test_publish_gate_has_no_question_block(client, fixture_repo):
    """A content_publish gate keeps its approval form — no question UI."""
    _write_gate(fixture_repo, "content-pub-q", {
        "id": "content-pub-q",
        "kind": "content_publish",
        "source_layer": 2,
        "target_layer": 3,
        "risk_level": "low",
        "requires_human": True,
        "current_mode": "manual_required",
        "channel": "linkedin",
        "draft_body": "Post a publier normalement.",
        "actions": ["approve", "reject", "modify", "defer"],
    })
    r = client.get("/gate/fixture/content-pub-q")
    assert r.status_code == 200, r.text
    assert "gate-question-block" not in r.text
    assert 'value="approve"' in r.text
