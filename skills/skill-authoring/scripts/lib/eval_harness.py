#!/usr/bin/env python3
"""eval_harness.py — ARM A eval-with/without gate for the skill-authoring agent.

THE ANTI-BLOAT MECHANISM (#1222, Joris: "eval-gated: only author if it
demonstrably helps"). For a DRAFT candidate skill, run each probe task twice —
once WITHOUT the draft and once WITH it injected — and produce the paired
outputs so the AGENT can judge whether the draft measurably helped. If it
doesn't help, the agent discards it. This is what keeps the registry from
bloating with skills that read well but change nothing.

DOCTRINE SPLIT (tools-as-evidence / agent-as-judgment):
  - This harness MECHANICALLY produces the paired WITH/WITHOUT transcripts.
    It does NOT decide pass/fail — a naive string-diff "helped?" heuristic
    would be exactly the deterministic-judgment anti-pattern. The agent reads
    both outputs and rules.
  - "WITH" injects the draft SKILL.md body via --append-system-prompt (a cheap,
    isolated way to make the know-how available to that one run without
    registering the skill fleet-wide). "WITHOUT" is the identical prompt with no
    injection. Same model, same probe → the only variable is the draft.

COST: bounded — 2 cheap (Haiku) runs per probe, a handful of probes, only for
the few candidates that survived dedupe. --dry-run plans the runs without
spending a token (used by CI and when no draft has passed the earlier gates).

Import-safe; --dry-run needs no live `claude`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

WITH_PREAMBLE = (
    "You have the following SKILL available and should use it if relevant to the "
    "task. Treat it as loaded know-how:\n\n----- BEGIN SKILL -----\n{body}\n"
    "----- END SKILL -----\n"
)


def load_probes(probes_path: str | None) -> list[dict]:
    """Probes are a JSON list of {"id":..., "prompt":...}. The AGENT generates
    these from the mined examples (ARM A step 4) and writes them to a file; this
    harness just executes them. With no file, returns [] (nothing to eval)."""
    if not probes_path:
        return []
    p = Path(probes_path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return []
    if isinstance(data, list):
        out = []
        for i, item in enumerate(data):
            if isinstance(item, dict) and item.get("prompt"):
                out.append({"id": item.get("id", f"probe{i+1}"), "prompt": item["prompt"]})
        return out
    return []


def build_claude_cmd(model: str, prompt: str, append_system: str | None) -> list[str]:
    """Construct the headless claude invocation. Mirrors cloud-wiki-compile.sh:
    --print, --no-session-persistence, budget-capped, permissions skipped."""
    cmd = [
        "claude", "--print", "--no-session-persistence",
        "--setting-sources", "user", "--model", model,
        "--max-budget-usd", "1.00", "--output-format", "text",
        "--dangerously-skip-permissions",
    ]
    if append_system:
        cmd += ["--append-system-prompt", append_system]
    cmd += [prompt]
    return cmd


def run_one(cmd: list[str], timeout: int) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": "timeout"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"rc": 1, "stdout": "", "stderr": str(e)}


def eval_draft(
    draft_path: str,
    probes: list[dict],
    model: str,
    out_dir: str,
    timeout: int = 180,
    dry_run: bool = False,
) -> dict:
    """Run WITH/WITHOUT for each probe. Returns a structured result dict and,
    unless dry_run, writes per-probe output pairs under out_dir."""
    draft = Path(draft_path)
    body = draft.read_text(errors="replace") if draft.is_file() else ""
    if not body and not dry_run:
        return {"error": f"draft SKILL.md not readable: {draft_path}", "pairs": []}
    with_system = WITH_PREAMBLE.format(body=body)
    out = Path(out_dir)
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)

    pairs = []
    for probe in probes:
        pid = probe["id"]
        prompt = probe["prompt"]
        without_cmd = build_claude_cmd(model, prompt, None)
        with_cmd = build_claude_cmd(model, prompt, with_system)
        if dry_run:
            pairs.append({
                "probe": pid,
                "without_cmd": without_cmd[:6] + ["…", prompt[:60]],
                "with_cmd": with_cmd[:6] + ["…(+append-system-prompt draft)…", prompt[:60]],
                "planned": True,
            })
            continue
        without_res = run_one(without_cmd, timeout)
        with_res = run_one(with_cmd, timeout)
        (out / f"{pid}.without.txt").write_text(without_res["stdout"])
        (out / f"{pid}.with.txt").write_text(with_res["stdout"])
        pairs.append({
            "probe": pid,
            "prompt": prompt,
            "without_rc": without_res["rc"],
            "with_rc": with_res["rc"],
            "without_out": str(out / f"{pid}.without.txt"),
            "with_out": str(out / f"{pid}.with.txt"),
            "without_len": len(without_res["stdout"]),
            "with_len": len(with_res["stdout"]),
        })
    return {
        "draft": draft_path,
        "model": model,
        "probe_count": len(probes),
        "dry_run": dry_run,
        "pairs": pairs,
        "judgment_note": (
            "AGENT judges: read each probe's .with vs .without output and decide "
            "whether the draft MEASURABLY improved the task (correctness, fewer "
            "missteps, right recipe). Author ONLY if it demonstrably helps on the "
            "majority of probes; otherwise DISCARD (anti-bloat). Do not author on "
            "a tie."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Eval a draft skill WITH vs WITHOUT (ARM A anti-bloat gate).")
    ap.add_argument("--draft", required=True, help="Path to the draft SKILL.md.")
    ap.add_argument("--probes", default=None, help="JSON file: [{id,prompt},...] generated by the agent from mined examples.")
    ap.add_argument("--model", default="haiku", help="Cheap model for the eval runs (default haiku).")
    ap.add_argument("--out", default=None, help="Output dir for the paired transcripts (default: a temp dir).")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--dry-run", action="store_true", help="Plan the runs without spending tokens (CI-safe).")
    args = ap.parse_args(argv)

    out_dir = args.out or tempfile.mkdtemp(prefix="skill-eval-")
    probes = load_probes(args.probes)
    if not probes and not args.dry_run:
        print(json.dumps({"error": "no probes provided — agent must generate probes first", "draft": args.draft}))
        return 0
    result = eval_draft(args.draft, probes, args.model, out_dir, args.timeout, args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
