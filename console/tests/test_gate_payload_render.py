"""
test_gate_payload_render.py — gate DETAIL view renders the artifact a gate's
`approval_bridge` actually points at, inline (card #642 follow-up).

Gap (live report, 2026-07-16, Jade): Miranda's X-thread gates carry only the
thesis in `summary` — the real content (the 7 tweets) lives in a separate
payload file referenced by `approval_bridge: {source: payload, item_ref:
outputs/<date>/<layer>/<name>.md}`. The gate detail view rendered only the
summary + attachments, so Jade could not see what she was being asked to
approve. Same problem for `source: substack_queue_json` gates, whose note
text is looked up by id in substack/data/queue.json.

Fix: console/routes/gate.py's `_attach_payload_rendered` resolves + reads the
referenced artifact via two new read-only github_reader helpers —
`resolve_gate_payload_path`/`read_gate_payload_text` (payload class, modelled
on resolve_attachment_path) and `read_substack_queue_note` (queue.json id
lookup) — renders it through the SAME sanitized markdown pipeline as the
thesis (`render_markdown_safe`, nh3), and gate_card.html shows it in a
"Contenu à valider" section above the thesis.

This MUST:
  1. render inline for `source: payload` gates (.md payload under outputs/),
  2. render inline for `source: substack_queue_json` gates (id lookup),
  3. sanitize the rendered HTML (script tags stripped, same as thesis),
  4. reject path traversal / absolute paths / non-outputs paths / wrong
     extension / symlink escape — same layered guard as resolve_attachment_path,
  5. size-cap large payloads,
  6. fall back gracefully ("introuvable") when the referenced file/id is
     missing — never 500, never blank silently for the operator,
  7. no-op (no section rendered) when the gate has no approval_bridge at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_payload_md(repo_root: Path, date: str, layer: str, fname: str, text: str) -> Path:
    d = repo_root / "outputs" / date / layer
    d.mkdir(parents=True, exist_ok=True)
    p = d / fname
    p.write_text(text, encoding="utf-8")
    return p


def _add_gate(repo_root: Path, gate_id: str, extra: dict) -> None:
    gates = repo_root / "queues" / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": gate_id,
        "kind": "publish_proposal",
        "slug": "fixture",
        "channel": "x",
        "risk_level": "high",
        "requires_human": True,
        "current_mode": "manual_required",
        "actions": ["approve", "reject", "modify", "defer"],
        "summary": "Thesis only — real content is in the payload.",
    }
    doc.update(extra)
    (gates / f"{gate_id}.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def ben_root(fixture_root: Path) -> Path:
    return fixture_root / "bubble-ops-fixture"


# ── 1. source: payload — renders inline ──────────────────────────────────────

def test_payload_gate_renders_content_inline(client, ben_root):
    _write_payload_md(
        ben_root, "2026-07-16", "2", "x-thread.md",
        "# Thread\n\n**1/** First tweet.\n\n**2/** Second tweet.\n",
    )
    _add_gate(ben_root, "payload-1", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/x-thread.md",
        },
    })
    r = client.get("/gate/fixture/payload-1")
    assert r.status_code == 200, r.text
    assert "Contenu à valider" in r.text
    assert "First tweet" in r.text
    assert "Second tweet" in r.text
    # markdown was actually rendered, not shown as raw text
    assert "<strong>1/</strong>" in r.text or "<h1>" in r.text


def test_payload_gate_sanitizes_script_tags(client, ben_root):
    _write_payload_md(
        ben_root, "2026-07-16", "2", "x-thread.md",
        "Tweet text.\n\n<script>alert('xss')</script>\n\nMore text.",
    )
    _add_gate(ben_root, "payload-xss", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/x-thread.md",
        },
    })
    r = client.get("/gate/fixture/payload-xss")
    assert r.status_code == 200
    # nh3 strips the <script> tag AND its content entirely (verified against
    # markdown_render.render_markdown_safe directly) — "alert(" must not
    # survive anywhere in the response.
    assert "alert(" not in r.text
    assert "Tweet text" in r.text
    assert "More text" in r.text


def test_payload_gate_renders_txt(client, ben_root):
    _write_payload_md(
        ben_root, "2026-07-16", "2", "note.txt",
        "Plain text payload, no markdown syntax.",
    )
    _add_gate(ben_root, "payload-txt", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/note.txt",
        },
    })
    r = client.get("/gate/fixture/payload-txt")
    assert r.status_code == 200
    assert "Plain text payload" in r.text


# ── 2. source: substack_queue_json — renders inline via id lookup ───────────

def _write_queue_json(repo_root: Path, notes: list[dict]) -> Path:
    d = repo_root / "substack" / "data"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "queue.json"
    p.write_text(json.dumps({"notes": notes}), encoding="utf-8")
    return p


def test_substack_queue_gate_renders_note_text(client, ben_root):
    _write_queue_json(ben_root, [
        {"id": "n_2026-07-16_gen_EN_0", "text": "The Note text Jade must validate."},
        {"id": "n_other", "text": "Should not appear."},
    ])
    _add_gate(ben_root, "queue-1", {
        "approval_bridge": {
            "source": "substack_queue_json",
            "item_ref": "n_2026-07-16_gen_EN_0",
        },
    })
    r = client.get("/gate/fixture/queue-1")
    assert r.status_code == 200, r.text
    assert "The Note text Jade must validate" in r.text
    assert "Should not appear" not in r.text


def test_substack_queue_gate_missing_id_shows_fallback(client, ben_root):
    _write_queue_json(ben_root, [{"id": "n_other", "text": "irrelevant"}])
    _add_gate(ben_root, "queue-missing", {
        "approval_bridge": {
            "source": "substack_queue_json",
            "item_ref": "n_does_not_exist",
        },
    })
    r = client.get("/gate/fixture/queue-missing")
    assert r.status_code == 200
    assert "introuvable" in r.text.lower()


# ── 3. Missing payload file → graceful fallback, never a 500 ────────────────

def test_payload_gate_missing_file_shows_fallback(client, ben_root):
    _add_gate(ben_root, "payload-missing", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/does-not-exist.md",
        },
    })
    r = client.get("/gate/fixture/payload-missing")
    assert r.status_code == 200
    assert "introuvable" in r.text.lower()


# ── 4. No approval_bridge → no-op, section absent ────────────────────────────

def test_gate_without_bridge_renders_no_payload_section(client, ben_root):
    _add_gate(ben_root, "no-bridge", {})
    r = client.get("/gate/fixture/no-bridge")
    assert r.status_code == 200
    assert "Contenu à valider" not in r.text


def test_gate_with_unrecognized_source_renders_no_payload_section(client, ben_root):
    _add_gate(ben_root, "weird-source", {
        "approval_bridge": {"source": "something_else", "item_ref": "whatever"},
    })
    r = client.get("/gate/fixture/weird-source")
    assert r.status_code == 200
    assert "Contenu à valider" not in r.text


# ── 5. resolve_gate_payload_path — traversal / structural rejection ─────────

@pytest.mark.parametrize("bad", [
    # Parent-directory traversal
    "../../../../etc/passwd",
    "outputs/2026-07-16/2/../../../../etc/passwd",
    # Absolute path
    "/etc/passwd",
    "/home/claude/agents/bubble-ops-fixture/dept.yaml",
    # Outside outputs/ entirely
    "dept.yaml",
    "queues/gates/payload-1.yaml",
    # Disallowed extensions — a payload is prose, not a general attachment
    "outputs/2026-07-16/2/evil.html",
    "outputs/2026-07-16/2/evil.js",
    "outputs/2026-07-16/2/evil.exe",
    "outputs/2026-07-16/2/data.csv",
    "outputs/2026-07-16/2/chart.png",
    # Double-extension — last suffix is .html -> rejected
    "outputs/2026-07-16/2/evil.md.html",
    # No extension
    "outputs/2026-07-16/2/noext",
])
def test_resolve_gate_payload_path_rejects_bad_path(ben_root, bad, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    # Plant a legit file so a bypass would actually return content.
    _write_payload_md(ben_root, "2026-07-16", "2", "legit.md", "legit content")
    from console.services.github_reader import resolve_gate_payload_path
    result = resolve_gate_payload_path("fixture", bad)
    assert result is None, f"bad path {bad!r} was NOT rejected"


def test_resolve_gate_payload_path_accepts_legit_path(ben_root, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    _write_payload_md(ben_root, "2026-07-16", "2", "legit.md", "legit content")
    from console.services.github_reader import resolve_gate_payload_path
    result = resolve_gate_payload_path("fixture", "outputs/2026-07-16/2/legit.md")
    assert result is not None
    assert result.name == "legit.md"


def test_resolve_gate_payload_path_rejects_symlink_escape(ben_root, monkeypatch, tmp_path):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET-OUTSIDE-REPO", encoding="utf-8")
    outdir = ben_root / "outputs" / "2026-07-16" / "2"
    outdir.mkdir(parents=True, exist_ok=True)
    link = outdir / "escape.md"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    from console.services.github_reader import resolve_gate_payload_path
    result = resolve_gate_payload_path("fixture", "outputs/2026-07-16/2/escape.md")
    assert result is None


def test_resolve_gate_payload_path_unknown_dept_returns_none(ben_root, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.services.github_reader import resolve_gate_payload_path
    result = resolve_gate_payload_path("nonexistent-dept", "outputs/2026-07-16/2/legit.md")
    assert result is None


# ── 6. Size cap — large payload gets truncated, not rejected outright ───────

def test_payload_gate_large_file_is_truncated(client, ben_root):
    big_text = "word " * 60000  # well over the 100KB cap
    _write_payload_md(ben_root, "2026-07-16", "2", "big.md", big_text)
    _add_gate(ben_root, "payload-big", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/big.md",
        },
    })
    r = client.get("/gate/fixture/payload-big")
    assert r.status_code == 200
    assert "tronqué" in r.text


def test_read_gate_payload_text_caps_at_100kb(ben_root, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    big_text = "x" * (200 * 1024)
    _write_payload_md(ben_root, "2026-07-16", "2", "huge.md", big_text)
    from console.services.github_reader import read_gate_payload_text
    content = read_gate_payload_text("fixture", "outputs/2026-07-16/2/huge.md")
    assert content is not None
    assert len(content) < 200 * 1024
    assert "tronqué" in content


# ── 7. read_substack_queue_note — malformed / missing file handling ─────────

def test_read_substack_queue_note_missing_file_returns_none(ben_root, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.services.github_reader import read_substack_queue_note
    result = read_substack_queue_note("fixture", "n_whatever")
    assert result is None


def test_read_substack_queue_note_malformed_json_returns_none(ben_root, monkeypatch):
    monkeypatch.setenv("READ_FROM_DISK", str(ben_root.parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "tok")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    d = ben_root / "substack" / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "queue.json").write_text("{not valid json", encoding="utf-8")
    from console.services.github_reader import read_substack_queue_note
    result = read_substack_queue_note("fixture", "n_whatever")
    assert result is None


# ── 8. Auth required (payload content must not leak unauthenticated) ────────

def test_payload_gate_requires_auth(client_noauth, ben_root):
    _write_payload_md(ben_root, "2026-07-16", "2", "x-thread.md", "Secret tweet content.")
    _add_gate(ben_root, "payload-auth", {
        "approval_bridge": {
            "source": "payload",
            "item_ref": "outputs/2026-07-16/2/x-thread.md",
        },
    })
    r = client_noauth.get("/gate/fixture/payload-auth")
    assert r.status_code in (401, 403)
    assert "Secret tweet content" not in r.text
