"""Read-only latest Messages projection for the internal cockpit."""
from __future__ import annotations
import json, os, urllib.error, urllib.request

DATABASE_ID = "8119a624-0fe9-4465-b123-15a0e6f2f569"
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

def _value(prop):
    if not isinstance(prop, dict): return None
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop.get(kind, []))
    if kind in ("select", "status"):
        value = prop.get(kind); return value.get("name") if value else None
    if kind == "date":
        value = prop.get("date"); return value.get("start") if value else None
    return None

def fetch_recent_messages(*, api_key=None, opener=None, database_id=DATABASE_ID):
    """Return at most 15 display rows; never mutates Notion."""
    key = (api_key if api_key is not None else os.environ.get("NOTION_API_KEY", "")).strip()
    if not key: return [], "Messages indisponibles"
    body = {"page_size": 15, "sorts": [{"property": "Date envoi", "direction": "descending"}]}
    request = urllib.request.Request(
        f"{API}/databases/{database_id}/query",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Notion-Version": VERSION,
                 "Content-Type": "application/json"},
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=10) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return [], "Messages indisponibles"
    rows=[]
    for page in payload.get("results", [])[:15]:
        props=page.get("properties") or {}
        rows.append({
            "title": _value(props.get("Message")) or "Message",
            "type": _value(props.get("Type")) or "—",
            "status": _value(props.get("Statut")) or "—",
            "sent_at": _value(props.get("Date envoi")) or "—",
            "account": _value(props.get("Envoyé sur le compte")) or "—",
            "validated_by": _value(props.get("Validé par")) or "—",
        })
    return rows, None
