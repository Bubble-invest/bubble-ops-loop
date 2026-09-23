"""Tests for the App-signed structural-PR approval service (#1432 C2).

The top-level tests monkeypatch `_mint_token` (and, where the code path
reaches it, `_get`) wholesale — they exercise `submit_structural_pr_approval`'s
own logic regardless of how the token is obtained. The `TestTokenFileReading`
class exercises `_mint_token` ITSELF against the real timer-written token file
(board #1432 follow-up: the minter moved from a request-time `sudo -n`
subprocess call to reading `/run/bubble-cockpit-approver/token`, a root-owned
systemd timer's tmpfs output — mirrors
`console/services/github_reader._read_contents_token()` and its own test,
`test_local_decision_delivery.py::TestContentsTokenInjection`).
"""
from console.services import pr_approver

_SHA = "b" * 40


def test_not_provisioned_submits_nothing(monkeypatch):
    # No App key yet -> minter returns None -> we must NOT submit a review,
    # and we must never even try to verify the PR's head (nothing to verify
    # against without a token).
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_get", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    status, msg = pr_approver.submit_structural_pr_approval(
        "Bubble-invest", "bubble-ops-loop", 1, "joris", _SHA)
    assert status == "not_provisioned"
    assert called["n"] == 0


def test_approved_posts_app_review_pinned_to_head_sha(monkeypatch):
    """Board #1432 review: the review payload must carry `commit_id` pinned
    to the exact SHA the operator reviewed — not an unpinned approval of
    whatever GitHub considers HEAD at POST time."""
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    monkeypatch.setattr(pr_approver, "_get", lambda url, token: (200, {"head": {"sha": _SHA}}))
    seen = {}
    def fake_post(url, token, payload):
        seen["url"] = url; seen["token"] = token; seen["payload"] = payload
        return 201, {"id": 42}
    monkeypatch.setattr(pr_approver, "_post", fake_post)
    status, msg = pr_approver.submit_structural_pr_approval(
        "Bubble-invest", "bubble-ops-loop", 7, "joris", _SHA)
    assert status == "approved"
    assert seen["payload"]["event"] == "APPROVE"
    assert seen["payload"]["commit_id"] == _SHA
    assert "pulls/7/reviews" in seen["url"]
    assert seen["token"] == "ghs_faketoken"


def test_github_error_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    monkeypatch.setattr(pr_approver, "_get", lambda url, token: (200, {"head": {"sha": _SHA}}))
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: (422, {"message": "nope"}))
    status, msg = pr_approver.submit_structural_pr_approval(
        "Bubble-invest", "bubble-ops-loop", 7, "joris", _SHA)
    assert status == "error"
    assert "nope" in msg


def test_stale_head_refuses_and_posts_nothing(monkeypatch):
    """Board #1432 review, blocking finding: the PR's head moved since the
    operator loaded the deep-link page (e.g. the PR's own author — an
    explicitly adversarial actor in this design — pushed a new commit). Must
    refuse rather than silently approve whatever is current."""
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    monkeypatch.setattr(pr_approver, "_get", lambda url, token: (200, {"head": {"sha": "c" * 40}}))
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    status, msg = pr_approver.submit_structural_pr_approval(
        "Bubble-invest", "bubble-ops-loop", 7, "joris", _SHA)
    assert status == "stale"
    assert called["n"] == 0
    assert "reload" in msg.lower()


def test_head_verification_fetch_error_is_reported_not_swallowed(monkeypatch):
    """Can't verify the current head at all (network blip, PR vanished) ->
    "error", never silently proceed to approve an unverified head."""
    monkeypatch.setattr(pr_approver, "_mint_token", lambda: "ghs_faketoken")
    monkeypatch.setattr(pr_approver, "_get", lambda url, token: (404, {"message": "Not Found"}))
    called = {"n": 0}
    monkeypatch.setattr(pr_approver, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    status, msg = pr_approver.submit_structural_pr_approval(
        "Bubble-invest", "bubble-ops-loop", 7, "joris", _SHA)
    assert status == "error"
    assert called["n"] == 0


class TestTokenFileReading:
    """`_mint_token()` reads the timer-written tmpfs file — no subprocess, no
    sudo, and (board #1432 review, blocking finding) NO env-var fallback.
    Mirrors `test_local_decision_delivery.py`'s file-swap pattern."""

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
            "Bubble-invest", "bubble-ops-loop", 1, "joris", _SHA)
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

    def test_env_tokens_ignored_even_when_set(self, tmp_path, monkeypatch):
        """Board #1432 review, blocking finding: GH_TOKEN/GITHUB_TOKEN ARE set
        in production (console/deploy/bubble-ops-console.service.template's
        ExecStartPre derives GH_TOKEN from GITHUB_TOKEN unconditionally at
        every start) — an env fallback here would silently approve as a
        non-App identity whenever the token file is merely missing/stale.
        Must be ignored, not used as a fallback, even when both are set."""
        monkeypatch.setattr(pr_approver, "_APPROVER_TOKEN_FILE", str(tmp_path / "nope"))
        monkeypatch.setenv("GH_TOKEN", "ghs_envtoken")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_alsoset")
        assert pr_approver._mint_token() is None
