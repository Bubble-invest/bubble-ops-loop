"""
test_gate_decide_post.py — POST /gate/<dept>/<id>/decide.

Per Notion v5 line 1018 the POST writes inbox/decisions/<id>.yaml + commit.
For the in-tree disk-mode test, we assert the inbox file is materialized on
disk; the broker push is a separate concern handled by bubble-git-guard.
"""
from pathlib import Path
import yaml


def test_decide_post_writes_inbox_decision_file(client, fixture_root: Path):
    """POST approve -> writes inbox/decisions/<id>.yaml with action=approve."""
    r = client.post(
        "/gate/fixture/echo-1/decide",
        data={"action": "approve", "comment": "looks good"},
    )
    assert r.status_code in (200, 303), f"unexpected status: {r.status_code}"

    decision_path = (
        fixture_root / "bubble-ops-fixture" / "inbox" / "decisions" / "echo-1.yaml"
    )
    assert decision_path.exists(), f"decision file not written at {decision_path}"
    doc = yaml.safe_load(decision_path.read_text(encoding="utf-8"))
    assert doc.get("action") == "approve"
    assert doc.get("gate_id") == "echo-1"


def test_decide_post_rejects_invalid_action(client):
    """An action not in {approve,reject,modify,defer} must 400."""
    r = client.post(
        "/gate/fixture/echo-1/decide",
        data={"action": "delete_all"},
    )
    assert r.status_code == 400
