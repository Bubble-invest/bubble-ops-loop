#!/usr/bin/env python3
"""jev.py -- one CLI/library interface over swappable Jev / System-One backends.

Backends: local-decider | local-semif | local-laya | openrouter (the official
Jev, RECOMMENDED DEFAULT when the data is allowed off-box) | typesafe (the
direct API, not yet independently verified against a live account -- see
../references/engines.md). See ../SKILL.md for when to use which.

TWO wire formats, not one -- the local engines and the direct `typesafe` API
speak TypeSafe's own `/v1/systemone` protocol; `openrouter` (the officially
VERIFIED LIVE route, 230/230 calls OK on 2026-09-25 --
prototypes/jev-local/bench/results/20260925-openrouter/) speaks a different,
OpenRouter-specific endpoint and body shape. `request_url_and_payload()` is
the one place that knows the difference -- callers never branch on it.

  local-* / typesafe:
    POST <base_url>/v1/systemone
    {"state": <str-or-dict>, "questions": {"<qid>": {"type": "choice"|"score"|"noul",
                                                       "instructions": "...",
                                                       "criteria": {...}}}}
    -> {"model": "...", "answers": {"<qid>": {"type": ..., "choice": ..., "noul": ...,
                                               "probabilities": {...}, "legend": {...}}},
        "usage": {"input_tokens": ..., "output_tokens": ...}}

  openrouter (VERIFIED, see the sample raw row cited above):
    POST <base_url>/alpha/decisions
    {"model": "typesafe/jev-1.13", "state": <str-or-dict>, "questions": {...}}
    -> {"model": "typesafe/jev-1.13-...", "answers": {...same shape as above...},
        "usage": {"input_tokens": ..., "output_tokens": ..., "cost": <float USD>},
        "id": "...", "provider": "TypeSafe"}

Subcommands:
  start / stop / status   -- manage ONE local engine server (local-* backends only)
  ask                      -- score items (JSONL) against one or more questions -> JSONL,
                               tracking cumulative usage.cost per call and enforcing
                               --max-spend if set
  eval                     -- score `ask` results against a gold set -> precision/recall/
                               F1/ECE/P@1 per question, plus the trivial majority-class
                               baseline (ALWAYS compare against this -- see ../SKILL.md
                               "Mandatory rollout discipline" and ../references/eval.md)

Dependencies: stdlib + `requests` (only needed for `ask` against a real server; `eval`,
`start`, `stop`, `status` are stdlib-only). Install with the caller's own venv/pip.

API keys for openrouter/typesafe come ONLY from the environment (OPENROUTER_API_KEY /
TYPESAFE_API_KEY) -- never pass one as a CLI flag, never printed, and a missing key fails
loudly before any request is attempted.
"""
import argparse
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

N_ECE_BINS = 10
THRESHOLD_GRID = [i / 100 for i in range(5, 96, 5)]

# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

# port + the jev-local engine "name" (used for venv dir, pid file, start command)
LOCAL_BACKENDS = {
    "local-laya": {"port": 8781, "name": "laya"},
    "local-decider": {"port": 8782, "name": "decider"},
    "local-semif": {"port": 8783, "name": "semif"},
}
REMOTE_BACKENDS = {
    # "openrouter" -- VERIFIED LIVE 2026-09-25, 230/230 calls OK (see
    # prototypes/jev-local/bench/results/20260925-openrouter/{summary.md,
    # jev113_fr_set.jsonl}): a different endpoint + body shape than the
    # /v1/systemone protocol the local engines and the direct `typesafe` API
    # speak -- see request_url_and_payload() below, and the module docstring
    # for both wire formats side by side.
    "openrouter": {
        "base_url_env": "JEV_OPENROUTER_BASE",
        "default_base_url": "https://openrouter.ai/api",
        "key_env": "OPENROUTER_API_KEY",
        "path": "/alpha/decisions",
        "model": "typesafe/jev-1.13",
    },
    # "typesafe" -- the direct TypeSafe API, speaking /v1/systemone (same
    # protocol as the local engines). NOT independently verified against a
    # live account in this build (no key/network available) -- see
    # references/engines.md. Prefer `openrouter` (verified) unless you have
    # a specific reason to go direct.
    "typesafe": {
        "base_url_env": "JEV_TYPESAFE_BASE",
        "default_base_url": "https://api.typesafe.ai",
        "key_env": "TYPESAFE_API_KEY",
        "path": "/v1/systemone",
        "model": None,
    },
}
ALL_BACKENDS = list(LOCAL_BACKENDS) + list(REMOTE_BACKENDS)

# The official Jev via OpenRouter is the recommended DEFAULT backend for
# INTERNAL Bubble Invest fleet data (board cards, wiki, internal mail/ops,
# dept missions) now that the wire format is verified live and Joris cleared
# internal use (Telegram msg 9715, 2026-09-25: "for now it's internal use so
# it's ok"). EXTERNAL client data (client deliverables e.g. Gefineo/Delahaye,
# PEP-France/OpenSanctions screening subjects -- anything processed on behalf
# of a client) stays LOCAL-ONLY until EU residency/a DPA is confirmed in
# writing -- see SKILL.md "Backend choice" and references/engines.md. This
# constant is documentation, not an enforced gate -- `--backend` stays a
# required, explicit CLI flag on every subcommand precisely so a caller must
# consciously choose to send data off-box rather than silently defaulting to
# it.
RECOMMENDED_DEFAULT_BACKEND_FOR_INTERNAL_DATA = "openrouter"

DEFAULT_JEV_LOCAL_DIR = Path.home() / "claude-workspaces" / "Rick_RnD" / "prototypes" / "jev-local"


def jev_local_dir():
    env = os.environ.get("JEV_LOCAL_DIR")
    return Path(env) if env else DEFAULT_JEV_LOCAL_DIR


def pid_file(backend):
    name = LOCAL_BACKENDS[backend]["name"]
    return jev_local_dir() / f".jev-{name}.pid"


def log_file(backend):
    name = LOCAL_BACKENDS[backend]["name"]
    d = jev_local_dir() / "logs"
    return d / f"jev-{name}.log"


def port_listening(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def base_url_for(backend, override=None):
    if override:
        return override
    if backend in LOCAL_BACKENDS:
        return f"http://127.0.0.1:{LOCAL_BACKENDS[backend]['port']}"
    cfg = REMOTE_BACKENDS[backend]
    return os.environ.get(cfg["base_url_env"], cfg["default_base_url"])


def auth_headers_for(backend):
    """Remote backends only. Reads the key from env -- NEVER accepts one as a
    flag, NEVER logs/prints it. Fails clearly (not a stack trace) if unset."""
    if backend not in REMOTE_BACKENDS:
        return {}
    cfg = REMOTE_BACKENDS[backend]
    key = os.environ.get(cfg["key_env"])
    if not key:
        raise SystemExit(
            f"error: backend '{backend}' requires {cfg['key_env']} in the environment "
            f"(e.g. `export {cfg['key_env']}=...`). Refusing to call without it. "
            f"Never pass an API key as a command-line flag."
        )
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# start / stop / status  (local backends only)
# ---------------------------------------------------------------------------

def _start_command(backend):
    d = jev_local_dir()
    name = LOCAL_BACKENDS[backend]["name"]
    port = LOCAL_BACKENDS[backend]["port"]
    env = dict(os.environ)
    env["HF_HOME"] = str(d / "hf-cache")
    if backend == "local-laya":
        env["LAYA_HOST"] = "127.0.0.1"
        env["LAYA_PORT"] = str(port)
        cmd = [str(d / "laya" / ".venv" / "bin" / "laya-serve")]
    elif backend == "local-decider":
        env.setdefault("DECIDER_MODEL", "Mapika/decider-2b")
        cmd = [
            str(d / "decider" / ".venv" / "bin" / "uvicorn"), "decider.serve:app",
            "--app-dir", str(d / "decider-src"), "--host", "127.0.0.1", "--port", str(port),
        ]
    elif backend == "local-semif":
        env.setdefault("SEMIF_MLX_BITS", "4")
        env["SEMIF_HOST"] = "127.0.0.1"
        env["SEMIF_PORT"] = str(port)
        cmd = [str(d / "semif" / ".venv" / "bin" / "python"), str(d / "bench" / "semif_adapter_server.py")]
    else:
        raise ValueError(backend)
    return cmd, env, name, port


def cmd_start(args):
    backend = args.backend
    if backend not in LOCAL_BACKENDS:
        raise SystemExit(f"error: start/stop/status only apply to local backends ({', '.join(LOCAL_BACKENDS)}), got '{backend}'")

    # refuse to start a second engine -- only one at a time on a 16 GB Mac (see engines.md)
    already = [b for b in LOCAL_BACKENDS if port_listening(LOCAL_BACKENDS[b]["port"])]
    if already and backend not in already:
        raise SystemExit(
            f"error: {already[0]} is already listening on port {LOCAL_BACKENDS[already[0]]['port']}. "
            f"Only one local engine runs at a time (16 GB Mac budget) -- stop it first with "
            f"`jev.py stop --backend {already[0]}`."
        )
    if backend in already:
        print(f"{backend} already running on port {LOCAL_BACKENDS[backend]['port']}", file=sys.stderr)
        return

    d = jev_local_dir()
    if not d.exists():
        raise SystemExit(f"error: JEV_LOCAL_DIR not found: {d} (set JEV_LOCAL_DIR or install jev-local there)")

    cmd, env, name, port = _start_command(backend)
    exe = Path(cmd[0])
    if not exe.exists():
        raise SystemExit(f"error: engine executable not found: {exe} -- is {name}'s venv installed under {d}?")

    logf = log_file(backend)
    logf.parent.mkdir(parents=True, exist_ok=True)
    with open(logf, "ab") as out:
        proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=str(d))
    pid_file(backend).write_text(str(proc.pid))

    # poll for readiness rather than a fixed sleep
    deadline = time.time() + args.startup_timeout
    while time.time() < deadline:
        if port_listening(port):
            print(f"{backend} started (pid {proc.pid}), listening on 127.0.0.1:{port}. Log: {logf}")
            return
        if proc.poll() is not None:
            raise SystemExit(f"error: {backend} exited early (code {proc.returncode}) -- see {logf}")
        time.sleep(0.5)
    raise SystemExit(f"error: {backend} did not open port {port} within {args.startup_timeout}s -- see {logf}")


def cmd_stop(args):
    backend = args.backend
    if backend not in LOCAL_BACKENDS:
        raise SystemExit(f"error: start/stop/status only apply to local backends ({', '.join(LOCAL_BACKENDS)}), got '{backend}'")
    pf = pid_file(backend)
    port = LOCAL_BACKENDS[backend]["port"]
    if not pf.exists():
        if port_listening(port):
            print(f"warning: no pid file for {backend} but port {port} is listening -- "
                  f"kill it manually (lsof -ti :{port} | xargs kill)", file=sys.stderr)
        else:
            print(f"{backend} not running", file=sys.stderr)
        return
    pid = int(pf.read_text().strip())
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
    deadline = time.time() + 10
    while time.time() < deadline and port_listening(port):
        time.sleep(0.5)
    pf.unlink(missing_ok=True)
    if port_listening(port):
        raise SystemExit(f"error: {backend} (pid {pid}) still listening on {port} after SIGTERM -- check manually")
    print(f"{backend} stopped")


def cmd_status(args):
    if args.backend:
        backends = [args.backend]
    else:
        backends = list(LOCAL_BACKENDS)
    for b in backends:
        if b not in LOCAL_BACKENDS:
            print(f"{b}: not a local backend (no server to manage)")
            continue
        port = LOCAL_BACKENDS[b]["port"]
        listening = port_listening(port)
        pf = pid_file(b)
        pid = pf.read_text().strip() if pf.exists() else None
        print(f"{b}: {'RUNNING' if listening else 'stopped'} (port {port}"
              + (f", pid {pid}" if pid else "") + ")")


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

def request_url_and_payload(backend, base_url, state, questions):
    """The ONE place that knows the wire format differs by backend -- every
    caller (call_engine, tests) goes through this rather than hand-building a
    URL/payload. local-* and `typesafe` speak /v1/systemone; `openrouter`
    (VERIFIED LIVE 2026-09-25, see references/engines.md) speaks
    /alpha/decisions with a `model` field prepended to the same state/questions
    body -- see the module docstring for both shapes side by side."""
    if backend in REMOTE_BACKENDS:
        cfg = REMOTE_BACKENDS[backend]
        payload = {"state": state, "questions": questions}
        if cfg.get("model"):
            payload = {"model": cfg["model"], **payload}
        return f"{base_url}{cfg['path']}", payload
    return f"{base_url}/v1/systemone", {"state": state, "questions": questions}


def call_engine(backend, base_url, state, questions, headers=None, timeout=90):
    """POST one request in the backend's wire format. Returns {"ok", "latency_s",
    "response"|"error", "cost_usd"}. Adapted from bench/harness.py /
    pilot1-wiki-intent/scripts/run_engine.py's call_engine() -- same result
    shape, reused rather than reinvented -- extended with backend-aware
    URL/payload selection (request_url_and_payload) and usage.cost extraction
    (only `openrouter`'s verified response carries a real `usage.cost`; local
    engines and the unverified `typesafe` path have none, so cost_usd is None
    there -- see references/engines.md)."""
    import requests  # imported lazily so `eval`/`start`/`stop`/`status` never need it

    url, payload = request_url_and_payload(backend, base_url, state, questions)
    t0 = time.perf_counter()
    try:
        r = requests.post(url, json=payload, headers=headers or {}, timeout=timeout)
        elapsed = time.perf_counter() - t0
        r.raise_for_status()
        resp = r.json()
        cost = None
        usage = resp.get("usage") if isinstance(resp, dict) else None
        if isinstance(usage, dict) and usage.get("cost") is not None:
            cost = usage["cost"]
        return {"ok": True, "latency_s": elapsed, "response": resp, "cost_usd": cost}
    except Exception as e:  # noqa: BLE001 -- deliberately broad, mirrors the benchmark harness
        elapsed = time.perf_counter() - t0
        return {"ok": False, "latency_s": elapsed, "error": str(e)}


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _rewrite_with_valid_lines_only(path, lines):
    """Atomically replace `path` with exactly `lines` (each already a JSON
    string, no trailing newline) -- temp file in the same directory + fsync
    + os.replace, so the rewrite itself can't leave a half-written file
    either."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".jev-resume-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_jsonl_resumable(path):
    """Read a `--resume` target that may end in a torn/truncated last line.

    Per-row flush+fsync (see cmd_ask) guarantees every row that finished
    writing is durable, but it does NOT guarantee a kill can never land
    mid-write() -- os.write() of a multi-KB line is not atomic, so a process
    killed at the wrong instant can leave a partial line with no trailing
    newline (invalid JSON, or valid JSON with the string cut off). A plain
    `json.loads()` per line would raise on that fragment and crash the next
    --resume outright.

    This reads the file line by line, silently DROPS any line that fails to
    parse (a malformed line counts as not-done -- whatever item produced it
    is simply absent from the returned rows, so it will be re-attempted like
    it was never run), and then REWRITES the file in place (temp file +
    atomic os.replace, see _rewrite_with_valid_lines_only) to contain ONLY
    the valid, complete lines it kept. This must happen BEFORE the caller
    reopens the file in append mode -- otherwise a new row could get written
    right after a torn fragment, corrupting every line after it too."""
    if not os.path.exists(path):
        return []
    valid_lines, rows = [], []
    with open(path, "r") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                d = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # torn/truncated line -- drop; its item is not-done
            valid_lines.append(stripped)
            rows.append(d)
    _rewrite_with_valid_lines_only(path, valid_lines)
    return rows


def cmd_ask(args):
    if args.backend not in ALL_BACKENDS:
        raise SystemExit(f"error: unknown backend '{args.backend}' (choices: {', '.join(ALL_BACKENDS)})")

    with open(args.questions) as f:
        questions = json.load(f)
    if not isinstance(questions, dict) or not questions:
        raise SystemExit("error: --questions must be a non-empty JSON object of {question_id: {type, instructions, ...}}")

    items = load_jsonl(args.items)
    if not items:
        raise SystemExit(f"error: no items in {args.items}")
    for it in items:
        if "id" not in it or "state" not in it:
            raise SystemExit(f"error: every item needs 'id' and 'state' fields, got: {it}")

    base_url = base_url_for(args.backend, args.base_url)
    headers = auth_headers_for(args.backend)  # raises loudly if a remote backend has no key

    if args.backend in LOCAL_BACKENDS and not port_listening(LOCAL_BACKENDS[args.backend]["port"]):
        raise SystemExit(f"error: {args.backend} is not running -- `jev.py start --backend {args.backend}` first")

    max_spend = getattr(args, "max_spend", None)

    done_ids = set()
    kept_rows = []
    out_exists = os.path.exists(args.out)
    if args.resume:
        # load_jsonl_resumable tolerates (and drops) a torn/truncated last
        # line -- a kill mid-write() can leave one even with per-row
        # fsync -- and rewrites --out in place to contain only the valid
        # lines it kept, BEFORE we ever reopen it in append mode below. A
        # dropped/malformed line's item simply isn't in done_ids, so it's
        # treated as not-done and gets re-attempted, same as if it had
        # never been run.
        for row in load_jsonl_resumable(args.out):
            if row.get("_meta"):
                continue
            if row.get("ok"):
                kept_rows.append(row)
                done_ids.add(row["id"])
        n_before = len(items)
        items = [it for it in items if it["id"] not in done_ids]
        print(f"[resume] kept {len(kept_rows)} already-OK rows, skipping them; "
              f"{n_before - len(items)} items skipped, {len(items)} remaining", file=sys.stderr)

    running_total = sum(r.get("cost_usd") or 0.0 for r in kept_rows)
    if max_spend is not None and running_total >= max_spend:
        raise SystemExit(f"error: already-kept rows spent ${running_total:.4f} >= --max-spend ${max_spend:.4f}; nothing more to do")

    def score_one(item):
        res = call_engine(args.backend, base_url, item["state"], questions, headers=headers, timeout=args.timeout)
        row = {"id": item["id"], "backend": args.backend, "latency_s": res["latency_s"], "ok": res["ok"]}
        if res["ok"]:
            row["response"] = res["response"]
            if res.get("cost_usd") is not None:
                row["cost_usd"] = res["cost_usd"]
        else:
            row["error"] = res["error"]
        return row

    # Incremental, flushed+fsync'd writes -- a process kill mid-run must never
    # lose more than the single in-flight request. On --resume, the already-OK
    # rows are already on disk (we just read them above) so we APPEND rather
    # than rewrite the whole file; a fresh (non-resumed) run truncates once at
    # the start, then every row after that is appended+flushed as it's
    # computed. This is what makes --resume actually survive a kill, not just
    # a clean stop -- see run_engine.py's per-row `fout.flush()` precedent,
    # which this mirrors.
    out_mode = "a" if (args.resume and out_exists) else "w"
    fout = open(args.out, out_mode)

    def write_row(row):
        fout.write(json.dumps(row, ensure_ascii=False) + "\n")
        fout.flush()
        os.fsync(fout.fileno())

    n_written = len(kept_rows)
    n_errors = sum(1 for r in kept_rows if not r.get("ok", True))
    n_new = 0
    t_start = time.time()
    stopped_for_spend = False

    try:
        chunk_size = max(1, args.parallel)
        i = 0
        while i < len(items):
            if max_spend is not None and running_total >= max_spend:
                stopped_for_spend = True
                break
            chunk = items[i:i + chunk_size]
            if chunk_size > 1 and len(chunk) > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=chunk_size) as ex:
                    chunk_rows = list(ex.map(score_one, chunk))
            else:
                chunk_rows = [score_one(it) for it in chunk]
            for row in chunk_rows:
                cost = row.get("cost_usd")
                if cost:
                    running_total += cost
                    row["_running_total_spend_usd"] = running_total
                write_row(row)  # <-- on disk NOW, not just in memory
                n_written += 1
                n_new += 1
                if not row["ok"]:
                    n_errors += 1
            i += len(chunk)
            if n_new % 25 == 0 or i >= len(items):
                spend_note = f", spend ${running_total:.4f}" if (max_spend is not None or running_total) else ""
                print(f"[{args.backend}] {n_new}/{len(items)} ({time.time()-t_start:.0f}s, {n_errors} errors{spend_note})", file=sys.stderr)
    finally:
        meta = {"_meta": True, "backend": args.backend, "n": n_written,
                "n_errors": n_errors, "questions_file": args.questions}
        if max_spend is not None or running_total:
            meta["total_spend_usd"] = running_total
        if stopped_for_spend:
            meta["stopped_for_spend"] = True
            meta["max_spend_usd"] = max_spend
        write_row(meta)
        fout.close()

    if stopped_for_spend:
        print(f"[{args.backend}] STOPPED: spend ${running_total:.4f} reached --max-spend "
              f"${max_spend:.4f} after {n_new}/{len(items)} new items this run -- rerun with --resume "
              f"to continue once the cap is raised or a new run is approved", file=sys.stderr)
    print(f"[{args.backend}] done: {n_written} items total ({n_new} new this run), {n_errors} errors -> {args.out}", file=sys.stderr)


# ---------------------------------------------------------------------------
# eval  (stdlib-only, mirrors bench/summarize.py's metric style)
# ---------------------------------------------------------------------------

def extract_prediction(answer):
    """One answer object -> (predicted_label, probabilities_dict_or_None, p_of_predicted).
    Mirrors bench/summarize.py's extract_prediction(), which already normalises the
    engines' encodings (choice/score keep a probabilities dict + legend; noul is a
    single P(true))."""
    if not isinstance(answer, dict) or "error" in answer:
        return None, None, None
    qtype = answer.get("type")
    if qtype == "choice":
        probs = answer.get("probabilities") or {}
        pred = answer.get("choice")
        return pred, probs, (probs.get(pred) if probs else None)
    if qtype == "noul":
        p_yes = answer.get("noul")
        if p_yes is None:
            return None, None, None
        pred = "yes" if p_yes >= 0.5 else "no"
        probs = answer.get("probabilities") or {"yes": p_yes, "no": 1 - p_yes}
        return pred, probs, (p_yes if pred == "yes" else 1 - p_yes)
    if qtype == "score":
        raw = answer.get("probabilities") or {}
        legend = answer.get("legend")
        probs = {legend.get(k, k): v for k, v in raw.items()} if legend else raw
        if not probs:
            return None, None, None
        pred = max(probs, key=probs.get)
        return pred, probs, probs[pred]
    return None, None, None


def prf(tp, fp, fn):
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def confusion_at_threshold(pairs, threshold):
    """pairs: list of (p_yes, gold_is_positive: bool) for a noul question."""
    tp = fp = fn = tn = 0
    for p, is_pos in pairs:
        pred = p >= threshold
        if pred and is_pos:
            tp += 1
        elif pred and not is_pos:
            fp += 1
        elif not pred and is_pos:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def best_threshold(pairs, grid=THRESHOLD_GRID):
    best_t, best_f1 = None, -1.0
    for t in grid:
        tp, fp, fn, _ = confusion_at_threshold(pairs, t)
        _, _, f1 = prf(tp, fp, fn)
        if f1 > best_f1:
            best_t, best_f1 = t, f1
    return best_t, best_f1


def compute_ece(rows, n_bins=N_ECE_BINS):
    """rows: list of (confidence, correct_bool)."""
    rows = [(c, correct) for c, correct in rows if c is not None]
    if not rows:
        return None
    bins = [[] for _ in range(n_bins)]
    for conf, correct in rows:
        idx = min(int(conf * n_bins), n_bins - 1)
        bins[idx].append((conf, correct))
    total = len(rows)
    e = 0.0
    for b in bins:
        if not b:
            continue
        avg_conf = statistics.mean(c for c, _ in b)
        avg_acc = statistics.mean(1.0 if correct else 0.0 for _, correct in b)
        e += (len(b) / total) * abs(avg_conf - avg_acc)
    return e


def trivial_baseline_accuracy(gold_labels):
    """Accuracy of 'always predict the majority gold label'. This is the number
    every question's real accuracy must beat before it's allowed near real work
    (SKILL.md's "Mandatory rollout discipline", step 4)."""
    if not gold_labels:
        return None
    counts = Counter(gold_labels)
    majority_n = counts.most_common(1)[0][1]
    return majority_n / len(gold_labels)


def cmd_eval(args):
    results = {r["id"]: r for r in load_jsonl(args.results) if not r.get("_meta")}
    gold_rows = [g for g in load_jsonl(args.gold) if not g.get("_meta")]
    if not gold_rows:
        raise SystemExit(f"error: no gold rows in {args.gold}")

    # group gold by question_id
    by_question = {}
    for g in gold_rows:
        for key in ("id", "question_id", "gold"):
            if key not in g:
                raise SystemExit(f"error: gold row missing '{key}': {g}")
        by_question.setdefault(g["question_id"], []).append(g)

    report = {"n_gold_rows": len(gold_rows), "questions": {}}

    for qid, rows in by_question.items():
        pairs = []          # (p_yes, gold_is_positive) for noul-style questions
        label_pairs = []    # (gold_label, pred_label) for choice/score
        conf_correct = []   # (confidence, correct) for ECE, all types pooled
        gold_labels = []
        p1_hits, p1_n = 0, 0
        n_missing = 0

        for g in rows:
            item = results.get(g["id"])
            gold_val = g["gold"]
            gold_labels.append(gold_val if not isinstance(gold_val, bool) else ("yes" if gold_val else "no"))
            if item is None or not item.get("ok"):
                n_missing += 1
                continue
            answer = (item.get("response") or {}).get("answers", {}).get(qid)
            pred, probs, p_pred = extract_prediction(answer)
            if pred is None:
                n_missing += 1
                continue
            qtype = answer.get("type")
            if qtype == "noul":
                gold_bool = gold_val if isinstance(gold_val, bool) else str(gold_val).lower() in ("yes", "true", "1")
                p_yes = answer.get("noul")
                pairs.append((p_yes, gold_bool))
                correct = (p_yes >= 0.5) == gold_bool
                conf_correct.append((p_pred, correct))
            else:
                gold_label = gold_val
                label_pairs.append((gold_label, pred))
                correct = (pred == gold_label)
                conf_correct.append((p_pred, correct))
                if probs:
                    p1_n += 1
                    if max(probs, key=probs.get) == gold_label:
                        p1_hits += 1

        q_report = {"n_gold": len(rows), "n_missing_or_error": n_missing}
        q_report["trivial_baseline_accuracy"] = trivial_baseline_accuracy(gold_labels)

        if pairs:
            t, f1_at_t = best_threshold(pairs)
            tp, fp, fn, tn = confusion_at_threshold(pairs, t)
            prec, rec, _ = prf(tp, fp, fn)
            q_report["type"] = "noul"
            q_report["best_threshold"] = t
            q_report["precision"] = prec
            q_report["recall"] = rec
            q_report["f1"] = f1_at_t
            q_report["accuracy_at_0.5"] = sum(1 for p, is_pos in pairs if (p >= 0.5) == is_pos) / len(pairs)
        elif label_pairs:
            tp = sum(1 for g, p in label_pairs if g == p)
            q_report["type"] = "choice_or_score"
            q_report["accuracy"] = tp / len(label_pairs)
            q_report["p_at_1"] = (p1_hits / p1_n) if p1_n else None
            labels = sorted(set(g for g, _ in label_pairs) | set(p for _, p in label_pairs))
            f1s = []
            for lab in labels:
                ltp = sum(1 for g, p in label_pairs if p == lab and g == lab)
                lfp = sum(1 for g, p in label_pairs if p == lab and g != lab)
                lfn = sum(1 for g, p in label_pairs if p != lab and g == lab)
                _, _, lf1 = prf(ltp, lfp, lfn)
                f1s.append(lf1)
            q_report["macro_f1"] = statistics.mean(f1s) if f1s else None
        else:
            q_report["type"] = None

        q_report["ece"] = compute_ece(conf_correct)
        report["questions"][qid] = q_report

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    for qid, q in report["questions"].items():
        acc = q.get("accuracy") or q.get("accuracy_at_0.5")
        base = q.get("trivial_baseline_accuracy")
        flag = ""
        if acc is not None and base is not None and acc < base:
            flag = "  <-- DOES NOT BEAT TRIVIAL BASELINE, do not ship"
        print(f"[{qid}] n={q['n_gold']} type={q.get('type')} "
              f"acc={acc if acc is not None else '-'} baseline={base if base is not None else '-'} "
              f"ece={q.get('ece')}{flag}", file=sys.stderr)

    print(f"eval report -> {args.out}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="jev.py", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="start a local engine server")
    p_start.add_argument("--backend", required=True, choices=list(LOCAL_BACKENDS))
    p_start.add_argument("--startup-timeout", type=float, default=120.0)
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop a local engine server")
    p_stop.add_argument("--backend", required=True, choices=list(LOCAL_BACKENDS))
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="show local engine server status")
    p_status.add_argument("--backend", choices=list(LOCAL_BACKENDS), default=None,
                           help="omit to show all three local backends")
    p_status.set_defaults(func=cmd_status)

    p_ask = sub.add_parser("ask", help="score items against one or more typed questions")
    p_ask.add_argument("--backend", required=True, choices=ALL_BACKENDS)
    p_ask.add_argument("--base-url", default=None, help="override the backend's default base URL")
    p_ask.add_argument("--questions", required=True, help="JSON file: {question_id: {type, instructions, ...}}")
    p_ask.add_argument("--items", required=True, help="JSONL file: one {id, state} per line")
    p_ask.add_argument("--out", required=True, help="output JSONL path")
    p_ask.add_argument("--resume", action="store_true",
                        help="skip items whose id already has an ok:true row in --out")
    p_ask.add_argument("--parallel", type=int, default=1, help="concurrent item requests (default 1 = sequential)")
    p_ask.add_argument("--timeout", type=float, default=90.0)
    p_ask.add_argument("--max-spend", type=float, default=None,
                        help="stop issuing new requests once cumulative usage.cost across THIS RUN's new "
                             "calls (plus any --resume'd rows' recorded cost) reaches this many USD. "
                             "Remote backends only carry real cost (openrouter is verified; typesafe is "
                             "unverified); local calls always cost $0 so the guard never fires for them. "
                             "NOTE with --parallel N: the cap is checked once per batch of N concurrent "
                             "requests, not per individual call, so a batch already in flight when the cap "
                             "is reached still completes -- actual spend can overshoot --max-spend by up to "
                             "roughly one batch's worth of cost (up to N in-flight requests). Use --parallel 1 "
                             "for an exact cap. Unset = no cap.")
    p_ask.set_defaults(func=cmd_ask)

    p_eval = sub.add_parser("eval", help="score ask results against a gold set")
    p_eval.add_argument("--results", required=True, help="JSONL from `ask`")
    p_eval.add_argument("--gold", required=True,
                         help="JSONL: one {id, question_id, gold} per gold (item, question) pair")
    p_eval.add_argument("--out", required=True, help="output JSON report path")
    p_eval.set_defaults(func=cmd_eval)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
