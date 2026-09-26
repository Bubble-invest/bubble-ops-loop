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
                               --max-spend if set. Every row is a decision RECEIPT (see
                               ../references/contract.md): state_digest, question_set_version,
                               backend, model (served, when available), latency_ms, timestamp,
                               plus the existing ok/response/cost_usd fields.
  eval                     -- score `ask` results against a gold set -> precision/recall/
                               F1/ECE/P@1 per question, plus the trivial majority-class
                               baseline (ALWAYS compare against this -- see ../SKILL.md
                               "Mandatory rollout discipline" and ../references/eval.md)
  lint                     -- lint a questions.json for contract issues: no-match option
                               missing on a Choice, empty/missing instructions or criteria,
                               Score criteria without observable anchors, duplicate question
                               ids. Exits non-zero on errors only (warnings don't fail CI).
  replay                   -- re-run the states from a previous `ask` run's results against a
                               (possibly changed) questions file/backend and report per-question
                               drift: how many answers changed class, mean |delta p|, and the
                               flipped ids. See ../references/contract.md.

State can be a string, a JSON object, or a JSON array on EVERY backend (local and remote
alike) -- jev.py forwards it unchanged in the request body and never re-serializes it, and
each local engine's own wire-format module (`decider.systemone.render_state`,
`semif_phase1.core`, `laya.common`) accepts str|dict|list and serializes non-strings itself.
A structured state (named fields, not a flattened paragraph) works on every backend -- see
../references/contract.md's "State design" section.

Dependencies: stdlib + `requests` (only needed for `ask` against a real server; `eval`,
`start`, `stop`, `status` are stdlib-only). Install with the caller's own venv/pip.

API keys for openrouter/typesafe come ONLY from the environment (OPENROUTER_API_KEY /
TYPESAFE_API_KEY) -- never pass one as a CLI flag, never printed, and a missing key fails
loudly before any request is attempted.
"""
import argparse
import hashlib
import json
import math
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
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
        "key_env": ["JEV_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"],
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
    # Fleet-dedicated key first (JEV_OPENROUTER_API_KEY, capped "fleet-jev" key,
    # board #1505) so it never collides with an agent's own OPENROUTER_API_KEY
    # (e.g. Morty's Hermes key in the shared SOPS env); generic name as fallback.
    names = [cfg["key_env"]] if isinstance(cfg["key_env"], str) else list(cfg["key_env"])
    key = next((os.environ[n] for n in names if os.environ.get(n)), None)
    if not key:
        raise SystemExit(
            f"error: backend '{backend}' requires one of {', '.join(names)} in the environment "
            f"(e.g. `export {names[0]}=...`). Refusing to call without it. "
            f"Never pass an API key as a command-line flag."
        )
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Decision contract: canonical digests, question-set versioning, receipts
# (see ../references/contract.md -- this is the "harness" that records and
# replays a decision, not the model itself).
# ---------------------------------------------------------------------------

def canonical_json(obj):
    """Deterministic serialization used for both state_digest and the
    fallback question_set_version -- sort_keys + no extra whitespace so the
    same logical object always hashes the same way regardless of dict
    insertion order."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def state_digest(state):
    """A receipt records this digest, never the raw state, by default (see
    ../references/contract.md's state-design section, and the field guide's
    "do not log secrets or raw customer state by default") -- `replay` needs
    the real state back, which is why it takes --items rather than trying to
    reconstruct it from a digest."""
    return sha256_hex(canonical_json(state))


def strip_meta_keys(questions_raw):
    """Question ids that start with '_' (currently just an optional
    "_version" string) are contract metadata, never sent to an engine."""
    return {k: v for k, v in questions_raw.items() if not k.startswith("_")}


def question_set_version(questions_raw):
    """The version recorded on every receipt and checked by --resume. Uses
    the explicit optional top-level "_version" key when the questions file
    declares one; otherwise falls back to a sha256 of the canonical wire
    question set (meta keys stripped) so an unversioned file still gets a
    real, content-derived version rather than a constant placeholder."""
    v = questions_raw.get("_version")
    if v is not None:
        return str(v)
    return sha256_hex(canonical_json(strip_meta_keys(questions_raw)))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


_NO_MATCH_RE = re.compile(
    r"(other|unknown|unsure|uncertain|needs?[-_ ]?review|not[-_ ]?applicable|\bn/?a\b|^none$)",
    re.IGNORECASE,
)


def _looks_like_no_match_option(option_key):
    return bool(_NO_MATCH_RE.search(str(option_key)))


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
    wire_questions = strip_meta_keys(questions)
    if not wire_questions:
        raise SystemExit("error: --questions has no question definitions (only meta keys like _version)")
    qsv = question_set_version(questions)

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
            if not row.get("ok"):
                continue
            recorded_qsv = row.get("question_set_version")
            # Version-aware resume (board #1505 upgrade 5): a row recorded
            # under a DIFFERENT question_set_version than the current
            # questions file is stale evidence -- the contract changed, so
            # its answer must not be reused; drop it from kept_rows so its
            # item is treated as not-done and gets re-attempted below. A row
            # with NO recorded version at all is a legacy (pre-receipt) row
            # written before this field existed -- there is no way to know
            # whether it matches the current contract, so it is kept as
            # before (backward compatible with runs made before this
            # upgrade). Only an EXPLICIT mismatch invalidates the cache.
            if recorded_qsv is not None and recorded_qsv != qsv:
                continue
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
        res = call_engine(args.backend, base_url, item["state"], wire_questions, headers=headers, timeout=args.timeout)
        # Every row is a decision receipt (board #1505, ../references/contract.md):
        # state_digest + question_set_version + backend + timestamp + latency are
        # recorded even on failure (useful for audit/replay); model/response/cost_usd
        # only when the call actually succeeded. All fields here are ADDITIONS --
        # every field a pre-upgrade row had (id/backend/latency_s/ok/response/error/
        # cost_usd) keeps the exact same meaning, so old tooling reading old or new
        # rows still works.
        row = {
            "id": item["id"], "backend": args.backend,
            "latency_s": res["latency_s"], "latency_ms": res["latency_s"] * 1000.0,
            "ok": res["ok"],
            "state_digest": state_digest(item["state"]),
            "question_set_version": qsv,
            "timestamp": utc_now_iso(),
        }
        if res["ok"]:
            row["response"] = res["response"]
            served_model = res["response"].get("model") if isinstance(res["response"], dict) else None
            if served_model:
                row["model"] = served_model
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
                "n_errors": n_errors, "questions_file": args.questions,
                "question_set_version": qsv}
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
# lint  (stdlib-only, see ../references/contract.md "Question design")
# ---------------------------------------------------------------------------

def _load_questions_detecting_duplicates(path):
    """Plain json.load silently keeps only the LAST of two duplicate top-level
    keys -- a real authoring bug (one question definition quietly discarded)
    that a normal parse can never surface. object_pairs_hook sees every
    (key, value) pair BEFORE the dict is collapsed, so a duplicate is caught
    here or nowhere."""
    dup_ids = []

    def hook(pairs):
        seen = set()
        d = {}
        for k, v in pairs:
            if k in seen:
                dup_ids.append(k)
            seen.add(k)
            d[k] = v
        return d

    with open(path) as f:
        try:
            questions = json.load(f, object_pairs_hook=hook)
        except json.JSONDecodeError as e:
            raise SystemExit(f"error: invalid JSON in {path}: {e}")
    return questions, sorted(set(dup_ids))


def lint_questions(questions_raw, dup_ids):
    """-> (errors: [str], warnings: [str]). Pure function over an already-
    parsed questions dict so tests can exercise it without a file on disk."""
    errors, warnings = [], []
    for d in dup_ids:
        errors.append(f"duplicate question id '{d}' -- plain JSON silently kept only the last "
                       f"definition; rename one of them")

    wire = strip_meta_keys(questions_raw)
    if not wire:
        errors.append("no question definitions found (only meta keys like _version)")

    for qid, qdef in wire.items():
        if not isinstance(qdef, dict):
            errors.append(f"[{qid}] question definition must be an object, got {type(qdef).__name__}")
            continue
        qtype = qdef.get("type")
        instructions = qdef.get("instructions")
        criteria = qdef.get("criteria")
        has_noul_criteria_instructions = (
            qtype in ("noul", "bool") and isinstance(criteria, dict)
            and any(criteria.get(k) not in (None, "") for k in ("true", "false", True, False))
        )
        if (not instructions or not str(instructions).strip()) and not has_noul_criteria_instructions:
            errors.append(f"[{qid}] missing or empty 'instructions' "
                           f"(a noul question may instead describe true/false in 'criteria')")

        if qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                errors.append(f"[{qid}] choice question needs a non-empty 'criteria' object of "
                               f"{{option: description}}")
            elif not any(_looks_like_no_match_option(opt) for opt in criteria):
                warnings.append(f"[{qid}] no no-match option (e.g. 'other'/'unknown'/'needs_review') -- "
                                 f"if the real world can fall outside this list, the model must still "
                                 f"pick one of the listed options")
        elif qtype == "score":
            if not isinstance(criteria, list) or not criteria:
                errors.append(f"[{qid}] score question needs a non-empty 'criteria' list of ordered, "
                               f"observable level descriptions")
            else:
                if len(criteria) < 2:
                    warnings.append(f"[{qid}] score has fewer than 2 levels -- not a real scale")
                if len(criteria) != len(set(criteria)):
                    warnings.append(f"[{qid}] score criteria has duplicate level descriptions -- levels "
                                     f"must be distinct, observable anchors")
        elif qtype in ("noul", "bool"):
            pass  # criteria optional -- covered by the instructions check above
        else:
            errors.append(f"[{qid}] unknown or missing 'type' (expected choice/score/noul), got {qtype!r}")

    return errors, warnings


def cmd_lint(args):
    questions_raw, dup_ids = _load_questions_detecting_duplicates(args.questions)
    if not isinstance(questions_raw, dict) or not questions_raw:
        raise SystemExit(f"error: --questions must be a non-empty JSON object, got {args.questions}")

    errors, warnings = lint_questions(questions_raw, dup_ids)
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)
    print(f"lint: {len(errors)} error(s), {len(warnings)} warning(s) in {args.questions}", file=sys.stderr)
    if errors:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# replay  (see ../references/contract.md "Decision receipts and replay")
# ---------------------------------------------------------------------------

def cmd_replay(args):
    if args.backend not in ALL_BACKENDS:
        raise SystemExit(f"error: unknown backend '{args.backend}' (choices: {', '.join(ALL_BACKENDS)})")

    with open(args.questions) as f:
        questions_raw = json.load(f)
    if not isinstance(questions_raw, dict) or not questions_raw:
        raise SystemExit("error: --questions must be a non-empty JSON object")
    wire_questions = strip_meta_keys(questions_raw)
    if not wire_questions:
        raise SystemExit("error: --questions has no question definitions (only meta keys)")
    new_qsv = question_set_version(questions_raw)

    all_old_rows = load_jsonl(args.results)
    old_meta = next((r for r in all_old_rows if r.get("_meta")), {})
    old_qsv = old_meta.get("question_set_version")
    old_by_id = {r["id"]: r for r in all_old_rows if not r.get("_meta") and r.get("ok")}
    if not old_by_id:
        raise SystemExit(f"error: no ok:true rows found in {args.results}")

    # States: `ask` does NOT store the raw state on a row by default (only
    # state_digest -- see contract.md), so replay normally needs --items
    # (the original items JSONL) to get real states back. A results row that
    # happens to carry its own "state" field (e.g. a hand-built results file)
    # is honoured too, so --items is a requirement in practice, not in code.
    state_by_id = {}
    if args.items:
        for it in load_jsonl(args.items):
            if "id" in it and "state" in it:
                state_by_id[it["id"]] = it["state"]
    for rid, row in old_by_id.items():
        if rid not in state_by_id and "state" in row:
            state_by_id[rid] = row["state"]

    missing = sorted(rid for rid in old_by_id if rid not in state_by_id)
    if missing:
        raise SystemExit(
            f"error: {len(missing)} item(s) have no recoverable state -- `ask` results rows don't carry "
            f"raw state by default, pass --items pointing at the original items JSONL. Missing ids "
            f"(first 5): {missing[:5]}"
        )

    base_url = base_url_for(args.backend, args.base_url)
    headers = auth_headers_for(args.backend)
    if args.backend in LOCAL_BACKENDS and not port_listening(LOCAL_BACKENDS[args.backend]["port"]):
        raise SystemExit(f"error: {args.backend} is not running -- `jev.py start --backend {args.backend}` first")

    ids = list(old_by_id.keys())
    running_total = 0.0
    max_spend = getattr(args, "max_spend", None)

    def score_one(rid):
        res = call_engine(args.backend, base_url, state_by_id[rid], wire_questions, headers=headers, timeout=args.timeout)
        return rid, res

    new_by_id = {}
    chunk_size = max(1, args.parallel)
    i = 0
    stopped_for_spend = False
    while i < len(ids):
        if max_spend is not None and running_total >= max_spend:
            stopped_for_spend = True
            break
        chunk = ids[i:i + chunk_size]
        if chunk_size > 1 and len(chunk) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=chunk_size) as ex:
                chunk_results = list(ex.map(score_one, chunk))
        else:
            chunk_results = [score_one(r) for r in chunk]
        for rid, res in chunk_results:
            if res["ok"]:
                new_by_id[rid] = res["response"]
                cost = res.get("cost_usd")
                if cost:
                    running_total += cost
        i += len(chunk)

    # Diff per question: reuse extract_prediction (the same normalizer `eval`
    # uses) so choice/score/noul are compared the same way here as everywhere
    # else in this file -- "changed class" = the predicted label/choice
    # flipped; |delta p| = |new confidence in its own pick - old confidence
    # in its own pick|.
    by_question = {}
    for rid in ids:
        old_answers = (old_by_id[rid].get("response") or {}).get("answers", {})
        new_answers = (new_by_id.get(rid) or {}).get("answers", {})
        for qid in set(old_answers) | set(new_answers):
            old_ans, new_ans = old_answers.get(qid), new_answers.get(qid)
            if old_ans is None or new_ans is None:
                continue  # question added/removed between versions -- not comparable
            old_pred, _, old_p = extract_prediction(old_ans)
            new_pred, _, new_p = extract_prediction(new_ans)
            if old_pred is None or new_pred is None:
                continue
            q = by_question.setdefault(qid, {"n_compared": 0, "n_changed_class": 0,
                                              "flipped_ids": [], "_deltas": []})
            q["n_compared"] += 1
            if old_pred != new_pred:
                q["n_changed_class"] += 1
                q["flipped_ids"].append(rid)
            if old_p is not None and new_p is not None:
                q["_deltas"].append(abs(new_p - old_p))

    report = {
        "backend": args.backend,
        "old_question_set_version": old_qsv,
        "new_question_set_version": new_qsv,
        "n_items_compared": len(ids),
        "n_items_missing_new_answer": len(ids) - len(new_by_id),
        "stopped_for_spend": stopped_for_spend,
        "questions": {},
    }
    for qid, q in by_question.items():
        deltas = q.pop("_deltas")
        q["mean_abs_delta_p"] = (sum(deltas) / len(deltas)) if deltas else None
        report["questions"][qid] = q

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"replay: {report['n_items_compared']} item(s) compared, "
          f"{report['n_items_missing_new_answer']} missing a new answer"
          + (" (stopped: --max-spend reached)" if stopped_for_spend else ""), file=sys.stderr)
    for qid, q in report["questions"].items():
        print(f"[{qid}] compared={q['n_compared']} changed_class={q['n_changed_class']} "
              f"mean|dp|={q['mean_abs_delta_p']} flipped={len(q['flipped_ids'])}", file=sys.stderr)
    print(f"replay report -> {args.out}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Agent verbs: filter / classify / rank / find  (board #1540)
#
# Design PORTED from the MIT-licensed quicksilver skill (github.com/UditAkhourii/
# quicksilver, skills/quicksilver/scripts/qs.mjs, read 2026-09-26) -- the UX (one
# line per hit, a `p` in front, a `?` on the borderline band, a closing receipt,
# --lines/--items input handling) is deliberately close to it. NOT a port of its
# code: this reuses jev.py's OWN backends, call_engine()/request_url_and_payload()
# and auth_headers_for() (openrouter by default via JEV_OPENROUTER_API_KEY, local
# engines as a no-network fallback) rather than quicksilver's TypeSafe-only HTTP
# client and its separate ~/.quicksilver key store. Credit: quicksilver, MIT
# license, see SKILL.md's "Agent verbs" section.
# ---------------------------------------------------------------------------

AGENT_VERB_MAX_FILE_BYTES = 2 * 1024 * 1024

_AGENT_VERB_IGNORE_DIRS = {
    "node_modules", ".git", "dist", "build", "out", ".next", ".nuxt",
    "target", "vendor", "__pycache__", ".venv", "venv", "coverage",
    ".turbo", ".cache", ".idea", ".vscode",
}

# Deliberately broad / over-inclusive -- a false positive here just means one
# more file gets skipped and reported, never sent. Matches .env*, *.pem, *.key,
# id_rsa/id_ed25519/etc, and anything with "credential" or "secret" in the path.
_SECRET_PATH_RES = [
    re.compile(r"(^|[/\\])\.env(\..*)?$", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"(^|[/\\])id_[^/\\]+$", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
]


def _looks_secret(relpath):
    return any(p.search(relpath) for p in _SECRET_PATH_RES)


def _is_binary(buf):
    return b"\0" in buf[:8192]


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_k(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _rel(p):
    try:
        return os.path.relpath(p, os.getcwd())
    except ValueError:
        return str(p)


def _git_ls_files(dirpath):
    """Files git already knows about (tracked + untracked-but-not-ignored) under
    `dirpath`, respecting .gitignore -- or None if `dirpath` isn't inside a git
    work tree (caller falls back to a plain walk)."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", "."],
            cwd=str(dirpath), capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return [str(Path(dirpath) / p) for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except OSError:
        return None


def _git_check_ignored(paths):
    """Subset of absolute path strings `paths` that `git check-ignore` considers
    ignored, in ONE batched --stdin call. Best-effort: returns an empty set (no
    filtering) if the paths aren't inside a git work tree or git isn't available
    -- this is a belt-and-suspenders check for explicitly-named files; directory
    expansion already goes through _git_ls_files, which respects .gitignore on
    its own."""
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=os.path.dirname(paths[0]) or ".",
            input=("\0".join(paths) + "\0").encode("utf-8"),
            capture_output=True, timeout=30,
        )
        if proc.returncode not in (0, 1):
            return set()
        return set(x for x in proc.stdout.decode("utf-8", "replace").split("\0") if x)
    except OSError:
        return set()


def _walk_fs(dirpath):
    out = []
    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if d not in _AGENT_VERB_IGNORE_DIRS and not d.startswith(".")]
        for fn in files:
            out.append(os.path.join(root, fn))
    return out


def _expand_path_spec(spec):
    if any(ch in spec for ch in "*?["):
        import glob
        return [f for f in glob.glob(spec, recursive=True) if Path(f).is_file()]
    p = Path(spec)
    if not p.exists():
        raise SystemExit(f"error: no such file or directory: {spec}")
    if p.is_file():
        return [str(p)]
    listed = _git_ls_files(p)
    if listed is None:
        listed = _walk_fs(p)
    return [f for f in listed if Path(f).is_file()]


def collect_items(path_specs, lines=False, max_chars=60000, limit=5000, ext=None, items_jsonl=None):
    """Safe, agent-verb-wide input collection.

    Returns (items, skipped, total_chars): items is [{id, text}] (one per file,
    or one per non-blank line when `lines=True`); skipped is a list of
    human-readable reasons (secret-like / binary / too big / gitignored, never
    an exception -- the caller just doesn't see that content); total_chars is
    the sum of every collected item's UN-truncated length, used for the
    "Claude tokens not read" estimate."""
    items, skipped = [], []
    total_chars = 0
    exts = {"." + e.strip().lstrip(".").lower() for e in ext.split(",") if e.strip()} if ext else None

    if items_jsonl:
        raw = sys.stdin.read() if items_jsonl == "-" else open(items_jsonl).read()
        for i, line in enumerate(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                iid = str(obj.get("id", i + 1))
                text = obj.get("text")
                if text is None:
                    text = obj.get("content", "")
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
            else:
                iid, text = str(i + 1), line
            total_chars += len(text)
            items.append({"id": iid, "text": text[:max_chars] if max_chars else text})

    files = []
    for spec in path_specs or []:
        if spec == "-":
            text = sys.stdin.read()
            if lines:
                for i, l in enumerate(text.split("\n")):
                    if l.strip():
                        items.append({"id": f"stdin:{i + 1}", "text": l})
                        total_chars += len(l)
            else:
                items.append({"id": "stdin", "text": text[:max_chars] if max_chars else text})
                total_chars += len(text)
            continue
        files.extend(_expand_path_spec(spec))

    seen, uniq_files = set(), []
    for f in files:
        ap = os.path.abspath(f)
        if ap in seen:
            continue
        seen.add(ap)
        uniq_files.append(ap)

    ignored = _git_check_ignored(uniq_files) if uniq_files else set()

    for ap in uniq_files:
        rel = _rel(ap)
        if ap in ignored:
            continue
        if exts and Path(ap).suffix.lower() not in exts:
            continue
        if _looks_secret(rel):
            skipped.append(f"{rel} (secret-like, never sent)")
            continue
        try:
            size = os.path.getsize(ap)
        except OSError:
            continue
        if size > AGENT_VERB_MAX_FILE_BYTES:
            skipped.append(f"{rel} (>{AGENT_VERB_MAX_FILE_BYTES // (1024 * 1024)}MB)")
            continue
        try:
            with open(ap, "rb") as fh:
                buf = fh.read()
        except OSError:
            continue
        if _is_binary(buf):
            skipped.append(f"{rel} (binary)")
            continue
        text = buf.decode("utf-8", "replace")
        if not text.strip():
            continue
        if lines:
            for i, l in enumerate(text.split("\n")):
                if l.strip():
                    items.append({"id": f"{rel}:{i + 1}", "text": l})
                    total_chars += len(l)
        else:
            total_chars += len(text)
            items.append({"id": rel, "text": text[:max_chars] if max_chars else text})

    if limit and len(items) > limit:
        raise SystemExit(f"error: {len(items)} items exceeds --limit {limit}; narrow the input or raise --limit")
    return items, skipped, total_chars


def _require_local_running(backend):
    if backend in LOCAL_BACKENDS and not port_listening(LOCAL_BACKENDS[backend]["port"]):
        raise SystemExit(f"error: {backend} is not running -- `jev.py start --backend {backend}` first")


def _run_verb(items, question_for, backend, base_url, headers, parallel, max_spend, timeout=90):
    """Score every item against ONE question (built per-item by `question_for`).
    Same chunked-ThreadPoolExecutor / running-cost / --max-spend shape as
    cmd_ask/cmd_replay above -- reused, not reinvented. Returns
    (rows, running_total_spend_usd, stopped_for_spend); rows is
    [{item, ok, answer, cost_usd, latency_s, error}]."""
    rows = []
    running_total = 0.0
    stopped_for_spend = False
    chunk_size = max(1, parallel)

    def score_one(it):
        q = question_for(it)
        state = {"source": it["id"], "content": it["text"]}
        res = call_engine(backend, base_url, state, {"q": q}, headers=headers, timeout=timeout)
        return it, res

    i = 0
    while i < len(items):
        if max_spend is not None and running_total >= max_spend:
            stopped_for_spend = True
            break
        chunk = items[i:i + chunk_size]
        if chunk_size > 1 and len(chunk) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=chunk_size) as ex:
                results = list(ex.map(score_one, chunk))
        else:
            results = [score_one(it) for it in chunk]
        for it, res in results:
            cost = res.get("cost_usd") if res["ok"] else None
            if cost:
                running_total += cost
            answer = None
            if res["ok"] and isinstance(res.get("response"), dict):
                answer = (res["response"].get("answers") or {}).get("q")
            rows.append({"item": it, "ok": res["ok"], "answer": answer, "cost_usd": cost,
                         "latency_s": res.get("latency_s"), "error": res.get("error")})
        i += len(chunk)
    return rows, running_total, stopped_for_spend


def _finish(args, t0, n_scanned, total_input_chars, output_lines, n_matched, n_borderline, backend, spend, log_rows):
    """The ONE closing receipt line every agent verb ends with, plus the
    optional full JSONL receipt (--log)."""
    output_text = "\n".join(output_lines) if output_lines else "(no results)"
    print(output_text)
    elapsed = time.time() - t0
    tokens_not_read = max(0, total_input_chars - len(output_text)) // 4
    footer = (f"— {n_scanned} scanned · {n_matched} matched · {n_borderline} borderline · "
              f"{elapsed:.1f}s · {backend} · jev ${spend:.4f} · "
              f"~{_fmt_k(tokens_not_read)} Claude tokens not read")
    print(footer, file=sys.stderr)
    log_path = getattr(args, "log", None)
    if log_path:
        with open(log_path, "w") as f:
            for row in log_rows:
                item = row.get("item") if isinstance(row, dict) else None
                rec = {
                    "id": item["id"] if isinstance(item, dict) else row.get("id"),
                    "ok": row.get("ok"), "answer": row.get("answer"), "cost_usd": row.get("cost_usd"),
                    "latency_s": row.get("latency_s"), "error": row.get("error"),
                    "backend": backend, "timestamp": utc_now_iso(),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return footer


def parse_band(band_flag, threshold):
    """--band accepts either a half-width (e.g. 0.15, symmetric around
    --threshold, quicksilver's convention) or an explicit 'lo,hi' pair."""
    if band_flag is None:
        w = 0.15
        return max(0.0, threshold - w), min(1.0, threshold + w)
    s = str(band_flag)
    if "," in s:
        lo_s, hi_s = s.split(",", 1)
        return float(lo_s), float(hi_s)
    w = float(s)
    return max(0.0, threshold - w), min(1.0, threshold + w)


# Log/CSV lines repeat with different numbers/ids/timestamps; normalizing them
# to a shared template is how a run of near-identical lines is detected.
_TEMPLATE_HEX_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_TEMPLATE_LONGHEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_TEMPLATE_DIGIT_RE = re.compile(r"\d+")


def line_template(text):
    t = _TEMPLATE_HEX_RE.sub("#", text)
    t = _TEMPLATE_LONGHEX_RE.sub("#", t)
    t = _TEMPLATE_DIGIT_RE.sub("#", t)
    return re.sub(r"\s+", " ", t).strip()


def _parse_line_id(item_id):
    path, sep, n = item_id.rpartition(":")
    if not sep:
        return item_id, None
    try:
        return path, int(n)
    except ValueError:
        return item_id, None


def render_collapsed_line_rows(matched, lo, hi):
    """matched: [(row, p)] for --lines mode. Collapses RUNS of consecutive
    lines (same file, adjacent line numbers) that share the same line_template
    into one 'path:Lx-Ly (N×)' row -- a single line stays 'path:N'. Sorted by
    the run's max p, descending; a run gets the '?' prefix if any line in it
    falls in the [lo, hi] borderline band."""
    by_order = sorted(matched, key=lambda rp: _parse_line_id(rp[0]["item"]["id"]))
    runs = []
    cur = None
    for r, p in by_order:
        path, n = _parse_line_id(r["item"]["id"])
        tmpl = line_template(r["item"]["text"])
        if (cur and cur["path"] == path and cur["tmpl"] == tmpl
                and n is not None and cur["end"] is not None and n == cur["end"] + 1):
            cur["end"] = n
            cur["count"] += 1
            cur["max_p"] = max(cur["max_p"], p)
            cur["any_border"] = cur["any_border"] or (lo <= p <= hi)
        else:
            cur = {"path": path, "start": n, "end": n, "tmpl": tmpl, "count": 1,
                   "max_p": p, "text": r["item"]["text"], "any_border": lo <= p <= hi}
            runs.append(cur)
    runs.sort(key=lambda c: -c["max_p"])
    out = []
    for c in runs:
        prefix = "?" if c["any_border"] else " "
        loc = f"{c['path']}:{c['start']}" if c["count"] == 1 else f"{c['path']}:L{c['start']}-{c['end']} ({c['count']}×)"
        out.append(f"{prefix}{c['max_p']:.2f}  {loc}  {_clip(c['text'].strip(), 160)}")
    return out


def cmd_filter(args):
    t0 = time.time()
    backend = args.backend
    base_url = base_url_for(backend, args.base_url)
    headers = auth_headers_for(backend)
    _require_local_running(backend)

    items, skipped, total_chars = collect_items(args.paths, lines=args.lines, max_chars=args.max_chars, limit=args.limit, ext=args.ext)
    if not items:
        raise SystemExit("error: nothing to filter -- pass files, directories, globs, or -")

    thr = args.threshold
    lo, hi = parse_band(args.band, thr)

    def qfor(it):
        return {"type": "noul", "instructions": args.question}

    rows, spend, stopped = _run_verb(items, qfor, backend, base_url, headers, args.parallel, args.max_spend, args.timeout)

    scored = [(r, r["answer"]["noul"]) for r in rows if r["ok"] and r.get("answer") and r["answer"].get("noul") is not None]
    matched = [(r, p) for r, p in scored if p >= thr]
    borderline_n = sum(1 for _, p in matched if lo <= p <= hi)

    if args.lines:
        out_lines = render_collapsed_line_rows(matched, lo, hi)
    else:
        matched.sort(key=lambda rp: -rp[1])
        out_lines = [f"{'?' if lo <= p <= hi else ' '}{p:.2f}  {r['item']['id']}" for r, p in matched]

    if stopped:
        out_lines.append(f"(stopped: --max-spend ${args.max_spend:.4f} reached; {len(items) - len(rows)} item(s) not scored)")
    if skipped:
        out_lines.append(f"skipped {len(skipped)}: {_clip(', '.join(skipped), 300)}")

    _finish(args, t0, len(items), total_chars, out_lines, len(matched), borderline_n, backend, spend, rows)


def cmd_classify(args):
    t0 = time.time()
    backend = args.backend
    base_url = base_url_for(backend, args.base_url)
    headers = auth_headers_for(backend)
    _require_local_running(backend)

    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    if len(labels) < 2:
        raise SystemExit("error: classify needs at least 2 --labels")
    criteria = {l: None for l in labels}
    other_label = None
    if not args.no_other and not any(_looks_like_no_match_option(l) for l in labels):
        other_label = "other"
        n = 1
        while other_label in criteria:
            n += 1
            other_label = f"other{n}"
        criteria[other_label] = "None of the other labels fit; use this when the item genuinely matches none of them"

    if not args.paths and not args.items:
        raise SystemExit("error: classify needs --items FILE.jsonl or one or more <paths>")
    items, skipped, total_chars = collect_items(args.paths, lines=False, max_chars=args.max_chars, limit=args.limit, ext=args.ext, items_jsonl=args.items)
    if not items:
        raise SystemExit("error: nothing to classify")

    question = args.question or "Which label best describes this item?"

    def qfor(it):
        return {"type": "choice", "instructions": question, "criteria": criteria}

    rows, spend, stopped = _run_verb(items, qfor, backend, base_url, headers, args.parallel, args.max_spend, args.timeout)

    groups = {}
    low = []
    for r in rows:
        if not r["ok"] or not r.get("answer") or r["answer"].get("choice") is None:
            continue
        a = r["answer"]
        groups.setdefault(a["choice"], []).append(r["item"]["id"])
        if a.get("confidence", 1.0) < args.min_confidence:
            low.append((r, a))

    out_lines = []
    for l in criteria:
        ids = groups.get(l, [])
        if ids:
            out_lines.append(f"{l}: {','.join(ids)}")
    if low:
        out_lines.append("? low confidence — check these yourself:")
        for r, a in low:
            probs = a.get("probabilities") or {}
            runners = sorted(((k, v) for k, v in probs.items() if k != a.get("choice")), key=lambda kv: -kv[1])
            runner = runners[0][0] if runners else "?"
            out_lines.append(f"?{a.get('confidence', 0.0):.2f}  {a.get('choice')} (or {runner})  {r['item']['id']}")

    if stopped:
        out_lines.append(f"(stopped: --max-spend ${args.max_spend:.4f} reached; {len(items) - len(rows)} item(s) not scored)")
    if skipped:
        out_lines.append(f"skipped {len(skipped)}: {_clip(', '.join(skipped), 300)}")

    matched_n = sum(len(v) for v in groups.values())
    _finish(args, t0, len(items), total_chars, out_lines, matched_n, len(low), backend, spend, rows)


RANK_LEVELS = [
    "Unrelated to the query",
    "Shares a topic with the query but does not help answer it",
    "Partially relevant: contains some useful information for the query",
    "Relevant: substantially addresses the query",
    "Directly and specifically answers or matches the query",
]


def cmd_rank(args):
    t0 = time.time()
    backend = args.backend
    base_url = base_url_for(backend, args.base_url)
    headers = auth_headers_for(backend)
    _require_local_running(backend)

    items, skipped, total_chars = collect_items(args.paths, lines=False, max_chars=args.max_chars, limit=args.limit, ext=args.ext)
    if not items:
        raise SystemExit("error: nothing to rank -- pass files, directories, or globs")

    def qfor(it):
        return {"type": "score", "instructions": {"query": args.query, "question": "How relevant is `content` to `query`?"},
                "criteria": RANK_LEVELS}

    rows, spend, stopped = _run_verb(items, qfor, backend, base_url, headers, args.parallel, args.max_spend, args.timeout)

    max_lvl = len(RANK_LEVELS) - 1
    scored = [(r, r["answer"]["score"] / max_lvl) for r in rows if r["ok"] and r.get("answer") and r["answer"].get("score") is not None]
    scored.sort(key=lambda rp: -rp[1])
    shown = scored[:args.top]
    out_lines = [f"{p:.2f}  {r['item']['id']}" for r, p in shown]

    if stopped:
        out_lines.append(f"(stopped: --max-spend ${args.max_spend:.4f} reached; {len(items) - len(rows)} item(s) not scored)")
    if skipped:
        out_lines.append(f"skipped {len(skipped)}: {_clip(', '.join(skipped), 300)}")

    _finish(args, t0, len(items), total_chars, out_lines, len(shown), 0, backend, spend, rows)


def cmd_find(args):
    t0 = time.time()
    backend = args.backend
    base_url = base_url_for(backend, args.base_url)
    headers = auth_headers_for(backend)
    _require_local_running(backend)

    path = args.path
    items, skipped, _ = collect_items([path], lines=False, max_chars=None, limit=1, ext=None)
    if not items:
        raise SystemExit(f"error: cannot read {path} (missing, secret-like, binary, or over "
                          f"{AGENT_VERB_MAX_FILE_BYTES // (1024 * 1024)}MB){': ' + skipped[0] if skipped else ''}")
    text = items[0]["text"]
    rel = items[0]["id"]

    all_lines = [(i + 1, l) for i, l in enumerate(text.split("\n")) if l.strip()]
    chunk_n, overlap = args.chunk, min(args.overlap, args.chunk - 1)
    chunks = []
    i = 0
    while i < len(all_lines):
        piece = all_lines[i:i + chunk_n]
        if not piece:
            break
        chunks.append(piece)
        if i + chunk_n >= len(all_lines):
            break
        i += max(1, chunk_n - overlap)

    def score_chunk(piece):
        line_map = {str(n): _clip(t, 400) for n, t in piece}
        state = {"query": args.description, "lines": line_map}
        questions = {
            "where": {"type": "choice", "instructions": "Which line number in `lines` best matches `query`?",
                      "criteria": {**{k: None for k in line_map}, "none": "No line matches `query`"}},
            "exists": {"type": "noul", "instructions": "Does any line in `lines` match `query`?"},
        }
        res = call_engine(backend, base_url, state, questions, headers=headers, timeout=args.timeout)
        return line_map, res

    hits, running_total, stopped = [], 0.0, False
    chunk_size = max(1, args.parallel)
    i = 0
    while i < len(chunks):
        if args.max_spend is not None and running_total >= args.max_spend:
            stopped = True
            break
        batch = chunks[i:i + chunk_size]
        if chunk_size > 1 and len(batch) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=chunk_size) as ex:
                batch_results = list(ex.map(score_chunk, batch))
        else:
            batch_results = [score_chunk(b) for b in batch]
        for line_map, res in batch_results:
            if not res["ok"]:
                continue
            cost = res.get("cost_usd")
            if cost:
                running_total += cost
            answers = (res["response"] or {}).get("answers", {}) if isinstance(res.get("response"), dict) else {}
            exists_p = (answers.get("exists") or {}).get("noul") or 0.0
            probs = (answers.get("where") or {}).get("probabilities") or {}
            for n, p in probs.items():
                if n == "none":
                    continue
                hits.append({"line": int(n), "text": line_map.get(n, ""), "score": p * exists_p})
        i += len(batch)

    hits.sort(key=lambda h: -h["score"])
    kept = [h for h in hits if h["score"] >= args.min_score][:args.top]
    out_lines = [f"{h['score']:.2f}  {rel}:{h['line']}  {_clip(h['text'].strip(), 160)}" for h in kept]
    if stopped:
        out_lines.append(f"(stopped: --max-spend ${args.max_spend:.4f} reached after {i}/{len(chunks)} chunk(s))")

    n_scanned = len(all_lines)
    total_chars = sum(len(t) for _, t in all_lines)
    log_rows = [{"item": {"id": f"{rel}:{h['line']}"}, "ok": True, "answer": {"score": h["score"]},
                 "cost_usd": None, "latency_s": None} for h in kept]
    _finish(args, t0, n_scanned, total_chars, out_lines, len(kept), 0, backend, running_total, log_rows)


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

    p_lint = sub.add_parser("lint", help="lint a questions.json for contract issues")
    p_lint.add_argument("--questions", required=True, help="JSON file: {question_id: {type, instructions, ...}}")
    p_lint.set_defaults(func=cmd_lint)

    p_replay = sub.add_parser("replay", help="re-run a previous ask run's states against a "
                                              "(possibly changed) questions file/backend and diff the answers")
    p_replay.add_argument("--results", required=True, help="JSONL from a previous `ask` run")
    p_replay.add_argument("--questions", required=True, help="the (possibly changed) questions JSON to replay against")
    p_replay.add_argument("--backend", required=True, choices=ALL_BACKENDS)
    p_replay.add_argument("--base-url", default=None, help="override the backend's default base URL")
    p_replay.add_argument("--items", default=None,
                           help="JSONL of {id, state} for the original run -- required unless the results "
                                "rows themselves carry a 'state' field (ask does not store one by default)")
    p_replay.add_argument("--out", required=True, help="output JSON diff report path")
    p_replay.add_argument("--parallel", type=int, default=1, help="concurrent item requests (default 1 = sequential)")
    p_replay.add_argument("--timeout", type=float, default=90.0)
    p_replay.add_argument("--max-spend", type=float, default=None,
                           help="same semantics as `ask --max-spend` -- stop issuing new requests once "
                                "cumulative usage.cost reaches this many USD")
    p_replay.set_defaults(func=cmd_replay)

    # ---- agent verbs (board #1540) ----
    def _add_common_verb_args(p):
        p.add_argument("--backend", choices=ALL_BACKENDS, default=RECOMMENDED_DEFAULT_BACKEND_FOR_INTERNAL_DATA,
                       help=f"default: {RECOMMENDED_DEFAULT_BACKEND_FOR_INTERNAL_DATA} (internal data only -- see SKILL.md)")
        p.add_argument("--base-url", default=None)
        p.add_argument("--ext", default=None, help="comma-separated extensions to keep, e.g. py,ts")
        p.add_argument("--max-chars", type=int, default=60000, help="truncate each file's content to this many chars")
        p.add_argument("--limit", type=int, default=5000, help="refuse to run over more than this many items")
        p.add_argument("--parallel", type=int, default=8, help="concurrent item requests")
        p.add_argument("--max-spend", type=float, default=None, help="stop issuing new requests once cumulative usage.cost (USD) reaches this")
        p.add_argument("--timeout", type=float, default=90.0)
        p.add_argument("--log", default=None, help="write a full JSONL receipt (one row per item) to this path")

    p_filter = sub.add_parser("filter", help="keep items where a yes/no question is answered yes")
    p_filter.add_argument("question")
    p_filter.add_argument("paths", nargs="+", help="files, directories, globs, or - for stdin")
    p_filter.add_argument("--lines", action="store_true", help="judge each non-blank line separately")
    p_filter.add_argument("--threshold", type=float, default=0.5)
    p_filter.add_argument("--band", default=None, help="borderline band: a half-width (e.g. 0.15) or explicit 'lo,hi'")
    _add_common_verb_args(p_filter)
    p_filter.set_defaults(func=cmd_filter)

    p_classify = sub.add_parser("classify", help="put each item in one labeled bucket")
    p_classify.add_argument("paths", nargs="*", help="files, directories, or globs (or use --items)")
    p_classify.add_argument("--labels", required=True, help="comma-separated label names, e.g. bug,feature,question")
    p_classify.add_argument("--items", default=None, help="JSONL of {id, text} instead of / in addition to paths")
    p_classify.add_argument("--question", default=None)
    p_classify.add_argument("--no-other", action="store_true", help="do not add an automatic no-match label")
    p_classify.add_argument("--min-confidence", type=float, default=0.6)
    _add_common_verb_args(p_classify)
    p_classify.set_defaults(func=cmd_classify)

    p_rank = sub.add_parser("rank", help="order items by relevance to a query")
    p_rank.add_argument("query")
    p_rank.add_argument("paths", nargs="+", help="files, directories, or globs")
    p_rank.add_argument("--top", type=int, default=10)
    _add_common_verb_args(p_rank)
    p_rank.set_defaults(func=cmd_rank)

    p_find = sub.add_parser("find", help="locate the line range(s) in one big file that match a description")
    p_find.add_argument("description")
    p_find.add_argument("path", help="one file")
    p_find.add_argument("--top", type=int, default=5)
    p_find.add_argument("--chunk", type=int, default=150, help="lines per chunk")
    p_find.add_argument("--overlap", type=int, default=30, help="overlapping lines between consecutive chunks")
    p_find.add_argument("--min-score", type=float, default=0.05)
    _add_common_verb_args(p_find)
    p_find.set_defaults(func=cmd_find)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
