#!/usr/bin/env python3
"""notify_layer.py — fire the per-layer "work done" Telegram ping.

Thin CLI around scripts/lib/loop_notify.py so CLAUDE.md STEP F stays simple.
Reads TELEGRAM_BOT_TOKEN from env, chat_id from config.yaml (accounts.Operator).
Never crashes the loop: a missing token/chat_id prints a warning + exits 0.

Usage:
  # immediate single-layer ping (L1 / L4):
  python3 tools/notify_layer.py fired --layer 4 --summary outputs/2026-06-06/4/risk-brief.md
  # batched L2/L3 counts:
  python3 tools/notify_layer.py batched --counts 2=3,3=1

Board #521 (fleet-wide L1/L4 brief delivery) — the ARTIFACT GATE below used to
drop a "L<N> fired" send entirely whenever ``--summary`` didn't point at a
real, non-empty file — even when the layer's REAL BRIEF (e.g.
outputs/<date>/1/morning_brief.md, configured via config.yaml
``brief_artifacts``) had been written right next to it. That's why Joris saw
an intermittent, silent drop instead of a brief (observed 4x in Tony's
transcripts vs 110x sent — the summary.md just wasn't always written, even
though the brief was). The gate now ALSO checks for the dept's configured
brief artifact (mirrors ``loop_notify._resolve_brief_path``'s resolution) and
only refuses to send when NEITHER the summary NOR the brief exists — so a
brief-only tick (or vice versa) no longer gets silently swallowed.
"""
import argparse
import os
import sys
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


def _has_real_artifact(path) -> bool:
    """True iff ``path`` is a non-empty file. Defensive — never raises."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:  # noqa: BLE001
        return False


def _brief_artifact_present(summary_path, layer, cfg) -> bool:
    """Board #521 fix: does the dept's CONFIGURED brief artifact for
    ``layer`` exist (non-empty) alongside ``summary_path``? Mirrors
    ``loop_notify._resolve_brief_path`` but kept import-light here (no hard
    dependency on loop_notify's internals — this module already imports it
    lazily inside ``main()``). Returns False on any missing config / path /
    error (fail-closed for the gate check itself, NOT for the overall send —
    the caller still falls back to the summary-only gate).
    """
    if not summary_path:
        return False
    try:
        from loop_notify import _configured_brief_filename  # local import, see main()
    except Exception:  # noqa: BLE001
        return False
    layer_str = str(layer).lstrip("L").lstrip("l")
    filename = _configured_brief_filename(cfg or {}, layer_str)
    if not filename:
        return False
    try:
        candidate = Path(summary_path).parent / filename
        return _has_real_artifact(candidate)
    except Exception:  # noqa: BLE001
        return False


def _default_notify_log_path() -> str:
    """Where the observability log lives when the caller doesn't override it
    — alongside the dept's outputs, so it travels with the rest of the
    per-tick evidence. Board #521 cause 4."""
    env_override = os.environ.get("BUBBLE_NOTIFY_LOG_PATH")
    if env_override:
        return env_override
    return str(ROOT / "outputs" / "notify.log")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fired")
    f.add_argument("--layer", required=True)
    f.add_argument("--summary", default=None)
    f.add_argument("--test", action="store_true",
        help="mark as a verification ping (🧪 TEST prefix); bypasses the artifact gate")
    b = sub.add_parser("batched")
    b.add_argument("--counts", required=True,
        help="comma list like 2=3,3=1")
    a = ap.parse_args()
    try:
        from loop_notify import notify_layer_fired, notify_layers_batched
    except Exception as e:
        print(f"[notify_layer] lib import failed (non-fatal): {e}"); return 0
    cfg = _cfg(); dept = _dept()
    notify_log_path = _default_notify_log_path()
    try:
        if a.cmd == "fired":
            # ARTIFACT GATE (board #521 fix) — send "L<N> fired" when EITHER
            # the layer wrote a real, non-empty summary.md OR its configured
            # brief artifact (config.yaml brief_artifacts) exists. Previously
            # this gated on summary.md ALONE, so a tick that wrote the real
            # brief but not a separate summary.md was silently dropped (the
            # observed intermittent stub/no-send behavior). A test ping must
            # still pass --test (prefixes 🧪 TEST, bypasses the gate).
            has_summary = _has_real_artifact(a.summary)
            has_brief = _brief_artifact_present(a.summary, a.layer, cfg)
            has_artifact = has_summary or has_brief
            if not has_artifact and not a.test:
                print(f"[notify_layer] NOT sending L{a.layer} fired — no real summary "
                      f"artifact AND no configured brief artifact found "
                      f"at {a.summary!r} (use --test for a verification ping).")
                from loop_notify import log_notify_event
                log_notify_event(
                    {
                        "dept": dept,
                        "layer": str(a.layer),
                        "kind": "layer_fired",
                        "success": False,
                        "error": "artifact gate: no summary and no brief artifact found",
                        "summary_path": a.summary,
                    },
                    notify_log_path=notify_log_path,
                )
                return 0
            summary_arg = a.summary if (has_summary or has_brief) else None
            r = notify_layer_fired(
                dept, a.layer, summary_arg, config=cfg, test=a.test,
                notify_log_path=notify_log_path,
            )
        else:
            counts = {}
            for tok in a.counts.split(","):
                if "=" in tok:
                    k, v = tok.split("=", 1); counts[k.strip()] = int(v)
            r = notify_layers_batched(dept, counts, config=cfg, notify_log_path=notify_log_path)
        ok = getattr(r, "success", r)
        if ok is False:
            print(f"[notify_layer] send failed (non-fatal): {getattr(r,'error',None)}")
        else:
            print(f"[notify_layer] sent ({a.cmd}).")
    except Exception as e:
        print(f"[notify_layer] error (non-fatal): {e}")
        try:
            from loop_notify import log_notify_event
            log_notify_event(
                {
                    "dept": dept,
                    "kind": a.cmd,
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                },
                notify_log_path=notify_log_path,
            )
        except Exception:  # noqa: BLE001 - logging-the-error must never itself raise
            pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
