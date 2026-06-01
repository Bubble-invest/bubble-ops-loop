#!/usr/bin/env python3
"""notion_logbook.py — shared Notion logbook writer for ops-loop depts.

Every dept's Layer 4 (evening debrief) writes ONE honest narrative entry
to the shared "Agent Logbook" Notion DB (32ccfc52-…). This is the lifted,
generalized version of Claudette's logbook script: the only difference is
the agent slug is parameterized (LOGBOOK_AGENT_ID env or --agent), so the
same code serves every dept.

The DB is shared and multi-agent: the `Agent` select column distinguishes
who wrote what. Schema (verified 2026-06-01):
  Résumé (title), Contenu/body (paragraph children), Date (date),
  Agent (select), Tags (multi_select), Pour (multi_select).

Usage (from a dept's Layer 4):
    LOGBOOK_AGENT_ID=maya python3 notion_logbook.py write \\
        --title "..." --body "..." [--tags a,b] [--for joris,jade]

Env:
  NOTION_API_KEY    — required to actually POST.
  LOGBOOK_AGENT_ID  — the dept slug for the Agent column (default "dispatch").
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

LOGBOOK_DB_ID = "32ccfc52-0644-8159-afe3-c76b38b629a1"
NOTION_VERSION = "2022-06-28"


def agent_id() -> str:
    return os.environ.get("LOGBOOK_AGENT_ID", "dispatch").strip() or "dispatch"


def _split_text(text: str, max_len: int) -> List[str]:
    """Split a body into <=max_len chunks on paragraph/space boundaries."""
    text = text or ""
    if len(text) <= max_len:
        return [text] if text else []
    out: List[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n ")
    if remaining:
        out.append(remaining)
    return out


def build_logbook_payload(
    agent: str,
    title: str,
    body: str,
    tags: Optional[List[str]] = None,
    pour: Optional[List[str]] = None,
    date: Optional[str] = None,
) -> dict:
    """Build the Notion create-page payload (pure — no I/O).

    The agent slug is always included in Tags + set as the Agent select.
    Body is chunked into <=2000-char paragraph blocks (Notion's limit)."""
    tags = list(tags or [])
    if agent not in tags:
        tags = [agent] + tags
    pour = list(pour or [])
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    properties = {
        "Résumé": {"title": [{"text": {"content": (title or "")[:2000]}}]},
        "Agent": {"select": {"name": agent}},
        "Date": {"date": {"start": date}},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
    }
    if pour:
        properties["Pour"] = {"multi_select": [{"name": p} for p in pour]}

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        }
        for chunk in _split_text(body, 2000)
    ]

    return {
        "parent": {"database_id": LOGBOOK_DB_ID},
        "properties": properties,
        "children": children[:100],
    }


def write_logbook(
    title: str,
    body: str,
    tags: Optional[List[str]] = None,
    pour: Optional[List[str]] = None,
    date: Optional[str] = None,
    agent: Optional[str] = None,
) -> Optional[str]:
    """POST one logbook entry. Returns the page id, or None on failure.

    Uses stdlib urllib (no third-party dep, so it runs in any dept venv).
    Never raises — a failed logbook write must not crash a Layer-4 run."""
    key = os.environ.get("NOTION_API_KEY") or ""
    if not key:
        print("NOTION_API_KEY not set — skipping logbook write", file=sys.stderr)
        return None
    import json as _json
    import urllib.request
    import urllib.error

    a = (agent or agent_id())
    payload = build_logbook_payload(a, title, body, tags, pour, date)
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            page_id = _json.loads(resp.read().decode("utf-8")).get("id")
        print(f"Logbook entry created [{a}]: {title[:60]} (id={page_id})")
        return page_id
    except urllib.error.HTTPError as e:  # noqa: BLE001 — never crash L4
        print(f"Logbook write failed: HTTP {e.code}", file=sys.stderr)
        try:
            print(e.read().decode("utf-8")[:300], file=sys.stderr)
        except Exception:
            pass
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Logbook write failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Shared ops-loop Notion logbook")
    sub = p.add_subparsers(dest="cmd")
    w = sub.add_parser("write")
    w.add_argument("--title", required=True)
    w.add_argument("--body", required=True)
    w.add_argument("--agent", default=None, help="Agent slug (else LOGBOOK_AGENT_ID).")
    w.add_argument("--tags", default="")
    w.add_argument("--for", dest="pour", default="")
    w.add_argument("--date", default=None)
    args = p.parse_args()

    if args.cmd == "write":
        tags = [t for t in args.tags.split(",") if t.strip()]
        pour = [x for x in args.pour.split(",") if x.strip()]
        pid = write_logbook(args.title, args.body, tags=tags, pour=pour,
                            date=args.date, agent=args.agent)
        return 0 if pid else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
