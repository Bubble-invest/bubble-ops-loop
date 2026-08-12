"""
test_thesis_sparklines.py — fence for the empty-sparkline gap on Thesis Book
(tab 1) of the Living Portfolio Report.

THE GAP
-------
console/templates/thesis_book.html draws each ticker's sparkline client-side
from the numeric array `node.sparkline_6m` (buildSparkline draws a dotted
placeholder when the array has <2 points). But the live producer
(bubble-ops-ben/tools/vault_to_graph.py) never emits `sparkline_6m` — its node
keys are id/name/sector/quality/tag/themes/thesis/catalyst/return_1y/return_3y/
roe/fwd_pe/held/weight_pct/broker/market_value/cluster/price/moat. So every
sparkline rendered empty.

THE FIX
-------
The price series already exist on disk: the nightly artifact
outputs/<date>/portfolio-review-data.json carries `prices` (yahoo_symbol ->
[[date, price], …]) and `model.lines` (holding identity -> yahoo symbol).
build_thesis_data() now reads that file and attaches `node.sparkline_6m` by
joining node.id -> model.lines -> yahoo -> prices[yahoo].

These tests use a SYNTHETIC data.json with the real SHAPE (this repo is public).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import json


def _review_payload() -> dict:
    """A portfolio-review-data.json shaped like the nightly artifact: `prices`
    keyed by yahoo symbol, and `model.lines` mapping holding identity -> yahoo.
    LIN and ROBO have series; NOMATCH has no line and no price series."""
    return {
        "prices": {
            "LIN": [["2026-02-12", 321.86], ["2026-03-12", 330.10],
                    ["2026-04-12", 340.55], ["2026-05-12", 335.20]],
            "ROBO": [["2026-02-12", 55.10], ["2026-03-12", 57.30],
                     ["2026-04-12", 54.90]],
            # A yahoo symbol that differs from the node.id, reachable only via
            # the model.lines join (node.id "SMH" -> line.yahoo "SMH.US").
            "SMH.US": [["2026-02-12", 210.0], ["2026-03-12", 215.0],
                       ["2026-04-12", 220.0]],
        },
        "model": {
            "lines": [
                {"key": "lin", "label": "Linde plc", "ticker_display": "LIN",
                 "yahoo": "LIN", "broker": "ibkr", "sleeve": "core",
                 "mv_usd": 12000.0},
                {"key": "robo", "label": "Robotics ETF", "ticker_display": "ROBO",
                 "yahoo": "ROBO", "broker": "ibkr", "sleeve": "thematic",
                 "mv_usd": 8000.0},
                {"key": "smh", "label": "Semis ETF", "ticker_display": "SMH",
                 "yahoo": "SMH.US", "broker": "ibkr", "sleeve": "thematic",
                 "mv_usd": 9000.0},
            ],
        },
    }


def _nodes() -> list:
    return [
        {"id": "LIN", "name": "Linde", "held": True},       # -> prices["LIN"]
        {"id": "ROBO", "name": "Robotics", "held": True},   # -> prices["ROBO"]
        {"id": "SMH", "name": "Semis", "held": True},       # -> line.yahoo SMH.US
        {"id": "NOMATCH", "name": "Watchlist name", "held": False},  # no series
    ]


# ── unit: the join itself ────────────────────────────────────────────────────

def test_attach_sparklines_joins_via_model_lines_and_direct():
    from console.services.thesis_book import _attach_sparklines

    nodes = _nodes()
    _attach_sparklines(nodes, _review_payload(), "fixture")
    by_id = {n["id"]: n for n in nodes}

    # Direct node.id == yahoo key.
    assert by_id["LIN"]["sparkline_6m"] == [321.86, 330.10, 340.55, 335.20]
    assert by_id["ROBO"]["sparkline_6m"] == [55.10, 57.30, 54.90]

    # node.id -> model.lines.yahoo ("SMH" -> "SMH.US") -> prices.
    assert by_id["SMH"]["sparkline_6m"] == [210.0, 215.0, 220.0]
    assert len(by_id["SMH"]["sparkline_6m"]) == 3

    # No line, no price series -> keeps the placeholder (no key added).
    assert "sparkline_6m" not in by_id["NOMATCH"]


def test_attach_sparklines_series_is_chronological_floats():
    from console.services.thesis_book import _attach_sparklines

    # Deliberately out-of-order dates; the loader must sort to chronological.
    review = {
        "prices": {"AAA": [["2026-04-01", 3.0], ["2026-02-01", 1.0],
                           ["2026-03-01", 2.0]]},
        "model": {"lines": [{"ticker_display": "AAA", "yahoo": "AAA"}]},
    }
    nodes = [{"id": "AAA"}]
    _attach_sparklines(nodes, review, "fixture")
    assert nodes[0]["sparkline_6m"] == [1.0, 2.0, 3.0]
    assert all(isinstance(x, float) for x in nodes[0]["sparkline_6m"])


def test_attach_sparklines_no_prices_is_a_noop():
    from console.services.thesis_book import _attach_sparklines

    nodes = _nodes()
    _attach_sparklines(nodes, {}, "fixture")            # empty review
    _attach_sparklines(nodes, {"model": {"lines": []}}, "fixture")  # no prices
    assert all("sparkline_6m" not in n for n in nodes)


def test_single_point_series_stays_a_placeholder():
    """buildSparkline needs >=2 points; a 1-point series must NOT be attached
    (it would draw nothing yet look 'present')."""
    from console.services.thesis_book import _attach_sparklines

    review = {"prices": {"ONE": [["2026-02-01", 5.0]]},
              "model": {"lines": [{"ticker_display": "ONE", "yahoo": "ONE"}]}}
    nodes = [{"id": "ONE"}]
    _attach_sparklines(nodes, review, "fixture")
    assert "sparkline_6m" not in nodes[0]


# ── loader: latest outputs/<date>/portfolio-review-data.json ─────────────────

def test_load_latest_review_data_reads_todays_file(tmp_path: Path):
    from console.services.thesis_book import _load_latest_review_data

    day = tmp_path / "outputs" / date.today().isoformat()
    day.mkdir(parents=True)
    payload = _review_payload()
    (day / "portfolio-review-data.json").write_text(json.dumps(payload))

    loaded = _load_latest_review_data(tmp_path)
    assert loaded.get("prices", {}).get("LIN")
    assert loaded["model"]["lines"][0]["yahoo"] == "LIN"


def test_load_latest_review_data_missing_is_empty(tmp_path: Path):
    from console.services.thesis_book import _load_latest_review_data

    assert _load_latest_review_data(tmp_path) == {}


# ── full-universe: non-held nodes from watchlist-prices.json ─────────────────
# portfolio-review-data.json only prices the ~84 HELD names, so only held nodes
# got a sparkline. bubble-ops-ben's watchlist_momentum.py now also persists
# outputs/<date>/watchlist-prices.json ({yahoo: [[date, close]]}) for the ~500
# NON-HELD watchlist universe; _attach_sparklines merges both (held wins).

def _watchlist_prices() -> dict:
    """A watchlist-prices.json payload: the SAME shape as review `prices`, but
    for non-held names. WATCH1 is only here (no model.lines, not in review);
    LIN is ALSO here with a DIFFERENT (stale) series to prove held precedence."""
    return {
        "WATCH1": [["2026-02-12", 10.0], ["2026-03-12", 11.0],
                   ["2026-04-12", 12.5]],
        # Same ticker as a held name, but a divergent series — held must win.
        "LIN": [["2026-02-12", 1.0], ["2026-03-12", 2.0]],
    }


def test_non_held_node_gets_sparkline_from_watchlist_prices():
    """A non-held node whose ticker is ONLY in watchlist-prices.json (no
    model.lines entry, absent from review prices) now gets a sparkline via the
    direct node.id -> prices fallback."""
    from console.services.thesis_book import _attach_sparklines

    nodes = [
        {"id": "WATCH1", "name": "Watchlist only", "held": False},
        {"id": "NOMATCH", "name": "Truly unpriced", "held": False},
    ]
    _attach_sparklines(nodes, _review_payload(), "fixture",
                       watchlist_prices=_watchlist_prices())
    by_id = {n["id"]: n for n in nodes}

    # Non-held, watchlist-only -> attached from watchlist-prices.json.
    assert by_id["WATCH1"]["sparkline_6m"] == [10.0, 11.0, 12.5]
    # Still no series anywhere -> keeps the placeholder.
    assert "sparkline_6m" not in by_id["NOMATCH"]


def test_held_prices_take_precedence_when_ticker_in_both():
    """When a ticker is in BOTH sources, the held (portfolio-review-data.json)
    series wins — the held marks are the book's own."""
    from console.services.thesis_book import _attach_sparklines

    nodes = [{"id": "LIN", "name": "Linde", "held": True}]
    _attach_sparklines(nodes, _review_payload(), "fixture",
                       watchlist_prices=_watchlist_prices())
    # review LIN series (4 pts), NOT the watchlist LIN series (2 pts).
    assert nodes[0]["sparkline_6m"] == [321.86, 330.10, 340.55, 335.20]


def test_watchlist_only_source_still_attaches_when_no_review():
    """Even with an empty review payload, a watchlist-prices-only series still
    reaches non-held nodes (the full-universe path must not depend on the held
    artifact existing)."""
    from console.services.thesis_book import _attach_sparklines

    nodes = [{"id": "WATCH1", "held": False}]
    _attach_sparklines(nodes, {}, "fixture",
                       watchlist_prices=_watchlist_prices())
    assert nodes[0]["sparkline_6m"] == [10.0, 11.0, 12.5]


def test_attach_sparklines_no_sources_is_a_noop():
    from console.services.thesis_book import _attach_sparklines

    nodes = _nodes()
    _attach_sparklines(nodes, {}, "fixture", watchlist_prices={})
    _attach_sparklines(nodes, {}, "fixture", watchlist_prices=None)
    assert all("sparkline_6m" not in n for n in nodes)


# ── loader: latest outputs/<date>/watchlist-prices.json ──────────────────────

def test_load_latest_watchlist_prices_reads_todays_file(tmp_path: Path):
    from console.services.thesis_book import _load_latest_watchlist_prices

    day = tmp_path / "outputs" / date.today().isoformat()
    day.mkdir(parents=True)
    (day / "watchlist-prices.json").write_text(json.dumps(_watchlist_prices()))

    loaded = _load_latest_watchlist_prices(tmp_path)
    assert loaded.get("WATCH1") == [["2026-02-12", 10.0], ["2026-03-12", 11.0],
                                    ["2026-04-12", 12.5]]


def test_load_latest_watchlist_prices_missing_is_empty(tmp_path: Path):
    from console.services.thesis_book import _load_latest_watchlist_prices

    assert _load_latest_watchlist_prices(tmp_path) == {}
