"""
test_thesis_book_total_renderer.py — the fence for the recurring Thesis Book
(tab 1) outage on /dept/<slug>/portfolio.

THE OUTAGE
----------
Tab 1 is assembled LIVE per request: routes/thesis_book.py calls
services/thesis_book.py::build_thesis_data(), JSON-dumps it into
templates/thesis_book.html as `const DATA = {...}`, and the whole renderer runs
as ONE IIFE. So a single JS throw blanks the ENTIRE tab — no partial page, no
error state, just ~8.6KB of empty scaffolding where ~37-40KB of report should
be. On 2026-08-12 that throw was:

    Uncaught TypeError: Cannot read properties of undefined (reading 'toFixed')
    thesis_book.html:786
    expB.textContent = fmtUsd(m.exposure_usd)+' / '+m.exposure_pct_nav.toFixed(1)+'% NAV';

`macro[].exposure_pct_nav` is not produced by ANY code path — the producer
(bubble-ops-ben/tools/vault_to_graph.py::parse_macro_views) emits only
id/title/subtitle/signal/indicators/themes/portfolio_expression.

WHY IT KEPT COMING BACK
-----------------------
The template was copied from a hand-authored artifact whose data was richer
than what the producer emits, and each fix patched only that day's symptom. The
previous one (`base.setdefault("macro", [])`) made an EMPTY macro list survive —
so the page came back, and then died again the moment macro became populated
and execution reached the first unguarded scalar inside it. Guarding line 786
alone would have set up the identical failure at the theme-card twin
(`theme.exposure_pct_nav.toFixed(2)`), which is unreachable today only because
`themes[].held_tickers` is also never produced.

WHAT THIS FILE FENCES
---------------------
1. The producer/consumer CONTRACT (always runs, no browser):
   build_thesis_data() must return every key the template dereferences, with
   safe defaults, and must shout when the producer omits one.
2. The TEMPLATE's totality (always runs, static): no `.toFixed()` on a
   DATA-derived member expression, ever again.
3. A real-browser SMOKE test (skips without playwright, same convention as
   test_dept_page_mobile_playwright.py): render the live thesis page in
   headless chromium and fail on (a) any JS error and (b) a suspiciously small
   `#pane-thesis`. Measured on the real payload: broken ≈ 8.6KB, healthy
   ≈ 37-40KB — 15KB is a wide, stable floor.

The fixture payload below is SYNTHETIC (this repo is public — no real NAV,
positions or brokers here). What is real about it is the SHAPE: it carries
exactly the key set `vault_to_graph.py` emits today, i.e. the shape that broke
production.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest
import yaml

TEMPLATE = (Path(__file__).resolve().parents[1] / "templates"
            / "thesis_book.html")

TEST_BEARER = "test-token-thesis-renderer"

#: Measured floor. The broken page rendered 8,678 bytes of `#pane-thesis`; a
#: healthy one rendered 37,320. Anything under 15KB means a render step died.
MIN_PANE_BYTES = 15_000


# ── the producer's ACTUAL shape (synthetic values, real key set) ─────────────

def _producer_shaped_payload() -> dict:
    """A graph-data payload with exactly the keys vault_to_graph.py emits —
    notably WITHOUT macro[].exposure_pct_nav / exposure_usd / return_wtd /
    research_note / themes_driven, and WITHOUT themes[].held_tickers /
    exposure_usd / exposure_pct_nav / theme_return_wtd / watchlist_return_avg.
    That absence is the entire point of this fixture."""
    sectors = ["Technology", "Financials", "Healthcare", "Energy", "Industrials",
               "Materials", "Utilities", "Consumer Staples", "Real Estate",
               "Communication Services", "Commodities"]
    nodes = []
    for i in range(60):
        nodes.append({
            "id": f"TK{i:03d}",
            "name": f"Synthetic Holding {i}",
            "sector": sectors[i % len(sectors)],
            "quality": ["PASS", "WATCH", "CORE"][i % 3],
            "tag": "synthetic",
            "themes": [f"theme-{i % 12}"],
            "thesis": "Synthetic thesis text for fixture purposes only. "
                      "Second sentence so sentenceSplit() has something to do.",
            "catalyst": "Q4 print",
            "return_1y": 1.5 * i, "return_3y": 2.5 * i,
            "roe": 12.0, "fwd_pe": 18.0,
            "held": i < 9,
            "weight_pct": 0.5 if i < 9 else 0.0,
            "broker": "synthetic-broker" if i < 9 else "",
            "market_value": 1000.0 * i if i < 9 else 0.0,
            "cluster": "cluster-a", "price": 100.0 + i, "moat": "narrow",
        })
    themes = []
    for i in range(39):
        themes.append({
            "id": f"theme-{i}",
            "name": f"Synthetic Theme {i} — the long qualifier half",
            "status": "active",
            "conviction": "medium",
            "last_verified": "2026-08-01",
            "review_by": "2026-09-01",
            "tickers": [f"TK{(i * 3 + k) % 60:03d}" for k in range(4)],
            "ticker_count": 4,
        })
    macro = []
    for i in range(6):
        macro.append({
            "id": f"macro-{i}",
            "title": f"Synthetic Regime {i}",
            "subtitle": "A one-line framing of the regime.",
            "signal": ["green", "neutral", "warning"][i % 3],
            "indicators": [
                {"name": "IDX", "value": 100.0 + i, "unit": "$",
                 "signal": "neutral"},
                {"name": "VOL", "value": 15.0, "unit": "", "signal": "green"},
            ],
            # NOTE: the producer calls this `themes`; the template historically
            # read `themes_driven`. Keep it as `themes` — that rename is part
            # of what this fixture must keep exercising.
            "themes": [f"theme-{i * 6 + k}" for k in range(6)],
            "portfolio_expression": "- **cluster** (~2% NAV): synthetic only",
        })
    return {
        "generated_at": "2026-08-12T06:00:00+00:00",
        "nav": 100000.0,
        "since_rebase_pct": 5.0,
        "node_count": len(nodes),
        "theme_count": len(themes),
        "cluster_count": 1,
        "held_count": 9,
        "nodes": nodes,
        "themes": themes,
        "macro": macro,
        "clusters": [{"id": "cluster-a", "name": "Cluster — A", "nav_pct": 2.0,
                      "members": ["TK000"], "worst_30d": -5.0, "enb": 1.1}],
        "sectors": [{"id": s.lower().replace(" ", "-"), "name": s,
                     "ticker_count": 3, "pass_count": 1, "held_count": 1,
                     "weight_pct": 1.0 + i, "acwi_weight_pct": 2.0 + i}
                    for i, s in enumerate(sectors)],
        "acwi_sector_weights": {s: 2.0 + i for i, s in enumerate(sectors)},
        "global_macro": {
            "date": "2026-08-12",
            "narrative": "Synthetic macro narrative.\n\nSecond paragraph.",
            "key_signals": [{"indicator": "VIX", "value": 15.0,
                             "signal": "green", "tripwire": "25"}],
        },
        "portfolio_overview": {
            "nav": 100000.0, "nav_date": "2026-08-12",
            "since_rebase_pct": 5.0, "day_change_usd": 250.0,
            "day_change_pct": 0.25, "mtd_pct": 1.0, "ytd_pct": 4.0,
            "drawdown_pct": -2.0, "max_drawdown_itd_pct": -8.0,
            "sharpe_90d": 1.1, "sortino_90d": 1.4, "vs_acwi_pct": 0.5,
            "acwi_return_pct": 4.5,
            "brokers": [{"name": "Synthetic", "nav_usd": 100000.0,
                         "positions": 9, "status": "live"}],
            "pending_decisions": [{"id": "dec1", "ticker": "TK000",
                                   "action": "buy", "status": "proposed"}],
            # cost_usd / cost_pct_nav deliberately absent — the live producer
            # emits only instrument + status here.
            "hedge_overlay": {"instrument": "SYN PUT", "status": "live"},
        },
    }


def _build_fixture_root(root: Path) -> None:
    """A minimal on-disk dept the console will treat as Live."""
    live = root / "bubble-ops-fixture"
    live.mkdir(parents=True)
    (live / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops",
                           "mandate": "MVP fixture"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "onboarding").mkdir()
    (live / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": "fixture", "display_name": "Fixture",
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "queues" / "gates").mkdir(parents=True)
    (live / "outputs" / date.today().isoformat()).mkdir(parents=True)


# ══════════════════════════════════════════════════════════════════════════
# 1. The contract — build_thesis_data() must satisfy every key the template
#    dereferences, and must say so loudly when the producer does not.
# ══════════════════════════════════════════════════════════════════════════

def test_normalize_fills_every_documented_top_level_key():
    from console.services.thesis_book import (
        REQUIRED_THESIS_KEYS, normalize_thesis_data)

    out = normalize_thesis_data({}, "fixture")
    for key, default in REQUIRED_THESIS_KEYS.items():
        assert key in out, f"contract key {key!r} missing from payload"
        if isinstance(default, list):
            assert isinstance(out[key], list), f"{key} must be a list"
        elif isinstance(default, dict):
            assert isinstance(out[key], dict), f"{key} must be a dict"


def test_normalize_fills_every_documented_macro_and_theme_key():
    """The 2026-08-12 outage was a SCALAR inside a list that had itself been
    successfully defaulted — so list-level defaulting is not enough."""
    from console.services.thesis_book import (
        MACRO_ITEM_KEYS, THEME_ITEM_KEYS, normalize_thesis_data)

    out = normalize_thesis_data(_producer_shaped_payload(), "fixture")
    for m in out["macro"]:
        for key in MACRO_ITEM_KEYS:
            assert key in m, f"macro[] missing contract key {key!r}"
    for t in out["themes"]:
        for key in THEME_ITEM_KEYS:
            assert key in t, f"themes[] missing contract key {key!r}"

    # The specific field that blanked the tab.
    assert all("exposure_pct_nav" in m for m in out["macro"])
    assert all("exposure_pct_nav" in t for t in out["themes"])


def test_producer_theme_rename_does_not_empty_the_macro_grouping():
    """vault_to_graph emits a macro's theme list as `themes`; the template reads
    `themes_driven`. If that bridge breaks, every theme silently falls into
    "Cross-Cutting & Idiosyncratic" and nobody notices."""
    from console.services.thesis_book import normalize_thesis_data

    out = normalize_thesis_data(_producer_shaped_payload(), "fixture")
    assert out["macro"][0]["themes_driven"] == out["macro"][0]["themes"]
    assert out["macro"][0]["themes_driven"], "macro→theme grouping is empty"


def test_missing_contract_keys_are_logged_not_swallowed(caplog):
    """A producer that drops a key we did not expect must leave a trail — that
    is the difference between "we found out from the logs" and "we found out
    because Joris opened the page"."""
    from console.services import thesis_book as tb

    tb._last_contract_signature.clear()
    with caplog.at_level(logging.INFO,
                         logger="console.services.thesis_book"):
        tb.normalize_thesis_data({"macro": [{"id": "m"}]}, "loudtest")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "render contract" in joined or "known-absent" in joined
    assert "REQUIRED_THESIS_KEYS" in joined or "defaulted" in joined


def test_build_thesis_data_never_returns_a_partial_payload(monkeypatch,
                                                           tmp_path):
    """Including the no-repo path, which used to `return {}` — a payload with
    no `nodes` at all, i.e. the exact shape the template cannot survive
    unguarded."""
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.services.thesis_book import (
        REQUIRED_THESIS_KEYS, build_thesis_data)

    out = build_thesis_data("does-not-exist")
    for key in REQUIRED_THESIS_KEYS:
        assert key in out


# ══════════════════════════════════════════════════════════════════════════
# 2. The template — static totality checks (no browser needed)
# ══════════════════════════════════════════════════════════════════════════

#: `x.toFixed(` where x is itself a member access (`m.exposure_pct_nav`) is the
#: exact bug shape: a DATA-derived field used as a number without a guard.
#: Numbers that have passed through num() are plain locals, so they never match.
_MEMBER_TOFIXED = re.compile(r"\b[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\.toFixed\s*\(")


def _executable_source() -> str:
    """The template minus comments and backticked prose — the comments in
    thesis_book.html quote the historical bug on purpose and must not trip the
    scan that exists because of it."""
    src = TEMPLATE.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)   # block + CSS comments
    src = re.sub(r"//[^\n]*", " ", src)                # line comments
    src = re.sub(r"`[^`]*`", " ", src)                 # code quoted in prose
    return src


def test_template_has_no_toFixed_on_a_data_derived_field():
    offenders = _MEMBER_TOFIXED.findall(_executable_source())
    assert not offenders, (
        "unguarded .toFixed() on a DATA-derived field — this is the outage "
        f"shape. Route it through fmtNum()/fmtPct()/fmtUsd(): {offenders}")


def test_template_exposes_the_nullable_formatter_set():
    """The guards only hold if the helpers exist; a future refactor that drops
    them should fail here rather than in production."""
    src = TEMPLATE.read_text(encoding="utf-8")
    for helper in ("function num(", "function arr(", "function fmtNum(",
                   "function fmtUsd(", "function fmtPct(",
                   "function exposureText(", "function bootStep("):
        assert helper in src, f"missing nullable-render helper: {helper}"


def test_template_isolates_each_render_step():
    """One throw must not blank the whole tab: every boot call goes through
    bootStep(), which logs to console.error (so the smoke test below still
    fails) but keeps the other sections alive."""
    src = TEMPLATE.read_text(encoding="utf-8")
    boot = src[src.index("/* ---------- boot ----------"):]
    for step in ("renderHeader", "renderOverview", "renderGlobalMacro",
                 "renderMacroGrid", "renderThemeGroups", "renderFooter"):
        assert f"bootStep('{step}'" in boot, f"{step} is not isolated"


def test_template_normalizes_iterated_fields_before_use():
    src = TEMPLATE.read_text(encoding="utf-8")
    assert "['nodes','themes','macro','clusters','sectors'].forEach" in src, (
        "the template must coerce every iterated DATA field to an array before "
        "the first dereference")


# ══════════════════════════════════════════════════════════════════════════
# 3. The smoke test — a real browser, the real route.
#    Skips cleanly without playwright, same as
#    test_dept_page_mobile_playwright.py (playwright is deliberately NOT a
#    pinned console dependency).
# ══════════════════════════════════════════════════════════════════════════

# NOTE: importorskip lives in the `browser` fixture, NOT at module level — the
# contract + static checks above must run in CI, which does not install
# playwright.
try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def thesis_server(tmp_path_factory) -> Iterator[str]:
    """Boot the real app with a Live fixture dept whose vault_to_graph output
    is the producer-shaped payload above. TestClient cannot catch this bug
    class at all — the crash is in the browser, not the response."""
    if uvicorn is None:
        pytest.skip("uvicorn not installed")

    root = tmp_path_factory.mktemp("thesis_renderer_fixture")
    _build_fixture_root(root)

    import os
    os.environ["CONSOLE_BEARER_TOKEN"] = TEST_BEARER
    os.environ["READ_FROM_DISK"] = str(root)

    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]

    # Stand in for the producer we do not own, with its real key set.
    from console.services import thesis_book as tb
    payload = _producer_shaped_payload()
    tb._run_vault_to_graph = lambda _root: json.loads(json.dumps(payload))
    tb._cache.clear()

    from console.main import create_app
    app = create_app()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("thesis_server did not start in time")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium not installed for playwright: {exc}")
        yield b
        b.close()


def _load_thesis_tab(browser, base_url):
    """Return (page, js_errors, pane_bytes). `js_errors` collects real script
    failures only — a missing chart PNG is a resource 404, not a render bug."""
    js_errors: list[str] = []
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))

    def _on_console(msg):
        if msg.type == "error" and "Failed to load resource" not in msg.text:
            js_errors.append(f"console.{msg.type}: {msg.text}")

    page.on("console", _on_console)
    page.goto(f"{base_url}/dept/fixture/portfolio?token={TEST_BEARER}")
    page.wait_for_timeout(800)
    pane_bytes = page.evaluate(
        "() => { const e = document.getElementById('pane-thesis');"
        " return e ? e.innerHTML.length : -1; }")
    return context, page, js_errors, pane_bytes


def test_thesis_tab_renders_with_zero_js_errors(browser, thesis_server):
    """(a) of the fence. The 2026-08-12 break showed up here as exactly one
    pageerror: "Cannot read properties of undefined (reading 'toFixed')"."""
    context, _page, js_errors, _bytes = _load_thesis_tab(browser,
                                                         thesis_server)
    try:
        assert not js_errors, (
            "the thesis tab threw — a single throw blanks the WHOLE tab "
            f"(one IIFE): {js_errors}")
    finally:
        context.close()


def test_thesis_tab_is_not_a_blank_shell(browser, thesis_server):
    """(b) of the fence. A throw is not the only way to lose the tab — a
    silently-empty render looks fine to a status check and is useless to
    Joris. Broken measured 8,678 bytes; healthy measured 37,320."""
    context, _page, _errs, pane_bytes = _load_thesis_tab(browser,
                                                         thesis_server)
    try:
        assert pane_bytes > MIN_PANE_BYTES, (
            f"#pane-thesis rendered only {pane_bytes} bytes "
            f"(floor {MIN_PANE_BYTES}) — a render step died silently")
    finally:
        context.close()


def test_thesis_tab_renders_the_macro_and_theme_sections(browser,
                                                          thesis_server):
    """Byte count alone can be gamed by one fat section. Assert the sections
    that actually died in the outage are present and populated."""
    context, page, _errs, _bytes = _load_thesis_tab(browser, thesis_server)
    try:
        macro_cards = page.evaluate(
            "() => document.querySelectorAll('.macro-card').length")
        theme_cards = page.evaluate(
            "() => document.querySelectorAll('.theme-card').length")
        assert macro_cards == 6, f"expected 6 macro cards, got {macro_cards}"
        assert theme_cards >= 39, f"expected >=39 theme cards, got {theme_cards}"
        # The outage line's output, now total: no "undefined", no "NaN".
        exposure_text = page.evaluate(
            "() => Array.from(document.querySelectorAll('.macro-exposure'))"
            ".map(e => e.textContent).join(' | ')")
        assert "undefined" not in exposure_text
        assert "NaN" not in exposure_text
    finally:
        context.close()


#: Payloads that a drifting producer could plausibly hand the template. Each
#: one used to be, or is one rename away from being, a blank tab.
_HOSTILE_PAYLOADS = {
    "empty object": {},
    "null": None,
    "macro + themes with every optional key stripped": {
        "macro": [{"id": "m1"}], "themes": [{"id": "t1"}],
        "nodes": [{"id": "AAA", "held": True}], "sectors": [{"name": "Tech"}],
        "portfolio_overview": {"brokers": [{}], "pending_decisions": [{}],
                               "top_movers": [{}], "hedge_overlay": {}},
        "global_macro": {"key_signals": [{}]},
    },
    # the LATENT twin of the outage: reachable the day held_tickers appears
    "held theme with no exposure_pct_nav": {
        "macro": [{"id": "m1", "title": "M", "themes": ["t1"]}],
        "themes": [{"id": "t1", "name": "T — one", "ticker_count": 3,
                    "tickers": ["AAA", "BBB", "CCC"], "held_tickers": ["AAA"]}],
        "nodes": [{"id": "AAA", "name": "Alpha", "held": True}],
    },
    "wrong types throughout": {
        "nav": "lots", "since_rebase_pct": "up", "node_count": "many",
        "macro": {"not": "a list"}, "themes": "nope", "nodes": 7,
        "sectors": None, "acwi_sector_weights": "x",
        "portfolio_overview": {"nav": "n/a", "brokers": "x"},
        "generated_at": "not-a-date",
    },
}


@pytest.mark.parametrize("label", sorted(_HOSTILE_PAYLOADS))
def test_thesis_tab_survives_a_drifting_producer(browser, thesis_server,
                                                  label):
    """The regression that keeps happening is a producer CHANGE. Swap the
    embedded payload for a degraded one and re-render: it must not throw.

    The swap is done on the served HTML (the `const DATA = (...) || {};`
    literal), so this exercises the SHIPPED template, not a copy."""
    import urllib.request

    req = urllib.request.Request(
        f"{thesis_server}/dept/fixture/portfolio",
        headers={"Authorization": f"Bearer {TEST_BEARER}"})
    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")

    prefix, suffix = "const DATA = (", ") || {};"
    i = html.index(prefix)
    j = html.index(suffix, i)
    payload = json.dumps(_HOSTILE_PAYLOADS[label], separators=(",", ":"))
    html = html[:i + len(prefix)] + payload + html[j:]

    js_errors: list[str] = []
    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: js_errors.append(m.text)
            if m.type == "error" and "Failed to load resource" not in m.text
            else None)
    try:
        page.set_content(html)
        page.wait_for_timeout(400)
        assert not js_errors, (
            f"renderer threw on a degraded producer payload ({label}): "
            f"{js_errors}")
    finally:
        context.close()
