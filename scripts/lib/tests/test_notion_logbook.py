"""test_notion_logbook.py — shared ops-loop Notion logbook payload builder."""
from __future__ import annotations

import importlib
import os

import pytest

# Import the module under test. It lives in scripts/lib/.
from scripts.lib import notion_logbook as nl


def test_agent_select_is_set():
    p = nl.build_logbook_payload("maya", "T", "body")
    assert p["properties"]["Agent"]["select"]["name"] == "maya"


def test_title_and_db_parent():
    p = nl.build_logbook_payload("tony", "Mon résumé", "body")
    assert p["parent"]["database_id"] == nl.LOGBOOK_DB_ID
    assert p["properties"]["Résumé"]["title"][0]["text"]["content"] == "Mon résumé"


def test_agent_always_in_tags_first():
    p = nl.build_logbook_payload("cgp", "T", "b", tags=["compliance"])
    names = [t["name"] for t in p["properties"]["Tags"]["multi_select"]]
    assert names[0] == "cgp"
    assert "compliance" in names


def test_agent_not_duplicated_in_tags():
    p = nl.build_logbook_payload("maya", "T", "b", tags=["maya", "x"])
    names = [t["name"] for t in p["properties"]["Tags"]["multi_select"]]
    assert names.count("maya") == 1


def test_pour_optional_absent_when_empty():
    p = nl.build_logbook_payload("maya", "T", "b")
    assert "Pour" not in p["properties"]


def test_pour_present_when_given():
    p = nl.build_logbook_payload("maya", "T", "b", pour=["operator", "operator2"])
    names = [x["name"] for x in p["properties"]["Pour"]["multi_select"]]
    assert names == ["operator", "operator2"]


def test_default_date_is_today_utc():
    import datetime as dt
    p = nl.build_logbook_payload("maya", "T", "b")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert p["properties"]["Date"]["date"]["start"] == today


def test_explicit_date_respected():
    p = nl.build_logbook_payload("maya", "T", "b", date="2026-01-15")
    assert p["properties"]["Date"]["date"]["start"] == "2026-01-15"


def test_long_body_chunked_into_paragraphs():
    body = "x" * 5000  # > 2 chunks of 2000
    p = nl.build_logbook_payload("maya", "T", body)
    blocks = p["children"]
    assert len(blocks) >= 3
    for b in blocks:
        content = b["paragraph"]["rich_text"][0]["text"]["content"]
        assert len(content) <= 2000


def test_empty_body_no_children():
    p = nl.build_logbook_payload("maya", "T", "")
    assert p["children"] == []


def test_agent_id_reads_env(monkeypatch):
    monkeypatch.setenv("LOGBOOK_AGENT_ID", "tony")
    importlib.reload(nl)
    assert nl.agent_id() == "tony"


def test_agent_id_default_when_unset(monkeypatch):
    monkeypatch.delenv("LOGBOOK_AGENT_ID", raising=False)
    importlib.reload(nl)
    assert nl.agent_id() == "dispatch"


def test_write_logbook_noop_without_api_key(monkeypatch):
    """No NOTION_API_KEY → returns None, does not raise (must not crash L4)."""
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    assert nl.write_logbook("T", "b", agent="maya") is None
