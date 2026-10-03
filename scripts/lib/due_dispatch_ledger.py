"""Append Mac due-dispatch successes without replacing the VPS dispatch ledger."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid
from zoneinfo import ZoneInfo

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _owned_regular(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError("refusing nonregular or foreign-owned due-dispatch file")


def _check_file(fd: int, name: str) -> bool:
    try:
        info = os.lstat(name, dir_fd=fd)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError("refusing nonregular or foreign-owned due-dispatch file")
    return True


def append_success(dept_dir: Path, mission: str, period: str,
                   completed_at: dt.datetime, leased_at: dt.datetime | None = None) -> None:
    """Serialize read/append/replace through pinned, owner-only output paths.

    Completion-only runs have no invented start time. Identical acknowledgements
    are idempotent; distinct continuous acknowledgements retain their history.
    """
    def stamp(value):
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    record = dict(mission_id=mission, period=period, completed_at=stamp(completed_at))
    if leased_at is not None:
        record["dispatched_at"] = stamp(leased_at)
        record["leased_at"] = stamp(leased_at)
    date = completed_at.astimezone(ZoneInfo("Europe/Paris")).date().isoformat()
    path = Path(os.path.abspath(dept_dir))
    descriptors = []
    temporary = None
    try:
        fd = os.open(path.anchor, DIR_FLAGS)
        descriptors.append(fd)
        for part in path.parts[1:]:
            fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            descriptors.append(fd)
        if os.fstat(fd).st_uid != os.geteuid():
            raise OSError("refusing foreign-owned department")
        for part in ("outputs", date):
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            descriptors.append(fd)
            if os.fstat(fd).st_uid != os.geteuid():
                raise OSError("refusing foreign-owned output directory")
        name = "due-dispatch.json"
        lock_name = ".due-dispatch.lock"
        _check_file(fd, lock_name)
        lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            lock_fd = os.open(lock_name, lock_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        except FileExistsError:
            lock_fd = os.open(lock_name, lock_flags, dir_fd=fd)
        descriptors.append(lock_fd)
        _owned_regular(lock_fd)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        records = []
        if _check_file(fd, name):
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(handle, "r", encoding="utf-8") as stream:
                _owned_regular(stream.fileno())
                records = json.load(stream)
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise ValueError("invalid due-dispatch ledger")
        if any(all(previous.get(key) == record[key]
                   for key in ("mission_id", "period", "completed_at")) for previous in records):
            return
        records.append(record)
        temporary = f".due-dispatch.{uuid.uuid4().hex}.tmp"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(records, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _check_file(fd, name)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        temporary = None
        os.fsync(fd)
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        finally:
            for handle in reversed(descriptors):
                os.close(handle)
