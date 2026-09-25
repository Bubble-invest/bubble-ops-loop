#!/usr/bin/env python3
"""jev.py -- one CLI/library interface over swappable Jev / System-One backends.

Backends (all speak the same wire format -- swapping is a config change, not a
rewrite): local-decider (default) | local-semif | local-laya | openrouter | typesafe.
See ../SKILL.md for when to use which, and ../references/engines.md for measured
perf/failure modes per backend.

Wire format, identical across every backend (see prototypes/jev-local/README.md
in the R&D workspace, and bench/harness.py / pilot1-wiki-intent/scripts/run_engine.py,
which this script's HTTP client logic is adapted from):

    POST <base_url>/v1/systemone
    {"state": <str-or-dict>, "questions": {"<qid>": {"type": "choice"|"score"|"noul",
                                                       "instructions": "...",
                                                       "criteria": {...}}}}
    -> {"model": "...", "answers": {"<qid>": {"type": ..., "choice": ..., "noul": ...,
                                               "probabilities": {...}, "legend": {...}}}}

Subcommands:
  start / stop / status   -- manage ONE local engine server (local-* backends only)
  ask                      -- score items (JSONL) against one or more questions -> JSONL
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
    # Both remote backends are assumed to speak the same /v1/systemone wire format
    # as the local engines (TypeSafe's own protocol -- see references/engines.md).
    # NOTE (open question, flagged in the PR): the exact OpenRouter proxy path for
    # typesafe/jev-1.13 was not independently verified against a live account for
    # this build -- verify against a real key before relying on the openrouter
    # backend in production; typesafe (the direct API) is the better-documented path.
    "openrouter": {
        "base_url_env": "JEV_OPENROUTER_BASE",
        "default_base_url": "https://openrouter.ai/api",
        "key_env": "OPENROUTER_API_KEY",
    },
    "typesafe": {
        "base_url_env": "JEV_TYPESAFE_BASE",
        "default_base_url": "https://api.typesafe.ai",
        "key_env": "TYPESAFE_API_KEY",
    },
}
ALL_BACKENDS = list(LOCAL_BACKENDS) + list(REMOTE_BACKENDS)

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

def call_engine(base_url, state, questions, headers=None, timeout=90):
    """POST one /v1/systemone request. Returns {"ok", "latency_s", "response"|"error"}.
    Adapted from bench/harness.py / pilot1-wiki-intent/scripts/run_engine.py's
    call_engine() -- same wire format, same shape of result, reused rather than
    reinvented."""
    import requests  # imported lazily so `eval`/`start`/`stop`/`status` never need it

    payload = {"state": state, "questions": questions}
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{base_url}/v1/systemone", json=payload, headers=headers or {}, timeout=timeout)
        elapsed = time.perf_counter() - t0
        r.raise_for_status()
        return {"ok": True, "latency_s": elapsed, "response": r.json()}
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

    done_ids = set()
    kept_rows = []
    if args.resume:
        try:
            for row in load_jsonl(args.out):
                if row.get("_meta"):
                    continue
                if row.get("ok"):
                    kept_rows.append(row)
                    done_ids.add(row["id"])
        except FileNotFoundError:
            pass
        n_before = len(items)
        items = [it for it in items if it["id"] not in done_ids]
        print(f"[resume] kept {len(kept_rows)} already-OK rows, skipping them; "
              f"{n_before - len(items)} items skipped, {len(items)} remaining", file=sys.stderr)

    def score_one(item):
        res = call_engine(base_url, item["state"], questions, headers=headers, timeout=args.timeout)
        row = {"id": item["id"], "backend": args.backend, "latency_s": res["latency_s"], "ok": res["ok"]}
        if res["ok"]:
            row["response"] = res["response"]
        else:
            row["error"] = res["error"]
        return row

    results = list(kept_rows)
    n_errors = sum(1 for r in kept_rows if not r.get("ok", True))
    t_start = time.time()

    if args.parallel > 1 and len(items) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            for i, row in enumerate(ex.map(score_one, items)):
                results.append(row)
                if not row["ok"]:
                    n_errors += 1
                if (i + 1) % 25 == 0 or (i + 1) == len(items):
                    print(f"[{args.backend}] {i+1}/{len(items)} ({time.time()-t_start:.0f}s, {n_errors} errors)", file=sys.stderr)
    else:
        for i, item in enumerate(items):
            row = score_one(item)
            results.append(row)
            if not row["ok"]:
                n_errors += 1
            if (i + 1) % 25 == 0 or (i + 1) == len(items):
                print(f"[{args.backend}] {i+1}/{len(items)} ({time.time()-t_start:.0f}s, {n_errors} errors)", file=sys.stderr)

    with open(args.out, "w") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.write(json.dumps({"_meta": True, "backend": args.backend, "n": len(results),
                             "n_errors": n_errors, "questions_file": args.questions}) + "\n")

    print(f"[{args.backend}] done: {len(results)} items, {n_errors} errors -> {args.out}", file=sys.stderr)


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
