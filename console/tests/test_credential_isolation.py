"""Console tests must not inherit operator credentials (#1598)."""

import os


def test_inherited_credentials_are_removed():
    assert not any(name in os.environ for name in (
        "TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"
    ))
