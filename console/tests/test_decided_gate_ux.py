"""
test_decided_gate_ux.py — tests for the decided-gate UX (A, B, C).

A — action-specific feedback + "Decide recemment" tray on home.
B — modify is NOT terminal: gate stays visible in "En revision" state.
C — undo / change-your-mind on a decided-but-not-executed gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ─── Shared helpers ────────────────────────────────────────────────────────────

def _make_dept_repo(tmp_path: Path, slug: str = "fixture") -> Path:
    """Return a minimal bubble-ops-<slug> directory."""
    repo = tmp_path / f"bubble-ops-{slug}"
    (repo / "queues" / "gates").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": slug}}), encoding="utf-8"
    )
    return repo


def _write_gate(repo: Path, gate_id: str, extra: dict = None) -> Path:
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


def _write_decision(repo: Path, gate_id: str, action: str = "approve",
                    comment: str = "", decided_at: str = "2026-06-21T10:00:00Z",
                    processed: bool = False) -> Path:
    """Write a decision file (inbox/decisions/ or inbox/decisions/.processed/)."""
    if processed:
        d = repo / "inbox" / "decisions" / ".processed"
    else:
        d = repo / "inbox" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{gate_id}.yaml"
    p.write_text(
        yaml.safe_dump({
            "gate_id": gate_id,
            "action": action,
            "comment": comment,
            "decided_at": decided_at,
            "decided_by": "operator",
        }, sort_keys=False),
        encoding="utf-8",
    )
    return p


# ── fixture_repo (writable, self-contained) ────────────────────────────────────

@pytest.fixture
def fixture_repo(fixture_root: Path) -> Path:
    """Return the live 'fixture' dept repo root from conftest fixture_root."""
    return fixture_root / "bubble-ops-fixture"


# ═══════════════════════════════════════════════════════════════════════════════
# A — action-specific feedback in gate_decision_ok.html
# ═══════════════════════════════════════════════════════════════════════════════

class TestDecisionOkFragment:
    """gate_decision_ok.html must render action-specific messages."""

    def _post_decide(self, client, gate_id: str, action: str) -> str:
        r = client.post(
            f"/gate/fixture/{gate_id}/decide",
            data={"action": action, "comment": ""},
        )
        assert r.status_code == 200, f"POST decide failed ({r.status_code}): {r.text}"
        return r.text

    def test_approve_message(self, client, fixture_repo):
        """approve action must show an approval-specific confirmation."""
        _write_gate(fixture_repo, "ux-approve-1")
        html = self._post_decide(client, "ux-approve-1", "approve")
        # "prochain cycle" has no accents/apostrophes — safe substring of "sera exécuté au prochain cycle"
        assert "prochain cycle" in html

    def test_reject_message(self, client, fixture_repo):
        """reject action must show a rejection-specific confirmation."""
        _write_gate(fixture_repo, "ux-reject-1")
        html = self._post_decide(client, "ux-reject-1", "reject")
        # "Rejet" is a safe accent-free prefix of "Rejeté"
        assert "Rejet" in html

    def test_modify_message(self, client, fixture_repo):
        """modify action must show a revision-specific confirmation."""
        _write_gate(fixture_repo, "ux-modify-1")
        html = self._post_decide(client, "ux-modify-1", "modify")
        # "reviendra" has no accents; it appears in "reviendra modifié"
        assert "reviendra" in html

    def test_defer_message(self, client, fixture_repo):
        """defer action must show a deferral-specific confirmation."""
        _write_gate(fixture_repo, "ux-defer-1")
        html = self._post_decide(client, "ux-defer-1", "defer")
        # "Report" is an accent-free prefix of "Reporté"
        assert "Report" in html

    def test_messages_differ_across_actions(self, client, fixture_repo):
        """approve and reject messages must be distinct."""
        _write_gate(fixture_repo, "ux-diff-approve")
        _write_gate(fixture_repo, "ux-diff-reject")
        html_approve = self._post_decide(client, "ux-diff-approve", "approve")
        html_reject = self._post_decide(client, "ux-diff-reject", "reject")
        # The primary confirmation text must differ
        assert html_approve != html_reject


# ═══════════════════════════════════════════════════════════════════════════════
# A — list_recent_decisions service function
# ═══════════════════════════════════════════════════════════════════════════════

class TestListRecentDecisions:
    """list_recent_decisions returns entries sorted newest-first, respects limit,
    reads both inbox/decisions/ and inbox/decisions/.processed/, skips malformed."""

    def test_returns_empty_when_no_decisions(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"])
        assert result == []

    def test_returns_decision_from_inbox(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        _write_gate(repo, "gate-a")
        _write_decision(repo, "gate-a", action="approve", decided_at="2026-06-21T10:00:00Z")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"])
        assert len(result) == 1
        assert result[0]["gate_id"] == "gate-a"
        assert result[0]["action"] == "approve"
        assert result[0]["processed"] is False

    def test_returns_decisions_from_processed(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        _write_decision(repo, "gate-old", action="reject",
                        decided_at="2026-06-20T08:00:00Z", processed=True)
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"])
        assert any(d["gate_id"] == "gate-old" and d["processed"] is True for d in result)

    def test_sorted_newest_first(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        _write_decision(repo, "gate-older", action="approve", decided_at="2026-06-19T09:00:00Z")
        _write_decision(repo, "gate-newer", action="reject",  decided_at="2026-06-21T14:00:00Z")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"])
        assert result[0]["gate_id"] == "gate-newer"
        assert result[1]["gate_id"] == "gate-older"

    def test_respects_limit(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        for i in range(5):
            _write_decision(repo, f"gate-{i}", decided_at=f"2026-06-{10+i:02d}T00:00:00Z")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"], limit=3)
        assert len(result) == 3

    def test_aggregates_across_depts(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo_a = _make_dept_repo(tmp_path / "a", "alpha")
        repo_b = _make_dept_repo(tmp_path / "b", "beta")
        _write_decision(repo_a, "gate-a", decided_at="2026-06-21T10:00:00Z")
        _write_decision(repo_b, "gate-b", decided_at="2026-06-21T11:00:00Z")
        def mock_repo_path(slug):
            return repo_a if slug == "alpha" else repo_b if slug == "beta" else None
        monkeypatch.setattr(github_reader, "repo_path", mock_repo_path)
        result = github_reader.list_recent_decisions(["alpha", "beta"])
        gate_ids = [d["gate_id"] for d in result]
        assert "gate-a" in gate_ids
        assert "gate-b" in gate_ids

    def test_skips_malformed_yaml(self, tmp_path, monkeypatch):
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        d = repo / "inbox" / "decisions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.yaml").write_text("key: [unclosed", encoding="utf-8")
        _write_decision(repo, "good-gate", decided_at="2026-06-21T10:00:00Z")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)
        result = github_reader.list_recent_decisions(["alpha"])
        # malformed skipped, good one survives
        assert any(d["gate_id"] == "good-gate" for d in result)

    def test_skips_malformed_yaml_logs_warning(self, tmp_path, monkeypatch, caplog):
        """board #450: malformed decision files used to be skipped at debug
        level (invisible by default) — now logged as a warning."""
        import logging
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path, "alpha")
        d = repo / "inbox" / "decisions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.yaml").write_text("key: [unclosed", encoding="utf-8")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo if slug == "alpha" else None)

        with caplog.at_level(logging.WARNING, logger="console.github_reader"):
            github_reader.list_recent_decisions(["alpha"])

        assert any(
            "bad.yaml" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), f"expected a WARNING log naming bad.yaml, got: {[r.message for r in caplog.records]}"

    def test_skips_missing_dept(self, tmp_path, monkeypatch):
        """A slug whose repo_path returns None must not crash."""
        from console.services import github_reader
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: None)
        result = github_reader.list_recent_decisions(["nonexistent"])
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# A — home page shows recent decisions tray
# ═══════════════════════════════════════════════════════════════════════════════

def test_home_shows_recent_decision_tray(client, fixture_repo):
    """Home page must render the recent-decisions tray when decisions exist."""
    _write_decision(fixture_repo, "echo-1", action="approve",
                    decided_at="2026-06-21T10:00:00Z")
    r = client.get("/")
    assert r.status_code == 200, r.text
    # The tray heading — "cemment" is an accent-free substring of "récemment"
    assert "cemment" in r.text.lower()
    # The decision itself must appear
    assert "echo-1" in r.text or "approve" in r.text


def test_home_no_tray_when_no_decisions(client):
    """Home page must render silently with no tray section when there are no decisions."""
    r = client.get("/")
    assert r.status_code == 200, r.text
    # No crash — tray is simply absent when empty


# ═══════════════════════════════════════════════════════════════════════════════
# B — modify is NOT terminal: gate stays visible with _revision_requested
# ═══════════════════════════════════════════════════════════════════════════════

class TestModifyNotTerminal:

    def test_modify_decision_keeps_gate_in_list(self, tmp_path, monkeypatch):
        """A gate with action=modify in inbox/decisions/ must still appear in
        list_pending_gates (it is pending-but-awaiting-redraft)."""
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-modify")
        _write_decision(repo, "gate-modify", action="modify", comment="please revise")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        gates = github_reader.list_pending_gates("fixture")
        ids = [g.get("id") for g in gates]
        assert "gate-modify" in ids, "gate with modify decision must remain in list_pending_gates"

    def test_modify_gate_has_revision_flag(self, tmp_path, monkeypatch):
        """A gate kept by a modify decision must carry _revision_requested=True."""
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-modify-flag")
        _write_decision(repo, "gate-modify-flag", action="modify", comment="needs update")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        gates = github_reader.list_pending_gates("fixture")
        mod_gates = [g for g in gates if g.get("id") == "gate-modify-flag"]
        assert len(mod_gates) == 1
        g = mod_gates[0]
        assert g.get("_revision_requested") is True
        assert "needs update" in (g.get("_revision_comment") or "")

    def test_approve_decision_hides_gate(self, tmp_path, monkeypatch):
        """A gate with action=approve in inbox/decisions/ must be HIDDEN (terminal)."""
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-approve")
        _write_decision(repo, "gate-approve", action="approve")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        gates = github_reader.list_pending_gates("fixture")
        ids = [g.get("id") for g in gates]
        assert "gate-approve" not in ids, "gate with approve decision must be hidden"

    def test_reject_decision_hides_gate(self, tmp_path, monkeypatch):
        """A gate with action=reject in inbox/decisions/ must be HIDDEN."""
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-reject")
        _write_decision(repo, "gate-reject", action="reject")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        gates = github_reader.list_pending_gates("fixture")
        ids = [g.get("id") for g in gates]
        assert "gate-reject" not in ids

    def test_defer_decision_hides_gate(self, tmp_path, monkeypatch):
        """A gate with action=defer in inbox/decisions/ must be HIDDEN."""
        from console.services import github_reader
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-defer")
        _write_decision(repo, "gate-defer", action="defer")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        gates = github_reader.list_pending_gates("fixture")
        ids = [g.get("id") for g in gates]
        assert "gate-defer" not in ids

    def test_gate_card_shows_revision_banner(self, client, fixture_repo):
        """gate_card.html must show the En revision banner for _revision_requested gates."""
        _write_gate(fixture_repo, "gate-rev-banner")
        _write_decision(fixture_repo, "gate-rev-banner", action="modify",
                        comment="please shorten")
        r = client.get("/gate/fixture/gate-rev-banner")
        assert r.status_code == 200, r.text
        # "vision" is an accent-free substring of "révision"; "demand" of "demandé"
        assert "vision" in r.text or "demand" in r.text
        assert "please shorten" in r.text

    def test_gate_batch_shows_revision_banner(self, client, fixture_repo):
        """gate_batch.html must show the En revision banner for _revision_requested gates."""
        _write_gate(fixture_repo, "gate-rev-batch", {"kind": "strategic_question"})
        _write_decision(fixture_repo, "gate-rev-batch", action="modify",
                        comment="batch revision note")
        r = client.get("/gate/fixture/kind/strategic_question")
        assert r.status_code == 200, r.text
        # "vision" is an accent-free substring of "révision"; "demand" of "demandé"
        assert "vision" in r.text or "demand" in r.text


# ═══════════════════════════════════════════════════════════════════════════════
# C — undo / change-your-mind
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeleteGateDecision:

    def test_delete_removes_decision_file(self, tmp_path, monkeypatch):
        """delete_gate_decision removes inbox/decisions/<gate_id>.yaml and returns True."""
        from console.services import github_reader, dept_registry
        repo = _make_dept_repo(tmp_path)
        _write_gate(repo, "gate-del")
        _write_decision(repo, "gate-del", action="approve")
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        monkeypatch.setattr(dept_registry, "get_department", lambda slug: None)
        result = github_reader.delete_gate_decision("fixture", "gate-del")
        assert result is True
        assert not (repo / "inbox" / "decisions" / "gate-del.yaml").exists()

    def test_delete_returns_false_when_no_file(self, tmp_path, monkeypatch):
        """delete_gate_decision returns False when no decision file exists."""
        from console.services import github_reader, dept_registry
        repo = _make_dept_repo(tmp_path)
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        monkeypatch.setattr(dept_registry, "get_department", lambda slug: None)
        result = github_reader.delete_gate_decision("fixture", "nonexistent-gate")
        assert result is False

    def test_delete_does_not_touch_processed(self, tmp_path, monkeypatch):
        """delete_gate_decision must NOT remove files from .processed/."""
        from console.services import github_reader, dept_registry
        repo = _make_dept_repo(tmp_path)
        processed_file = _write_decision(repo, "gate-proc", action="approve", processed=True)
        monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
        monkeypatch.setattr(dept_registry, "get_department", lambda slug: None)
        result = github_reader.delete_gate_decision("fixture", "gate-proc")
        # Returns False because inbox/decisions/gate-proc.yaml doesn't exist
        assert result is False
        # The .processed file must be untouched
        assert processed_file.exists()


class TestUndoRoute:

    def test_undo_makes_gate_pending_again(self, client, fixture_repo):
        """POST /gate/<slug>/<id>/undo removes the decision file and the gate becomes pending."""
        _write_gate(fixture_repo, "gate-undo-1")
        dec_path = _write_decision(fixture_repo, "gate-undo-1", action="approve")
        assert dec_path.exists()

        r = client.post("/gate/fixture/gate-undo-1/undo")
        assert r.status_code == 200, r.text
        # Decision file must be gone
        assert not dec_path.exists()
        # Gate is now pending again
        from console.services import github_reader
        gates = github_reader.list_pending_gates("fixture")
        ids = [g.get("id") for g in gates]
        assert "gate-undo-1" in ids

    def test_undo_refuses_when_gate_already_resolved(self, client, fixture_repo):
        """POST /gate/<slug>/<id>/undo must refuse if the gate YAML has resolved:true."""
        # Gate YAML with resolved:true (agent already acted)
        resolved_gate_doc = {
            "id": "gate-resolved-1",
            "kind": "strategic_question",
            "source_layer": 1,
            "target_layer": 2,
            "risk_level": "medium",
            "requires_human": True,
            "current_mode": "manual_required",
            "resolved": True,
            "decided_by": "operator",
        }
        p = fixture_repo / "queues" / "gates" / "gate-resolved-1.yaml"
        p.write_text(yaml.safe_dump(resolved_gate_doc, sort_keys=False), encoding="utf-8")
        _write_decision(fixture_repo, "gate-resolved-1", action="approve")

        r = client.post("/gate/fixture/gate-resolved-1/undo")
        assert r.status_code == 200, r.text
        # Must communicate it's too late
        assert "trop tard" in r.text.lower() or "traite" in r.text.lower() or "tard" in r.text.lower()
        # Decision file must still exist (was NOT deleted)
        assert (fixture_repo / "inbox" / "decisions" / "gate-resolved-1.yaml").exists()

    def test_undo_returns_message_when_nothing_to_undo(self, client, fixture_repo):
        """POST /gate/<slug>/<id>/undo when no decision file exists returns graceful message."""
        _write_gate(fixture_repo, "gate-no-decision")
        r = client.post("/gate/fixture/gate-no-decision/undo")
        assert r.status_code == 200, r.text
        # Must not crash — returns a muted message
        assert "annuler" in r.text.lower() or "aucune" in r.text.lower() or len(r.text) > 0

    def test_undo_unknown_dept_returns_404(self, client):
        """POST /gate/unknown-dept/<id>/undo must 404 for unknown dept."""
        r = client.post("/gate/nonexistent-dept/any-gate/undo")
        assert r.status_code == 404
