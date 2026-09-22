"""Tests for the App-signed structural-PR approval service (#1432 C2).

The three tests below monkeypatch `_mint_token` wholesale — they exercise
`submit_structural_pr_approval`'s own logic regardless of how the token is
obtained. The `TestTokenFileReading` class exercises `_mint_token` ITSELF
against the real timer-written token file (board #1432 follow-up: the minter
moved from a request-time `sudo -n` subprocess call to reading
`/run/bubble-cockpit-approver/token`, a root-owned systemd timer's tmpfs
output — mirrors `console/services/github_reader._read_contents_token()` and
its own test, `test_local_decision_delivery.py::TestContentsTokenInjection`).
"""
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


class TestTokenFileReading:
    """`_mint_token()` reads the timer-written tmpfs file — no subprocess, no
    sudo. Mirrors `test_local_decision_delivery.py`'s file-swap pattern."""

    def test_reads_token_from_file(self, tmp_path, monkeypatch):
        tokfile = tmp_path / "approver-token"
        tokfile.write_text("ghs_faketoken123")
        monkeypatch.setattr(pr_approver, "_APPROVER_TOKEN_FILE", str(tokfile))
        assert pr_approver._mint_token() == "ghs_faketoken123"

    def test_missing_file_is_not_provisioned(self, tmp_path, monkeypatch):
        """No file yet (key never dropped, or the timer hasn't minted) -> None,
        and end-to-end that becomes the clean 'not_provisioned' status with
        NOTHING posted to GitHub."""
        monkeypatch.setattr(pr_approver, "_APPROVER_TOKEN_FILE", str(tmp_path / "nope"))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert pr_approver._mint_token() is None

        called = {"n": 0}
        monkeypatch.setattr(pr_approver, "_post",
                             lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        status, _ = pr_approver.submit_structural_pr_approval(
            "Bubble-invest", "bubble-ops-loop", 1, "joris")
        assert status == "not_provisioned"
        assert called["n"] == 0

    def test_corrupt_file_content_is_rejected(self, tmp_path, monkeypatch):
        """A truncated/corrupt write (e.g. caught mid-mv) must never be
        forwarded as a bearer credential — defense in depth beyond the
        install script's atomic mv-into-place."""
        tokfile = tmp_path / "approver-token"
        tokfile.write_text("not-a-real-token")
        monkeypatch.setattr(pr_approver, "_APPROVER_TOKEN_FILE", str(tokfile))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert pr_approver._mint_token() is None

    def test_env_token_fallback_when_no_file(self, tmp_path, monkeypatch):
        """When no token file exists, GH_TOKEN env is used (dev/CI) —
        mirrors github_reader._read_contents_token()'s fallback exactly."""
        monkeypatch.setattr(pr_approver, "_APPROVER_TOKEN_FILE", str(tmp_path / "nope"))
        monkeypatch.setenv("GH_TOKEN", "ghs_envtoken")
        assert pr_approver._mint_token() == "ghs_envtoken"
