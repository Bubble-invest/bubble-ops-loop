"""Tests for markdown_render — the whiteboard's markdown→safe-HTML renderer.

Security is the point of this module (agent-authored content), so the tests
center on: tables render, AND script/handlers/dangerous schemes are stripped.
"""
from console.services.markdown_render import render_markdown_safe


def test_empty_returns_none():
    assert render_markdown_safe(None) is None
    assert render_markdown_safe("") is None
    assert render_markdown_safe("   \n  ") is None


def test_table_renders():
    md = (
        "| Sleeve | Now |\n"
        "|---|---|\n"
        "| ETF | 60.7% |\n"
    )
    html = str(render_markdown_safe(md))
    assert "<table>" in html
    assert "<th>Sleeve</th>" in html
    assert "<td>60.7%</td>" in html


def test_headings_and_emphasis_render():
    html = str(render_markdown_safe("# Title\n\nsome *italic* and **bold**"))
    assert "<h1>Title</h1>" in html
    assert "<em>italic</em>" in html
    assert "<strong>bold</strong>" in html


def test_image_allowed_for_embedded_charts():
    html = str(render_markdown_safe("![chart](/gate/ben/chart?path=outputs/x.png)"))
    assert "<img" in html
    assert "src=\"/gate/ben/chart?path=outputs/x.png\"" in html


def test_script_tag_stripped():
    html = str(render_markdown_safe("hello <script>alert('xss')</script> world"))
    assert "<script>" not in html
    assert "alert" not in html  # nh3 drops script *content* too


def test_event_handler_attr_stripped():
    html = str(render_markdown_safe('<a href="/x" onclick="steal()">link</a>'))
    assert "onclick" not in html
    assert "steal" not in html


def test_javascript_scheme_stripped():
    html = str(render_markdown_safe("[click](javascript:alert(1))"))
    assert "javascript:" not in html


def test_iframe_stripped():
    html = str(render_markdown_safe('<iframe src="https://evil.example"></iframe>'))
    assert "<iframe" not in html


def test_data_uri_image_stripped():
    # data: scheme is not allowlisted → the src is dropped (no data-uri exfil/abuse)
    html = str(render_markdown_safe('![x](data:text/html;base64,PHNjcmlwdD4=)'))
    assert "data:text/html" not in html
