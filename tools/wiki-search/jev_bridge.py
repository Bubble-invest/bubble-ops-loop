#!/usr/bin/env python3
"""Thin wrapper that REUSES the fleet's own `skills/system-one-decisions/
scripts/jev.py` (board #1505's own decision layer) rather than
reimplementing the OpenRouter wire format, auth-header handling, or
decision-receipt fields -- fleet doctrine: verify the standard, don't
duplicate. Ported from the step-1 research's `jev_client.py`; the only
change is resolving `jev.py`'s path RELATIVE TO THIS FILE instead of a
hardcoded `~/claude-workspaces/...` -- both `tools/wiki-search/` and
`skills/system-one-decisions/` hang off the same repo root in the
framework checkout AND in every dept tree this tool gets vendored into
(vendor-dept-libs.sh's KANBAN_MAP ships jev.py to the same relative path),
so the relative resolution is what makes this portable fleet-wide.

Receipts written here match jev.py `ask`'s own row shape
(`references/contract.md` sec 5: state_digest, question_set_version,
backend, model, ok, response/error, latency_s/ms, cost_usd, timestamp) --
raw state is deliberately NOT included, by the same design as jev.py itself.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent  # tools/wiki-search -> tools -> <repo root>
DEFAULT_JEV_PY_PATH = REPO_ROOT / "skills/system-one-decisions/scripts/jev.py"
JEV_PY_PATH = Path(os.environ.get("WIKI_SEARCH_JEV_PY", str(DEFAULT_JEV_PY_PATH)))

_jev_mod = None


def jev():
    global _jev_mod
    if _jev_mod is None:
        spec = importlib.util.spec_from_file_location("jev_module", JEV_PY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _jev_mod = mod
    return _jev_mod


class SpendCap:
    def __init__(self, max_spend=None):
        self.max_spend = max_spend
        self.total = 0.0
        self.stopped = False

    def check(self):
        if self.max_spend is not None and self.total >= self.max_spend:
            self.stopped = True
        return not self.stopped

    def add(self, cost):
        if cost:
            self.total += cost


def ask_openrouter(state, questions, spend_cap: SpendCap, backend="openrouter", timeout=90):
    """One call to the Jev backend (default: openrouter, the recommended
    backend for INTERNAL Bubble data per system-one-decisions/SKILL.md --
    the wiki is internal fleet data). Returns a receipt dict matching
    jev.py `ask`'s row shape."""
    j = jev()
    if not spend_cap.check():
        return {"ok": False, "error": "max-spend reached, call skipped", "cost_usd": 0.0}

    base_url = j.base_url_for(backend, None)
    headers = j.auth_headers_for(backend)
    qsv = j.question_set_version(questions)
    digest = j.state_digest(state)
    # jev.py's own cmd_ask strips meta keys (e.g. "_version") before the wire
    # call -- call_engine()/request_url_and_payload() do NOT strip them
    # themselves, so this must be done here too or a "_version" key gets
    # sent to the API and rejected (400 from /alpha/decisions).
    wire_questions = j.strip_meta_keys(questions)

    result = j.call_engine(backend, base_url, state, wire_questions, headers=headers, timeout=timeout)
    cost = result.get("cost_usd")
    spend_cap.add(cost)

    receipt = {
        "backend": backend,
        "ok": result["ok"],
        "state_digest": digest,
        "question_set_version": qsv,
        "latency_s": result.get("latency_s"),
        "latency_ms": (result.get("latency_s") or 0) * 1000,
        "cost_usd": cost,
        "timestamp": j.utc_now_iso(),
    }
    if result["ok"]:
        resp = result["response"] or {}
        receipt["model"] = resp.get("model")
        receipt["response"] = resp
    else:
        receipt["error"] = result.get("error")
    return receipt


def load_openrouter_key_into_env():
    """The fleet convention (system-one-decisions/SKILL.md) is
    JEV_OPENROUTER_API_KEY / OPENROUTER_API_KEY from the environment only,
    never a flag -- every VPS dept already has JEV_OPENROUTER_API_KEY
    provisioned into its systemd env (the fleet-dedicated "fleet-jev" key,
    board #1505). Rick's Mac (and any other Mac without that systemd
    provisioning) keeps its copy at ~/.config/jev/openrouter.key instead --
    load it into the process env ONLY if neither var is already set. This is
    NOT a new key-resolution mechanism: it populates the exact same env var
    jev.py already reads; jev.py itself is never modified or duplicated."""
    if os.environ.get("JEV_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY"):
        return True
    key_path = Path(os.environ.get("WIKI_SEARCH_JEV_KEY_FILE", str(Path.home() / ".config/jev/openrouter.key")))
    if not key_path.exists():
        return False
    try:
        key = key_path.read_text().strip()
    except OSError:
        return False
    if not key:
        return False
    os.environ["JEV_OPENROUTER_API_KEY"] = key
    return True


def append_receipt(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")
