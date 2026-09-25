"""Unit tests for scripts/jev.py -- no network. Every HTTP call is mocked via
monkeypatching `requests.post` (or the local port simply isn't listening, which
`cmd_ask`/`cmd_start` must handle without ever dialing out).

Run: `python3 -m pytest skills/system-one-decisions/evals/test_jev.py -v`
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "jev.py"

spec = importlib.util.spec_from_file_location("jev", SCRIPT_PATH)
jev = importlib.util.module_from_spec(spec)
sys.modules["jev"] = jev
spec.loader.exec_module(jev)


# ---------------------------------------------------------------------------
# Backend selection / base URL / auth
# ---------------------------------------------------------------------------

def test_local_backend_base_url_is_localhost_only():
    for backend in jev.LOCAL_BACKENDS:
        url = jev.base_url_for(backend)
        assert url.startswith("http://127.0.0.1:"), f"{backend} base url must be localhost-only, got {url}"


def test_local_backend_ports_are_distinct_and_match_engines_md():
    ports = {b: cfg["port"] for b, cfg in jev.LOCAL_BACKENDS.items()}
    assert ports == {"local-laya": 8781, "local-decider": 8782, "local-semif": 8783}


def test_base_url_override_wins():
    assert jev.base_url_for("local-decider", override="http://example.test:9999") == "http://example.test:9999"


def test_remote_backend_default_base_url(monkeypatch):
    monkeypatch.delenv("JEV_TYPESAFE_BASE", raising=False)
    monkeypatch.delenv("JEV_OPENROUTER_BASE", raising=False)
    assert jev.base_url_for("typesafe") == "https://api.typesafe.ai"
    assert jev.base_url_for("openrouter") == "https://openrouter.ai/api"


def test_remote_backend_base_url_env_override(monkeypatch):
    monkeypatch.setenv("JEV_TYPESAFE_BASE", "https://staging.typesafe.example")
    assert jev.base_url_for("typesafe") == "https://staging.typesafe.example"


def test_missing_api_key_fails_clearly_and_does_not_print_a_key(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        jev.auth_headers_for("typesafe")
    msg = str(exc.value)
    assert "TYPESAFE_API_KEY" in msg
    assert "sk-" not in msg  # nothing that looks like a leaked key


def test_present_api_key_produces_bearer_header_and_is_not_logged(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-should-not-be-printed-12345")
    headers = jev.auth_headers_for("openrouter")
    assert headers["Authorization"] == "Bearer sk-test-should-not-be-printed-12345"
    out, err = capsys.readouterr()
    assert "sk-test-should-not-be-printed-12345" not in out
    assert "sk-test-should-not-be-printed-12345" not in err


def test_local_backend_has_no_auth_header():
    assert jev.auth_headers_for("local-decider") == {}


# ---------------------------------------------------------------------------
# start: refuses a second local engine
# ---------------------------------------------------------------------------

def test_start_refuses_second_engine(monkeypatch, tmp_path):
    # pretend local-decider is already listening
    monkeypatch.setattr(jev, "port_listening", lambda port, host="127.0.0.1", timeout=0.5: port == jev.LOCAL_BACKENDS["local-decider"]["port"])
    monkeypatch.setenv("JEV_LOCAL_DIR", str(tmp_path))

    args = type("Args", (), {"backend": "local-laya", "startup_timeout": 5.0})()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_start(args)
    assert "local-decider" in str(exc.value)
    assert "Only one local engine" in str(exc.value)


def test_start_rejects_non_local_backend():
    args = type("Args", (), {"backend": "openrouter", "startup_timeout": 5.0})()
    with pytest.raises(SystemExit):
        jev.cmd_start(args)


def test_start_missing_jev_local_dir_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: False)
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("JEV_LOCAL_DIR", str(missing))
    args = type("Args", (), {"backend": "local-decider", "startup_timeout": 5.0})()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_start(args)
    assert "JEV_LOCAL_DIR" in str(exc.value) or str(missing) in str(exc.value)


def test_jev_local_dir_defaults_to_rick_rnd_prototypes_path(monkeypatch):
    monkeypatch.delenv("JEV_LOCAL_DIR", raising=False)
    d = jev.jev_local_dir()
    assert str(d).endswith("claude-workspaces/Rick_RnD/prototypes/jev-local")


def test_jev_local_dir_env_override(monkeypatch):
    monkeypatch.setenv("JEV_LOCAL_DIR", "/tmp/some-other-jev-local")
    assert str(jev.jev_local_dir()) == "/tmp/some-other-jev-local"


# ---------------------------------------------------------------------------
# ask: mocked HTTP, resume, backend validation
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _noul_response(p_yes):
    return {"model": "fake-engine", "answers": {"bucket": {"type": "noul", "noul": p_yes}}}


def test_call_engine_success(monkeypatch):
    calls = []

    def fake_post(url, json, headers=None, timeout=None):
        calls.append((url, json, headers))
        return _FakeResponse(_noul_response(0.9))

    monkeypatch.setattr("requests.post", fake_post)
    res = jev.call_engine("local-decider", "http://127.0.0.1:8782", {"body": "hi"}, {"bucket": {"type": "noul"}})
    assert res["ok"] is True
    assert res["response"]["answers"]["bucket"]["noul"] == 0.9
    assert calls[0][0] == "http://127.0.0.1:8782/v1/systemone"


def test_call_engine_failure_is_caught_not_raised(monkeypatch):
    def fake_post(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr("requests.post", fake_post)
    res = jev.call_engine("local-decider", "http://127.0.0.1:8782", "state", {})
    assert res["ok"] is False
    assert "boom" in res["error"]


# ---------------------------------------------------------------------------
# openrouter wire format (VERIFIED LIVE 2026-09-25, 230/230 calls OK --
# prototypes/jev-local/bench/results/20260925-openrouter/) -- different
# endpoint + body shape than the local/typesafe /v1/systemone protocol.
# ---------------------------------------------------------------------------

# Adapted from the verified live sample row (jev113_fr_set.jsonl line 1,
# 2026-09-25 -- no key in it, as the coordinator confirmed) -- see
# references/engines.md for the full run's summary.
OPENROUTER_SAMPLE_RESPONSE = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "routing": {
            "type": "choice", "choice": "facturation",
            "probabilities": {"commercial": 0, "resiliation": 0, "support_technique": 0,
                               "conformite": 0, "facturation": 1},
            "confidence": 1,
        },
    },
    "usage": {"input_tokens": 477, "output_tokens": 64, "cost": 2.0034e-05},
    "id": "gen-dec-1790343415-inSeHAXsszvgcV2Ok5hm",
    "provider": "TypeSafe",
}


def test_request_url_and_payload_openrouter_uses_decisions_endpoint_and_model():
    url, payload = jev.request_url_and_payload("openrouter", "https://openrouter.ai/api",
                                                 "state text", {"q": {"type": "noul"}})
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert payload["model"] == "typesafe/jev-1.13"
    assert payload["state"] == "state text"
    assert payload["questions"] == {"q": {"type": "noul"}}


def test_request_url_and_payload_local_backend_unchanged():
    url, payload = jev.request_url_and_payload("local-decider", "http://127.0.0.1:8782", "s", {"q": {}})
    assert url == "http://127.0.0.1:8782/v1/systemone"
    assert "model" not in payload


def test_request_url_and_payload_typesafe_direct_has_no_model_field():
    url, payload = jev.request_url_and_payload("typesafe", "https://api.typesafe.ai", "s", {"q": {}})
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert "model" not in payload


def test_call_engine_openrouter_uses_verified_endpoint_and_extracts_cost(monkeypatch):
    calls = []

    def fake_post(url, json, headers=None, timeout=None):
        calls.append((url, json, headers))
        return _FakeResponse(OPENROUTER_SAMPLE_RESPONSE)

    monkeypatch.setattr("requests.post", fake_post)
    res = jev.call_engine("openrouter", "https://openrouter.ai/api", "quote text",
                           {"routing": {"type": "choice"}}, headers={"Authorization": "Bearer x"})
    assert res["ok"] is True
    assert res["cost_usd"] == pytest.approx(2.0034e-05)
    url, payload, headers = calls[0]
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert payload["model"] == "typesafe/jev-1.13"
    assert headers == {"Authorization": "Bearer x"}


def test_call_engine_local_backend_has_no_cost(monkeypatch):
    def fake_post(url, json, headers=None, timeout=None):
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post)
    res = jev.call_engine("local-decider", "http://127.0.0.1:8782", "s", {"bucket": {"type": "noul"}})
    assert res["ok"] is True
    assert res["cost_usd"] is None


def test_cmd_ask_rejects_unknown_backend(tmp_path):
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "state": "x"}\n')
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out = tmp_path / "out.jsonl"
    args = type("Args", (), {
        "backend": "not-a-real-backend", "questions": str(questions), "items": str(items),
        "out": str(out), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    with pytest.raises(SystemExit):
        jev.cmd_ask(args)


def test_cmd_ask_resume_skips_completed_ids(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"
    # pre-seed out.jsonl with item "a" already OK
    out_path.write_text(json.dumps({"id": "a", "ok": True, "response": _noul_response(0.7)}) + "\n")

    calls = []

    def fake_post(url, json, headers=None, timeout=None):
        calls.append(json["state"])
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": True, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    jev.cmd_ask(args)

    # only "b" should have been called over HTTP -- "a" was already done
    assert calls == ["item b"]
    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    ids = {r["id"] for r in rows if not r.get("_meta")}
    assert ids == {"a", "b"}


def test_cmd_ask_refuses_when_local_engine_not_running(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text(json.dumps({"id": "a", "state": "x"}) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: False)
    args = type("Args", (), {
        "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_ask(args)
    assert "not running" in str(exc.value)


def test_cmd_ask_remote_backend_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    items_path = tmp_path / "items.jsonl"
    items_path.write_text(json.dumps({"id": "a", "state": "x"}) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"
    args = type("Args", (), {
        "backend": "typesafe", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_ask(args)
    assert "TYPESAFE_API_KEY" in str(exc.value)


# ---------------------------------------------------------------------------
# --max-spend guard (openrouter carries real usage.cost; local backends don't)
# ---------------------------------------------------------------------------

def test_cmd_ask_max_spend_guard_stops_early(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b", "c", "d"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"routing": {"type": "choice", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    def fake_post(url, json, headers=None, timeout=None):
        resp = dict(OPENROUTER_SAMPLE_RESPONSE)
        resp["usage"] = {"input_tokens": 100, "output_tokens": 10, "cost": 0.01}
        return _FakeResponse(resp)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    args = type("Args", (), {
        "backend": "openrouter", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None,
        "max_spend": 0.025,  # 3 calls @ $0.01 = $0.03 >= cap -> stops after the 3rd
    })()
    jev.cmd_ask(args)

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    data_rows = [r for r in rows if not r.get("_meta")]
    meta = next(r for r in rows if r.get("_meta"))
    assert len(data_rows) == 3  # "d" never attempted
    assert meta.get("stopped_for_spend") is True
    assert meta["total_spend_usd"] == pytest.approx(0.03)


def test_cmd_ask_no_max_spend_means_unbounded(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"routing": {"type": "choice", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    def fake_post(url, json, headers=None, timeout=None):
        resp = dict(OPENROUTER_SAMPLE_RESPONSE)
        resp["usage"] = {"input_tokens": 100, "output_tokens": 10, "cost": 100.0}  # deliberately huge
        return _FakeResponse(resp)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    args = type("Args", (), {
        "backend": "openrouter", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None,
        "max_spend": None,
    })()
    jev.cmd_ask(args)
    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    data_rows = [r for r in rows if not r.get("_meta")]
    assert len(data_rows) == 2  # both ran -- no cap set, guard never fires


# ---------------------------------------------------------------------------
# incremental writes -- a kill mid-run must not lose already-computed rows,
# and --resume must genuinely pick up where it left off (not just claim to)
# ---------------------------------------------------------------------------

def test_cmd_ask_incremental_write_survives_interruption_and_resumes(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b", "c", "d"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    calls = []

    def fake_post_kill_on_third(url, json, headers=None, timeout=None):
        calls.append(json["state"])
        if len(calls) == 3:
            # Something call_engine's `except Exception` does NOT swallow --
            # propagates all the way out of cmd_ask, exactly like a real
            # process kill/interrupt would. If cmd_ask only wrote its output
            # at the very end (the pre-fix behavior), this would lose every
            # row computed so far, including the two calls before this one.
            raise KeyboardInterrupt("simulated kill mid-run")
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post_kill_on_third)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    def make_args(resume):
        return type("Args", (), {
            "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
            "out": str(out_path), "resume": resume, "parallel": 1, "timeout": 5.0, "base_url": None,
            "max_spend": None,
        })()

    with pytest.raises(KeyboardInterrupt):
        jev.cmd_ask(make_args(resume=False))

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    ids_written = [r["id"] for r in rows if not r.get("_meta")]
    # "a" and "b" (the 2 calls before the simulated kill) are already on disk
    # -- the whole point of per-row flush+fsync. "c" (the killed call) and
    # "d" (never reached) are NOT written.
    assert ids_written == ["a", "b"]

    # --resume must re-call only "c" and "d" -- not re-pay for "a"/"b", and
    # not silently skip "c" either.
    calls.clear()

    def fake_post_resume(url, json, headers=None, timeout=None):
        calls.append(json["state"])
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post_resume)
    jev.cmd_ask(make_args(resume=True))
    assert calls == ["item c", "item d"]

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    ids_written = sorted(r["id"] for r in rows if not r.get("_meta"))
    assert ids_written == ["a", "b", "c", "d"]


def test_cmd_ask_resume_handles_torn_last_line_without_crashing(monkeypatch, tmp_path):
    """Per-row fsync (tested above) guarantees a FINISHED write is durable,
    but os.write() of a multi-KB line is not atomic -- a kill at the wrong
    instant can still leave a partial line with no trailing newline: a
    fragment json.loads() can't parse. A naive --resume that calls plain
    json.loads() on every line would crash here. It must instead: drop the
    fragment, treat its item as not-done, and rewrite --out to contain only
    the valid lines BEFORE appending anything new -- so the new row for the
    re-done item never lands concatenated onto the old fragment."""
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    # "a" finished writing cleanly. "b"'s row was mid-write() when the
    # process died: a truncated JSON fragment, no trailing newline.
    valid_row = json.dumps({"id": "a", "backend": "local-decider", "ok": True, "response": _noul_response(0.5)})
    torn_fragment = '{"id": "b", "backend": "local-decider", "latency_s": 0.4, "ok": true, "respo'
    out_path.write_bytes((valid_row + "\n" + torn_fragment).encode())  # deliberately no trailing newline

    calls = []

    def fake_post(url, json, headers=None, timeout=None):
        calls.append(json["state"])
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": True, "parallel": 1, "timeout": 5.0, "base_url": None,
        "max_spend": None,
    })()

    jev.cmd_ask(args)  # must NOT raise json.JSONDecodeError

    # "b" (the torn item) got re-done over HTTP; "a" (the clean, valid row) did not.
    assert calls == ["item b"]

    # the file parses fully now, end to end -- no leftover fragment anywhere.
    parsed_rows = []
    with open(out_path) as f:
        for line in f:
            line = line.strip()
            if line:
                parsed_rows.append(json.loads(line))  # must not raise

    ids = [r["id"] for r in parsed_rows if not r.get("_meta")]
    assert sorted(ids) == ["a", "b"]
    assert len(ids) == len(set(ids))  # no duplicates -- "a" wasn't re-appended


# ---------------------------------------------------------------------------
# eval math
# ---------------------------------------------------------------------------

def test_trivial_baseline_accuracy():
    labels = ["a"] * 34 + ["b"] * 3 + ["c"] * 3
    assert jev.trivial_baseline_accuracy(labels) == pytest.approx(34 / 40)


def test_trivial_baseline_accuracy_empty():
    assert jev.trivial_baseline_accuracy([]) is None


def test_prf_perfect():
    prec, rec, f1 = jev.prf(tp=10, fp=0, fn=0)
    assert (prec, rec, f1) == (1.0, 1.0, 1.0)


def test_prf_no_positives_predicted():
    prec, rec, f1 = jev.prf(tp=0, fp=0, fn=5)
    assert prec == 0.0 and rec == 0.0 and f1 == 0.0


def test_confusion_at_threshold():
    pairs = [(0.9, True), (0.8, True), (0.2, False), (0.6, False)]
    tp, fp, fn, tn = jev.confusion_at_threshold(pairs, 0.5)
    assert (tp, fp, fn, tn) == (2, 1, 0, 1)


def test_best_threshold_picks_perfect_separator():
    pairs = [(0.9, True), (0.85, True), (0.1, False), (0.05, False)]
    t, f1 = jev.best_threshold(pairs)
    tp, fp, fn, tn = jev.confusion_at_threshold(pairs, t)
    prec, rec, actual_f1 = jev.prf(tp, fp, fn)
    assert actual_f1 == pytest.approx(1.0)


def test_ece_perfectly_calibrated_is_zero():
    # confidence == empirical accuracy in every bin -> ECE 0
    rows = [(0.9, True)] * 9 + [(0.9, False)] * 1  # bin ~0.9, 90% correct
    ece = jev.compute_ece(rows, n_bins=10)
    assert ece == pytest.approx(0.0, abs=0.01)


def test_ece_overconfident_is_penalized():
    rows = [(0.95, False)] * 10  # says 95% sure, always wrong
    ece = jev.compute_ece(rows, n_bins=10)
    assert ece > 0.9


def test_extract_prediction_noul():
    pred, probs, p = jev.extract_prediction({"type": "noul", "noul": 0.83})
    assert pred == "yes"
    assert p == pytest.approx(0.83)


def test_extract_prediction_choice():
    ans = {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.7, "tech": 0.3}}
    pred, probs, p = jev.extract_prediction(ans)
    assert pred == "billing"
    assert p == pytest.approx(0.7)


def test_extract_prediction_handles_error_object():
    pred, probs, p = jev.extract_prediction({"error": "timeout"})
    assert pred is None and probs is None and p is None


def test_cmd_eval_end_to_end(tmp_path):
    results_path = tmp_path / "results.jsonl"
    results_path.write_text("\n".join([
        json.dumps({"id": "a", "ok": True, "response": _noul_response(0.9)}),
        json.dumps({"id": "b", "ok": True, "response": _noul_response(0.1)}),
        json.dumps({"id": "c", "ok": True, "response": _noul_response(0.4)}),
    ]) + "\n")
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text("\n".join([
        json.dumps({"id": "a", "question_id": "bucket", "gold": True}),
        json.dumps({"id": "b", "question_id": "bucket", "gold": False}),
        json.dumps({"id": "c", "question_id": "bucket", "gold": False}),
    ]) + "\n")
    out_path = tmp_path / "report.json"

    args = type("Args", (), {"results": str(results_path), "gold": str(gold_path), "out": str(out_path)})()
    jev.cmd_eval(args)

    report = json.loads(out_path.read_text())
    q = report["questions"]["bucket"]
    assert q["n_gold"] == 3
    assert q["type"] == "noul"
    assert q["accuracy_at_0.5"] == pytest.approx(1.0)  # 0.9->yes(correct), 0.1->no(correct), 0.4->no(correct)
    assert q["trivial_baseline_accuracy"] == pytest.approx(2 / 3)  # majority label is "no" (2 of 3)


def test_openrouter_prefers_dedicated_jev_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "generic-key")
    monkeypatch.setenv("JEV_OPENROUTER_API_KEY", "dedicated-key")
    assert jev.auth_headers_for("openrouter")["Authorization"] == "Bearer dedicated-key"


def test_openrouter_falls_back_to_generic_key(monkeypatch):
    monkeypatch.delenv("JEV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "generic-key")
    assert jev.auth_headers_for("openrouter")["Authorization"] == "Bearer generic-key"


def test_openrouter_missing_both_keys_names_both(monkeypatch):
    monkeypatch.delenv("JEV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import pytest
    with pytest.raises(SystemExit) as e:
        jev.auth_headers_for("openrouter")
    assert "JEV_OPENROUTER_API_KEY" in str(e.value) and "OPENROUTER_API_KEY" in str(e.value)
