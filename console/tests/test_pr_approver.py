"""Tests for the App-signed structural-PR approval service (#1432 C2)."""
from console.services import pr_approver


def test_not_provisioned_submits_nothing(monkeypatch):
    # No App key yet -> minter returns None -> we must NOT submit a review.
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    status, msg = pr_approver.submit_structural_pr_approval("Bubble-invest", "bubble-ops-loop", 1, "joris")
    assert status == "not_provisioned"
    assert called["n"] == 0


def test_approved_posts_app_review(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    seen = {}
    def fake_post(url, token, payload):
        seen["url"] = url; seen["token"] = token; seen["payload"] = payload
        return 201, {"id": 42}
    monkeypatch.setattr(pr_approver, "_post", fake_post)
    status, msg = pr_approver.submit_structural_pr_approval("Bubble-invest", "bubble-ops-loop", 7, "joris")
    assert status == "approved"
    assert seen["payload"]["event"] == "APPROVE"
    assert "pulls/7/reviews" in seen["url"]
    assert seen["token"] == "ghs_faketoken"


def test_github_error_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: (422, {"message": "nope"}))
    status, msg = pr_approver.submit_structural_pr_approval("Bubble-invest", "bubble-ops-loop", 7, "joris")
    assert status == "error"
    assert "nope" in msg
