#!/usr/bin/env python3
"""memory_compact.py - lossless, deterministic compaction of a dept's WORKING_MEMORY.md.

Board #1665. Runs INSIDE the dept (its own UID, from its existing daily
`session_handoff` L4 mission) - no transport, no new cadence.

Contract it reuses (nothing new invented):
  * WORKING_MEMORY.md.template: `## Active topics` (one dated bullet per topic,
    `- [YYYY-MM-DD ...] ...`) + `## Archive`; "move finished items to Archive;
    don't delete (audit trail)".
  * memory_hygiene_notify.py doctrine: ARCHIVE (move-only), never delete.

What it does (only when the file is over the cap):
  * keeps head, intro, and every Active entry that is PINNED (`[pin]`,
    `[standing]`, `[keep]` or `#pin` anywhere in the entry), UNDATED, or dated
    within the last KEEP_DAYS (shrinks the window 14->10->7->5->3->2->1 if still over cap);
  * moves every other entry, plus any content under `## Archive`, VERBATIM to
    `<dir of WORKING_MEMORY.md>/memory/archive/WORKING_MEMORY-YYYY-MM.md`
    (append-only; a content-hash marker makes a retry a no-op);
  * leaves one stable pointer line under `## Archive`.
No LLM judgment here: structure + dates + explicit pins only. The dept's
session_handoff agent curates (pins standing rules / open actions) BEFORE running it.

Exit codes: 0 ok (compacted or already within cap) | 2 REFUSED, nothing changed
(unparseable / unsafe) | 3 compacted but still over cap (pins alone exceed it).
Stdout: one JSON line {status,before,after,cap,moved_entries,archive,...}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

DEFAULT_CAP_BYTES = 24576          # dept.yaml `working_memory_cap_bytes` overrides
WINDOWS = (14, 10, 7, 5, 3, 2, 1)   # keep-days ladder
ACTIVE_RE = re.compile(r"^## Active topics[ \t]*\r?\n?$")
H2_RE = re.compile(r"^## ")
ARCHIVE_RE = re.compile(r"^## Archive[ \t]*\r?\n?$")
ENTRY_RE = re.compile(r"^- ")
DATE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})")
PIN_RE = re.compile(r"\[(?:pin|standing|keep)\]|#pin", re.I)
POINTER = ("> Older entries live verbatim in `memory/archive/WORKING_MEMORY-*.md` "
           "(append-only; grep before re-asking). Maintained by "
           "scripts/lib/memory_compact.py (#1665).\n")
POINTER_RE = re.compile(r"^> Older entries live verbatim in ")
COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")


class Refuse(Exception):
    pass


def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _size(s: str) -> int:
    return len(s.encode("utf-8"))


def parse(text: str):
    """-> (head, intro, entries, archive_head, tail). Concatenated == text (minus pointer)."""
    lines = _lines(text)
    ai = [i for i, l in enumerate(lines) if ACTIVE_RE.match(l)]
    if len(ai) != 1:
        raise Refuse("expected exactly one '## Active topics' heading, found %d" % len(ai))
    a = ai[0]
    end = len(lines)
    for i in range(a + 1, len(lines)):
        if H2_RE.match(lines[i]):
            end = i
            break
    if end < len(lines) and not ARCHIVE_RE.match(lines[end]):
        raise Refuse("unknown section after Active topics: %r" % lines[end].strip()[:60])
    body, rest = lines[a + 1:end], lines[end:]
    intro, entries, fence = [], [], False
    for l in body:
        if l.lstrip().startswith("```"):
            fence = not fence
        if ENTRY_RE.match(l) and not fence:
            entries.append([l])
        elif entries:
            entries[-1].append(l)
        else:
            intro.append(l)
    head = lines[:a + 1]
    arch_head, tail = [], []
    if rest:
        arch_head = [rest[0]]
        j = 1
        while j < len(rest) and (not rest[j].strip() or COMMENT_RE.match(rest[j])
                                 or POINTER_RE.match(rest[j])):
            if not POINTER_RE.match(rest[j]):
                arch_head.append(rest[j])
            j += 1
        tail = rest[j:]
    if fence:
        raise Refuse("unterminated code fence in Active topics")
    return head, intro, ["".join(e) for e in entries], arch_head, "".join(tail)


def classify(entry: str, today: dt.date, keep_days: int) -> bool:
    """True = keep in ACTIVE."""
    if PIN_RE.search(entry):
        return True
    m = DATE_RE.match(entry)
    if not m:
        return True                       # undated: cannot classify safely -> keep
    try:
        d = dt.date.fromisoformat(m.group(1))
    except ValueError:
        return True
    return (today - d).days <= keep_days


def render(head, intro, kept, arch_head) -> str:
    if arch_head:
        ah = list(arch_head)
    else:
        ah = ["## Archive\n", "\n"]
    while ah and not ah[-1].strip():
        ah.pop()
    out = "".join(head) + "".join(intro) + "".join(kept)
    if out and not out.endswith("\n"):
        out += "\n"
    if not out.endswith("\n\n"):
        out += "\n"
    return out + "".join(ah) + "\n" + POINTER


def _atomic_write(path: Path, data: str, mode: int | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mc-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def compact(wm: Path, cap: int, today: dt.date, dry_run: bool = False,
            archive_dir: Path | None = None) -> tuple[int, dict]:
    try:
        st0 = wm.stat()
        raw = wm.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return 2, {"status": "refused", "reason": "cannot read as utf-8: %s" % e}
    before = len(raw)
    res = {"status": "ok", "before": before, "after": before, "cap": cap, "moved_entries": 0}
    try:
        head, intro, entries, arch_head, tail = parse(text)
    except Refuse as e:
        return 2, {"status": "refused", "reason": str(e), "before": before}
    if before <= cap:
        res["note"] = "within cap, no change"
        return 0, res
    for keep_days in WINDOWS:
        kept = [e for e in entries if classify(e, today, keep_days)]
        new = render(head, intro, kept, arch_head)
        if _size(new) <= cap:
            break
    moved = [e for e in entries if not classify(e, today, keep_days)]
    if not moved and not tail.strip():
        res.update(status="over_cap", note="nothing movable (all pinned/undated/recent)")
        return 3, res
    body = "".join(m if m.endswith("\n") else m + "\n" for m in moved)
    if tail.strip():
        body += "\n### Former '## Archive' content (moved verbatim)\n" + tail
        if not tail.endswith("\n"):
            body += "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    adir = archive_dir or (wm.parent / "memory" / "archive")
    afile = adir / ("WORKING_MEMORY-%s.md" % today.strftime("%Y-%m"))
    res.update(moved_entries=len(moved), archive=str(afile), keep_days=keep_days,
               after=_size(new), sha=digest)
    res["moved_preview"] = [m.splitlines()[0][:90] for m in moved[:200]]
    if dry_run:
        res["status"] = "dry_run"
        return 0, res
    try:
        adir.mkdir(parents=True, exist_ok=True)
        existing = afile.read_text(encoding="utf-8") if afile.exists() else ""
        marker = "<!-- memory_compact sha256=%s -->" % digest
        if marker not in existing:
            add = "%s\n## Compacted %s (%d entries)\n\n%s" % (
                marker, today.isoformat(), len(moved), body)
            if existing and not existing.endswith("\n"):
                existing += "\n"
            _atomic_write(afile, (existing + ("\n" if existing else "") + add))
        if body not in afile.read_text(encoding="utf-8"):
            return 2, {"status": "refused", "reason": "archive verification failed; WORKING_MEMORY untouched"}
        # compare-and-swap: abort if anything touched WORKING_MEMORY.md since we read it.
        # (The archive append is idempotent via its sha marker, so a retry adds no dupes.)
        st1 = wm.stat()
        if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns) \
                or wm.read_bytes() != raw:
            return 2, {"status": "refused",
                       "reason": "WORKING_MEMORY.md changed during compaction; untouched, retry"}
        _atomic_write(wm, new, mode=st1.st_mode & 0o7777)
    except OSError as e:
        return 2, {"status": "refused", "reason": "io error: %s" % e}
    res["after"] = wm.stat().st_size
    if res["after"] > cap:
        res["status"] = "over_cap"
        return 3, res
    return 0, res


def cap_from_dept_yaml(path: Path) -> int | None:
    try:
        import yaml
        v = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("working_memory_cap_bytes")
        return int(v) if v else None
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wm", type=Path)
    ap.add_argument("--cap", type=int)
    ap.add_argument("--dept-yaml", type=Path)
    ap.add_argument("--today", help="YYYY-MM-DD (tests)")
    ap.add_argument("--archive-dir", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    cap = a.cap or (cap_from_dept_yaml(a.dept_yaml) if a.dept_yaml else None) or DEFAULT_CAP_BYTES
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    rc, res = compact(a.wm, cap, today, a.dry_run, a.archive_dir)
    print(json.dumps(res, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
