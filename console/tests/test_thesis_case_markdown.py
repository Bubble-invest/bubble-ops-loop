"""
test_thesis_case_markdown.py — the Thesis Book (tab 1) `mdInline` renderer for
AUTHOR free-text (per-line case / node.thesis).

BUG
---
The case/thesis text is authored in Markdown but the tab renders it as plain
text (`el({text:...})` -> textContent, and the local `esc()` only stringifies),
so readers saw literal `**` and `- ` instead of bold and bullets.

FIX
---
templates/thesis_book.html gains `mdInline(s)`: it HTML-escapes `& < >` FIRST
(does NOT rely on esc()), THEN applies a LIMITED subset on the escaped string —
`**bold**` -> <strong>, `*italic*`/`_italic_` -> <em>, leading `- `/`* ` -> a
`•` bullet, newline -> <br>. Nothing else. Because escaping runs first, an
injection like `**<script>**` renders as escaped text, never a live tag. Only
free-text case/thesis is routed through it (via innerHTML on a dedicated
element) — never numeric/label fields — and it never throws.

TESTING
-------
Two layers, mirroring test_thesis_book_total_renderer.py:
  1. STATIC (always runs, no browser): the template defines mdInline, escapes
     before it transforms, routes the chip-detail-body + thesis paragraphs
     through it via innerHTML, and is guarded against throwing.
  2. FUNCTIONAL (skips cleanly without node, same spirit as the playwright
     smoke): extract mdInline from the template and execute it in node against
     bold+bullet+injection inputs.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).resolve().parents[1] / "templates" / "thesis_book.html")


def _template_src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    """Return the source of `function <name>(...)` { ... } via brace matching."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name} not found in template"
    i = src.index("{", m.end())
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


# --------------------------------------------------------------------------
# 1) STATIC — always runs
# --------------------------------------------------------------------------
def test_template_defines_mdInline():
    assert "function mdInline(" in _template_src()


def test_mdInline_escapes_before_it_transforms():
    """The HTML-escape of & < > MUST precede the bold/italic transforms in the
    function body — escape-before-transform is the whole XSS guarantee."""
    fn = _extract_fn(_template_src(), "mdInline")
    esc_at = fn.index("&amp;")
    bold_at = fn.index("<strong>")
    assert esc_at < bold_at, "mdInline must HTML-escape BEFORE applying markdown"
    # escapes all three sensitive chars
    assert "&amp;" in fn and "&lt;" in fn and "&gt;" in fn


def test_mdInline_supports_only_the_limited_subset():
    fn = _extract_fn(_template_src(), "mdInline")
    assert "<strong>" in fn and "<em>" in fn      # bold + italic
    assert "<br>" in fn                            # line breaks
    assert "•" in fn                               # bullets
    # no links / images / raw-html passthrough
    assert "<a " not in fn and "href" not in fn and "<img" not in fn


def test_mdInline_never_throws():
    fn = _extract_fn(_template_src(), "mdInline")
    assert "try" in fn and "catch" in fn, "mdInline must be guarded (never throw)"
    assert "s===null" in fn and "s===undefined" in fn


def test_case_thesis_routed_through_mdInline_via_innerHTML():
    src = _template_src()
    # chip-detail-body: authored node.thesis -> innerHTML = mdInline(...)
    assert re.search(r"chip-detail-body[^\n]*\n?\s*\w+\.innerHTML\s*=\s*mdInline\(",
                     src) or \
           re.search(r"innerHTML\s*=\s*mdInline\(\s*thesisText\s*\)", src), \
           "chip-detail-body must render thesisText through mdInline via innerHTML"
    # theme thesis paragraphs also route through mdInline
    assert "innerHTML = mdInline(p)" in src.replace("innerHTML=mdInline(p)",
                                                     "innerHTML = mdInline(p)")


def test_chip_detail_body_no_longer_uses_textContent_for_authored_thesis():
    src = _template_src()
    # the old plain-text render of thesisText must be gone
    assert "chip-detail-body', text: thesisText" not in src


# --------------------------------------------------------------------------
# 2) FUNCTIONAL — skips cleanly without node
# --------------------------------------------------------------------------
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not installed (static tests cover the rest)")
def test_mdInline_functional_bold_bullet_and_injection():
    fn = _extract_fn(_template_src(), "mdInline")
    cases = {
        "bullet_bold": "- **EM diversifier.** Cheap vs developed world",
        "injection": "**<script>alert(1)</script>**",
        "italics": "an *italic* and _also_",
        "amp": "a & b",
        "nullish": None,
    }
    script = (
        fn + "\n"
        + "const IN = " + json.dumps(cases) + ";\n"
        + "const OUT = {}; for (const k in IN) OUT[k] = mdInline(IN[k]);\n"
        + "process.stdout.write(JSON.stringify(OUT));\n"
    )
    proc = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # bold + bullet render
    assert "<strong>EM diversifier.</strong>" in out["bullet_bold"]
    assert out["bullet_bold"].startswith("• ")

    # injection is escaped — no live tag
    assert "<script>" not in out["injection"]
    assert out["injection"] == "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>"

    # italics both forms
    assert "<em>italic</em>" in out["italics"] and "<em>also</em>" in out["italics"]

    # ampersand escaped
    assert out["amp"] == "a &amp; b"

    # nullish safe
    assert out["nullish"] == ""


if __name__ == "__main__":
    import sys
    ok = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok  {name}")
                ok += 1
            except pytest.skip.Exception as e:  # type: ignore[attr-defined]
                print(f"skip {name}: {e}")
    print(f"done ({ok} ran)")
    sys.exit(0)
