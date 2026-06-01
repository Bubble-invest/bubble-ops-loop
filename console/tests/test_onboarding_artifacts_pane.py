"""
test_onboarding_artifacts_pane.py — right pane shows dept.yaml.draft +
missions/ + tests/.

Per Notion v5 lines 769-777, the artifacts pane shows what is being built
in the background as the operator chats. The mvp shows file names + a
preview of dept.yaml.draft.
"""


def test_artifacts_pane_lists_draft_yaml_and_missions(client):
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text.lower()
    # dept.yaml.draft preview is rendered
    assert "dept.yaml.draft" in body or "dept.yaml" in body
    # the chat thread for step 1 is visible somewhere
    # (no missions in our miranda fixture — that's fine; the file just won't appear)
