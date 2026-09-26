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


# ---------------------------------------------------------------------------
# Decision contract: state_digest / question_set_version / receipt fields
# (board #1505, ../references/contract.md)
# ---------------------------------------------------------------------------

def test_state_digest_is_deterministic_and_order_independent():
    a = jev.state_digest({"x": 1, "y": 2})
    b = jev.state_digest({"y": 2, "x": 1})  # different key order, same object
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_state_digest_differs_for_different_state():
    assert jev.state_digest({"x": 1}) != jev.state_digest({"x": 2})


def test_strip_meta_keys_removes_underscore_prefixed_only():
    raw = {"_version": "v1", "route": {"type": "noul"}, "_other_meta": 1}
    assert jev.strip_meta_keys(raw) == {"route": {"type": "noul"}}


def test_question_set_version_uses_explicit_version_key():
    raw = {"_version": "v7", "route": {"type": "noul", "instructions": "?"}}
    assert jev.question_set_version(raw) == "v7"


def test_question_set_version_falls_back_to_content_hash_when_unversioned():
    raw = {"route": {"type": "noul", "instructions": "?"}}
    v = jev.question_set_version(raw)
    assert len(v) == 64  # sha256 hex, not a placeholder
    # deterministic and independent of an added meta key (meta stripped before hashing)
    raw_with_meta = {"_ignored": "anything", "route": {"type": "noul", "instructions": "?"}}
    assert jev.question_set_version(raw_with_meta) == v
    # changing an actual question changes the version
    changed = {"route": {"type": "noul", "instructions": "different?"}}
    assert jev.question_set_version(changed) != v


def test_ask_row_carries_decision_receipt_fields(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text(json.dumps({"id": "a", "state": {"message": "hi"}}) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    def fake_post(url, json, headers=None, timeout=None):
        resp = dict(_noul_response(0.7))
        resp["model"] = "fake-engine-served"
        return _FakeResponse(resp)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    jev.cmd_ask(args)

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    row = next(r for r in rows if not r.get("_meta"))
    meta = next(r for r in rows if r.get("_meta"))

    expected_qsv = jev.question_set_version({"bucket": {"type": "noul", "instructions": "?"}})
    assert row["state_digest"] == jev.state_digest({"message": "hi"})
    assert row["question_set_version"] == expected_qsv
    assert row["model"] == "fake-engine-served"
    assert row["latency_ms"] == pytest.approx(row["latency_s"] * 1000.0)
    assert "timestamp" in row and row["timestamp"]
    # existing fields untouched
    assert row["response"]["answers"]["bucket"]["noul"] == 0.7
    assert meta["question_set_version"] == expected_qsv


def test_ask_strips_meta_keys_before_sending_over_the_wire(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text(json.dumps({"id": "a", "state": "s"}) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"_version": "v1", "bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"

    sent_payloads = []

    def fake_post(url, json, headers=None, timeout=None):
        sent_payloads.append(json)
        return _FakeResponse(_noul_response(0.5))

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "backend": "local-decider", "questions": str(questions_path), "items": str(items_path),
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None, "max_spend": None,
    })()
    jev.cmd_ask(args)

    assert "_version" not in sent_payloads[0]["questions"]
    assert "bucket" in sent_payloads[0]["questions"]

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    row = next(r for r in rows if not r.get("_meta"))
    assert row["question_set_version"] == "v1"


# ---------------------------------------------------------------------------
# Version-aware --resume (a changed question_set_version must never reuse an
# old answer; a legacy row with no recorded version is still resumable)
# ---------------------------------------------------------------------------

def test_resume_reruns_item_when_recorded_version_differs(monkeypatch, tmp_path):
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"_version": "v2", "bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"
    # "a" was answered under the OLD contract (v1) -- must be re-run under v2.
    out_path.write_text(json.dumps({
        "id": "a", "ok": True, "backend": "local-decider", "question_set_version": "v1",
        "response": _noul_response(0.7),
    }) + "\n")

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

    # both "a" (stale version) and "b" (never run) get called
    assert calls == ["item a", "item b"]
    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    new_a_rows = [r for r in rows if r.get("id") == "a" and r.get("question_set_version") == "v2"]
    assert len(new_a_rows) == 1


def test_resume_keeps_legacy_row_with_no_recorded_version(monkeypatch, tmp_path):
    """A row written before this upgrade has no question_set_version field at
    all. There is no way to know whether it matches the current contract, so
    -- unlike an explicit mismatch -- it is still treated as done (backward
    compatible with every --resume file written before board #1505's receipt
    upgrade)."""
    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": i, "state": f"item {i}"}) for i in ["a", "b"]) + "\n")
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out_path = tmp_path / "out.jsonl"
    out_path.write_text(json.dumps({"id": "a", "ok": True, "backend": "local-decider",
                                     "response": _noul_response(0.7)}) + "\n")

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

    assert calls == ["item b"]  # "a" was skipped, same as pre-upgrade behavior


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

def test_lint_clean_questions_has_no_findings():
    questions = {
        "route": {"type": "choice", "instructions": "Which team?",
                   "criteria": {"billing": "Payments", "technical": "Bugs", "other": "None of the above"}},
    }
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert errors == [] and warnings == []


def test_lint_warns_choice_without_no_match_option():
    questions = {"route": {"type": "choice", "instructions": "Which?",
                            "criteria": {"billing": "x", "technical": "y"}}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert errors == []
    assert any("no-match" in w for w in warnings)


def test_lint_errors_on_missing_instructions():
    questions = {"bucket": {"type": "noul"}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert any("instructions" in e for e in errors)


def test_lint_allows_noul_with_criteria_instead_of_instructions():
    questions = {"bucket": {"type": "noul", "criteria": {"true": "is urgent", "false": "not urgent"}}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert errors == []


def test_lint_errors_on_empty_choice_criteria():
    questions = {"route": {"type": "choice", "instructions": "Which?", "criteria": {}}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert any("criteria" in e for e in errors)


def test_lint_warns_score_with_fewer_than_two_levels():
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["Critical"]}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert errors == []
    assert any("fewer than 2 levels" in w for w in warnings)


def test_lint_warns_score_with_duplicate_levels():
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["Low", "Low", "High"]}}
    errors, warnings = jev.lint_questions(questions, dup_ids=[])
    assert any("duplicate level" in w for w in warnings)


def test_lint_flags_duplicate_question_ids_as_error():
    errors, warnings = jev.lint_questions(
        {"route": {"type": "noul", "instructions": "?"}}, dup_ids=["route"])
    assert any("duplicate question id 'route'" in e for e in errors)


def test_lint_detects_duplicate_ids_from_raw_json_text(tmp_path):
    # a plain dict literally can't hold two "route" keys -- the duplicate only
    # exists in the raw text, so this exercises the object_pairs_hook path.
    raw = '{"route": {"type": "noul", "instructions": "first"}, "route": {"type": "noul", "instructions": "second"}}'
    qpath = tmp_path / "dup.json"
    qpath.write_text(raw)
    questions, dup_ids = jev._load_questions_detecting_duplicates(str(qpath))
    assert dup_ids == ["route"]
    assert questions["route"]["instructions"] == "second"  # plain JSON semantics: last wins


def test_cmd_lint_exits_nonzero_on_error_only(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"bucket": {"type": "noul"}}))
    args = type("Args", (), {"questions": str(bad)})()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_lint(args)
    assert exc.value.code == 1


def test_cmd_lint_exits_zero_with_only_warnings(tmp_path):
    ok_with_warning = tmp_path / "warn.json"
    ok_with_warning.write_text(json.dumps({
        "route": {"type": "choice", "instructions": "Which?", "criteria": {"billing": "x", "technical": "y"}},
    }))
    args = type("Args", (), {"questions": str(ok_with_warning)})()
    jev.cmd_lint(args)  # must not raise


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _write_ask_style_results(path, rows, meta=None):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        if meta is not None:
            f.write(json.dumps(meta) + "\n")


def test_replay_reports_flipped_ids_and_mean_delta(monkeypatch, tmp_path):
    old_qsv = jev.question_set_version({"route": {"type": "choice", "instructions": "old", "criteria": {"billing": "x", "technical": "y"}}})
    old_rows = [
        {"id": "a", "ok": True, "response": {"model": "old", "answers": {
            "route": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9, "technical": 0.1}}}}},
        {"id": "b", "ok": True, "response": {"model": "old", "answers": {
            "route": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.8, "technical": 0.2}}}}},
    ]
    results_path = tmp_path / "results.jsonl"
    _write_ask_style_results(results_path, old_rows, meta={"_meta": True, "question_set_version": old_qsv})

    items_path = tmp_path / "items.jsonl"
    items_path.write_text("\n".join(json.dumps({"id": r["id"], "state": f"state-{r['id']}"}) for r in old_rows) + "\n")

    questions_path = tmp_path / "q_new.json"
    questions_path.write_text(json.dumps({"_version": "v2",
        "route": {"type": "choice", "instructions": "new", "criteria": {"billing": "x", "technical": "y"}}}))

    def fake_post(url, json, headers=None, timeout=None):
        state = json["state"]
        flip = state == "state-a"
        return _FakeResponse({"model": "new", "answers": {
            "route": {"type": "choice", "choice": "technical" if flip else "billing",
                       "probabilities": {"billing": 0.3 if flip else 0.85, "technical": 0.7 if flip else 0.15}}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    out_path = tmp_path / "diff.json"
    args = type("Args", (), {
        "backend": "local-decider", "results": str(results_path), "questions": str(questions_path),
        "items": str(items_path), "out": str(out_path), "base_url": None, "parallel": 1,
        "timeout": 5.0, "max_spend": None,
    })()
    jev.cmd_replay(args)

    report = json.loads(out_path.read_text())
    assert report["old_question_set_version"] == old_qsv
    assert report["new_question_set_version"] == "v2"
    q = report["questions"]["route"]
    assert q["n_compared"] == 2
    assert q["n_changed_class"] == 1
    assert q["flipped_ids"] == ["a"]
    # delta is |Δp of each item's OWN predicted label|, not the raw probability of one
    # fixed option: a flips billing(0.9)->technical(0.7), |0.7-0.9|=0.2; b stays on
    # billing, 0.8->0.85, |0.85-0.8|=0.05 -> mean (0.2+0.05)/2 = 0.125
    assert q["mean_abs_delta_p"] == pytest.approx(0.125)


def test_replay_requires_items_when_state_not_recoverable(monkeypatch, tmp_path):
    old_rows = [{"id": "a", "ok": True, "response": {"answers": {
        "route": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9}}}}}]
    results_path = tmp_path / "results.jsonl"
    _write_ask_style_results(results_path, old_rows)
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"route": {"type": "choice", "instructions": "?", "criteria": {"billing": "x"}}}))

    args = type("Args", (), {
        "backend": "local-decider", "results": str(results_path), "questions": str(questions_path),
        "items": None, "out": str(tmp_path / "out.json"), "base_url": None, "parallel": 1,
        "timeout": 5.0, "max_spend": None,
    })()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_replay(args)
    assert "--items" in str(exc.value)


def test_replay_uses_state_embedded_in_results_row_when_no_items_given(monkeypatch, tmp_path):
    old_rows = [{"id": "a", "ok": True, "state": "embedded state", "response": {"answers": {
        "route": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9}}}}}]
    results_path = tmp_path / "results.jsonl"
    _write_ask_style_results(results_path, old_rows)
    questions_path = tmp_path / "q.json"
    questions_path.write_text(json.dumps({"route": {"type": "choice", "instructions": "?", "criteria": {"billing": "x"}}}))

    seen_states = []

    def fake_post(url, json, headers=None, timeout=None):
        seen_states.append(json["state"])
        return _FakeResponse({"answers": {"route": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9}}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "backend": "local-decider", "results": str(results_path), "questions": str(questions_path),
        "items": None, "out": str(tmp_path / "out.json"), "base_url": None, "parallel": 1,
        "timeout": 5.0, "max_spend": None,
    })()
    jev.cmd_replay(args)
    assert seen_states == ["embedded state"]


# ---------------------------------------------------------------------------
# Agent verbs (board #1540): filter / classify / rank / find
# Design ported from quicksilver (MIT) -- see SKILL.md's "Agent verbs" section.
# ---------------------------------------------------------------------------

def _common_verb_kwargs(**overrides):
    base = {
        "base_url": None, "ext": None, "max_chars": 60000, "limit": 5000,
        "parallel": 1, "max_spend": None, "timeout": 5.0, "log": None,
    }
    base.update(overrides)
    return base


# -- collect_items: safety (secret-like files, binaries, oversized, .gitignore) --

def test_collect_items_skips_secret_like_files(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".env.production").write_text("SECRET=1")
    (tmp_path / "id_ed25519").write_text("PRIVATE KEY")
    (tmp_path / "server.pem").write_text("CERT")
    (tmp_path / "token.key").write_text("KEY")
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "my_secret.txt").write_text("shh")
    (tmp_path / "normal.txt").write_text("hello world, nothing sensitive here")

    items, skipped, _ = jev.collect_items([str(tmp_path)])

    ids = {Path(it["id"]).name for it in items}
    assert ids == {"normal.txt"}
    assert len(skipped) == 7
    assert all("secret-like" in s for s in skipped)


def test_collect_items_skips_binary_and_oversized(monkeypatch, tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02binary-looking-data")
    (tmp_path / "small.txt").write_text("fits fine")
    (tmp_path / "big.txt").write_text("x" * 500)
    monkeypatch.setattr(jev, "AGENT_VERB_MAX_FILE_BYTES", 100)

    items, skipped, _ = jev.collect_items([str(tmp_path)])

    ids = {Path(it["id"]).name for it in items}
    assert ids == {"small.txt"}
    assert any("binary" in s for s in skipped)
    assert any("MB" in s for s in skipped)


def test_collect_items_respects_gitignore_for_explicit_file_args(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("should never be sent")
    (repo / "kept.txt").write_text("normal content")

    items, skipped, _ = jev.collect_items([str(repo / "ignored.txt"), str(repo / "kept.txt")])

    names = {Path(it["id"]).name for it in items}
    assert names == {"kept.txt"}


def test_collect_items_directory_walk_respects_gitignore(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("should never be sent")
    (repo / "kept.txt").write_text("normal content")

    items, skipped, _ = jev.collect_items([str(repo)])

    names = {Path(it["id"]).name for it in items}
    assert "kept.txt" in names
    assert "ignored.txt" not in names


# -- --lines run collapsing --

def test_render_collapsed_line_rows_collapses_contiguous_runs():
    def row(item_id, text, p):
        return ({"item": {"id": item_id, "text": text}}, p)

    matched = [
        row("app.log:104", "ERROR worker 42 died", 0.95),
        row("app.log:105", "ERROR worker 17 died", 0.90),
        row("app.log:106", "ERROR worker 3 died", 0.93),
        row("app.log:200", "FATAL disk full", 0.99),
    ]
    out = jev.render_collapsed_line_rows(matched, lo=0.97, hi=0.99)

    assert out[0].startswith("?0.99") and "app.log:200" in out[0]
    assert "app.log:L104-106 (3×)" in out[1]
    assert not out[1].lstrip().startswith("?") and not out[1].startswith("?")


# -- filter: borderline `?`, matched/borderline counts, receipt fields --

def test_cmd_filter_marks_borderline_and_reports_receipt(monkeypatch, tmp_path, capsys):
    (tmp_path / "a.txt").write_text("clearly matches the question")
    (tmp_path / "b.txt").write_text("maybe matches the question")
    (tmp_path / "c.txt").write_text("does not match at all")

    p_by_id = {"a.txt": 0.9, "b.txt": 0.5, "c.txt": 0.2}

    def fake_post(url, json, headers=None, timeout=None):
        src = json["state"]["source"]
        p = next(v for k, v in p_by_id.items() if src.endswith(k))
        return _FakeResponse({"model": "fake", "answers": {"q": {"type": "noul", "noul": p}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "question": "Does this match?", "paths": [str(tmp_path)], "lines": False,
        "threshold": 0.5, "band": None, "backend": "local-decider",
        **_common_verb_kwargs(),
    })()
    jev.cmd_filter(args)

    out, err = capsys.readouterr()
    lines = [l for l in out.splitlines() if l.strip()]
    a_line = next(l for l in lines if "a.txt" in l)
    b_line = next(l for l in lines if "b.txt" in l)
    assert not a_line.strip().startswith("?")   # 0.9 >= hi (0.65): a sure match
    assert b_line.strip().startswith("?")        # 0.5 is inside the default [0.35, 0.65] band
    assert not any("c.txt" in l for l in lines)   # 0.2 < threshold: not printed at all

    assert "2 matched" in err
    assert "1 borderline" in err
    assert "local-decider" in err
    assert "jev $" in err
    assert "Claude tokens not read" in err


def test_cmd_filter_explicit_band_overrides_default():
    lo, hi = jev.parse_band("0.4,0.6", threshold=0.5)
    assert (lo, hi) == (0.4, 0.6)
    lo, hi = jev.parse_band("0.2", threshold=0.5)
    assert (lo, hi) == pytest.approx((0.3, 0.7))
    lo, hi = jev.parse_band(None, threshold=0.5)
    assert (lo, hi) == pytest.approx((0.35, 0.65))


# -- classify: automatic "other" label, --no-other, low-confidence "?" group --

def test_cmd_classify_injects_other_label_unless_no_other(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("a bug report")
    (tmp_path / "b.txt").write_text("a feature idea")
    seen_criteria = []

    def fake_post(url, json, headers=None, timeout=None):
        seen_criteria.append(json["questions"]["q"]["criteria"])
        return _FakeResponse({"model": "fake", "answers": {"q": {
            "type": "choice", "choice": "bug", "confidence": 0.9,
            "probabilities": {"bug": 0.9, "feature": 0.1}}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    base = {
        "paths": [str(tmp_path)], "labels": "bug,feature", "items": None, "question": None,
        "min_confidence": 0.6, "backend": "local-decider", **_common_verb_kwargs(),
    }
    jev.cmd_classify(type("Args", (), {**base, "no_other": False})())
    assert "other" in seen_criteria[-1]

    seen_criteria.clear()
    jev.cmd_classify(type("Args", (), {**base, "no_other": True})())
    assert "other" not in seen_criteria[-1]


def test_cmd_classify_low_confidence_goes_to_question_group(monkeypatch, tmp_path, capsys):
    (tmp_path / "a.txt").write_text("an ambiguous item")

    def fake_post(url, json, headers=None, timeout=None):
        return _FakeResponse({"model": "fake", "answers": {"q": {
            "type": "choice", "choice": "bug", "confidence": 0.4,
            "probabilities": {"bug": 0.4, "feature": 0.35, "other": 0.25}}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "paths": [str(tmp_path)], "labels": "bug,feature", "items": None, "question": None,
        "no_other": False, "min_confidence": 0.6, "backend": "local-decider", **_common_verb_kwargs(),
    })()
    jev.cmd_classify(args)

    out, err = capsys.readouterr()
    assert "? low confidence" in out
    assert "bug (or feature)" in out


def test_cmd_classify_requires_items_or_paths():
    args = type("Args", (), {
        "paths": [], "labels": "a,b", "items": None, "question": None,
        "no_other": False, "min_confidence": 0.6, "backend": "local-decider", **_common_verb_kwargs(),
    })()
    with pytest.raises(SystemExit):
        jev.cmd_classify(args)


# -- rank: top-K, and the shared --max-spend guard also covers a verb (not just `ask`) --

def test_cmd_rank_max_spend_guard_stops_early(monkeypatch, tmp_path):
    for name in ["a", "b", "c", "d"]:
        (tmp_path / f"{name}.txt").write_text(f"content about {name}")
    calls = []

    def fake_post(url, json, headers=None, timeout=None):
        calls.append(json["state"]["source"])
        resp = dict(OPENROUTER_SAMPLE_RESPONSE)
        resp["answers"] = {"q": {"type": "score", "score": 2, "confidence": 0.5,
                                  "probabilities": {"0": 0.2, "1": 0.2, "2": 0.2, "3": 0.2, "4": 0.2}}}
        resp["usage"] = {"input_tokens": 10, "output_tokens": 5, "cost": 0.01}
        return _FakeResponse(resp)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    args = type("Args", (), {
        "query": "find x", "paths": [str(tmp_path)], "top": 10, "backend": "openrouter",
        **_common_verb_kwargs(max_spend=0.025),
    })()
    jev.cmd_rank(args)

    assert len(calls) == 3  # 3 calls @ $0.01 = $0.03 >= cap -> the 4th file never scored


def test_cmd_rank_orders_by_score_and_respects_top(monkeypatch, tmp_path, capsys):
    scores = {"a.txt": 4, "b.txt": 1, "c.txt": 3}
    for name in scores:
        (tmp_path / name).write_text(f"content {name}")

    def fake_post(url, json, headers=None, timeout=None):
        src = json["state"]["source"]
        score = next(v for k, v in scores.items() if src.endswith(k))
        return _FakeResponse({"model": "fake", "answers": {"q": {
            "type": "score", "score": score, "confidence": 0.8,
            "probabilities": {str(i): 0.2 for i in range(5)}}}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "query": "find x", "paths": [str(tmp_path)], "top": 2, "backend": "local-decider",
        **_common_verb_kwargs(),
    })()
    jev.cmd_rank(args)

    out, err = capsys.readouterr()
    lines = [l for l in out.splitlines() if l.strip() and "matched" not in l]
    assert len(lines) == 2
    assert "a.txt" in lines[0]  # score 4/4 = 1.00, ranked first
    assert "c.txt" in lines[1]  # score 3/4 = 0.75, ranked second
    assert "2 matched" in err


# -- find: chunked search over one big file --

def test_cmd_find_ranks_line_hits(monkeypatch, tmp_path, capsys):
    f = tmp_path / "big.log"
    f.write_text("\n".join(f"line {i}: nothing interesting" for i in range(1, 50)) + "\nline 50: the target needle\n")

    def fake_post(url, json, headers=None, timeout=None):
        lines_in_chunk = json["state"]["lines"]
        if "50" in lines_in_chunk:
            return _FakeResponse({"model": "fake", "answers": {
                "where": {"type": "choice", "choice": "50",
                          "probabilities": {**{k: 0.0 for k in lines_in_chunk}, "none": 0.0, "50": 0.95}},
                "exists": {"type": "noul", "noul": 0.9},
            }})
        return _FakeResponse({"model": "fake", "answers": {
            "where": {"type": "choice", "choice": "none",
                      "probabilities": {**{k: 0.0 for k in lines_in_chunk}, "none": 1.0}},
            "exists": {"type": "noul", "noul": 0.01},
        }})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(jev, "port_listening", lambda *a, **k: True)

    args = type("Args", (), {
        "description": "the target needle", "path": str(f), "top": 5, "chunk": 150, "overlap": 30,
        "min_score": 0.05, "backend": "local-decider", **_common_verb_kwargs(),
    })()
    jev.cmd_find(args)

    out, err = capsys.readouterr()
    lines = [l for l in out.splitlines() if l.strip()]
    assert any("big.log:50" in l for l in lines)
    assert "matched" in err and "local-decider" in err and "Claude tokens not read" in err


def test_cmd_find_requires_a_single_readable_file(tmp_path):
    missing = tmp_path / "does-not-exist.log"
    args = type("Args", (), {
        "description": "x", "path": str(missing), "top": 5, "chunk": 150, "overlap": 30,
        "min_score": 0.05, "backend": "local-decider", **_common_verb_kwargs(),
    })()
    with pytest.raises(SystemExit):
        jev.cmd_find(args)
