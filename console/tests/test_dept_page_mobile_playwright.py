"""
test_dept_page_mobile_playwright.py — #642 W19 regression fence.

Joris's live mobile review of /dept/<slug> found two real-browser bugs no
existing test caught (every other console test drives the FastAPI
TestClient, which never lays out CSS or dispatches a real tap):

  1. On mobile (<=768px), tapping ANY sidebar nav link silently did
     nothing — the CSS-only hamburger opened, but `.sidebar-overlay`
     (a `position: fixed; inset: 0` tap-catcher, root-level sibling of
     `.app-shell`) painted OVER the open sidebar and ate every tap.
     Root cause: `body.m4 .app-shell { position: relative; z-index: 1; }`
     (base.html's home-page "atmospheric blobs" rule, but `body.m4`
     is on EVERY page) turns `.app-shell` into its own stacking
     context — so `.sidebar`'s z-index:100 can never out-rank a root-level
     sibling no matter how high it is; the overlay (root z-index:99) then
     beats `.app-shell`'s root z-index:1. Fixed by raising `.app-shell`'s
     z-index above the mobile toggle/overlay (101/99) — see style.css.

  2. The Moment 3 ("L'exécution") card was rendering a raw runtime
     summary.md verbatim — literal `**bold**`, `` `code` ``, and
     unrendered markdown table rows — overflowing the card's border on
     any viewport. Fixed on both sides: github_reader.load_recent_layer_output
     now markdown-strips + caps the excerpt at 140 chars server-side, and
     `.moment` / `.moment-last-run` / `.moment-summary-excerpt` clip
     client-side (overflow:hidden + line-clamp) as a second guarantee.

Requires `playwright` (not a pinned console dependency — install with
`pip install playwright && playwright install chromium` to run this file
locally). Skips cleanly if playwright/its browser isn't available, same
as any other optional-heavy-dependency test in this repo.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None

MOBILE_VIEWPORT = {"width": 390, "height": 844}
TEST_BEARER = "test-token-mobile-playwright"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_fixture_root(root: Path) -> None:
    """Same shape as conftest.py's `fixture_root`, plus outputs/.../summary.md
    with the exact raw-markdown shape Joris's screenshot showed leaking."""
    live = root / "bubble-ops-fixture"
    live.mkdir(parents=True)
    (live / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": "fixture", "level": "ops", "mandate": "MVP fixture"},
            "layers": {"subscribed": [1, 2, 3, 4]},
            "gate_policies": {"echo_action": {
                "current_mode": "manual_required",
                "eligible_future_modes": ["auto_if_policy_passed"],
            }},
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
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "queues" / "gates").mkdir(parents=True)
    (live / "queues" / "gates" / "echo-1.yaml").write_text(
        yaml.safe_dump({
            "id": "echo-1", "kind": "echo_action", "source_layer": 2,
            "target_layer": 3, "risk_level": "low", "requires_human": True,
            "current_mode": "manual_required", "gate_policy_id": "echo_action",
            "actions": ["approve", "reject", "modify", "defer"],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (live / "outputs").mkdir()

    l3_dir = live / "outputs" / "2026-07-16" / "3"
    l3_dir.mkdir(parents=True)
    (l3_dir / ".last-run").write_text("2026-07-16T18:25:00Z", encoding="utf-8")
    (l3_dir / "summary.md").write_text(
        "# L3 run\n"
        "**Trigger:** 5 new `inbox/decisions/` items synced from cockpit "
        "(decided_by: operator, ~18:21-18:25Z). **All 5 = `action: reject`.** "
        "Zero approvals -> **nothing published** (L3 STEP 0: "
        "action!=approve -> archive + stop).\n"
        "No browser/publish/preflight run. | gate | channel | operator comment |\n"
        "| --- | --- | --- |\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def live_server(tmp_path_factory) -> Iterator[str]:
    """Boot the real FastAPI app under uvicorn in a background thread so
    Playwright can drive a real browser against it (TestClient has no
    real socket/CSS layout — that's exactly the class of bug this file
    exists to catch)."""
    if uvicorn is None:
        pytest.skip("uvicorn not installed")

    root = tmp_path_factory.mktemp("mobile_playwright_fixture")
    _build_fixture_root(root)

    import os
    os.environ["CONSOLE_BEARER_TOKEN"] = TEST_BEARER
    os.environ["READ_FROM_DISK"] = str(root)

    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    app = create_app()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
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
        pytest.fail("live_server did not start in time")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium not installed for playwright: {exc}")
        yield b
        b.close()


def _authed_page(browser, live_server, viewport):
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    page.goto(f"{live_server}/?token={TEST_BEARER}")
    return context, page


# ── Issue 2: mobile nav tap must actually navigate ─────────────────────────

def test_mobile_hamburger_nav_link_navigates(browser, live_server):
    """At 390px, opening the hamburger then tapping a sidebar nav link
    (Kanban) must navigate there. Before the fix, `.sidebar-overlay`
    intercepted the tap (document.elementFromPoint at the link's own
    center returned the overlay LABEL, not the link) and the click was a
    silent no-op — this asserts BOTH the element-at-point AND the actual
    resulting navigation, since a element-only check can't tell a tap
    reaches the right target from it merely existing."""
    context, page = _authed_page(browser, live_server, MOBILE_VIEWPORT)
    try:
        page.goto(f"{live_server}/dept/fixture")
        page.wait_for_load_state("networkidle")

        page.locator(".sidebar-toggle-label").click()
        page.wait_for_timeout(300)
        assert page.locator("#sidebar-toggle").is_checked(), (
            "hamburger toggle checkbox must be checked after tapping the label"
        )

        kanban_link = page.locator(".sidebar a[href='/kanban']")
        box = kanban_link.bounding_box()
        assert box is not None, "kanban nav link must be visible when menu is open"
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        el_at_point = page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y); "
            "return el ? el.className : null; }",
            [cx, cy],
        )
        assert "sidebar-overlay" not in (el_at_point or ""), (
            f"the overlay is intercepting taps at the nav link's own coordinates "
            f"(element at point: {el_at_point!r}) — the sidebar is painting "
            f"BELOW the tap-catching overlay"
        )

        kanban_link.click(timeout=3000)
        page.wait_for_load_state("networkidle")
        assert page.url.rstrip("/").endswith("/kanban"), (
            f"tapping the Kanban nav link on mobile did not navigate "
            f"(still on {page.url})"
        )
    finally:
        context.close()


def test_mobile_hamburger_overlay_closes_menu_on_tap(browser, live_server):
    """Tapping the overlay itself (outside the sidebar) must still close
    the menu — the fix must not turn the overlay into a dead click-through
    element, only stop it from shadowing the sidebar's own content."""
    context, page = _authed_page(browser, live_server, MOBILE_VIEWPORT)
    try:
        page.goto(f"{live_server}/dept/fixture")
        page.wait_for_load_state("networkidle")
        page.locator(".sidebar-toggle-label").click()
        page.wait_for_timeout(300)
        assert page.locator("#sidebar-toggle").is_checked()

        # Tap far right, outside the 280px-wide sidebar (390px viewport).
        page.mouse.click(370, 400)
        page.wait_for_timeout(300)
        assert not page.locator("#sidebar-toggle").is_checked(), (
            "tapping outside the open sidebar must close the mobile menu "
            "(the overlay must still be clickable, just not tap-blocking "
            "the sidebar's own nav links)"
        )
    finally:
        context.close()


# ── Issue 1 + 3: no card overflows its boundary on mobile ──────────────────

def test_moment_card_excerpt_is_clean_and_does_not_overflow(browser, live_server):
    """The Moment 3 card must show a markdown-stripped 'dernier passage'
    line (no literal **/`` ` ``/table-pipe syntax) and must not overflow
    its own card boundary at 390px."""
    context, page = _authed_page(browser, live_server, MOBILE_VIEWPORT)
    try:
        page.goto(f"{live_server}/dept/fixture")
        page.wait_for_load_state("networkidle")

        excerpt = page.locator(".moment-summary-excerpt").first
        text = excerpt.text_content() or ""
        for forbidden in ("**", "`", "| ---", "|---"):
            assert forbidden not in text, (
                f"raw markdown syntax {forbidden!r} leaked into the moment "
                f"card excerpt: {text!r}"
            )
        assert len(text) <= 145, (
            f"excerpt exceeds the ~140-char cap ({len(text)} chars): {text!r}"
        )

        overflow = page.evaluate(
            "() => Array.from(document.querySelectorAll('.moment')).map("
            "m => ({sw: m.scrollWidth, cw: m.clientWidth}))"
        )
        for i, m in enumerate(overflow):
            assert m["sw"] <= m["cw"] + 1, (
                f".moment[{i}] overflows its own box at 390px "
                f"(scrollWidth={m['sw']} > clientWidth={m['cw']})"
            )
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 480])
def test_dept_page_no_horizontal_page_overflow(browser, live_server, width):
    """The page itself must not gain a horizontal scrollbar at phone
    widths — the single strongest signal that SOME element is bleeding
    past its container."""
    context, page = _authed_page(browser, live_server, {"width": width, "height": 900})
    try:
        page.goto(f"{live_server}/dept/fixture")
        page.wait_for_load_state("networkidle")
        doc = page.evaluate(
            "() => ({sw: document.documentElement.scrollWidth, "
            "cw: document.documentElement.clientWidth})"
        )
        assert doc["sw"] <= doc["cw"] + 1, (
            f"page has horizontal overflow at {width}px "
            f"(scrollWidth={doc['sw']} > clientWidth={doc['cw']})"
        )
    finally:
        context.close()
