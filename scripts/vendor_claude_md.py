#!/usr/bin/env python3
"""vendor_claude_md — single-source the fleet CLAUDE.md doctrine snippets (#1318).

THE PROBLEM IT SOLVES
---------------------
Fleet doctrine that must appear in EVERY agent's CLAUDE.md (the operator-intent
alignment directives, the standard session-start wiki-read block) has no single
source: it was hand-copied per agent, so it drifted / went missing per machine
(the #917 / #1314 failure mode — Géraldine had ZERO wiki, Ellie thin, and the
intent-consult directive was absent fleet-wide).

THE MECHANISM (mirrors vendor-dept-libs.sh: framework = single source of truth)
-------------------------------------------------------------------------------
The canonical text lives ONCE, in this repo, under `shared/snippets/<name>.md`.
This tool inserts/updates a DELIMITED block in a target CLAUDE.md, rendered from
that canonical file. The block is machine-managed (DO NOT EDIT by hand); re-running
the tool refreshes it, and `--check` fails if a deployed block has drifted from the
canonical source — so a reverted or hand-edited copy is caught, not trusted.

The directive text still physically appears in each CLAUDE.md (an agent reads its
CLAUDE.md at session start — there is no include mechanism), exactly like the
on-disk vendored libs: many copies on disk, ONE canonical source, drift self-caught.

Usage:
    vendor_claude_md.py apply  --snippet <name> [<name>...] <target-CLAUDE.md>
    vendor_claude_md.py check  --snippet <name> [<name>...] <target-CLAUDE.md>
    vendor_claude_md.py apply  --manifest <fleet/claude-md-targets.yaml> --root <hostroot>
    vendor_claude_md.py check  --manifest <fleet/claude-md-targets.yaml> --root <hostroot>

`apply` writes the file; `check` is read-only and exits non-zero on drift/absence.
Idempotent: applying twice is a no-op. Snippet source dir is resolved relative to
this script (`<repo>/shared/snippets/`) unless $BUBBLE_SNIPPETS_DIR overrides it.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SELF = Path(__file__).resolve()
DEFAULT_SNIPPETS_DIR = SELF.parent.parent / "shared" / "snippets"

BEGIN = "<!-- BEGIN VENDORED:{name} source:bubble-ops-loop/shared/snippets/{name}.md — DO NOT EDIT; regenerate via scripts/vendor_claude_md.py -->"
END = "<!-- END VENDORED:{name} -->"


def snippets_dir() -> Path:
    return Path(os.environ.get("BUBBLE_SNIPPETS_DIR", str(DEFAULT_SNIPPETS_DIR)))


def load_snippet(name: str) -> str:
    src = snippets_dir() / f"{name}.md"
    if not src.is_file():
        raise FileNotFoundError(f"canonical snippet not found: {src}")
    return src.read_text(encoding="utf-8").strip("\n")


def render_block(name: str) -> str:
    body = load_snippet(name)
    return f"{BEGIN.format(name=name)}\n{body}\n{END.format(name=name)}"


def block_re(name: str) -> re.Pattern:
    return re.compile(
        re.escape(BEGIN.format(name=name)) + r".*?" + re.escape(END.format(name=name)),
        re.DOTALL,
    )


def upsert(text: str, name: str) -> str:
    """Insert or replace the named vendored block. Idempotent."""
    block = render_block(name)
    rx = block_re(name)
    if rx.search(text):
        return rx.sub(lambda _m: block, text)
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + "\n" + block + "\n"


def current_block(text: str, name: str) -> str | None:
    m = block_re(name).search(text)
    return m.group(0) if m else None


def apply_file(path: Path, names: list[str]) -> bool:
    text = path.read_text(encoding="utf-8")
    new = text
    for name in names:
        new = upsert(new, name)
    if new != text:
        path.write_text(new, encoding="utf-8")
        return True
    return False


def check_file(path: Path, names: list[str]) -> list[str]:
    """Return a list of problems (empty = OK)."""
    problems: list[str] = []
    if not path.is_file():
        return [f"{path}: MISSING file"]
    text = path.read_text(encoding="utf-8")
    for name in names:
        cur = current_block(text, name)
        want = render_block(name)
        if cur is None:
            problems.append(f"{path}: block '{name}' ABSENT")
        elif cur != want:
            problems.append(f"{path}: block '{name}' DRIFTED from canonical")
    return problems


def load_manifest(manifest: Path) -> list[dict]:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("vendor_claude_md: PyYAML required for --manifest mode")
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return data.get("targets", [])


def run_manifest(mode: str, manifest: Path, root: Path | None) -> int:
    targets = load_manifest(manifest)
    rc = 0
    for t in targets:
        # host_local targets are resolved only when their root is reachable.
        rel = t.get("path")
        names = t.get("snippets", [])
        if not rel or not names:
            continue
        base = root if root is not None else Path("/")
        path = (base / rel).expanduser() if root is not None else Path(rel).expanduser()
        if mode == "check":
            if not path.is_file():
                print(f"SKIP (not on this host): {path}")
                continue
            probs = check_file(path, names)
            for p in probs:
                print(f"DRIFT: {p}")
                rc = 1
            if not probs:
                print(f"OK: {path} [{', '.join(names)}]")
        else:
            if not path.is_file():
                print(f"SKIP (not on this host): {path}")
                continue
            changed = apply_file(path, names)
            print(f"{'UPDATED' if changed else 'unchanged'}: {path} [{', '.join(names)}]")
    return rc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Vendor fleet doctrine snippets into CLAUDE.md")
    ap.add_argument("mode", choices=["apply", "check"])
    ap.add_argument("--snippet", default="", help="snippet name(s), comma-separated")
    ap.add_argument("--manifest", help="fleet manifest yaml (targets mode)")
    ap.add_argument("--root", help="host root to resolve manifest relative paths against")
    ap.add_argument("target", nargs="?", help="target CLAUDE.md path (single-file mode)")
    # parse_known_args keeps us order-tolerant: an argparse optional-positional
    # (`target`) placed after a value option is otherwise fragile (unrecognized-arg).
    a, extras = ap.parse_known_args(argv)
    if a.target is None and extras:
        a.target, extras = extras[0], extras[1:]
    if extras:
        ap.error(f"unrecognized arguments: {' '.join(extras)}")
    names = [s.strip() for s in a.snippet.split(",") if s.strip()]

    if a.manifest:
        return run_manifest(a.mode, Path(a.manifest), Path(a.root) if a.root else None)

    if not names or not a.target:
        ap.error("single-file mode needs --snippet NAME[,NAME...] and a target path")
    path = Path(a.target).expanduser()

    if a.mode == "check":
        probs = check_file(path, names)
        for p in probs:
            print(f"DRIFT: {p}")
        if probs:
            return 1
        print(f"OK: {path} [{', '.join(names)}]")
        return 0

    if not path.is_file():
        sys.exit(f"vendor_claude_md: target not found: {path}")
    changed = apply_file(path, names)
    print(f"{'UPDATED' if changed else 'unchanged'}: {path} [{', '.join(names)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
