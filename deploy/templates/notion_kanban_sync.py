#!/usr/bin/env python3
"""
Notion <-> Kanban bidirectional sync.

Runs on VPS (always-on). Accesses kanban_state.json on Mac via SSH.
Syncs every 15 minutes via systemd timer.

Phase 1: Merge both kanbans (no data loss)
Phase 2: Continuous bidirectional sync

Architecture:
  VPS (this script) --SSH--> Mac (kanban_state.json)
  VPS (this script) --HTTPS--> Notion API

Data model:
  - Notion page <-> kanban structured["notion:<page_id>"] = inline card dict
  - New kanban cards (no notion_id) -> create Notion pages
  - morty-agentic-audit entries: never synced to Notion (auto-generated noise)
"""

import json
import os
import subprocess
import sys
import time
import hashlib
from datetime import datetime, timezone

# ── Configuration ───────────────────────────────────────────────────────────

NOTION_DB_ID = "0c3c7178-83c3-445d-8161-7771c25ea8c7"
NOTION_KEY_PATH = "/home/claude/.config/notion/api_key"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

MAC_SSH_TARGET = "joris@{{INTERNAL_IP}}"
MAC_SSH_KEY = "/root/.ssh/id_ed25519_morty"
MAC_KANBAN_PATH = "/Users/joris/claude-workspaces/Rick_RnD/monitoring/kanban_state.json"

STATE_DIR = "/home/claude/bubble-ops-loop/state"
STATE_FILE = os.path.join(STATE_DIR, "notion_kanban_sync.json")

# Tasks to skip when creating Notion pages (auto-generated noise)
SKIP_NOTION_CREATE_TASKS = {
    "morty-agentic-audit",
    "morty-security-audit",
    "skills-audit-security",
    "skills-audit-content",
    "skills-audit-invest",
    "skills-audit-main",
    "skills-audit-claudette",
    "security-daily-audit",
}

# Tasks to skip entirely (not even notion->kanban updates for these)
SKIP_KANBAN_TASKS = set()

DRY_RUN = "--dry-run" in sys.argv
VERBOSE = "--verbose" in sys.argv or DRY_RUN

# ── Notion API helpers ──────────────────────────────────────────────────────

def _notion_key():
    try:
        with open(NOTION_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"ERROR: Notion API key not found at {NOTION_KEY_PATH}", file=sys.stderr)
        sys.exit(1)

def notion_request(path, method="GET", body=None):
    """Make a Notion API request. Returns (data, error)."""
    import urllib.request
    import urllib.error

    key = _notion_key()
    url = f"{NOTION_API_BASE}/{path}"
    data = json.dumps(body).encode() if body else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        return None, f"HTTP {e.code}: {err_body[:500]}"
    except Exception as e:
        return None, str(e)

def notion_query_all(database_id):
    """Fetch all pages from a Notion database (handles pagination)."""
    all_results = []
    start_cursor = None
    has_more = True

    while has_more:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data, err = notion_request(f"databases/{database_id}/query", method="POST", body=body)
        if err:
            print(f"ERROR querying Notion: {err}", file=sys.stderr)
            return None, err

        all_results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return all_results, None

def notion_get_page(page_id):
    """Get a single Notion page."""
    return notion_request(f"pages/{page_id}")

def notion_create_page(database_id, properties):
    """Create a new page in a Notion database."""
    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    return notion_request("pages", method="POST", body=body)

def notion_update_page(page_id, properties):
    """Update a Notion page's properties."""
    body = {"properties": properties}
    return notion_request(f"pages/{page_id}", method="PATCH", body=body)

# ── Notion property extraction ──────────────────────────────────────────────

def extract_title(page):
    """Extract the title text from a Notion page (title property is 'Projet')."""
    props = page.get("properties", {})
    # The title property is called "Projet" in this DB
    for key, prop in props.items():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_parts)
    return "Untitled"

def extract_select(page, prop_name):
    """Extract a select property value."""
    props = page.get("properties", {})
    prop = props.get(prop_name, {})
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    return ""

def extract_status(page, prop_name="Statut"):
    """Extract a status property value."""
    props = page.get("properties", {})
    prop = props.get(prop_name, {})
    if prop.get("type") == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    return extract_select(page, prop_name)

def extract_rich_text(page, prop_name):
    """Extract a rich_text property as plain string."""
    props = page.get("properties", {})
    prop = props.get(prop_name, {})
    if prop.get("type") == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    return ""

def notion_page_to_card(page):
    """Convert a Notion page to a kanban card dict."""
    props = page.get("properties", {})

    title = extract_title(page)  # from Projet (title property)
    status = extract_status(page)  # from Statut (status property)
    domaine = extract_select(page, "Domaine")
    priorite = extract_select(page, "Priorité")
    agent = extract_select(page, "Agent")  # Agent is a select, not rich_text
    notes = extract_rich_text(page, "Notes")
    github_link = ""
    lien = props.get("Lien / GitHub", {})
    if lien.get("type") == "url" and lien.get("url"):
        github_link = lien["url"]

    # Map Notion status to kanban status
    # Observed statuses: "🔨 En cours", "📋 Backlog", "✅ Terminé"
    status_lower = status.lower()
    if "en cours" in status_lower or "in progress" in status_lower:
        kanban_status = "investigating"
    elif "terminé" in status_lower or "complete" in status_lower or "done" in status_lower:
        kanban_status = "resolved"
    elif "bloqué" in status_lower or "en attente" in status_lower:
        kanban_status = "waiting"
    else:
        kanban_status = "open"  # Backlog / À faire → open

    # Map Notion priorite to kanban priority
    priority_lower = priorite.lower()
    if "high" in priority_lower or "haute" in priority_lower or "urgent" in priority_lower:
        priority = "high"
    elif "low" in priority_lower or "basse" in priority_lower:
        priority = "low"
    else:
        priority = "normal"

    # Build body from notes + domaine
    body_parts = []
    if domaine:
        body_parts.append(f"Domaine: {domaine}")
    if notes:
        body_parts.append(notes)
    body = "\n".join(body_parts)

    return {
        "task": "notion-import",
        "ts": page.get("created_time", datetime.now(timezone.utc).isoformat()),
        "title": title,
        "body": body[:2000],
        "type": "incident",
        "priority": priority,
        "status": kanban_status,
        "owner": agent or None,
        "actions": [],
        "context_url": github_link or None,
        "telegram_ref": None,
        "summary": title,
        "notion_id": page["id"],
        "notion_last_edited": page.get("last_edited_time", ""),
    }

# ── Mac kanban access via SSH ───────────────────────────────────────────────

def ssh_mac(command, timeout=30):
    """Run a command on the Mac via SSH. Returns (stdout, stderr, returncode)."""
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-i", MAC_SSH_KEY,
        MAC_SSH_TARGET,
        command,
    ]
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout, result.stderr, result.returncode

def read_kanban_state():
    """Read kanban_state.json from Mac via SSH cat."""
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-i", MAC_SSH_KEY,
        MAC_SSH_TARGET,
        f"cat {MAC_KANBAN_PATH}",
    ]
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ERROR reading kanban state: {result.stderr}", file=sys.stderr)
        return {"cards": {}, "structured": {}, "version": 1}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR parsing kanban state (first 200 chars): {result.stdout[:200]}", file=sys.stderr)
        return {"cards": {}, "structured": {}, "version": 1}

def write_kanban_state(state):
    """Write kanban_state.json back to Mac via SSH stdin -> cat -> file."""
    if DRY_RUN:
        structured_count = len(state.get("structured", {}))
        print(f"[DRY-RUN] Would write kanban_state.json with {structured_count} structured entries")
        return True

    json_str = json.dumps(state, ensure_ascii=False, indent=2)

    # Pipe JSON via SSH stdin directly into cat > file on Mac
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-i", MAC_SSH_KEY,
        MAC_SSH_TARGET,
        f"cat > {MAC_KANBAN_PATH}",
    ]

    result = subprocess.run(
        ssh_cmd,
        input=json_str,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"ERROR writing kanban state (rc={result.returncode}): {result.stderr}", file=sys.stderr)
        return False

    # Verify the file was written correctly (basic size check)
    verify_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-i", MAC_SSH_KEY,
        MAC_SSH_TARGET,
        f"wc -c < {MAC_KANBAN_PATH}",
    ]
    vr = subprocess.run(verify_cmd, capture_output=True, text=True, timeout=15)
    try:
        size_on_disk = int(vr.stdout.strip())
        if size_on_disk < 10:
            print(f"ERROR: kanban_state.json on Mac is too small ({size_on_disk} bytes)", file=sys.stderr)
            return False
    except ValueError:
        pass

    return True

# ── Sync state management ───────────────────────────────────────────────────

def load_sync_state():
    """Load the sync state (last sync timestamps, notion->kanban mappings)."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_sync_ts": None,
            "last_notion_cursor": None,
            "notion_page_hashes": {},  # page_id -> body hash (for change detection)
            "kanban_to_notion": {},     # structured_key -> notion_id
            "stats": {"notion_to_kanban": 0, "kanban_to_notion": 0, "total_runs": 0},
        }

def save_sync_state(state):
    """Save the sync state."""
    if DRY_RUN:
        return True
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
    return True

# ── Main sync logic ─────────────────────────────────────────────────────────

def hash_card(card):
    """Create a stable hash of a card's content for change detection."""
    content = json.dumps({
        "title": card.get("title", ""),
        "body": card.get("body", ""),
        "priority": card.get("priority", ""),
        "status": card.get("status", ""),
        "owner": card.get("owner", ""),
        "context_url": card.get("context_url", ""),
    }, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()[:16]

def sync_notion_to_kanban(notion_pages, kanban_state, sync_state):
    """Import/update Notion pages as kanban cards."""
    structured = kanban_state.setdefault("structured", {})
    notion_hashes = sync_state.setdefault("notion_page_hashes", {})
    changes = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}

    # Track which notion IDs we see in this run (for detecting deletions)
    seen_notion_ids = set()

    for page in notion_pages:
        page_id = page["id"]
        seen_notion_ids.add(page_id)
        notion_key = f"notion:{page_id}"

        # Check if this page was deleted/archived in Notion
        if page.get("in_trash") or page.get("is_archived"):
            if notion_key in structured:
                del structured[notion_key]
                changes["deleted"] += 1
                if VERBOSE:
                    print(f"  [DELETE] {extract_title(page)} (trashed/archived)")
            continue

        card = notion_page_to_card(page)
        current_hash = hash_card(card)

        if notion_key in structured:
            existing = structured[notion_key]
            existing_hash = notion_hashes.get(page_id)

            # Check if changed
            if existing_hash == current_hash:
                changes["unchanged"] += 1
                continue

            # Update existing card
            if existing.get("notion_last_edited") != page.get("last_edited_time"):
                if VERBOSE:
                    print(f"  [UPDATE] {card['title'][:60]}")
                structured[notion_key] = card
                notion_hashes[page_id] = current_hash
                changes["updated"] += 1
            else:
                changes["unchanged"] += 1
        else:
            # New Notion page -> create kanban card
            if VERBOSE:
                print(f"  [CREATE] {card['title'][:60]}")
            structured[notion_key] = card
            notion_hashes[page_id] = current_hash
            changes["created"] += 1

    # Handle deletions: pages that were in our hashes but not in Notion anymore
    for page_id in list(notion_hashes.keys()):
        if page_id not in seen_notion_ids:
            notion_key = f"notion:{page_id}"
            if notion_key in structured:
                del structured[notion_key]
                changes["deleted"] += 1
                if VERBOSE:
                    print(f"  [DELETE] notion:{page_id} (removed from Notion)")
            del notion_hashes[page_id]

    return changes

def sync_kanban_to_notion(kanban_state, sync_state):
    """Create Notion pages for new kanban cards that lack a notion_id."""
    structured = kanban_state.get("structured", {})
    k2n = sync_state.setdefault("kanban_to_notion", {})
    changes = {"created": 0, "skipped": 0, "errors": 0}

    for key, card in list(structured.items()):
        if not isinstance(card, dict):
            continue

        # Skip notion-imported cards (they already have a Notion page)
        if key.startswith("notion:"):
            continue

        # Skip cards that already have a notion_id
        if card.get("notion_id"):
            continue

        # Skip cards we've already processed
        if key in k2n:
            continue

        task = card.get("task", "")

        # Skip auto-generated noise
        if task in SKIP_NOTION_CREATE_TASKS:
            changes["skipped"] += 1
            continue

        # Only create Notion pages for cards with meaningful content
        title = card.get("title", "").strip()
        if not title or len(title) < 5:
            changes["skipped"] += 1
            continue

        # Skip cards that are just cron heartbeats (no human interaction)
        if task and "notion-import" not in task:
            # Check if this card has been manually interacted with (persisted state in cards[])
            has_history = False
            persisted = kanban_state.get("cards", {}).get(key)
            if isinstance(persisted, dict):
                has_history = len(persisted.get("history", [])) > 0

            # Skip heartbeat-only cards without human interaction
            if not has_history and card.get("priority", "normal") != "urgent":
                changes["skipped"] += 1
                continue

        # Create Notion page
        if VERBOSE:
            print(f"  [NOTION CREATE] {title[:60]}")

        if DRY_RUN:
            changes["created"] += 1
            continue

        # Build Notion properties (schema validated 2026-06-16)
        # - Projet = title property (DB has no "Name" property)
        # - Agent = select (not rich_text)
        # - Priorité = select: 🔥 High, 🟡 Medium, 🟢 Low
        # - Statut = status: 📋 Backlog, 🔨 En cours, ✅ Terminé
        priority_map = {"urgent": "🔥 High", "high": "🔥 High", "normal": "🟡 Medium", "low": "🟢 Low"}
        status_map = {"open": "📋 Backlog", "investigating": "🔨 En cours", "waiting": "📋 Backlog", "resolved": "✅ Terminé"}

        properties = {
            "Projet": {"title": [{"text": {"content": title[:200]}}]},
            "Notes": {"rich_text": [{"text": {"content": card.get("body", "")[:2000]}}]},
        }

        priority = card.get("priority", "normal")
        if priority in priority_map:
            properties["Priorité"] = {"select": {"name": priority_map[priority]}}

        status = card.get("status", "open")
        if status in status_map:
            properties["Statut"] = {"status": {"name": status_map[status]}}

        owner = card.get("owner", "")
        if owner:
            # Agent is a select property. Valid options: main, tony, ben, maya, miranda, morty, claudette, rick / rnd, content, rnd
            # Normalize common aliases to valid DB options
            agent_map = {
                "ricky": "main", "tony": "main", "main-strategist": "main",
                "rick": "rnd", "rnd": "rnd", "lab": "rnd", "rick / rnd": "rick / rnd",
                "ben": "ben", "maya": "maya", "miranda": "content", "content": "content",
                "morty": "morty", "claudette": "claudette",
            }
            agent_value = agent_map.get(owner.lower().strip(), owner)
            properties["Agent"] = {"select": {"name": agent_value}}

        data, err = notion_create_page(NOTION_DB_ID, properties)
        if err:
            print(f"  ERROR creating Notion page: {err}", file=sys.stderr)
            changes["errors"] += 1
            continue

        notion_id = data.get("id")
        if notion_id:
            # Link back: add notion_id to the card and create a notion:<id> entry
            card["notion_id"] = notion_id
            card["notion_last_edited"] = data.get("created_time", "")
            notion_key = f"notion:{notion_id}"
            structured[notion_key] = dict(card)  # Copy to new key

            k2n[key] = notion_id
            changes["created"] += 1
            if VERBOSE:
                print(f"    -> notion:{notion_id}")

    return changes

def sync_kanban_status_to_notion(kanban_state, sync_state):
    """Update Notion page status when kanban card status changes."""
    structured = kanban_state.get("structured", {})
    cards = kanban_state.get("cards", {})
    changes = {"updated": 0, "unchanged": 0, "errors": 0}

    status_map = {
        "open": "📋 Backlog",
        "investigating": "🔨 En cours",
        "waiting": "📋 Backlog",
        "resolved": "✅ Terminé",
        "snoozed": "📋 Backlog",
        "reopened": "📋 Backlog",
    }

    for key, card in list(structured.items()):
        if not isinstance(card, dict):
            continue
        if not key.startswith("notion:"):
            continue

        notion_id = card.get("notion_id")
        if not notion_id:
            continue

        # Get the current kanban status (check persisted cards state first)
        kanban_status = None
        persisted = cards.get(key)
        if isinstance(persisted, dict) and persisted.get("status"):
            kanban_status = persisted["status"]
        else:
            kanban_status = card.get("status", "open")

        notion_status = status_map.get(kanban_status)
        if not notion_status:
            continue

        # Check if the Notion page needs updating
        # We track the last status we synced in the card's notion_last_status field
        last_synced_status = card.get("_last_synced_status", "")
        if last_synced_status == kanban_status:
            changes["unchanged"] += 1
            continue

        if VERBOSE:
            print(f"  [STATUS UPDATE] notion:{notion_id[:8]}... -> {notion_status}")

        if DRY_RUN:
            changes["updated"] += 1
            card["_last_synced_status"] = kanban_status
            continue

        _, err = notion_update_page(notion_id, {
            "Statut": {"status": {"name": notion_status}},
        })
        if err:
            print(f"  ERROR updating Notion status for {notion_id}: {err}", file=sys.stderr)
            changes["errors"] += 1
            continue

        card["_last_synced_status"] = kanban_status
        changes["updated"] += 1

    return changes

def run_sync():
    """Main sync function."""
    print(f"=== Notion-Kanban Sync {datetime.now().isoformat()} ===")
    if DRY_RUN:
        print("[DRY-RUN MODE]")

    # Load state
    sync_state = load_sync_state()
    sync_state["stats"]["total_runs"] += 1

    # 1. Fetch all Notion pages
    print("Fetching Notion pages...")
    notion_pages, err = notion_query_all(NOTION_DB_ID)
    if err:
        print(f"FATAL: Cannot fetch Notion pages: {err}")
        return False
    print(f"  Got {len(notion_pages)} pages from Notion")

    # 2. Read kanban state from Mac
    print("Reading kanban state from Mac...")
    kanban_state = read_kanban_state()
    structured_count = len(kanban_state.get("structured", {}))
    print(f"  Got {structured_count} structured entries, {len(kanban_state.get('cards', {}))} persisted cards")

    # 3. Sync Notion -> Kanban
    print("Syncing Notion -> Kanban...")
    n2k_changes = sync_notion_to_kanban(notion_pages, kanban_state, sync_state)
    print(f"  Created: {n2k_changes['created']}, Updated: {n2k_changes['updated']}, "
          f"Unchanged: {n2k_changes['unchanged']}, Deleted: {n2k_changes['deleted']}")
    sync_state["stats"]["notion_to_kanban"] += n2k_changes["created"] + n2k_changes["updated"]

    # 4. Sync Kanban -> Notion (create Notion pages for new kanban cards)
    print("Syncing Kanban -> Notion (new cards)...")
    k2n_changes = sync_kanban_to_notion(kanban_state, sync_state)
    print(f"  Created: {k2n_changes['created']}, Skipped: {k2n_changes['skipped']}, Errors: {k2n_changes['errors']}")
    sync_state["stats"]["kanban_to_notion"] += k2n_changes["created"]

    # 5. Sync Kanban status -> Notion (update status on notion-linked cards)
    print("Syncing Kanban status -> Notion...")
    status_changes = sync_kanban_status_to_notion(kanban_state, sync_state)
    print(f"  Updated: {status_changes['updated']}, Unchanged: {status_changes['unchanged']}, Errors: {status_changes['errors']}")

    # 6. Write kanban state back to Mac
    print("Writing kanban state back to Mac...")
    if not write_kanban_state(kanban_state):
        print("FATAL: Cannot write kanban state")
        return False

    # 7. Save sync state
    sync_state["last_sync_ts"] = datetime.now(timezone.utc).isoformat()
    save_sync_state(sync_state)

    total_changes = (n2k_changes["created"] + n2k_changes["updated"] + n2k_changes["deleted"] +
                     k2n_changes["created"] + status_changes["updated"])
    errors = n2k_changes.get("errors", 0) + k2n_changes.get("errors", 0) + status_changes.get("errors", 0)
    print(f"Sync complete. Total changes: {total_changes}, Errors: {errors}")
    print(f"  State: {STATE_FILE}")
    print()
    # Zero changes is healthy (nothing to sync). Only fail on actual errors.
    return errors == 0

# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--init" in sys.argv:
        # First run: just do a full import from Notion, don't create Notion pages from kanban
        print("=== INITIAL SYNC (Notion -> Kanban only) ===")
        if DRY_RUN:
            print("[DRY-RUN MODE]")

        notion_pages, err = notion_query_all(NOTION_DB_ID)
        if err:
            print(f"FATAL: {err}")
            sys.exit(1)
        print(f"Got {len(notion_pages)} pages from Notion")

        kanban_state = read_kanban_state()
        print(f"Read kanban: {len(kanban_state.get('structured', {}))} structured entries")

        sync_state = load_sync_state()
        changes = sync_notion_to_kanban(notion_pages, kanban_state, sync_state)
        print(f"Notion->Kanban: Created={changes['created']} Updated={changes['updated']} "
              f"Unchanged={changes['unchanged']} Deleted={changes['deleted']}")

        if write_kanban_state(kanban_state):
            sync_state["last_sync_ts"] = datetime.now(timezone.utc).isoformat()
            sync_state["stats"]["notion_to_kanban"] += changes["created"] + changes["updated"]
            save_sync_state(sync_state)
            print("Initial sync complete.")
        else:
            print("FATAL: Cannot write kanban state")
            sys.exit(1)
    else:
        ok = run_sync()
        if not ok:
            sys.exit(1)
