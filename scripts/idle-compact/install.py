#!/usr/bin/env python3
"""Transactional idle-compaction installers; Python 3.9+, no dependencies."""
import argparse
import json
import math
import os
from pathlib import Path
import plistlib
import pwd
import re
import subprocess
import tempfile
from xml.sax.saxutils import escape

from idle_compact import fleet_status


class RollbackFailure(Exception):
    """Content-free diagnostics retain the original and every rollback failure."""
    def __init__(self, original_error, failures):
        self.original_error, self.failures = original_error, failures
        super().__init__("original=" + type(original_error).__name__ + " rollback=" +
                         ",".join(stage + ":" + type(error).__name__ for stage, error in failures))


def rollback(error, steps):
    failures = []
    for stage, action in steps:
        try:
            action()
        except Exception as exc:
            failures.append((stage, exc))
    if failures:
        raise RollbackFailure(error, failures) from error


def command(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, timeout=30)


def publish(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def original(paths):
    for path in paths:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("refusing symlinked install destination")
    return {p: p.read_bytes() if p.exists() else None for p in paths}


def restore(saved):
    for path, data in saved.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            publish(path, data)


def encoded(data):
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def harness(selector, slug):
    # Morty is a Hermes concierge; do not guess Claude if its selector is absent.
    if slug == "morty":
        return "hermes"
    value = selector.read_text().strip() if os.path.lexists(selector) else "claude"
    if value not in ("", "claude", "hermes"):
        raise ValueError("unknown harness selector")
    return value or "claude"


def configuration(args, slug, home, dept, config, unit, selector):
    transcript = args.transcript_dir or home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(dept))
    state_dir = home / ".local/state/idle-compact"
    runtime = dict(tmux_session=args.tmux_session or "ops-loop-" + slug,
                   transcript_dir=str(transcript),
                   ledger=str(args.ledger or home / (".claude/channels/telegram-" + slug) / "delivery-ledger.jsonl"),
                   state=str(state_dir / (slug + ".json")),
                   log=str(home / ("Library/Logs/idle-compact-" + slug + ".log") if args.platform == "mac"
                           else state_dir / (slug + ".log")),
                   idle_min=55, min_context=200000, quiet_sec=120, dry_run=args.dry_run,
                   machine_wake_patterns=[],
                   tmux_bin=args.tmux_bin, meeting_marker=str(args.meeting_marker or dept / "state/meeting-poll.active"),
                   harness_selector=str(selector))
    runtime["transport"] = "tmux" if args.platform == "mac" else "dtach"
    if args.platform == "vps":
        runtime.update(slug=slug,
                       dtach_socket=str(getattr(args, "dtach_socket", None) or
                                        Path("/run/bubble-agent-" + slug + "/dtach.sock")),
                       dtach_bin=getattr(args, "dtach_bin", "/usr/bin/dtach"),
                       confirm_min=getattr(args, "confirm_min", 10))
    service = "gui/{}/com.bubble.idle-compact-{}".format(os.getuid(), slug) if args.platform == "mac" else "idle-compact@" + slug + ".timer"
    return dict(slug=slug, home=str(home), platform=args.platform, runtime=runtime, service=service,
                unit_path=str(unit), config_path=str(config))


def install_mac(args):
    home, slug = args.home, args.slug
    selector = args.selector_dir / ("harness-" + slug)
    if harness(selector, slug) == "hermes":
        print("SKIP slug=" + slug + " reason=hermes_harness")
        return
    if os.geteuid() == 0:
        raise ValueError("run the Mac installer as the agent's login user")
    dept = args.dept_dir
    label = "com.bubble.idle-compact-" + slug
    plist = home / "Library/LaunchAgents" / (label + ".plist")
    config = home / ".local/state/idle-compact/configs" / (slug + ".json")
    data = configuration(args, slug, home, dept, config, plist, selector)
    values = dict(SLUG=slug, HOME=str(home), FRAMEWORK_ROOT=str(args.framework_root), CONFIG=str(config))
    rendered = re.sub(r"@([A-Z_]+)@", lambda m: escape(values[m[1]]),
                      (args.framework_root / "deploy/templates/com.bubble.idle-compact.plist.template").read_text()).encode()
    plistlib.loads(rendered)
    # Mandatory native validation of a candidate before touching the previous plist.
    with tempfile.NamedTemporaryFile(suffix=".plist") as candidate:
        candidate.write(rendered)
        candidate.flush()
        command("plutil", "-lint", candidate.name)
    loaded = command("launchctl", "print", data["service"], check=False).returncode == 0
    saved = original([plist, config])
    if saved[config] is not None:
        previous = json.loads(saved[config])
        data["runtime"]["machine_wake_patterns"] = previous["runtime"].get("machine_wake_patterns", [])
    if loaded and saved[plist] is None:
        raise ValueError("loaded LaunchAgent has no restorable plist")
    if loaded and saved[plist] == rendered and saved[config] == encoded(data):
        print("UNCHANGED slug=" + slug)
        return
    (home / "Library/Logs").mkdir(parents=True, exist_ok=True)
    bootstrap_attempted = False
    try:
        if loaded:
            command("launchctl", "bootout", data["service"])
        publish(config, encoded(data))
        publish(plist, rendered)
        bootstrap_attempted = True
        command("launchctl", "bootstrap", "gui/" + str(os.getuid()), str(plist))
    except Exception as error:
        def unload_partial():
            if bootstrap_attempted and command("launchctl", "print", data["service"], check=False).returncode == 0:
                command("launchctl", "bootout", data["service"])
        def reload_previous():
            if loaded:
                try:
                    present = command("launchctl", "print", data["service"], check=False).returncode == 0
                except Exception:
                    command("launchctl", "bootstrap", "gui/" + str(os.getuid()), str(plist))
                    raise
                if not present:
                    command("launchctl", "bootstrap", "gui/" + str(os.getuid()), str(plist))
        rollback(error, [("bootout_partial", unload_partial), ("restore", lambda: restore(saved)),
                         ("rebootstrap", reload_previous)])
        raise
    print("INSTALLED slug=" + slug)


def install_vps(args):
    if os.geteuid() != 0:
        raise ValueError("run VPS installer as root; runtime runs as agent-<slug>")
    if args.framework_root != Path("/opt/bubble-ops-loop"):
        raise ValueError("VPS installation requires framework at /opt/bubble-ops-loop")
    slugs = [args.slug] if args.slug else sorted(p.name for p in args.agents_root.iterdir() if p.is_dir())
    plans = []
    for slug in slugs:
        validate_slug(slug)
        selector = args.selector_dir / slug
        if harness(selector, slug) == "hermes":
            print("SKIP slug=" + slug + " reason=hermes_harness")
            continue
        account = pwd.getpwnam("agent-" + slug)
        home = Path(account.pw_dir)
        # The sibling units use these fixed isolated home/workdir conventions.
        if home != Path("/home/agent-" + slug):
            raise ValueError("unexpected agent home; supply a systemd drop-in before installing")
        dept = args.dept_dir or args.agents_root / slug
        if dept != Path("/srv/agents") / slug:
            raise ValueError("VPS unit requires /srv/agents/<slug>")
        config = args.config_dir / (slug + ".json")
        data = configuration(args, slug, home, dept, config, args.unit_dir / "idle-compact@.timer", selector)
        # --all preserves installed per-agent overrides, instead of overwriting them with guesses.
        if args.all and config.exists():
            previous = json.loads(config.read_text())
            data["runtime"].update(previous["runtime"])
            if args.dry_run:
                data["runtime"]["dry_run"] = True
        # Upgrade old tmux-only VPS configurations while retaining per-agent paths/rules.
        data["runtime"].update(transport="dtach", slug=slug)
        plans.append((data, account))
    if not plans:
        return
    units = {args.unit_dir / name: (args.framework_root / "deploy/templates" / name).read_bytes()
             for name in ("idle-compact@.service", "idle-compact@.timer")}
    # Unit/config paths are deliberately fixed in the systemd template.
    if args.config_dir != Path("/etc/bubble-idle-compact"):
        raise ValueError("VPS config directory is /etc/bubble-idle-compact")
    files = dict(units)
    for data, account in plans:
        files[Path(data["config_path"])] = encoded(data)
    saved = original(files)
    timers = {}
    for data, _ in plans:
        service = data["service"]
        enabled = command("systemctl", "is-enabled", service, check=False)
        if enabled.stdout.strip() == "masked":
            raise ValueError("timer is masked; unmask explicitly before installation")
        timers[service] = (enabled.returncode == 0,
                           command("systemctl", "is-active", service, check=False).returncode == 0)
    changed = {path: body for path, body in files.items() if saved[path] != body}
    try:
        for data, account in plans:
            state = Path(data["runtime"]["state"]).parent
            home = Path(account.pw_dir)
            for directory in (home / ".local", home / ".local/state", state):
                created = not directory.exists()
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if created or directory == state:
                    os.chown(directory, account.pw_uid, account.pw_gid)
                    os.chmod(directory, 0o700)
        for path, body in changed.items():
            publish(path, body)
        if changed:
            command("systemctl", "daemon-reload")
        for service, (enabled, active) in timers.items():
            if not enabled or not active:
                command("systemctl", "enable", "--now", service)
    except Exception as error:
        # Restore all definitions, then restore each timer's prior enabled/active state.
        steps = [("restore", lambda: restore(saved)), ("daemon_reload", lambda: command("systemctl", "daemon-reload"))]
        for service, (enabled, active) in timers.items():
            for verb, needed in (("stop", not active), ("disable", not enabled), ("enable", enabled), ("start", active)):
                if needed:
                    steps.append((verb + "_" + service, lambda verb=verb, service=service: command("systemctl", verb, service)))
        rollback(error, steps)
        raise
    for data, _ in plans:
        print(("INSTALLED" if changed else "UNCHANGED") + " slug=" + data["slug"])


def uninstall_mac(args):
    if os.geteuid() == 0:
        raise ValueError("run the Mac installer as the agent's login user")
    plist = args.home / "Library/LaunchAgents" / ("com.bubble.idle-compact-" + args.slug + ".plist")
    config = args.config_dir / (args.slug + ".json")
    original([plist, config])  # Refuse symlinked removal destinations.
    service = "gui/{}/com.bubble.idle-compact-{}".format(os.getuid(), args.slug)
    if command("launchctl", "print", service, check=False).returncode == 0:
        command("launchctl", "bootout", service)
    plist.unlink(missing_ok=True)
    config.unlink(missing_ok=True)
    print("UNINSTALLED slug=" + args.slug + " state_and_logs=retained")


def uninstall_vps(args):
    if os.geteuid() != 0:
        raise ValueError("run VPS installer as root")
    if args.config_dir != Path("/etc/bubble-idle-compact"):
        raise ValueError("VPS config directory is /etc/bubble-idle-compact")
    slugs = [args.slug] if args.slug else sorted({p.stem for p in args.config_dir.glob("*.json")} |
        ({p.name for p in args.agents_root.iterdir() if p.is_dir()} if args.agents_root.exists() else set()))
    units = [args.unit_dir / name for name in ("idle-compact@.service", "idle-compact@.timer")]
    configs = [args.config_dir / (slug + ".json") for slug in slugs]
    for slug in slugs:
        validate_slug(slug)
    original([*units, *configs])
    for slug, config in zip(slugs, configs):
        timer = "idle-compact@" + slug + ".timer"
        if not config.exists() and command("systemctl", "show", timer, "-p", "LoadState", "--value", check=False).stdout.strip() != "loaded":
            print("SKIP slug=" + slug + " reason=not_installed")
            continue
        command("systemctl", "disable", "--now", timer)
        service = "idle-compact@" + slug + ".service"
        if command("systemctl", "is-active", service, check=False).returncode == 0:
            command("systemctl", "stop", service)
        config.unlink(missing_ok=True)
        print("UNINSTALLED slug=" + slug + " state_and_logs=retained")
    # These templates are shared: keep them while another configured agent uses them.
    if not any(args.config_dir.glob("*.json")):
        for unit in units:
            unit.unlink(missing_ok=True)
    command("systemctl", "daemon-reload")


def validate_slug(slug):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", slug):
        raise ValueError("invalid slug")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("mac", "vps"))
    parser.add_argument("--slug")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--uninstall", action="store_true", help="disable checks and remove definitions; retain state/logs")
    parser.add_argument("--framework-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--home", type=Path, default=Path.home())
    for name in ("dept-dir", "transcript-dir", "ledger", "meeting-marker", "selector-dir", "config-dir"):
        parser.add_argument("--" + name, type=lambda p: Path(p).expanduser())
    parser.add_argument("--tmux-session")
    parser.add_argument("--tmux-bin", default="tmux")
    parser.add_argument("--dtach-socket", type=lambda p: Path(p).expanduser())
    parser.add_argument("--dtach-bin", default="/usr/bin/dtach")
    parser.add_argument("--confirm-min", type=float, default=10)
    parser.add_argument("--dry-run", action="store_true", help="install checks that never send /compact")
    parser.add_argument("--unit-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--agents-root", type=Path, default=Path("/srv/agents"))
    args = parser.parse_args()
    args.framework_root = args.framework_root.resolve()
    args.home = args.home.expanduser().absolute()
    if args.dept_dir:
        args.dept_dir = args.dept_dir.absolute()
    args.selector_dir = args.selector_dir or (args.home / "Library/Application Support/bubble-ops-loop" if args.platform == "mac" else Path("/etc/bubble-harness"))
    args.config_dir = args.config_dir or (args.home / ".local/state/idle-compact/configs" if args.platform == "mac" else Path("/etc/bubble-idle-compact"))
    if args.status:
        fleet_status(args.config_dir)
        return
    if bool(args.slug) == bool(args.all) or (args.all and args.platform == "mac"):
        parser.error("choose --slug or VPS --all")
    if args.all and any((args.dept_dir, args.transcript_dir, args.ledger, args.tmux_session,
                         args.meeting_marker, args.dtach_socket)):
        parser.error("per-agent path/session overrides require --slug")
    if args.slug:
        validate_slug(args.slug)
    if (not math.isfinite(args.confirm_min) or args.confirm_min <= 0 or
            (args.dtach_socket and not args.dtach_socket.is_absolute())):
        parser.error("confirmation minutes must be positive and dtach socket absolute")
    if args.platform == "mac" and not args.uninstall and not (args.dept_dir and args.tmux_session):
        parser.error("Mac requires --dept-dir and --tmux-session")
    try:
        action = (uninstall_mac if args.platform == "mac" else uninstall_vps) if args.uninstall else (
            install_mac if args.platform == "mac" else install_vps)
        action(args)
    except RollbackFailure as exc:
        parser.exit(1, "idle-compact install failed: " + str(exc) + "\n")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Do not print command output or payloads on failure.
        parser.exit(1, "idle-compact operation failed: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
