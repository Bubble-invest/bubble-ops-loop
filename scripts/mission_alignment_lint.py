#!/usr/bin/env python3
"""Report mission mappings without gating loops. Findings always exit zero."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib.mission_alignment import alignment_report, default_intents_dir, read_intent_slugs


def scan(depts_root: Path, intents_dir: Path) -> dict:
    manifests, errors = [], []
    if not depts_root.is_dir():
        errors.append("departments root unavailable")
    try:
        paths = sorted(depts_root.rglob("dept.yaml"))
        for path in paths:
            dept = str(path.parent.relative_to(depts_root))
            try:
                manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                manifest = None
            manifests.append((dept, manifest))
    except OSError:
        errors.append("cannot enumerate department manifests")
    report = alignment_report(manifests, read_intent_slugs(intents_dir))
    report["errors"] = errors
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depts-root", type=Path, required=True)
    parser.add_argument("--intents-dir", type=Path, default=default_intents_dir())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = scan(args.depts_root, args.intents_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if not report["catalog_available"]:
            print("Intent catalog unavailable; intent references are unverified.")
        for error in report["errors"]:
            print(f"ERROR: {error}")
        for dept in report["departments"]:
            for error in dept["errors"]:
                print(f'{dept["dept"]}: ERROR: {error}')
            for mission in dept["missions"]:
                print(f'{dept["dept"]}/{mission["mission"]}: '
                      + ", ".join(mission["statuses"])
                      + (" (" + ", ".join(mission["unknown_intents"]) + ")" if mission["unknown_intents"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
