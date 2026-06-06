#!/usr/bin/env python3
"""notify_layer.py — fire the per-layer "work done" Telegram ping.

Thin CLI around scripts/lib/loop_notify.py so CLAUDE.md STEP F stays simple.
Reads TELEGRAM_BOT_TOKEN from env, chat_id from config.yaml (accounts.Joris).
Never crashes the loop: a missing token/chat_id prints a warning + exits 0.

Usage:
  # immediate single-layer ping (L1 / L4):
  python3 tools/notify_layer.py fired --layer 4 --summary outputs/2026-06-06/4/risk-brief.md
  # batched L2/L3 counts:
  python3 tools/notify_layer.py batched --counts 2=3,3=1
"""
import argparse, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

def _cfg():
    import yaml
    p = ROOT / "config.yaml"
    return yaml.safe_load(open(p)) if p.exists() else {}

def _dept():
    # dept slug = repo dir name minus the bubble-ops- prefix
    name = ROOT.name
    return name[len("bubble-ops-"):] if name.startswith("bubble-ops-") else name

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fired"); f.add_argument("--layer", required=True)
    f.add_argument("--summary", default=None)
    b = sub.add_parser("batched"); b.add_argument("--counts", required=True,
        help="comma list like 2=3,3=1")
    a = ap.parse_args()
    try:
        from loop_notify import notify_layer_fired, notify_layers_batched
    except Exception as e:
        print(f"[notify_layer] lib import failed (non-fatal): {e}"); return 0
    cfg = _cfg(); dept = _dept()
    try:
        if a.cmd == "fired":
            r = notify_layer_fired(dept, a.layer, a.summary, config=cfg)
        else:
            counts = {}
            for tok in a.counts.split(","):
                if "=" in tok:
                    k, v = tok.split("=", 1); counts[k.strip()] = int(v)
            r = notify_layers_batched(dept, counts, config=cfg)
        ok = getattr(r, "success", r)
        if ok is False:
            print(f"[notify_layer] send failed (non-fatal): {getattr(r,'error',None)}")
        else:
            print(f"[notify_layer] sent ({a.cmd}).")
    except Exception as e:
        print(f"[notify_layer] error (non-fatal): {e}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
