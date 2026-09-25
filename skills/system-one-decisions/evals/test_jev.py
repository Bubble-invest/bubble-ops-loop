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
    res = jev.call_engine("http://127.0.0.1:8782", {"body": "hi"}, {"bucket": {"type": "noul"}})
    assert res["ok"] is True
    assert res["response"]["answers"]["bucket"]["noul"] == 0.9
    assert calls[0][0] == "http://127.0.0.1:8782/v1/systemone"


def test_call_engine_failure_is_caught_not_raised(monkeypatch):
    def fake_post(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr("requests.post", fake_post)
    res = jev.call_engine("http://127.0.0.1:8782", "state", {})
    assert res["ok"] is False
    assert "boom" in res["error"]


def test_cmd_ask_rejects_unknown_backend(tmp_path):
    items = tmp_path / "items.jsonl"
    items.write_text('{"id": "a", "state": "x"}\n')
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps({"bucket": {"type": "noul", "instructions": "?"}}))
    out = tmp_path / "out.jsonl"
    args = type("Args", (), {
        "backend": "not-a-real-backend", "questions": str(questions), "items": str(items),
        "out": str(out), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None,
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
        "out": str(out_path), "resume": True, "parallel": 1, "timeout": 5.0, "base_url": None,
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
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None,
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
        "out": str(out_path), "resume": False, "parallel": 1, "timeout": 5.0, "base_url": None,
    })()
    with pytest.raises(SystemExit) as exc:
        jev.cmd_ask(args)
    assert "TYPESAFE_API_KEY" in str(exc.value)


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
