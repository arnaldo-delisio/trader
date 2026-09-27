"""trader/jev.py against a fake HTTP layer. No network: every request goes to FakeHTTP."""

import io
import json
import urllib.error

import pytest

from trader import jev

ENV = {"JEV_API_KEY": "jev-fake-key-000000", "GROQ_API_KEY": "groq-fake-key-000000"}
STATE = {"btc_return_24h_pct": 1.5, "breadth_above_ema50_pct": 60}


def jev_body(choice="risk_on", confidence=0.9):
    # the shape a real call returned on 2026-09-26 (jev-1.13.0)
    return {"model": "jev-1.13.0", "answers": {"market_regime": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": {"risk_on": 0.9, "neutral": 0.1, "risk_off": 0.0}}},
        "usage": {"input_tokens": 400, "output_tokens": 40}}


def groq_body(choice="risk_off"):
    return {"model": jev.GROQ_MODEL, "choices": [{"message": {"content": json.dumps({"choice": choice})}}]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """Answers per host: a dict is returned as JSON, an int is an HTTP error, an exception is raised."""

    def __init__(self, jev_reply=None, groq_reply=None):
        self.replies = {"api.typesafe.ai": jev_reply if jev_reply is not None else jev_body(),
                        "api.groq.com": groq_reply if groq_reply is not None else groq_body()}
        self.calls = []

    def __call__(self, req, timeout=None):
        host = req.host
        self.calls.append((host, json.loads(req.data), timeout, req.get_header("Authorization")))
        r = self.replies[host]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            raise urllib.error.HTTPError(req.full_url, r, "err", {}, io.BytesIO(b"{}"))
        return Resp(r.encode() if isinstance(r, str) else json.dumps(r).encode())

    def hosts(self):
        return [c[0] for c in self.calls]


def classify(http, env=ENV, **kw):
    return jev.classify(STATE, env=env, opener=http, **kw)


def test_confident_answer_sets_the_regime_and_its_multiplier():
    http = FakeHTTP(jev_body("risk_on", 0.93))
    r = classify(http)
    assert (r.regime, r.multiplier, r.source, r.error) == ("risk_on", 1.0, "jev", "")
    host, body, timeout, auth = http.calls[0]
    assert host == "api.typesafe.ai" and timeout == jev.TIMEOUT and auth == "Bearer " + ENV["JEV_API_KEY"]
    q = body["questions"]["market_regime"]
    assert q["type"] == "choice" and set(q["criteria"]) == set(jev.REGIMES)
    assert json.loads(body["state"]) == STATE


def test_multiplier_mapping_never_sizes_up():
    assert [jev.multiplier_for(x) for x in ("risk_on", "neutral", "risk_off")] == [1.0, 0.7, 0.4]
    assert jev.multiplier_for("anything else") == 0.7
    assert max(jev.MULTIPLIERS.values()) <= 1.0


def test_low_confidence_is_rejected_as_neutral():
    r = classify(FakeHTTP(jev_body("risk_on", 0.59)))
    assert (r.regime, r.multiplier) == ("neutral", 0.7)
    assert r.choice == "risk_on" and "sotto la soglia" in r.error


def test_missing_confidence_is_rejected_as_neutral():
    body = jev_body("risk_off")
    del body["answers"]["market_regime"]["confidence"]
    r = classify(FakeHTTP(body))
    assert r.regime == "neutral" and r.error


def test_unknown_choice_is_rejected_as_neutral():
    r = classify(FakeHTTP(jev_body("moon", 0.99)))
    assert r.regime == "neutral" and "sconosciuta" in r.error


@pytest.mark.parametrize("failure", [529, 429, 500, urllib.error.URLError("down"), TimeoutError()])
def test_jev_outage_uses_the_fallback(failure):
    http = FakeHTTP(jev_reply=failure, groq_reply=groq_body("risk_off"))
    r = classify(http)
    assert http.hosts() == ["api.typesafe.ai", "api.groq.com"]
    assert (r.regime, r.multiplier, r.source) == ("risk_off", 0.4, "fallback")
    assert "non disponibile" in r.error  # the outage is still reported


def test_missing_jev_key_goes_straight_to_the_fallback():
    http = FakeHTTP(groq_reply=groq_body("risk_on"))
    r = classify(http, env={"GROQ_API_KEY": ENV["GROQ_API_KEY"]})
    assert http.hosts() == ["api.groq.com"] and r.regime == "risk_on" and r.source == "fallback"


def test_untrusted_fallback_is_rejected_as_neutral():
    r = classify(FakeHTTP(jev_reply=529, groq_reply=groq_body("risk_on")),
                 thresholds={"confidence_floor": 0.6, "trust_fallback": False})
    assert r.regime == "neutral" and "affidabile" in r.error


@pytest.mark.parametrize("code", [401, 422])
def test_our_bug_does_not_fall_back_and_is_neutral(code):
    http = FakeHTTP(jev_reply=code)
    r = classify(http)
    assert http.hosts() == ["api.typesafe.ai"]
    assert r.regime == "neutral" and r.error.startswith("bug")


@pytest.mark.parametrize("groq", [500, urllib.error.URLError("down"), "not json", {"choices": []},
                                  {"model": "x", "choices": [{"message": {"content": "[1, 2]"}}]},
                                  groq_body("sideways")])
def test_fallback_failure_is_neutral_and_never_raises(groq):
    r = classify(FakeHTTP(jev_reply=529, groq_reply=groq))
    assert (r.regime, r.multiplier) == ("neutral", 0.7) and r.error


def test_no_keys_at_all_is_neutral():
    http = FakeHTTP()
    r = classify(http, env={})
    assert http.calls == [] and r.regime == "neutral" and "GROQ_API_KEY" in r.error


@pytest.mark.parametrize("jev_reply", ["<html>", {"answers": {}}, {"model": "x"}, [1]])
def test_malformed_jev_reply_is_neutral(jev_reply):
    r = classify(FakeHTTP(jev_reply=jev_reply))
    assert r.regime == "neutral" and r.error


def test_record_is_journal_ready_and_carries_no_key():
    r = classify(FakeHTTP())
    d = r.as_dict()
    assert set(d) == {"regime", "multiplier", "source", "model", "choice", "confidence", "latency_ms", "error"}
    text = json.dumps(d)
    assert all(v not in text for v in ENV.values())


def test_thresholds_file_cannot_lower_the_floor_below_the_code_minimum(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / jev.THRESHOLDS_FILE).write_text(json.dumps(
        {"market_regime": {"type": "choice", "confidence_floor": 0.1}}))
    th, problem = jev.load_thresholds(tmp_path)
    assert th["confidence_floor"] == jev.MIN_CONFIDENCE_FLOOR and problem == ""
    r = classify(FakeHTTP(jev_body("risk_on", 0.3)), thresholds=th)
    assert r.regime == "neutral"


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"market_regime": {"type": "noul"}}),
                                     json.dumps({"market_regime": {"type": "choice", "confidence_floor": "high"}}),
                                     json.dumps({"market_regime": {"type": "choice", "confidence_floor": 2}})])
def test_bad_thresholds_file_gives_defaults_and_a_reason(tmp_path, content):
    (tmp_path / "config").mkdir()
    (tmp_path / jev.THRESHOLDS_FILE).write_text(content)
    th, problem = jev.load_thresholds(tmp_path)
    assert th == jev.DEFAULT_THRESHOLDS and problem


def test_repo_thresholds_file_is_valid():
    from tests.conftest import REPO
    th, problem = jev.load_thresholds(REPO)
    assert problem == "" and th["confidence_floor"] >= jev.MIN_CONFIDENCE_FLOOR
