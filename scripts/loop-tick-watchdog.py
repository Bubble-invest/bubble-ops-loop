#!/usr/bin/env python3
"""loop-tick-watchdog.py — ONE iterating watchdog that re-kicks any dept loop
whose tick died mid-flight (board #724). Host-aware: VPS (systemd) and Mac
(launchd) depts are discovered at runtime — a new dept adds ZERO config.

Decision logic lives in scripts/lib/loop_tick_watchdog.py (pure, tested).
This file only does discovery + side effects, every one of which is behind an
overridable hook so the bash harness can run it hermetically:

  BUBBLE_TICKWD_DISCOVER_CMD   prints one JSON dept spec per line (see DeptSpec)
  BUBBLE_TICKWD_ALIVE_CMD      <slug> → exit 0 iff the live session/poller is up
  BUBBLE_TICKWD_RESTART_CMD    <slug> → performs the runtime restart
  BUBBLE_TICKWD_NOTIFY_CMD     <slug> <chat_id> <text> → sends the alert
  BUBBLE_TICKWD_STATE          history JSONL (default <repo>/state/loop-tick-watchdog.jsonl)
  BUBBLE_TICKWD_DRY_RUN=1      decide + log only; never inject/restart/notify
  BUBBLE_TICKWD_RESTART=0      disable level-2 restarts fleet-wide (default 1)
  BUBBLE_TICKWD_HANG_RESTART=1 restart (instead of alert) on a hung tool call (default 0)
  BUBBLE_TICKWD_STALL_IDLE_SEC / _COOLDOWN_SEC / _MAX_KICKS / _WINDOW_SEC /
  _BUSY_GRACE_SEC / _HANG_SEC  tunables (see the lib defaults)
  BUBBLE_TICKWD_CHAT_ID        alert recipient (default BUBBLE_OPERATOR_CHAT_ID or Joris)

Usage:
  scripts/loop-tick-watchdog.py                # one pass over every discovered dept
  scripts/loop-tick-watchdog.py --dry-run      # decide only
  scripts/loop-tick-watchdog.py --host vps|local|auto   (default auto)
  scripts/loop-tick-watchdog.py --dept ben     # restrict to one slug
  scripts/loop-tick-watchdog.py --json         # machine-readable per-dept verdicts

Exit code: 0 always for a completed pass (a watchdog must never trip its own
timer into `failed`); 2 on a structural error (bad args / unusable state).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO_ROOT)

from scripts.lib import loop_tick_watchdog as wd  # noqa: E402
from scripts.lib.auto_restart import CONCIERGE_DENYLIST  # noqa: E402

DEFAULT_CHAT_ID = "6532205130"   # same literal fallback as loop-backup.sh


@dataclass
class DeptSpec:
    slug: str
    dept_dir: str
    session_dir: str            # ~/.claude/projects/<encoded cwd>
    inject_file: str            # <TELEGRAM_STATE_DIR>/inject
    host: str                   # "vps" | "local"
    resumes_context: bool       # runtime launches claude with --continue
    env_file: str = ""          # where TELEGRAM_BOT_TOKEN can be read (never echoed)
    unit: str = ""              # systemd unit (vps)
    tmux_bin: str = ""          # (local)
    tmux_session: str = ""      # (local)
    bot_pid_file: str = ""


def log(msg: str) -> None:
    print(f"[{wd.now_iso()}] [loop-tick-watchdog] {msg}", flush=True)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    try:
        return int(v) if v else default
    except ValueError:
        return default


# ── Discovery ────────────────────────────────────────────────────────────────
def _dept_host_from_state(dept_dir: str) -> str:
    """Top-level `host:` in onboarding/STATE.yaml → "local" or "vps" (mirrors
    loop-backup.sh dept_host: anything but exactly `local` is vps)."""
    state = os.path.join(dept_dir, "onboarding", "STATE.yaml")
    try:
        with open(state, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r'^host:\s*["\']?([A-Za-z]+)', line)
                if m:
                    return "local" if m.group(1) == "local" else "vps"
    except OSError:
        pass
    return "vps"


def discover_vps(agents_root: str, projects_root: str, channels_root: str) -> List[DeptSpec]:
    out: List[DeptSpec] = []
    for d in sorted(os.listdir(agents_root) if os.path.isdir(agents_root) else []):
        if not d.startswith("bubble-ops-"):
            continue
        dept_dir = os.path.join(agents_root, d)
        if not os.path.isdir(dept_dir):
            continue
        slug = d[len("bubble-ops-"):]
        unit = f"ops-loop-{slug}.service"
        if _run(["systemctl", "is-enabled", unit], capture=True).returncode != 0:
            log(f"{slug}: skip — {unit} not enabled (paused/absent)")
            continue
        if _dept_host_from_state(dept_dir) == "local":
            log(f"{slug}: skip — host: local (runs on its own machine)")
            continue
        cat = _run(["systemctl", "cat", unit], capture=True).stdout or ""
        resumes = "--continue" in cat
        out.append(DeptSpec(
            slug=slug, dept_dir=dept_dir,
            session_dir=os.path.join(projects_root, wd.projects_dir_name(dept_dir)),
            inject_file=os.path.join(channels_root, f"telegram-{slug}", "inject"),
            host="vps", resumes_context=resumes,
            env_file=f"/run/claude-agent-{slug}/env", unit=unit,
            bot_pid_file=os.path.join(channels_root, f"telegram-{slug}", "bot.pid"),
        ))
    return out


def read_plist(path: str) -> dict:
    """Read a launchd plist LENIENTLY. The fleet's rendered
    com.bubble.ops-loop-<slug>.plist files carry an XML comment containing
    `--channels` — `--` inside a comment is illegal XML, which Apple's plutil
    tolerates but Python's expat (plistlib) rejects (verified on M1/M4/M5:
    "not well-formed (invalid token): line 8, column 65"). Order: plutil
    (authoritative on macOS) → plistlib → regex fallback (Linux tests)."""
    try:
        res = _run(["plutil", "-convert", "json", "-o", "-", path], capture=True, timeout=15)
        if res.returncode == 0 and (res.stdout or "").strip():
            d = json.loads(res.stdout)
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    try:
        import plistlib
        with open(path, "rb") as fh:
            d = plistlib.load(fh)
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    # Regex fallback: strip comments, then pull the two keys we need.
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = re.sub(r"<!--.*?-->", "", fh.read(), flags=re.S)
    except OSError:
        return {}
    out: dict = {}
    m = re.search(r"<key>WorkingDirectory</key>\s*<string>([^<]*)</string>", txt)
    if m:
        out["WorkingDirectory"] = m.group(1)
    m = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", txt, re.S)
    if m:
        out["ProgramArguments"] = re.findall(r"<string>([^<]*)</string>", m.group(1))
    return out


def discover_local(launch_agents_dir: str, projects_root: str, channels_root: str) -> List[DeptSpec]:
    """Mac: every ~/Library/LaunchAgents/com.bubble.ops-loop-<slug>.plist (not
    -backup-), reading WorkingDirectory + the wrapper it runs; the wrapper's
    TELEGRAM_STATE_DIR / TMUX_BIN / SESSION / --continue are parsed by regex."""
    out: List[DeptSpec] = []
    if not os.path.isdir(launch_agents_dir):
        return out
    for f in sorted(os.listdir(launch_agents_dir)):
        m = re.match(r"^com\.bubble\.ops-loop-([a-z0-9-]+)\.plist$", f)
        if not m or m.group(1).startswith("backup-"):
            continue
        slug = m.group(1)
        pl = read_plist(os.path.join(launch_agents_dir, f))
        if not pl:
            log(f"{slug}: skip — unreadable plist")
            continue
        dept_dir = pl.get("WorkingDirectory") or ""
        args = pl.get("ProgramArguments") or []
        wrapper = next((a for a in args if a.endswith("-wrapper.sh")), "")
        wtxt = ""
        if wrapper and os.path.isfile(wrapper):
            try:
                with open(wrapper, encoding="utf-8", errors="replace") as fh:
                    wtxt = fh.read()
            except OSError:
                wtxt = ""

        def _grab(key: str, default: str = "") -> str:
            mm = re.search(rf'^\s*(?:export\s+)?{key}=["\']?([^"\'\n]+)', wtxt, re.M)
            return mm.group(1).strip() if mm else default
        state_dir = _grab("TELEGRAM_STATE_DIR", os.path.join(channels_root, f"telegram-{slug}"))
        if not dept_dir:
            mm = re.search(r'^\s*cd\s+["\']([^"\']+)["\']', wtxt, re.M)
            dept_dir = mm.group(1) if mm else ""
        if not dept_dir:
            log(f"{slug}: skip — no WorkingDirectory in plist/wrapper")
            continue
        out.append(DeptSpec(
            slug=slug, dept_dir=dept_dir,
            session_dir=os.path.join(projects_root, wd.projects_dir_name(dept_dir)),
            inject_file=os.path.join(state_dir, "inject"),
            host="local", resumes_context=("--continue" in wtxt),
            env_file=os.path.join(state_dir, ".env"),
            tmux_bin=_grab("TMUX_BIN", "tmux"), tmux_session=_grab("SESSION", f"ops-loop-{slug}"),
            bot_pid_file=os.path.join(state_dir, "bot.pid"),
        ))
    return out


def discover(host: str) -> List[DeptSpec]:
    cmd = os.environ.get("BUBBLE_TICKWD_DISCOVER_CMD")
    if cmd:
        res = _run(shlex.split(cmd), capture=True)
        specs = []
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            fields = {k: d.get(k, "") for k in DeptSpec.__dataclass_fields__}
            fields["resumes_context"] = bool(d.get("resumes_context"))
            specs.append(DeptSpec(**fields))
        return specs
    home = os.path.expanduser("~")
    projects_root = os.environ.get("BUBBLE_TICKWD_PROJECTS_ROOT", os.path.join(home, ".claude", "projects"))
    channels_root = os.environ.get("BUBBLE_TICKWD_CHANNELS_ROOT", os.path.join(home, ".claude", "channels"))
    if host == "local":
        return discover_local(os.environ.get("BUBBLE_TICKWD_LAUNCH_AGENTS_DIR", os.path.join(home, "Library", "LaunchAgents")),
                              projects_root, channels_root)
    return discover_vps(os.environ.get("BUBBLE_TICKWD_AGENTS_ROOT", "/home/claude/agents"), projects_root, channels_root)


# ── Side effects (each behind a hook) ────────────────────────────────────────
def _run(argv: List[str], capture: bool = False, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=capture, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def session_alive(spec: DeptSpec) -> bool:
    cmd = os.environ.get("BUBBLE_TICKWD_ALIVE_CMD")
    if cmd:
        return _run(shlex.split(cmd) + [spec.slug], capture=True).returncode == 0
    if spec.host == "vps":
        pid = (_run(["systemctl", "show", spec.unit, "-p", "MainPID", "--value"], capture=True).stdout or "0").strip()
        if not pid.isdigit() or int(pid) <= 0:
            return False
        # A bun poller inside this unit's cgroup (same signal the telegram-watchdog uses).
        try:
            for p in os.listdir("/proc"):
                if not p.isdigit():
                    continue
                try:
                    with open(f"/proc/{p}/comm") as fh:
                        if fh.read().strip() != "bun":
                            continue
                    with open(f"/proc/{p}/cgroup") as fh:
                        if spec.unit in fh.read():
                            return True
                except OSError:
                    continue
        except OSError:
            pass
        return False
    # Mac: bot.pid is unreliable on the PTY pattern (empty even when healthy —
    # wiki telegram-recovery #10); the tmux session existing is the honest signal.
    return _run([spec.tmux_bin or "tmux", "has-session", "-t", spec.tmux_session], capture=True).returncode == 0


def do_inject(spec: DeptSpec, text: str) -> bool:
    if not os.path.isdir(os.path.dirname(spec.inject_file)):
        log(f"{spec.slug}: inject dir missing ({os.path.dirname(spec.inject_file)}) — cannot inject")
        return False
    try:
        with open(spec.inject_file, "a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", " ") + "\n")
        return True
    except OSError as e:
        log(f"{spec.slug}: inject write failed: {e}")
        return False


def do_restart(spec: DeptSpec) -> bool:
    cmd = os.environ.get("BUBBLE_TICKWD_RESTART_CMD")
    if cmd:
        return _run(shlex.split(cmd) + [spec.slug], capture=True).returncode == 0
    if spec.host == "vps":
        # Mirror the telegram-watchdog: stop → settle → drop stale bot.pid → start
        # (a bare `restart` can leave a zombie poller holding the getUpdates slot).
        # The canonical unit now carries --continue, so this resumes context.
        if _run(["sudo", "-n", "/usr/bin/systemctl", "stop", spec.unit], timeout=120).returncode != 0:
            return False
        time.sleep(3)
        try:
            if spec.bot_pid_file:
                os.remove(spec.bot_pid_file)
        except OSError:
            pass
        ok = _run(["sudo", "-n", "/usr/bin/systemctl", "start", spec.unit], timeout=120).returncode == 0
        _touch_external_mark(spec)
        return ok
    # Mac: kill the tmux session; the KeepAlive wrapper relaunches (with
    # --continue) within ~5s — the documented clean-restart recipe (wiki
    # mac-local-loop-runner-gotchas). Never `launchctl kickstart -k` here: that
    # kills only the wrapper and races the still-running claude/poller.
    return _run([spec.tmux_bin or "tmux", "kill-session", "-t", spec.tmux_session], capture=True).returncode == 0


def _external_mark_path(spec: DeptSpec) -> str:
    return f"/run/telegram-watchdog-{spec.slug}/last-restart" if spec.host == "vps" else ""


def _external_restart_epoch(spec: DeptSpec) -> Optional[float]:
    p = _external_mark_path(spec)
    try:
        return os.path.getmtime(p) if p else None
    except OSError:
        return None


def _touch_external_mark(spec: DeptSpec) -> None:
    p = _external_mark_path(spec)
    if not p:
        return
    try:
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError:
        pass  # dir may not exist / not ours — the telegram-watchdog will still see its own mark


def _read_token(spec: DeptSpec) -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok:
        return tok
    try:
        with open(spec.env_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r'^(?:export\s+)?TELEGRAM_BOT_TOKEN=["\']?([^"\'\s]+)', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def notify(spec: DeptSpec, text: str) -> None:
    """Best-effort Telegram ping; never raises, never echoes the token."""
    chat_id = os.environ.get("BUBBLE_TICKWD_CHAT_ID") or os.environ.get("BUBBLE_OPERATOR_CHAT_ID") or DEFAULT_CHAT_ID
    cmd = os.environ.get("BUBBLE_TICKWD_NOTIFY_CMD")
    if cmd:
        _run(shlex.split(cmd) + [spec.slug, chat_id, text], capture=True)
        return
    token = _read_token(spec)
    if not token:
        log(f"{spec.slug}: no TELEGRAM_BOT_TOKEN resolvable — alert logged only")
        return
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed host)
            if resp.status != 200:
                log(f"{spec.slug}: telegram sendMessage HTTP {resp.status}")
    except Exception as e:  # noqa: BLE001
        log(f"{spec.slug}: telegram send failed: {type(e).__name__}")


# ── One pass ─────────────────────────────────────────────────────────────────
def build_config(spec: DeptSpec) -> wd.Config:
    restart_enabled = os.environ.get("BUBBLE_TICKWD_RESTART", "1") == "1"
    return wd.Config(
        stall_idle_sec=_env_int("BUBBLE_TICKWD_STALL_IDLE_SEC", wd.DEFAULT_STALL_IDLE_SEC),
        busy_grace_sec=_env_int("BUBBLE_TICKWD_BUSY_GRACE_SEC", wd.DEFAULT_BUSY_GRACE_SEC),
        hang_sec=_env_int("BUBBLE_TICKWD_HANG_SEC", wd.DEFAULT_HANG_SEC),
        cooldown_sec=_env_int("BUBBLE_TICKWD_COOLDOWN_SEC", wd.DEFAULT_COOLDOWN_SEC),
        max_kicks=_env_int("BUBBLE_TICKWD_MAX_KICKS", wd.DEFAULT_MAX_KICKS),
        window_sec=_env_int("BUBBLE_TICKWD_WINDOW_SEC", wd.DEFAULT_WINDOW_SEC),
        # Restart only when it resumes context AND the slug is a dept (never a concierge).
        restart_allowed=restart_enabled and spec.resumes_context and spec.slug not in CONCIERGE_DENYLIST,
        hang_restart=os.environ.get("BUBBLE_TICKWD_HANG_RESTART", "0") == "1",
        external_restart_epoch=_external_restart_epoch(spec),
    )


def process_dept(spec: DeptSpec, state_path: str, dry_run: bool, now: float) -> dict:
    obs = wd.observe_transcript(spec.slug, spec.session_dir, inject_file=spec.inject_file)
    history = wd.read_events(state_path)
    cfg = build_config(spec)
    d = wd.decide(obs, history, now, cfg)
    verdict = {"slug": spec.slug, "host": spec.host, "action": d.action, "reason": d.reason,
               "level": d.level, "err_class": d.err_class, **d.facts}

    if not d.is_kick and not d.notify:
        log(f"{spec.slug}: {d.action} — {d.reason}")
        return verdict

    # Alerts (no kick): dedupe per cooldown, record, notify.
    if not d.is_kick:
        if wd.already_alerted(history, spec.slug, d.dedupe_key, now, cfg.cooldown_sec):
            log(f"{spec.slug}: {d.action} — {d.reason} (already alerted; quiet)")
            verdict["deduped"] = True
            return verdict
        log(f"{spec.slug}: {d.action} — {d.reason}")
        if dry_run:
            log(f"{spec.slug}: DRY_RUN — would alert")
            return verdict
        wd.append_event(state_path, wd.format_event(
            spec.slug, d.action, d.reason, err_ts=obs.error_ts, err_text=obs.error_text or "",
            extra={"dedupe_key": d.dedupe_key}))
        notify(spec, f"loop-tick-watchdog [{spec.slug}]: {d.action} — {d.reason}")
        return verdict

    # Kicks.
    log(f"{spec.slug}: {d.action} (level {d.level}) — {d.reason}")
    if dry_run:
        log(f"{spec.slug}: DRY_RUN — would {d.action}")
        verdict["dry_run"] = True
        return verdict

    if d.action == wd.KICK_INJECT:
        if not session_alive(spec):
            # Nothing to inject into. KeepAlive/systemd own process death; we
            # only record + alert so the gap is visible (no double-launch).
            reason = "session/poller not alive — inject impossible; supervisor owns process death"
            log(f"{spec.slug}: {reason}")
            if not wd.already_alerted(history, spec.slug, f"dead:{obs.error_ts}", now, cfg.cooldown_sec):
                wd.append_event(state_path, wd.format_event(spec.slug, wd.ALERT_INJECT_FAILED, reason,
                                err_ts=obs.error_ts, extra={"dedupe_key": f"dead:{obs.error_ts}"}))
                notify(spec, f"loop-tick-watchdog [{spec.slug}]: stalled on {(obs.error_text or '')[:80]!r} but the session is not alive — check the supervisor")
            verdict["action"] = wd.ALERT_INJECT_FAILED
            return verdict
        ok = do_inject(spec, wd.rearm_turn(spec.slug, obs.error_text or "", obs.error_ts or now))
        verdict["injected"] = ok
    else:  # KICK_RESTART
        ok = do_restart(spec)
        verdict["restarted"] = ok

    wd.append_event(state_path, wd.format_event(
        spec.slug, d.action, d.reason, err_ts=obs.error_ts, err_text=obs.error_text or "",
        level=d.level, extra={"ok": ok, "transcript": os.path.basename(obs.transcript or "")}))
    outcome = "sent" if ok else "FAILED"
    notify(spec, f"loop-tick-watchdog [{spec.slug}]: {d.action} {outcome} — {d.reason}")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", choices=["auto", "vps", "local"], default="auto")
    ap.add_argument("--dept", action="append", default=[], help="restrict to slug(s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state", default=os.environ.get("BUBBLE_TICKWD_STATE",
                    os.path.join(REPO_ROOT, "state", "loop-tick-watchdog.jsonl")))
    args = ap.parse_args()

    dry_run = args.dry_run or os.environ.get("BUBBLE_TICKWD_DRY_RUN", "0").lower() in ("1", "true", "yes", "on")
    host = args.host
    if host == "auto":
        host = "local" if platform.system() == "Darwin" else "vps"
    log(f"pass start host={host} dry_run={int(dry_run)} state={args.state}")

    try:
        specs = discover(host)
    except Exception as e:  # noqa: BLE001
        log(f"discovery failed: {type(e).__name__}: {e}")
        return 2
    if args.dept:
        specs = [s for s in specs if s.slug in set(args.dept)]
    if not specs:
        log("no depts discovered — nothing to do")
        return 0

    now = time.time()
    verdicts = []
    for spec in specs:
        try:
            verdicts.append(process_dept(spec, args.state, dry_run, now))
        except Exception as e:  # noqa: BLE001 — one dept must never abort the pass
            log(f"{spec.slug}: ERROR {type(e).__name__}: {e}")
            verdicts.append({"slug": spec.slug, "action": "error", "reason": str(e)})
    if args.json:
        print(json.dumps(verdicts, ensure_ascii=False))
    log(f"pass done depts={','.join(s.slug for s in specs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
