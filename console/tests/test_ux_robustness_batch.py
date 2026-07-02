"""
test_ux_robustness_batch.py — board #449 cockpit UX robustness batch.

Covers:
  1. Global htmx:responseError handler (base.html) — a 4xx on a htmx request
     must render a visible French error rather than silently no-op (#161
     class). We verify (a) the page ships the handler + banner element, and
     (b) a forced 400 on decide is a real HTTP error the handler would catch.
  2. hx-disabled-elt="this" on every decide/submit button (double-submit
     guard) — gate_card.html, gate_batch.html, partials/rnd_card_actions.html.
  3. Home group-card deep-link parity — covered by
     test_home_gate_grouping.py::test_two_gates_same_dept_same_kind_render_as_single_card
     (updated in this batch); not duplicated here.
  4. onerror fallback on attachment <img> — gate_card.html + gate_batch.html
     ship an onerror handler + "aperçu indisponible" fallback caption.
  5. Scroll-preserving kanban auto-refresh — scrollY persisted to
     sessionStorage before location.reload(), restored on load; existing
     guards (document.hidden / open <details> / text selection) untouched.
  6. Dead-UI cleanup — components/decision-card.html deleted (zero refs);
     orphaned .topbar/.topnav CSS removed; kanban prio_chip no longer reads
     item.severity (routes/kanban.py only ever sets "priority").
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


# ── 1. Global htmx error handler ────────────────────────────────────────────

class TestGlobalHtmxErrorHandler:

    def test_base_page_ships_responseerror_handler(self, client, fixture_repo):
        """Every page (base.html) must wire htmx:responseError to render a
        visible French error, not silently no-op on a 4xx/5xx."""
        _write_gate(fixture_repo, "err-1")
        r = client.get("/gate/fixture/err-1")
        assert r.status_code == 200, r.text
        assert "htmx:responseError" in r.text
        assert "Échec de l'action" in r.text

    def test_base_page_ships_error_toast_element(self, client, fixture_repo):
        """A fallback toast element must exist for errors with no live
        hx-target (or a target since removed from the DOM)."""
        _write_gate(fixture_repo, "err-2")
        r = client.get("/gate/fixture/err-2")
        assert r.status_code == 200, r.text
        assert 'id="htmx-error-toast"' in r.text
        assert "htmx-inline-error" in r.text

    def test_forced_invalid_action_returns_4xx(self, client, fixture_repo):
        """An invalid decide action is a real HTTP error — exactly what
        htmx:responseError fires on in the browser."""
        _write_gate(fixture_repo, "err-3")
        r = client.post(
            "/gate/fixture/err-3/decide",
            data={"action": "not-a-real-action", "comment": ""},
        )
        assert 400 <= r.status_code < 500, \
            f"expected a 4xx for an invalid action, got {r.status_code}"


# ── 2. Double-submit guard (hx-disabled-elt) ────────────────────────────────

class TestDoubleSubmitGuard:

    def test_gate_card_decide_buttons_disabled_inflight(self, client, fixture_repo):
        _write_gate(fixture_repo, "guard-1")
        r = client.get("/gate/fixture/guard-1")
        assert r.status_code == 200, r.text
        # 4 decide buttons: approve/reject/modify/defer.
        assert r.text.count('hx-disabled-elt="this"') >= 4, \
            "gate_card.html decide buttons must all carry hx-disabled-elt"

    def test_gate_batch_decide_buttons_disabled_inflight(self, client, fixture_repo):
        _write_gate(fixture_repo, "guard-2a", {"kind": "echo_action", "gate_policy_id": "echo_action"})
        _write_gate(fixture_repo, "guard-2b", {"kind": "echo_action", "gate_policy_id": "echo_action"})
        r = client.get("/gate/fixture/kind/echo_action")
        assert r.status_code == 200, r.text
        # 2 gates x 4 buttons each.
        assert r.text.count('hx-disabled-elt="this"') >= 8, \
            "gate_batch.html decide buttons must all carry hx-disabled-elt"

    def test_rnd_card_actions_buttons_disabled_inflight(self):
        """Template-level check (no board/GH dependency needed): every
        submit button in the shared rnd_card_actions partial carries the
        double-submit guard."""
        tpl = Path(__file__).resolve().parent.parent / "templates" / "partials" / "rnd_card_actions.html"
        text = tpl.read_text(encoding="utf-8")
        button_count = text.count('type="submit"')
        assert button_count >= 3  # approve/reject/defer
        assert text.count('hx-disabled-elt="this"') >= button_count, \
            "every submit button in rnd_card_actions.html must carry hx-disabled-elt"


# ── 4. Attachment image onerror fallback ────────────────────────────────────

class TestAttachmentImageFallback:

    def test_gate_card_attachment_has_onerror_fallback(self, client, fixture_repo):
        _write_gate(fixture_repo, "img-1", {
            "attachments": [{"path": "outputs/2026-07-02/attachments/broken.png",
                              "caption": "Aperçu"}],
        })
        r = client.get("/gate/fixture/img-1")
        assert r.status_code == 200, r.text
        assert "onerror=" in r.text
        assert "gate-attachment-broken" in r.text
        assert "aperçu indisponible" in r.text

    def test_gate_batch_attachment_has_onerror_fallback(self, client, fixture_repo):
        _write_gate(fixture_repo, "img-2", {
            "kind": "echo_action", "gate_policy_id": "echo_action",
            "attachments": [{"path": "outputs/2026-07-02/attachments/broken.png",
                              "caption": "Aperçu"}],
        })
        r = client.get("/gate/fixture/kind/echo_action")
        assert r.status_code == 200, r.text
        assert "onerror=" in r.text
        assert "gate-attachment-broken" in r.text
        assert "aperçu indisponible" in r.text

    def test_broken_fallback_css_defined(self):
        css = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")
        assert ".gate-attachment-broken img" in css
        assert ".gate-attachment-caption-broken" in css


# ── 5. Scroll-preserving kanban auto-refresh ────────────────────────────────

class TestScrollPreservingRefresh:

    def test_kanban_persists_scrolly_before_reload(self):
        tpl = Path(__file__).resolve().parent.parent / "templates" / "kanban.html"
        text = tpl.read_text(encoding="utf-8")
        assert "sessionStorage.setItem(SCROLL_KEY" in text
        assert "window.scrollY" in text
        assert "location.reload()" in text

    def test_kanban_restores_scrolly_on_load(self):
        tpl = Path(__file__).resolve().parent.parent / "templates" / "kanban.html"
        text = tpl.read_text(encoding="utf-8")
        assert "sessionStorage.getItem(SCROLL_KEY" in text
        assert "scrollTo(" in text

    def test_kanban_existing_refresh_guards_untouched(self):
        """document.hidden / open <details> / text-selection guards must
        still gate the reload — this batch only adds scroll persistence."""
        tpl = Path(__file__).resolve().parent.parent / "templates" / "kanban.html"
        text = tpl.read_text(encoding="utf-8")
        assert "document.hidden" in text
        assert "details[open]" in text
        assert "window.getSelection" in text


# ── 6. Dead-UI cleanup ───────────────────────────────────────────────────────

class TestDeadUiCleanup:

    def test_decision_card_component_deleted(self):
        tpl = Path(__file__).resolve().parent.parent / "templates" / "components" / "decision-card.html"
        assert not tpl.exists(), "components/decision-card.html was unreferenced and should be deleted"

    def test_no_remaining_references_to_deleted_component(self):
        templates_dir = Path(__file__).resolve().parent.parent / "templates"
        for f in templates_dir.rglob("*.html"):
            text = f.read_text(encoding="utf-8")
            assert "components/decision-card.html" not in text, \
                f"{f} still references deleted components/decision-card.html"

    def test_orphaned_topbar_css_removed(self):
        css = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")
        assert ".topbar" not in css
        assert ".topnav" not in css
        assert ".brand-name" not in css
        assert ".brand-sub" not in css

    def test_no_template_emits_topbar_classes(self):
        templates_dir = Path(__file__).resolve().parent.parent / "templates"
        for f in templates_dir.rglob("*.html"):
            text = f.read_text(encoding="utf-8")
            assert 'class="topbar' not in text and "class='topbar" not in text
            assert 'class="topnav' not in text and "class='topnav" not in text

    def test_prio_chip_no_longer_reads_severity(self):
        tpl = Path(__file__).resolve().parent.parent / "templates" / "kanban.html"
        text = tpl.read_text(encoding="utf-8")
        # isolate the macro body
        start = text.index("{% macro prio_chip")
        end = text.index("{% endmacro %}", start)
        macro_body = text[start:end]
        assert "severity" not in macro_body, \
            "prio_chip must no longer read item.severity (nothing sets it — routes/kanban.py only sets priority)"
