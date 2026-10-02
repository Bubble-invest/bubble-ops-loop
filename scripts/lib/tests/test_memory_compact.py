"""#1665 - memory_compact: lossless, capped, idempotent, atomic, refuses when unsafe."""
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memory_compact as mc  # noqa: E402

TODAY = dt.date(2026, 10, 2)


def make(n_old=40, n_new=3, extra_tail=True, pinned=False):
    L = ["# WORKING_MEMORY - T\n", "\n", "> intro\n", "\n", "## Active topics\n", "\n",
         "<!-- comment -->\n", "\n"]
    for i in range(n_new):
        L.append("- [2026-10-01 10:0%d] **recent %d** %s\n  continuation\n" % (i, i, "x" * 100))
    if pinned:
        L.append("- [2026-06-01] [pin] **standing rule** keep me\n  more\n")
    L.append("- undated entry stays\n")
    for i in range(n_old):
        L.append("- [2026-08-%02d] **old %d** %s\n  cont é à\n\n" % (i % 28 + 1, i, "y" * 300))
    L.append("\n## Archive\n\n<!-- Completed -->\n\n")
    if extra_tail:
        L.append("## 2026-06-24 - old section\n- verified stuff\n")
    return "".join(L)


def lines(s):
    return Counter(l for l in s.splitlines() if l.strip())


def run(tmp, text, cap=4000, **kw):
    wm = tmp / "WORKING_MEMORY.md"
    wm.write_text(text, encoding="utf-8")
    return wm, mc.compact(wm, cap, TODAY, **kw)


def archive_text(tmp):
    return (tmp / "memory/archive/WORKING_MEMORY-2026-10.md").read_text(encoding="utf-8")


def test_lossless_cap_pointer(tmp_path):
    orig = make()
    wm, (rc, res) = run(tmp_path, orig)
    assert rc == 0 and res["moved_entries"] > 0
    new = wm.read_text(encoding="utf-8")
    assert len(new.encode()) <= 4000 and res["after"] == len(new.encode())
    assert "memory/archive/WORKING_MEMORY-*.md" in new          # pointer present
    # lossless: every original line is in active or archive, with equal multiplicity
    got = lines(new) + lines(archive_text(tmp_path))
    pointer = Counter({l: 1 for l in new.splitlines() if l.startswith("> Older entries")})
    got = got - pointer
    for l in list(got):
        if l.startswith("<!-- memory_compact") or l.startswith("## Compacted") or l.startswith("### Former"):
            del got[l]
    assert got == lines(orig)
    assert "undated entry stays" in new and "recent 0" in new


def test_pin_survives(tmp_path):
    wm, (rc, _) = run(tmp_path, make(pinned=True))
    assert "standing rule" in wm.read_text(encoding="utf-8")


def test_idempotent(tmp_path):
    wm, (rc, _) = run(tmp_path, make())
    first = wm.read_bytes()
    arch = (tmp_path / "memory/archive/WORKING_MEMORY-2026-10.md").read_bytes()
    rc2, res2 = mc.compact(wm, 4000, TODAY)
    assert rc2 == 0 and wm.read_bytes() == first
    assert (tmp_path / "memory/archive/WORKING_MEMORY-2026-10.md").read_bytes() == arch
    # even forced (cap below size) a second pass has nothing movable -> no archive growth
    rc3, _ = mc.compact(wm, 10, TODAY)
    assert rc3 == 3 and (tmp_path / "memory/archive/WORKING_MEMORY-2026-10.md").read_bytes() == arch


def test_archive_is_append_only(tmp_path):
    arch_dir = tmp_path / "memory/archive"
    arch_dir.mkdir(parents=True)
    pre = "# earlier content\n- keep this\n"
    (arch_dir / "WORKING_MEMORY-2026-10.md").write_text(pre, encoding="utf-8")
    run(tmp_path, make())
    assert archive_text(tmp_path).startswith(pre)


def test_crash_between_archive_and_wm_is_not_lossy_and_retry_no_dupes(tmp_path, monkeypatch):
    wm = tmp_path / "WORKING_MEMORY.md"
    orig = make()
    wm.write_text(orig, encoding="utf-8")
    real = mc._atomic_write
    calls = []

    def boom(path, data, mode=None):
        calls.append(path.name)
        if path.name == "WORKING_MEMORY.md":
            raise OSError("disk full")
        return real(path, data, mode)
    monkeypatch.setattr(mc, "_atomic_write", boom)
    rc, res = mc.compact(wm, 4000, TODAY)
    assert rc == 2 and wm.read_text(encoding="utf-8") == orig       # untouched
    monkeypatch.setattr(mc, "_atomic_write", real)
    one = archive_text(tmp_path)
    rc, _ = mc.compact(wm, 4000, TODAY)
    assert rc == 0 and archive_text(tmp_path) == one                  # hash marker: no dupes
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".mc-")]


@pytest.mark.parametrize("bad", [
    "no headings at all\n" * 500,
    "## Active topics\n- a\n## Active topics\n- b\n" + "z" * 5000,
    "## Active topics\n- [2026-01-01] a\n## Surprise\nstuff\n" + "z" * 5000,
    "## Active topics\n```\n- [2026-01-01] a\n" + "z" * 5000,
])
def test_refuses_unparseable(tmp_path, bad):
    wm, (rc, res) = run(tmp_path, bad)
    assert rc == 2 and res["status"] == "refused"
    assert wm.read_text(encoding="utf-8") == bad and not (tmp_path / "memory").exists()


def test_refuses_non_utf8(tmp_path):
    wm = tmp_path / "WORKING_MEMORY.md"
    wm.write_bytes(b"## Active topics\n- \xff\xfe\n" * 400)
    rc, res = mc.compact(wm, 100, TODAY)
    assert rc == 2 and not (tmp_path / "memory").exists()


def test_within_cap_is_noop(tmp_path):
    t = make(n_old=1, n_new=1, extra_tail=False)
    wm, (rc, res) = run(tmp_path, t, cap=100000)
    assert rc == 0 and wm.read_text(encoding="utf-8") == t and not (tmp_path / "memory").exists()


def test_over_cap_when_only_pinned_returns_3(tmp_path):
    t = "## Active topics\n" + "".join("- [2026-01-01] [pin] %s\n" % ("p" * 200) for _ in range(30))
    wm, (rc, res) = run(tmp_path, t, cap=1000)
    assert rc == 3 and wm.read_text(encoding="utf-8") == t


def test_dry_run_changes_nothing(tmp_path):
    t = make()
    wm, (rc, res) = run(tmp_path, t, dry_run=True)
    assert res["status"] == "dry_run" and wm.read_text(encoding="utf-8") == t
    assert not (tmp_path / "memory").exists()


def test_cli_cap_from_dept_yaml(tmp_path, capsys):
    wm = tmp_path / "WORKING_MEMORY.md"
    wm.write_text(make(), encoding="utf-8")
    y = tmp_path / "dept.yaml"
    y.write_text("working_memory_cap_bytes: 5000\n")
    rc = mc.main([str(wm), "--dept-yaml", str(y), "--today", "2026-10-02"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["cap"] == 5000 and out["after"] <= 5000


def test_cas_abort_when_wm_changes_mid_run_then_retry_no_dupes(tmp_path, monkeypatch):
    wm = tmp_path / "WORKING_MEMORY.md"
    orig = make()
    wm.write_text(orig, encoding="utf-8")
    real = mc._atomic_write

    def racing(path, data, mode=None):
        real(path, data, mode)
        if path.name != "WORKING_MEMORY.md":      # after the archive write, someone appends
            with open(wm, "a", encoding="utf-8") as f:
                f.write("- [2026-10-02] concurrent append\n")
    monkeypatch.setattr(mc, "_atomic_write", racing)
    rc, res = mc.compact(wm, 4000, TODAY)
    assert rc == 2 and "changed during" in res["reason"]
    assert wm.read_text(encoding="utf-8") == orig + "- [2026-10-02] concurrent append\n"
    monkeypatch.setattr(mc, "_atomic_write", real)
    mc.compact(wm, 4000, TODAY)
    # appended at EOF (under ## Archive) -> moved verbatim to the archive, never lost
    assert "concurrent append" in wm.read_text(encoding="utf-8") + archive_text(tmp_path)
    assert archive_text(tmp_path).count("## Compacted") <= 2
    assert archive_text(tmp_path).count("**old 0**") == 1             # no duplicated entry
