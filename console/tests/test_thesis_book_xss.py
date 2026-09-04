"""test_thesis_book_xss.py — board #1116 (cockpit audit, stored-XSS).

Two independent injection points on /dept/<slug>/portfolio, both fed by
producers this repo does not own (Ben's L1 tools):

  1. `data_json` — thesis_book_service.build_thesis_data()'s dict, spliced
     UNQUOTED into an inline `const DATA = ({{ data_json | safe }});` script
     expression. Any string VALUE inside that dict containing a literal
     `</script>` (or `<!--`) breaks out of the script element at the HTML
     PARSER level, regardless of JS syntax — the operator origin is one
     same-origin POST away (gate approvals, dept comments).
  2. `review_html`/`maps_html` — Ben's pre-rendered, self-contained HTML
     artifacts (outputs/<date>/portfolio-review-artifact.html,
     value-chain-maps-panel.html), previously inlined raw via `{{ x | safe }}`.

Fixes exercised here:
  1. console/routes/thesis_book.py::_json_for_inline_script escapes
     `<`/`>`/`&` to `\\uXXXX` JSON unicode escapes before the dict ever
     reaches the template.
  2. console/templates/thesis_book.html now isolates review_html/maps_html
     inside a `sandbox=""` iframe via `srcdoc` (never `| safe` — Jinja's
     default attribute-escaping is exactly what a `srcdoc="..."` value
     needs), instead of inlining them raw.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from console.routes.thesis_book import _json_for_inline_script


# ─── 1. _json_for_inline_script — unit-level ────────────────────────────────


def test_escapes_script_breakout_sequence():
    payload = {"name": 'x</script><script>alert(document.cookie)</script>'}
    out = _json_for_inline_script(payload)
    assert "</script>" not in out, (
        f"a literal </script> sequence survived escaping — an HTML parser "
        f"would end the script element here regardless of JS syntax. Got: {out!r}"
    )
    assert "\\u003c" in out and "\\u003e" in out


def test_escapes_ampersand_and_html_comment_open():
    payload = {"note": "R&D <!-- inject --> more"}
    out = _json_for_inline_script(payload)
    assert "<!--" not in out
    assert "&" not in out.replace("\\u0026", "")  # only escaped forms remain


def test_round_trips_through_json_loads():
    """The escaped form must still be valid, semantically-identical JSON —
    \\uXXXX is a standard JSON string escape, so json.loads must decode it
    back to the exact original value (never corrupt legitimate data)."""
    payload = {
        "theme": "Playbook: <AI & Infra> — \"quoted\" 'single'",
        "count": 3,
        "nested": {"list": ["<script>", "safe", None, True]},
    }
    out = _json_for_inline_script(payload)
    assert json.loads(out) == payload


def test_no_op_on_content_without_special_chars():
    payload = {"a": 1, "b": "plain text", "c": [1, 2, 3]}
    assert json.loads(_json_for_inline_script(payload)) == payload


def test_output_is_safe_to_splice_unquoted_into_script_tag():
    """Simulates the EXACT template usage: const DATA = (<output>);
    Must not contain anything an HTML parser would treat as element-closing,
    at any position in the string (not just top-level values)."""
    payload = {"a": {"b": {"c": "</script src=x onerror=alert(1)>"}}}
    out = _json_for_inline_script(payload)
    script_src = f"<script>const DATA = ({out});</script>"
    # The only two `<script` occurrences must be the real open/close tags
    # THIS TEST wrote, not anything from the payload.
    assert script_src.count("<script") == 1
    assert script_src.count("</script>") == 1


# ─── 2. /dept/<slug>/portfolio — full-route integration ─────────────────────


def _write_review_artifacts(repo_root: Path, review_html: str, maps_html: str) -> None:
    today = date.today().isoformat()
    d = repo_root / "outputs" / today
    d.mkdir(parents=True, exist_ok=True)
    (d / "portfolio-review-artifact.html").write_text(review_html, encoding="utf-8")
    (d / "value-chain-maps-panel.html").write_text(maps_html, encoding="utf-8")


@pytest.fixture
def malicious_payload():
    return {
        "review_html": (
            '<div id="pr-root"><style>#pr-root{color:red}</style>'
            "<p>Real content</p>"
            '<script>window.parent.document.title="pwned-review";</script>'
            '<img src=x onerror="fetch(\'/gate/x/approve\',{method:\'POST\'})">'
            "</div>"
        ),
        "maps_html": (
            '<div id="vcm-root"><style>#vcm-root{color:blue}</style>'
            "<p>Real maps content</p>"
            "<script>document.cookie='stolen=1';</script>"
            "</div>"
        ),
    }


@pytest.fixture
def portfolio_client(client, fixture_root: Path, malicious_payload, monkeypatch):
    """The `fixture` dept (from conftest's fixture_root) as a live/complete
    dept, with malicious review/maps artifacts planted, and
    build_thesis_data monkeypatched to avoid needing a real vault/sqlite —
    it returns a dict with a script-breakout string VALUE to exercise the
    data_json injection point too."""
    repo = fixture_root / "bubble-ops-fixture"
    _write_review_artifacts(repo, malicious_payload["review_html"], malicious_payload["maps_html"])

    from console.services import thesis_book as thesis_book_service

    def _fake_build_thesis_data(slug):
        return {
            "themes": [{
                "name": "</script><script>alert(document.cookie)</script>",
                "tickers": [],
            }],
        }

    monkeypatch.setattr(thesis_book_service, "build_thesis_data", _fake_build_thesis_data)
    return client


def test_portfolio_page_data_json_has_no_script_breakout(portfolio_client):
    resp = portfolio_client.get("/dept/fixture/portfolio")
    assert resp.status_code == 200
    body = resp.text
    assert "const DATA = (" in body
    # The malicious theme name must never produce a literal </script> in the
    # rendered page except the template's own two script tags.
    assert body.count("</script>") == body.count("<script"), (
        "mismatched <script>/</script> tag counts — a payload-supplied "
        "</script> likely broke out of the intended script element"
    )
    assert "alert(document.cookie)" not in body.replace("\\u003c", "<").replace(
        "\\u003e", ">"
    ) or "\\u003cscript\\u003e" in body, (
        "the malicious payload string should only appear in its ESCAPED "
        "(\\u003c/\\u003e) form"
    )


def test_portfolio_page_review_html_isolated_in_sandboxed_iframe(portfolio_client, malicious_payload):
    resp = portfolio_client.get("/dept/fixture/portfolio")
    assert resp.status_code == 200
    body = resp.text

    # The dangerous bits must NEVER appear as live, parseable HTML/JS outside
    # an attribute string — i.e. no un-attributed <script> tag from the
    # artifact, no bare onerror= handler outside quotes.
    assert 'sandbox=""' in body, "review/maps iframes must carry an empty sandbox attribute"
    assert body.count('sandbox=""') == 2, "expected exactly 2 sandboxed iframes (review + maps)"

    # Extract the srcdoc attribute values and confirm they are HTML-escaped
    # (i.e. the literal, un-escaped payload markup does NOT appear directly
    # in the document — only inside an escaped attribute value).
    import re
    srcdoc_values = re.findall(r'srcdoc="([^"]*)"', body)
    assert len(srcdoc_values) == 2, f"expected 2 srcdoc attributes, found {len(srcdoc_values)}"

    from html import unescape
    decoded = [unescape(v) for v in srcdoc_values]
    assert malicious_payload["review_html"] in decoded
    assert malicious_payload["maps_html"] in decoded

    # The raw (unescaped) dangerous markup must NOT appear anywhere else in
    # the document body as a live tag — only inside the escaped attributes
    # captured above.
    body_minus_srcdocs = body
    for raw in re.finditer(r'srcdoc="[^"]*"', body):
        body_minus_srcdocs = body_minus_srcdocs.replace(raw.group(0), "", 1)
    assert "<script>window.parent" not in body_minus_srcdocs
    assert "onerror=\"fetch(" not in body_minus_srcdocs
    assert "document.cookie='stolen=1'" not in body_minus_srcdocs


def test_portfolio_page_iframe_sandbox_has_no_escape_hatches(portfolio_client):
    """The sandbox attribute must be present with NO allow-* tokens — any
    `allow-scripts`/`allow-same-origin`/`allow-forms`/`allow-top-navigation`
    would reopen the exact exploit this fix closes."""
    resp = portfolio_client.get("/dept/fixture/portfolio")
    body = resp.text
    import re
    for m in re.finditer(r'<iframe[^>]*\bsandbox="([^"]*)"', body):
        assert m.group(1) == "", (
            f"iframe sandbox attribute must be empty (maximally restrictive), "
            f"found: sandbox=\"{m.group(1)}\""
        )
