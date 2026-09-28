#!/usr/bin/env python3
"""
echo-tool — deterministic stub tool for bubble-ops-fixture.

Per Notion v4 §"Skill vs tool":
  Tool = fonction déterministe ... récupère, calcule, normalise. Ne raisonne pas.

This tool takes a JSON `{"message": "..."}` on stdin and returns a JSON
object with the echoed message, a UTC timestamp, and the sorted list of
input keys. Zero side-effects, fully testable in isolation.

Input/output shape matches schema.json (sibling file).

Usage:
    echo '{"message":"hello fixture"}' | python3 tool.py
"""
from __future__ import annotations

import datetime
import json
import sys


def echo(input_data: dict) -> dict:
    """Echo input back with a UTC timestamp + sorted input keys.

    Deterministic w.r.t. inputs given a fixed clock. No I/O, no globals.
    """
    return {
        "echoed": input_data.get("message", ""),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input_keys": sorted(input_data.keys()),
    }


if __name__ == "__main__":
    data = json.load(sys.stdin)
    json.dump(echo(data), sys.stdout)
