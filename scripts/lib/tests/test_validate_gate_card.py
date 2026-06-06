"""Tests for validate_gate_card (Rick 2026-06-06).

Guards the recurring bug where an unquoted colon in a gate-card scalar made the
whole YAML invalid, so the cockpit silently dropped the card and the human never
saw the gate (TLT/ROBO/SMH/URA, 2026-06-06).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.lib.dispatch_helpers import validate_gate_card


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_gate_card_passes(tmp_path):
    p = _w(tmp_path, "g.yaml", 'id: g1\nkind: trade_proposal\ninstrument: "iShares (NASDAQ: TLT)"\n')
    ok, msg = validate_gate_card(p)
    assert ok, msg


def test_unquoted_colon_fails_with_location(tmp_path):
    p = _w(tmp_path, "g.yaml", 'id: g1\nkind: trade_proposal\ninstrument: iShares (NASDAQ: TLT)\n')
    ok, msg = validate_gate_card(p)
    assert not ok
    assert "line 3" in msg
    assert "colon" in msg.lower()


def test_non_mapping_fails(tmp_path):
    p = _w(tmp_path, "g.yaml", "- just\n- a\n- list\n")
    ok, msg = validate_gate_card(p)
    assert not ok
    assert "mapping" in msg.lower()


def test_missing_required_keys_fails(tmp_path):
    p = _w(tmp_path, "g.yaml", "foo: bar\n")
    ok, msg = validate_gate_card(p)
    assert not ok
    assert "id" in msg and "kind" in msg


def test_missing_file_fails(tmp_path):
    ok, msg = validate_gate_card(tmp_path / "nope.yaml")
    assert not ok
    assert "not found" in msg
