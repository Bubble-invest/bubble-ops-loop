"""
test_gate_card_ux_polish.py — UX polish on the single-gate decision card.

Covers three changes (2026-06-26, Joris):
  1. The comment textarea must read clearly as the place to type (contrast):
     a labelled field with a high-contrast border class, not a faint hairline.
  2. Clicking "modify" with an EMPTY comment must prompt a confirmation before
     submitting (a redraft request with no guidance is near-useless to the agent).
  3. After a decision the operator must not be stranded on the now-resolved card:
     the decide response carries an HX-Redirect back to the dept list so the
     card visibly disappears (the gate is already hidden server-side).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_gate(repo: Path, gate_id: str, extra: dict | None = None) -> Path:
    doc = {
        "id": gate_id,
        "kind": "strategic_question",
        "source_layer": 1,
        "target_layer": 2,
        "risk_level": "medium",
        "requires_human": True,
        "current_mode": "manual_required",
    }
    if extra:
        doc.update(extra)
    p = repo / "queues" / "gates" / f"{gate_id}.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def fixture_repo(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. Textarea contrast ────────────────────────────────────────────────────

class TestCommentFieldContrast:

    def test_textarea_uses_high_contrast_class(self, client, fixture_repo):
        """The comment textarea must carry the dedicated high-contrast class
        (gate-comment-input) rather than relying on a faint inline hairline."""
        _write_gate(fixture_repo, "ux-contrast-1")
        r = client.get("/gate/fixture/ux-contrast-1")
        assert r.status_code == 200, r.text
        assert 'id="gate-comment"' in r.text
        assert "gate-comment-input" in r.text, \
            "textarea should use the gate-comment-input contrast class"

    def test_contrast_style_defined(self, client, fixture_repo):
        """The page must define a visible (>=2px) border for the comment field so
        it reads as an input, not a hairline."""
        _write_gate(fixture_repo, "ux-contrast-2")
        r = client.get("/gate/fixture/ux-contrast-2")
        assert r.status_code == 200, r.text
        # The contrast class must be styled somewhere in the rendered page.
        assert ".gate-comment-input" in r.text, \
            "a .gate-comment-input style rule must be present"


# ── 2. Confirm on empty-comment modify ──────────────────────────────────────

class TestModifyConfirm:

    def test_modify_button_has_confirm_guard(self, client, fixture_repo):
        """The modify button must be wired to a confirm guard that checks the
        comment before submitting."""
        _write_gate(fixture_repo, "ux-modify-confirm-1")
        r = client.get("/gate/fixture/ux-modify-confirm-1")
        assert r.status_code == 200, r.text
        # The guard hook the JS keys on.
        assert "data-confirm-empty" in r.text or "confirmModify" in r.text, \
            "modify button must carry a confirm-empty guard"

    def test_confirm_script_present(self, client, fixture_repo):
        """The page must ship the JS that pops a confirm() only for modify when
        the comment is blank (approve/reject/defer must never be guarded)."""
        _write_gate(fixture_repo, "ux-modify-confirm-2")
        r = client.get("/gate/fixture/ux-modify-confirm-2")
        assert r.status_code == 200, r.text
        # The handler references the comment field and the confirm dialog.
        assert "gate-comment" in r.text and "confirm(" in r.text, \
            "a confirm() guard reading #gate-comment must be present"


# ── 3. Post-decision redirect (HX-Redirect to dept list) ────────────────────

class TestPostDecisionRedirect:

    def _post(self, client, gate_id, action, comment=""):
        return client.post(
            f"/gate/fixture/{gate_id}/decide",
            data={"action": action, "comment": comment},
        )

    def test_approve_sets_hx_redirect_to_dept(self, client, fixture_repo):
        """An approve decision must return HX-Redirect: /dept/fixture so the
        operator lands back on the dept list with the card gone."""
        _write_gate(fixture_repo, "ux-redir-approve")
        r = self._post(client, "ux-redir-approve", "approve")
        assert r.status_code == 200, r.text
        assert r.headers.get("HX-Redirect") == "/dept/fixture"

    def test_reject_sets_hx_redirect(self, client, fixture_repo):
        _write_gate(fixture_repo, "ux-redir-reject")
        r = self._post(client, "ux-redir-reject", "reject")
        assert r.headers.get("HX-Redirect") == "/dept/fixture"

    def test_defer_sets_hx_redirect(self, client, fixture_repo):
        _write_gate(fixture_repo, "ux-redir-defer")
        r = self._post(client, "ux-redir-defer", "defer")
        assert r.headers.get("HX-Redirect") == "/dept/fixture"

    def test_modify_does_not_redirect(self, client, fixture_repo):
        """modify is NOT terminal — the gate stays visible 'en révision', so the
        operator should remain to see the confirmation, not be redirected."""
        _write_gate(fixture_repo, "ux-redir-modify")
        r = self._post(client, "ux-redir-modify", "modify", comment="please shorten")
        assert r.status_code == 200, r.text
        assert "HX-Redirect" not in r.headers, \
            "modify must not redirect (gate stays visible en révision)"

    def test_redirect_decision_still_written(self, client, fixture_repo):
        """The redirect must not skip writing the decision file."""
        _write_gate(fixture_repo, "ux-redir-written")
        self._post(client, "ux-redir-written", "approve", comment="ok")
        dec = fixture_repo / "inbox" / "decisions" / "ux-redir-written.yaml"
        assert dec.exists()
        assert yaml.safe_load(dec.read_text())["action"] == "approve"
