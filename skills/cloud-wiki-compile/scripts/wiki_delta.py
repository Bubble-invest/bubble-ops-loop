#!/usr/bin/env python3
"""Durable capture queue and bounded semantic planner for wiki compilation.

Complete JSONL rows are first captured into immutable, SHA-verified chunks.
Chunks leave the semantic queue only after a plan-bound model receipt. The
capture frontier stays frozen while its queue drains, so new appends join the
next cycle and cannot starve older work. A separate persistent full generation
uses the same bounded consumer without changing the live delta watermark.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

VERSION = 2
FOLDERS = (
    "tony_ceo", "tonio_extrnd", "maya_sales", "claudette", "morty",
    "rick_rnd", "ben_fund", "miranda_socials", "ellie_assistant",
    "geraldine_accounting",
)
PROJECTS = "/home/claude/.claude/projects"
SOURCE_PATTERNS = {
    "tony_ceo": (f"{PROJECTS}/_vps-tony/-srv-agents-tony", f"{PROJECTS}/_vps-tony-hermes"),
    "tonio_extrnd": (f"{PROJECTS}/_mac-joris/-Users-joris-claude-workspaces-Tony-CEO",),
    "maya_sales": (f"{PROJECTS}/_vps-maya/-srv-agents-maya", f"{PROJECTS}/_vps-maya-hermes"),
    "claudette": (f"{PROJECTS}/_vps-claudette/-srv-agents-claudette", f"{PROJECTS}/_vps-claudette-hermes"),
    "morty": (f"{PROJECTS}/_vps-morty/-srv-agents-morty", f"{PROJECTS}/_vps-morty-hermes"),
    "rick_rnd": (
        f"{PROJECTS}/_mac-joris/-Users-joris-claude-workspaces-Rick-RnD",
        f"{PROJECTS}/_mac-joris/-Users-joris-claude-workspaces-Rick-RnD-prototypes-deepseek-session",
        f"{PROJECTS}/_mac-jade/*Rick-RnD*",
    ),
    "ben_fund": (f"{PROJECTS}/_vps-ben/-srv-agents-ben", f"{PROJECTS}/_vps-ben-hermes"),
    "miranda_socials": (f"{PROJECTS}/_mac-jade/*bubble-ops-content*",),
    "ellie_assistant": (f"{PROJECTS}/_mac-jade/*ellie*",),
    "geraldine_accounting": (f"{PROJECTS}/_mac-jade/*bubble-ops-accountant*",),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "version": VERSION, "capture_files": {}, "delta_queue": [],
        "next_delta_queue": [], "next_delta_cycle": None,
        "full_generation": None, "weekly_backlog": [],
        "next_folder_index": 0, "next_queue_kind": "delta",
    }


def atomic_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_text(path: pathlib.Path, text: str) -> str:
    data = text.encode("utf-8")
    atomic_bytes(path, data)
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: pathlib.Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


@contextmanager
def exclusive_lock(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def sha256_prefix(path: pathlib.Path, size: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = size
    with path.open("rb") as handle:
        while remaining is None or remaining > 0:
            wanted = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            block = handle.read(wanted)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    if remaining not in (None, 0):
        raise ValueError(f"{path} shorter than expected prefix ({size} bytes)")
    return digest.hexdigest()


def source_roots(patterns: Iterable[str]) -> list[pathlib.Path]:
    roots: set[pathlib.Path] = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches and not glob.has_magic(pattern):
            matches = [pattern]
        roots.update(pathlib.Path(item) for item in matches if pathlib.Path(item).is_dir())
    return sorted(roots)


def scan_sources(source_map: dict[str, tuple[str, ...]] = SOURCE_PATTERNS) -> dict[str, list[pathlib.Path]]:
    scanned: dict[str, list[pathlib.Path]] = {}
    seen: set[pathlib.Path] = set()
    for folder in FOLDERS:
        found: list[pathlib.Path] = []
        for root in source_roots(source_map[folder]):
            for path in root.rglob("*.jsonl"):
                resolved = path.resolve()
                if resolved not in seen and path.is_file():
                    seen.add(resolved)
                    found.append(resolved)
        scanned[folder] = sorted(found)
    return scanned


def calendar_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from exc


def date_window(
    date_from: date | None, date_to: date | None,
) -> tuple[datetime | None, datetime | None]:
    if date_from and date_to and date_from > date_to:
        raise ValueError("--from must be <= --to")
    start = datetime.combine(date_from, time.min, timezone.utc) if date_from else None
    end = datetime.combine(date_to, time.max, timezone.utc) if date_to else None
    return start, end


def filter_sources_by_mtime(
    scanned: dict[str, list[pathlib.Path]], start: datetime | None, end: datetime | None,
) -> dict[str, list[pathlib.Path]]:
    def included(path: pathlib.Path) -> bool:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return (start is None or modified >= start) and (end is None or modified <= end)

    return {
        folder: [path for path in paths if included(path)]
        for folder, paths in scanned.items()
    }


def load_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != VERSION or not isinstance(state.get("capture_files"), dict):
        raise ValueError(f"unsupported/corrupt delta state: {path}")
    state.setdefault("next_delta_queue", [])
    state.setdefault("next_delta_cycle", None)
    return state


def result_envelope(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    return None


def result_is_success(path: pathlib.Path, run_id: str | None = None) -> bool:
    result = result_envelope(path)
    valid = bool(
        result and result.get("type") == "result"
        and result.get("is_error") is False
        and ("subtype" not in result or result.get("subtype") == "success")
    )
    if not valid or run_id is None:
        return valid
    return result.get("result") == f"WIKI_COMPILE_RECEIPT:{run_id}"


def verify_file(path: str, digest: str | None) -> bool:
    try:
        candidate = pathlib.Path(path)
        return bool(digest) and candidate.is_file() and sha256_prefix(candidate) == digest
    except OSError:
        return False


def verify_plan_feeds(plan: dict[str, Any]) -> bool:
    expected = dict(plan.get("feed_sha256", {}))
    expected[plan.get("aggregate_feed", "")] = plan.get("aggregate_sha256")
    expected[plan.get("weekly_aggregate_feed", "")] = plan.get("weekly_aggregate_sha256")
    return bool(expected) and all(verify_file(path, digest) for path, digest in expected.items())


def bootstrap_cutoff(log_dir: pathlib.Path, safety_hours: int) -> tuple[datetime, str]:
    successful = [
        path for path in log_dir.glob("compile-compile-*.log") if result_is_success(path)
    ] if log_dir.is_dir() else []
    if successful:
        newest = max(successful, key=lambda path: path.stat().st_mtime)
        at = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
        return at - timedelta(hours=safety_hours), f"last_success:{newest.name}"
    return datetime.now(timezone.utc) - timedelta(days=7), "fallback:7d"


def stable_snapshot(path: pathlib.Path) -> bytes:
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        data = handle.read()
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise RuntimeError(f"transcript changed while snapshotting: {path}")
    return data


def complete_prefix_size(data: bytes) -> int:
    newline = data.rfind(b"\n")
    return newline + 1 if newline >= 0 else 0


def reduced_row(raw: bytes, folder: str, basename: str) -> str | None:
    try:
        obj = json.loads(raw)
        role = obj.get("type")
        if role not in ("user", "assistant"):
            return None
        content = obj.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            return None
        timestamp = str(obj.get("timestamp", ""))[:19]
        text = " ".join(content[:500].split())
        return f"{folder} {basename} {timestamp} {role}] {text}\n"
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError) as exc:
        raise ValueError(f"malformed transcript row in {folder}/{basename}: {exc}") from exc


def parsed_rows(data: bytes, folder: str, basename: str) -> tuple[int, list[dict[str, Any]]]:
    end = complete_prefix_size(data)
    rows: list[dict[str, Any]] = []
    offset = 0
    for raw in data[:end].splitlines(keepends=True):
        start = offset
        offset += len(raw)
        reduced = reduced_row(raw, folder, basename)
        if reduced is not None:
            rows.append({"start": start, "end": offset, "text": reduced})
    return end, rows


def capture_start(data: bytes, entry: dict[str, Any] | None) -> int:
    if not entry:
        return 0
    offset = entry.get("offset")
    digest = entry.get("prefix_sha256")
    if not isinstance(offset, int) or offset < 0 or offset > len(data):
        return 0
    return offset if hashlib.sha256(data[:offset]).hexdigest() == digest else 0


def write_semantic_chunks(
    *, folder: str, path: pathlib.Path, start: int, rows: list[dict[str, Any]],
    cycle_id: str, kind: str, spool_root: pathlib.Path, chunk_chars: int,
    context_rows: int,
) -> list[dict[str, Any]]:
    prior = [item for item in rows if item["end"] <= start]
    new = [item for item in rows if item["start"] >= start]
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(new):
        selected: list[dict[str, Any]] = []
        size = 0
        while index < len(new):
            candidate = new[index]
            if selected and size + len(candidate["text"]) > chunk_chars:
                break
            selected.append(candidate)
            size += len(candidate["text"])
            index += 1
        groups.append(selected)

    chunks: list[dict[str, Any]] = []
    preceding = list(prior)
    for group_index, selected in enumerate(groups):
        backward = preceding[-context_rows:] if context_rows else []
        following = [item for group in groups[group_index + 1:] for item in group]
        forward = following[:context_rows] if context_rows else []
        feed = "".join(f"[CONTEXT_ONLY {item['text']}" for item in backward)
        feed += "".join(f"[NEW {item['text']}" for item in selected)
        feed += "".join(f"[CONTEXT_ONLY {item['text']}" for item in forward)
        size = sum(len(item["text"]) for item in selected)
        chunk_id = uuid.uuid4().hex
        feed_path = spool_root / cycle_id / f"{chunk_id}.txt"
        digest = atomic_text(feed_path, feed)
        chunks.append({
            "id": chunk_id, "kind": kind, "cycle_id": cycle_id,
            "folder": folder, "source_path": str(path), "captured_at": utc_now(),
            "new_start": selected[0]["start"], "new_end": selected[-1]["end"],
            "new_chars": size, "context_chars": len(feed) - size,
            "feed_path": str(feed_path), "feed_sha256": digest,
        })
        preceding.extend(selected)
    return chunks


def seed_bootstrap(
    state: dict[str, Any], scanned: dict[str, list[pathlib.Path]], cutoff: datetime, source: str
) -> None:
    for folder, paths in scanned.items():
        for path in paths:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= cutoff:
                continue
            data = stable_snapshot(path)
            end = complete_prefix_size(data)
            state["capture_files"][str(path)] = {
                "folder": folder, "offset": end,
                "prefix_sha256": hashlib.sha256(data[:end]).hexdigest(),
                "captured_at": utc_now(), "bootstrap_seeded": True,
            }
    state["bootstrap"] = {"cutoff": cutoff.isoformat(), "source": source, "created_at": utc_now()}


def promote_next_delta_cycle(state: dict[str, Any]) -> None:
    if state["delta_queue"]:
        return
    if not state["next_delta_queue"]:
        state["delta_cycle"] = None
        return
    state["delta_queue"] = state["next_delta_queue"]
    state["delta_cycle"] = state["next_delta_cycle"]
    state["next_delta_queue"] = []
    state["next_delta_cycle"] = None


def capture_delta_cycle(
    state: dict[str, Any], scanned: dict[str, list[pathlib.Path]], spool_root: pathlib.Path,
    chunk_chars: int, context_rows: int, prune_missing: bool = True,
) -> None:
    # The active queue is immutable/fair. New rows are still captured on every
    # invocation (including failed-plan replay) into a separate next frontier.
    # When active drains, promotion is atomic in the same state transaction.
    promote_next_delta_cycle(state)
    target_next = bool(state["delta_queue"])
    queue_key = "next_delta_queue" if target_next else "delta_queue"
    cycle_key = "next_delta_cycle" if target_next else "delta_cycle"
    cycle = state.get(cycle_key)
    cycle_id = cycle["id"] if cycle else f"delta-{uuid.uuid4().hex}"
    queue: list[dict[str, Any]] = list(state[queue_key])
    updated = dict(state["capture_files"])
    present: set[str] = set()
    for folder in FOLDERS:
        for path in scanned[folder]:
            key = str(path)
            present.add(key)
            data = stable_snapshot(path)
            end, rows = parsed_rows(data, folder, path.name)
            start = capture_start(data, updated.get(key))
            if end > start:
                queue.extend(write_semantic_chunks(
                    folder=folder, path=path, start=start, rows=rows,
                    cycle_id=cycle_id, kind="delta", spool_root=spool_root,
                    chunk_chars=chunk_chars, context_rows=context_rows,
                ))
                updated[key] = {
                    "folder": folder, "offset": end,
                    "prefix_sha256": hashlib.sha256(data[:end]).hexdigest(),
                    "captured_at": utc_now(), "cycle_id": cycle_id,
                }
    if prune_missing:
        for key in list(updated):
            if key not in present:
                del updated[key]
    state["capture_files"] = updated
    state[queue_key] = queue
    if queue:
        captured_at = cycle["captured_at"] if cycle else utc_now()
        state[cycle_key] = {
            "id": cycle_id, "captured_at": captured_at,
            "initial_chunks": len(queue),
        }


def start_full_generation(
    state: dict[str, Any], scanned: dict[str, list[pathlib.Path]], spool_root: pathlib.Path,
    chunk_chars: int, context_rows: int,
) -> None:
    if state.get("full_generation"):
        return
    generation_id = f"full-{uuid.uuid4().hex}"
    queue: list[dict[str, Any]] = []
    for folder in FOLDERS:
        for path in scanned[folder]:
            data = stable_snapshot(path)
            _, rows = parsed_rows(data, folder, path.name)
            queue.extend(write_semantic_chunks(
                folder=folder, path=path, start=0, rows=rows,
                cycle_id=generation_id, kind="full", spool_root=spool_root,
                chunk_chars=chunk_chars, context_rows=context_rows,
            ))
    state["full_generation"] = {
        "id": generation_id, "started_at": utc_now(),
        "initial_chunks": len(queue), "queue": queue,
    }


def queue_for_kind(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "delta":
        return state["delta_queue"]
    generation = state.get("full_generation")
    return generation["queue"] if generation else []


def choose_queue_kind(state: dict[str, Any]) -> str | None:
    delta = bool(state["delta_queue"])
    full = bool(queue_for_kind(state, "full"))
    if delta and full:
        return state.get("next_queue_kind", "delta")
    if delta:
        return "delta"
    if full:
        return "full"
    return None


def select_chunks(
    queue: list[dict[str, Any]], start_folder: int, max_folders: int, max_chars: int
) -> tuple[list[dict[str, Any]], int]:
    if not queue:
        return [], start_folder
    order = list(FOLDERS[start_folder:]) + list(FOLDERS[:start_folder])
    pending_folders = {item["folder"] for item in queue}
    folders = [folder for folder in order if folder in pending_folders][:max_folders]
    per_folder = max(1, max_chars // max(1, len(folders)))
    selected: list[dict[str, Any]] = []
    for folder in folders:
        used = 0
        for item in queue:
            if item["folder"] != folder:
                continue
            if used and used + item["new_chars"] > per_folder:
                break
            selected.append(item)
            used += item["new_chars"]
            if used >= per_folder:
                break
    next_index = (FOLDERS.index(folders[-1]) + 1) % len(FOLDERS) if folders else start_folder
    return selected, next_index


def make_batches(folder_feeds: dict[str, str], max_batches: int) -> list[list[str]]:
    folders = [folder for folder in FOLDERS if folder_feeds.get(folder)]
    if not folders:
        return []
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(min(max_batches, len(folders)))]
    for folder in sorted(folders, key=lambda name: len(folder_feeds[name]), reverse=True):
        index = min(range(len(bins)), key=lambda candidate: bins[candidate][0])
        size, names = bins[index]
        bins[index] = (size + len(folder_feeds[folder]), names + [folder])
    return [names for _, names in bins if names]


def verified_backlog_text(state: dict[str, Any]) -> str:
    text = ""
    for item in state.get("weekly_backlog", []):
        if not verify_file(item["aggregate_feed"], item.get("aggregate_sha256")):
            raise ValueError(f"weekly backlog spool missing or corrupt: {item['aggregate_feed']}")
        text += pathlib.Path(item["aggregate_feed"]).read_text(encoding="utf-8")
    return text


def upgrade_plan_to_weekly(plan: dict[str, Any], state: dict[str, Any], plan_path: pathlib.Path) -> None:
    if plan.get("weekly"):
        return
    weekly_path = pathlib.Path(plan["aggregate_feed"]).with_name("weekly-all-folders.txt")
    current = pathlib.Path(plan["aggregate_feed"]).read_text(encoding="utf-8")
    digest = atomic_text(weekly_path, verified_backlog_text(state) + current)
    plan["weekly"] = True
    plan["weekly_aggregate_feed"] = str(weekly_path)
    plan["weekly_aggregate_sha256"] = digest
    plan["weekly_consumes"] = [item["run_id"] for item in state["weekly_backlog"]]
    if current.strip() != "NO_REDUCED_TURNS":
        plan["weekly_consumes"].append(plan["run_id"])
    atomic_json(plan_path, plan)


def backlog_metrics(state: dict[str, Any], max_chars: int, max_folders: int) -> dict[str, Any]:
    queues = (
        list(state["delta_queue"]) + list(state["next_delta_queue"])
        + list(queue_for_kind(state, "full"))
    )
    total_chars = sum(item["new_chars"] for item in queues)
    folders = {item["folder"] for item in queues}
    char_runs = math.ceil(total_chars / max_chars) if total_chars else 0
    folder_runs = math.ceil(len(folders) / max_folders) if folders else 0
    chunk_runs = math.ceil(len(queues) / max_folders) if queues else 0
    oldest = min((item["captured_at"] for item in queues), default=None)
    age_hours = None
    if oldest:
        at = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
        age_hours = round((datetime.now(timezone.utc) - at).total_seconds() / 3600, 1)
    return {
        "chunks": len(queues), "new_chars": total_chars,
        "oldest_captured_at": oldest, "oldest_age_hours": age_hours,
        "estimated_successful_runs": max(char_runs, folder_runs, chunk_runs),
    }


def write_plan_feeds(
    selected: list[dict[str, Any]], run_dir: pathlib.Path, state: dict[str, Any], weekly: bool
) -> dict[str, Any]:
    folder_text: dict[str, str] = {}
    for item in selected:
        if not verify_file(item["feed_path"], item["feed_sha256"]):
            raise ValueError(f"queued semantic chunk missing or corrupt: {item['feed_path']}")
        folder_text.setdefault(item["folder"], "")
        folder_text[item["folder"]] += pathlib.Path(item["feed_path"]).read_text(encoding="utf-8")
    feeds: dict[str, str] = {}
    digests: dict[str, str] = {}
    for folder, text in folder_text.items():
        path = run_dir / f"{folder}.txt"
        feeds[folder] = str(path)
        digests[str(path)] = atomic_text(path, text)
    aggregate = run_dir / "all-folders.txt"
    aggregate_text = "".join(folder_text.values()) or "NO_REDUCED_TURNS\n"
    aggregate_digest = atomic_text(aggregate, aggregate_text)
    weekly_path = run_dir / "weekly-all-folders.txt"
    weekly_text = verified_backlog_text(state) + aggregate_text if weekly else aggregate_text
    weekly_digest = atomic_text(weekly_path, weekly_text)
    return {
        "feeds": feeds, "feed_sha256": digests,
        "aggregate_feed": str(aggregate), "aggregate_sha256": aggregate_digest,
        "weekly_aggregate_feed": str(weekly_path),
        "weekly_aggregate_sha256": weekly_digest,
    }


def command_plan(args: argparse.Namespace) -> int:
    state_path = pathlib.Path(args.state)
    plan_path = pathlib.Path(args.plan)
    state = load_state(state_path)
    full_scan = scan_sources()
    scanned = full_scan
    date_from = getattr(args, "date_from", None)
    date_to = getattr(args, "date_to", None)
    window_start, window_end = date_window(date_from, date_to)
    window = None
    if window_start or window_end:
        scanned = filter_sources_by_mtime(scanned, window_start, window_end)
        window = {
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
        }
    if not state_path.exists():
        if window:
            if window_start is not None:
                seed_bootstrap(
                    state, full_scan, window_start, "operator_date_window"
                )
                state["bootstrap"].update(window)
            else:
                # A --to-only window has no pre-window history to freeze.
                state["bootstrap"] = {
                    "source": "operator_date_window", **window, "created_at": utc_now(),
                }
        else:
            cutoff, source = bootstrap_cutoff(
                pathlib.Path(args.success_log_dir), args.bootstrap_safety_hours
            )
            seed_bootstrap(state, scanned, cutoff, source)
    spool_root = pathlib.Path(args.run_root) / "queue"
    chunk_chars = max(1, args.max_reduced_chars // args.max_folders)
    capture_delta_cycle(
        state, scanned, spool_root, chunk_chars, args.context_rows,
        prune_missing=window is None,
    )
    if args.full:
        start_full_generation(state, scanned, spool_root, chunk_chars, args.context_rows)
    # Capture state is independent of semantic-plan success. Persist it before
    # possibly resuming the old plan so post-frontier rows are never exposed to
    # source deletion while a budget-failed plan waits for retry.
    atomic_json(state_path, state)
    if plan_path.exists():
        old = json.loads(plan_path.read_text(encoding="utf-8"))
        if state.get("last_successful_run") != old.get("run_id") and verify_plan_feeds(old):
            if window is None or old.get("date_window") == window:
                if args.weekly and not old.get("weekly"):
                    upgrade_plan_to_weekly(old, state, plan_path)
                    print(f"delta_plan: upgraded pending run={old['run_id']} to weekly")
                else:
                    print(f"delta_plan: resume run={old['run_id']} (uncommitted durable plan)")
                return 0

    kind = choose_queue_kind(state)
    queue = queue_for_kind(state, kind) if kind else []
    if window:
        candidate_paths = {
            str(path) for paths in scanned.values() for path in paths
        }
        queue = [item for item in queue if item.get("source_path") in candidate_paths]
    selected, next_index = select_chunks(
        queue, int(state.get("next_folder_index", 0)), args.max_folders,
        args.max_reduced_chars,
    )
    run_id = uuid.uuid4().hex
    run_dir = pathlib.Path(args.run_root) / "plans" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    feed_info = write_plan_feeds(selected, run_dir, state, args.weekly)
    metrics = backlog_metrics(state, args.max_reduced_chars, args.max_folders)
    batch_inputs = {
        folder: pathlib.Path(path).read_text(encoding="utf-8")
        for folder, path in feed_info["feeds"].items()
    }
    plan = {
        "version": VERSION, "run_id": run_id, "generated_at": utc_now(),
        "state_path": str(state_path), "queue_kind": kind, "weekly": args.weekly,
        "selected_chunk_ids": [item["id"] for item in selected],
        "selected_folders": list(dict.fromkeys(item["folder"] for item in selected)),
        "next_folder_index": next_index,
        "batches": make_batches(batch_inputs, args.max_batches),
        "weekly_consumes": [item["run_id"] for item in state["weekly_backlog"]]
        + ([run_id] if args.weekly and selected else []),
        "backlog": metrics, "sla_target_runs": args.sla_target_runs,
        "sla_alert": metrics["estimated_successful_runs"] > args.sla_target_runs,
        "limits": {
            "max_folders": args.max_folders, "max_batches": args.max_batches,
            "max_reduced_chars": args.max_reduced_chars,
        },
        **feed_info,
    }
    if window:
        plan["date_window"] = window
    atomic_json(plan_path, plan)
    alert = " ALERT_SLA" if plan["sla_alert"] else ""
    print(
        f"delta_plan: run={run_id} kind={kind} selected={len(selected)} "
        f"backlog={metrics['chunks']} estimated_runs={metrics['estimated_successful_runs']}{alert}"
    )
    return 0


def command_accept_result(args: argparse.Namespace) -> int:
    plan = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
    if not result_is_success(pathlib.Path(args.result), plan["run_id"]):
        raise ValueError("compiler result missing exact plan-bound success receipt")
    if not verify_plan_feeds(plan):
        raise ValueError("planned reduced feed is missing or its SHA-256 changed")
    atomic_json(pathlib.Path(args.marker), {"version": VERSION, "run_id": plan["run_id"]})
    return 0


def command_commit(args: argparse.Namespace) -> int:
    plan = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
    marker_path = pathlib.Path(args.marker)
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
    if marker.get("run_id") != plan.get("run_id") or not verify_plan_feeds(plan):
        raise ValueError("completion marker/feed does not match plan")
    state_path = pathlib.Path(plan["state_path"])
    state = load_state(state_path)
    selected = set(plan["selected_chunk_ids"])
    kind = plan.get("queue_kind")
    if kind == "delta":
        state["delta_queue"] = [item for item in state["delta_queue"] if item["id"] not in selected]
        promote_next_delta_cycle(state)
    elif kind == "full" and state.get("full_generation"):
        generation = state["full_generation"]
        generation["queue"] = [item for item in generation["queue"] if item["id"] not in selected]
        if not generation["queue"]:
            state["last_full_generation"] = {
                "id": generation["id"], "started_at": generation["started_at"],
                "completed_at": utc_now(), "chunks": generation["initial_chunks"],
            }
            state["full_generation"] = None
    state["next_folder_index"] = plan["next_folder_index"]
    state["next_queue_kind"] = "full" if kind == "delta" else "delta"
    current = pathlib.Path(plan["aggregate_feed"]).read_text(encoding="utf-8")
    if selected and current.strip() != "NO_REDUCED_TURNS":
        state["weekly_backlog"].append({
            "run_id": plan["run_id"], "aggregate_feed": plan["aggregate_feed"],
            "aggregate_sha256": plan["aggregate_sha256"],
        })
    if plan.get("weekly"):
        consumed = set(plan["weekly_consumes"])
        state["weekly_backlog"] = [
            item for item in state["weekly_backlog"] if item["run_id"] not in consumed
        ]
    state["last_successful_run"] = plan["run_id"]
    state["last_successful_at"] = utc_now()
    atomic_json(state_path, state)
    marker_path.unlink(missing_ok=True)
    print(f"delta_commit: run={plan['run_id']} acknowledged_chunks={len(selected)}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--state", required=True)
    plan.add_argument("--plan", required=True)
    plan.add_argument("--run-root", required=True)
    plan.add_argument("--max-folders", type=int, default=4)
    plan.add_argument("--max-batches", type=int, default=3)
    plan.add_argument("--max-reduced-chars", type=int, default=240_000)
    plan.add_argument("--context-rows", type=int, default=8)
    plan.add_argument("--sla-target-runs", type=int, default=3)
    plan.add_argument("--bootstrap-safety-hours", type=int, default=48)
    plan.add_argument("--success-log-dir", default="/home/claude/logs/bubble-wiki")
    plan.add_argument(
        "--from", dest="date_from", type=calendar_date, metavar="YYYY-MM-DD",
        help="include transcripts modified on or after this UTC date",
    )
    plan.add_argument(
        "--to", dest="date_to", type=calendar_date, metavar="YYYY-MM-DD",
        help="include transcripts modified on or before this UTC date",
    )
    plan.add_argument("--weekly", action="store_true")
    plan.add_argument("--full", action="store_true")
    plan.set_defaults(func=command_plan)
    accept = commands.add_parser("accept-result")
    accept.add_argument("--plan", required=True)
    accept.add_argument("--marker", required=True)
    accept.add_argument("--result", required=True)
    accept.set_defaults(func=command_accept_result)
    commit = commands.add_parser("commit")
    commit.add_argument("--plan", required=True)
    commit.add_argument("--marker", required=True)
    commit.set_defaults(func=command_commit)
    return root


def main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.command == "plan":
        if min(args.max_folders, args.max_batches, args.max_reduced_chars) < 1:
            raise SystemExit("limits must be positive")
        try:
            date_window(args.date_from, args.date_to)
        except ValueError as exc:
            argument_parser.error(str(exc))
        state_path = pathlib.Path(args.state)
    else:
        plan = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
        state_path = pathlib.Path(plan["state_path"])
    with exclusive_lock(state_path.parent / ".planner.lock"):
        return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
