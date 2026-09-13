#!/usr/bin/env python3
"""telegram-gap-detector.py — make a SILENT Telegram inbound loss LOUD
(board #1284 pt E). Discovery + side effects; the decision logic is the pure,
unit-tested scripts/lib/telegram_gap_detector.py.

WHAT IT WATCHES
  For each dept's telegram state dir it reads <state>/delivery-ledger.jsonl (the
  append-only per-update_id record written by the delivery-ledger plugin patch,
  deploy/telegram-plugin/delivery-ledger.block.ts) and:

    * GAP    — a numeric jump in the update_id stream = updates Telegram acked
               but that never reached the session (the crash-loss fingerprint —
               the exact 2026-09-10→11 Claudette loss of ids 7527–7545).
    * DEAD   — bot.pid is not alive (poller down; inbound not received).
    * WEDGE  — (optional probe) Telegram reports pending updates the alive poller
               is not consuming across >=2 ticks (getWebhookInfo — read-only,
               does NOT consume updates). Off unless a pending-probe hook is set.

  On ANY of those it SIGNALS LOUDLY out-of-band (operator Telegram ping via the
  MAIN bot, so the alert lands even when the dept's own poller is dead) and,
  when enabled, kicks the poller to recover liveness — then persists state so the
  same gap/wedge is not re-alerted every tick.

WHY THIS AND NOT THE EXISTING WATCHDOGS
  telegram-kick-watchdog (Mac) / telegram-watchdog-<dept> + loop-tick-watchdog
  (VPS) only check LIVENESS ("is the poller up / is the tick progressing?").
  None of them checks COMPLETENESS ("did every update Telegram accepted reach the
  session?"). The 19 lost Claudette messages were invisible to all of them. This
  detector adds the missing completeness signal, reusing the same kick/notify
  side-effect shape.

EVERY side effect is behind an overridable hook so this runs hermetically under
test / dry-run:
  BUBBLE_GAP_DISCOVER_CMD   prints one JSON dept spec per line: {"slug","state_dir"}
  BUBBLE_GAP_ALIVE_CMD      <slug> <state_dir> → exit 0 iff bot.pid is alive
  BUBBLE_GAP_PENDING_CMD    <slug> <state_dir> → prints pending_update_count (int)
                            or nothing/non-int to skip the wedge probe this tick
  BUBBLE_GAP_NOTIFY_CMD     <slug> <chat_id> <text> → sends the out-of-band alert
  BUBBLE_GAP_RESTART_CMD    <slug> <state_dir> → recovers the poller
  BUBBLE_GAP_KANBAN_CMD     <slug> <text> → emits a kanban card (best-effort)
  BUBBLE_GAP_STATE_DIR      where per-dept detector state lives (default <repo>/state)
  BUBBLE_GAP_CHAT_ID        alert recipient (default BUBBLE_OPERATOR_CHAT_ID or Joris)
  BUBBLE_GAP_RESTART        1 to enable recovery restarts (default 0 — alert-only)
  BUBBLE_GAP_DRY_RUN        1 to decide + log only; never notify/restart/persist
  BUBBLE_GAP_LEDGER_MAX     rotate the ledger to its last N lines after reading
                            (default 20000; 0 disables rotation)

RAILS: never prints secret values (bot tokens). The notify hook receives text +
chat_id only; the default notifier reads the token from env and never echoes it.

Exit code: 0 for a completed pass (a watchdog must not trip its own timer into
`failed`); 2 on a structural error (bad args / unusable state).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO_ROOT)

from scripts.lib.telegram_gap_detector import (  # noqa: E402
    Decision,
    LedgerEntry,
    State,
    decide,
)

DEFAULT_CHAT_ID = "6532205130"  # Joris — same literal fallback as loop-backup.sh


def log(msg: str) -> None:
    sys.stderr.write(f"[telegram-gap-detector] {msg}\n")


def _run_hook(env_var: str, args: List[str]) -> Optional[str]:
    """Run a hook command (space-split + args appended). None if unset/failed."""
    cmd = os.environ.get(env_var)
    if not cmd:
        return None
    try:
        out = subprocess.run(
            shlex.split(cmd) + args,
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        log(f"{env_var} raised: {e}")
        return None
    if out.returncode != 0:
        log(f"{env_var} exit={out.returncode}: {out.stderr.strip()[:200]}")
        return None
    return out.stdout


def _hook_ok(env_var: str, args: List[str]) -> Optional[bool]:
    """Run a hook only for its exit status. None if unset."""
    cmd = os.environ.get(env_var)
    if not cmd:
        return None
    try:
        out = subprocess.run(shlex.split(cmd) + args, capture_output=True, text=True, timeout=30)
        return out.returncode == 0
    except Exception as e:  # noqa: BLE001
        log(f"{env_var} raised: {e}")
        return None


# ── discovery ────────────────────────────────────────────────────────────────

def discover(host: str, only: Optional[str]) -> List[dict]:
    hook = _run_hook("BUBBLE_GAP_DISCOVER_CMD", [])
    specs: List[dict] = []
    if hook is not None:
        for line in hook.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("slug") and d.get("state_dir"):
                    specs.append({"slug": d["slug"], "state_dir": d["state_dir"]})
            except ValueError:
                log(f"discover: bad spec line skipped: {line[:80]}")
    else:
        specs = _default_discover(host)
    if only:
        specs = [s for s in specs if s["slug"] == only]
    return specs


def _default_discover(host: str) -> List[dict]:
    """Best-effort default discovery from telegram state-dir layout.
    VPS: /home/agent-<slug>/.claude/channels/telegram-<slug>/
    Mac: ~/.claude/channels/telegram*/   (channel name → slug)
    A state dir is a candidate only if it already holds a delivery-ledger or a
    bot.pid (i.e. an active poller), so we never invent depts.
    """
    import glob

    specs: List[dict] = []
    if host == "vps":
        pattern = "/home/agent-*/.claude/channels/telegram-*/"
    else:
        pattern = os.path.expanduser("~/.claude/channels/telegram*/")
    for d in sorted(glob.glob(pattern)):
        d = d.rstrip("/")
        base = os.path.basename(d)  # telegram-<slug> or telegram
        slug = base[len("telegram-"):] if base.startswith("telegram-") else "main"
        if os.path.exists(os.path.join(d, "delivery-ledger.jsonl")) or os.path.exists(
            os.path.join(d, "bot.pid")
        ):
            specs.append({"slug": slug, "state_dir": d})
    return specs


# ── per-dept probes (with built-in defaults) ─────────────────────────────────

def read_ledger(state_dir: str) -> List[LedgerEntry]:
    path = os.path.join(state_dir, "delivery-ledger.jsonl")
    entries: List[LedgerEntry] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                e = LedgerEntry.from_line(line)
                if e is not None:
                    entries.append(e)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"ledger read failed for {state_dir}: {e}")
    return entries


def bot_alive(slug: str, state_dir: str) -> bool:
    hook = _hook_ok("BUBBLE_GAP_ALIVE_CMD", [slug, state_dir])
    if hook is not None:
        return hook
    pid_file = os.path.join(state_dir, "bot.pid")
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # alive, just owned by another uid
    except OSError:
        return False  # ProcessLookupError (dead) or anything else


def pending_count(slug: str, state_dir: str) -> Optional[int]:
    out = _run_hook("BUBBLE_GAP_PENDING_CMD", [slug, state_dir])
    if out is None:
        return None
    out = out.strip()
    try:
        return int(out)
    except ValueError:
        return None


# ── state persistence ────────────────────────────────────────────────────────

def state_path(slug: str) -> str:
    base = os.environ.get("BUBBLE_GAP_STATE_DIR", os.path.join(REPO_ROOT, "state"))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"telegram-gap-{slug}.json")


def load_state(slug: str) -> State:
    try:
        with open(state_path(slug)) as f:
            return State.load(json.load(f))
    except (FileNotFoundError, ValueError):
        return State()


def save_state(slug: str, state: State) -> None:
    p = state_path(slug)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state.dump(), f)
    os.replace(tmp, p)


def rotate_ledger(state_dir: str) -> None:
    try:
        keep = int(os.environ.get("BUBBLE_GAP_LEDGER_MAX", "20000"))
    except ValueError:
        keep = 20000
    if keep <= 0:
        return
    path = os.path.join(state_dir, "delivery-ledger.jsonl")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= keep:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[-keep:])
        os.replace(tmp, path)
        log(f"rotated ledger {state_dir} → last {keep} lines")
    except OSError as e:
        log(f"ledger rotate failed for {state_dir}: {e}")


# ── loud, out-of-band alert + recovery ───────────────────────────────────────

def notify(slug: str, chat_id: str, text: str, dry: bool) -> None:
    if dry:
        log(f"DRY notify → {chat_id}: {text.splitlines()[0]}")
        return
    if _run_hook("BUBBLE_GAP_NOTIFY_CMD", [slug, chat_id, text]) is not None:
        return
    _default_notify(chat_id, text)


def _default_notify(chat_id: str, text: str) -> None:
    """Out-of-band alert via the MAIN operator bot. Reads the token from env;
    NEVER prints it. Best-effort — a failed alert is logged, not fatal."""
    token = os.environ.get("BUBBLE_MAIN_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log("no operator bot token in env (BUBBLE_MAIN_BOT_TOKEN/TELEGRAM_BOT_TOKEN) — cannot send alert")
        return
    try:
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                log(f"alert sendMessage HTTP {resp.status}")
    except Exception as e:  # noqa: BLE001
        log(f"alert send failed: {e}")


def emit_kanban(slug: str, text: str, dry: bool) -> None:
    if dry:
        log(f"DRY kanban card for {slug}")
        return
    _run_hook("BUBBLE_GAP_KANBAN_CMD", [slug, text])  # best-effort, may be unset


def recover(slug: str, state_dir: str, dry: bool) -> None:
    if os.environ.get("BUBBLE_GAP_RESTART", "0") != "1":
        log(f"recovery disabled (BUBBLE_GAP_RESTART!=1) — alert-only for {slug}")
        return
    if dry:
        log(f"DRY recover {slug}")
        return
    if _hook_ok("BUBBLE_GAP_RESTART_CMD", [slug, state_dir]) is None:
        log(f"no BUBBLE_GAP_RESTART_CMD set — cannot auto-recover {slug} (alert only)")


# ── main ─────────────────────────────────────────────────────────────────────

def run_one(spec: dict, chat_id: str, dry: bool) -> Decision:
    slug, state_dir = spec["slug"], spec["state_dir"]
    entries = read_ledger(state_dir)
    alive = bot_alive(slug, state_dir)
    pend = pending_count(slug, state_dir)
    st = load_state(slug)
    d = decide(slug, entries, st, bot_alive=alive, pending_count=pend)

    if d.alert:
        text = d.alert_text()
        log(f"ALERT {slug}: gaps={len(d.new_gaps)} missing={d.total_missing} "
            f"dead={d.dead} wedged={d.wedged}")
        notify(slug, chat_id, text, dry)
        emit_kanban(slug, text, dry)
        if d.dead or d.wedged:
            recover(slug, state_dir, dry)
    else:
        log(f"ok {slug}: {len(entries)} updates, max_id={d.max_update_id}, alive={alive}")

    if not dry:
        save_state(slug, d.state)
        rotate_ledger(state_dir)
    return d


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Telegram delivery-gap detector (#1284 pt E)")
    ap.add_argument("--host", choices=["vps", "local", "auto"], default="auto")
    ap.add_argument("--dept", help="restrict to one slug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="print machine-readable verdicts")
    args = ap.parse_args(argv)

    host = args.host
    if host == "auto":
        host = "vps" if platform.system() == "Linux" and os.path.isdir("/home/agent-claudette") else "local"

    dry = args.dry_run or os.environ.get("BUBBLE_GAP_DRY_RUN") == "1"
    chat_id = os.environ.get("BUBBLE_GAP_CHAT_ID") or os.environ.get("BUBBLE_OPERATOR_CHAT_ID") or DEFAULT_CHAT_ID

    specs = discover(host, args.dept)
    if not specs:
        log(f"no telegram state dirs discovered (host={host}) — nothing to do")
        return 0

    verdicts = []
    for spec in specs:
        try:
            d = run_one(spec, chat_id, dry)
            verdicts.append({
                "slug": d.slug, "alert": d.alert, "new_gaps": len(d.new_gaps),
                "missing": d.total_missing, "dead": d.dead, "wedged": d.wedged,
                "max_update_id": d.max_update_id,
            })
        except Exception as e:  # noqa: BLE001 — one dept must not sink the pass
            log(f"dept {spec.get('slug')} raised: {e}")

    if args.json:
        print(json.dumps(verdicts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
