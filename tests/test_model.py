import json
import subprocess
import urllib.error

import pytest

from tests.conftest import reply
from trader.model import AnthropicAPI, ClaudeCLI, FakeModel, parse

GOOD = reply(("BTC/USD", "buy", 50), ("ETH/USD", "hold", 0))


def test_valid_proposal_is_accepted():
    p = parse(GOOD)
    assert p.valid and [d.action for d in p.decisions] == ["buy", "hold"]
    assert p.decisions[0].bull_case == "a favore di BTC/USD" and p.decisions[0].bear_case == "contro BTC/USD"


def test_schema_requires_both_cases():
    from trader.model import SCHEMA
    item = SCHEMA["properties"]["decisions"]["items"]
    assert {"bull_case", "bear_case"} <= set(item["required"]) == set(item["properties"])


@pytest.mark.parametrize("text", [
    "not json at all",
    "I'm sorry, I can't help with trading decisions.",
    "[]",
    json.dumps({"decisions": [], "market_view": "x"}),  # missing next_job
    json.dumps({"decisions": [], "market_view": "x", "next_job": "y", "extra": 1}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "short", "notional_usd": 5, "bull_case": "b", "bear_case": "c", "reason": "r"}],
                "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": -5, "bull_case": "b", "bear_case": "c", "reason": "r"}],
                "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": "50", "bull_case": "b", "bear_case": "c", "reason": "r"}],
                "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": True, "bull_case": "b", "bear_case": "c", "reason": "r"}],
                "market_view": "x", "next_job": "y"}),
    '{"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": NaN, "bull_case": "b", "bear_case": "c", "reason": "r"}], "market_view": "x", "next_job": "y"}',
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 5, "bull_case": "b", "bear_case": "c", "reason": "r"},
                              {"symbol": "BTC/USD", "action": "buy", "notional_usd": 5, "bull_case": "b", "bear_case": "c", "reason": "r"}],
                "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 5}],
                "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 5, "reason": "r"}],
                "market_view": "x", "next_job": "y"}),  # no bull_case / bear_case
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 5, "bull_case": "b",
                               "reason": "r"}], "market_view": "x", "next_job": "y"}),  # no bear_case
    json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 5, "bull_case": None,
                               "bear_case": "c", "reason": "r"}], "market_view": "x", "next_job": "y"}),
    json.dumps({"decisions": [{"symbol": f"S{i}", "action": "hold", "notional_usd": 0, "bull_case": "b", "bear_case": "c", "reason": "r"}
                              for i in range(21)], "market_view": "x", "next_job": "y"}),
])
def test_invalid_output_becomes_hold_with_a_reason(text):
    p = parse(text)
    assert not p.valid and p.error and p.decisions == []


class Run:
    def __init__(self, stdout="", code=0, exc=None):
        self.stdout, self.code, self.exc, self.calls = stdout, code, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, "")


def cli_out(**kw):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": ""}
    base.update(kw)
    return json.dumps(base)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-fake-dddddddd")


def test_cli_uses_structured_output(token):
    run = Run(cli_out(structured_output=json.loads(GOOD)))
    p = ClaudeCLI(runner=run).propose("prompt")
    assert p.valid and p.decisions[0].symbol == "BTC/USD"
    _cmd, kw = run.calls[0]
    assert kw["input"] == "prompt" and kw["timeout"] > 0


def test_cli_runs_without_tools_one_shot():
    cmd = ClaudeCLI().command()
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--json-schema" in cmd and "--no-session-persistence" in cmd


@pytest.mark.parametrize("runner", [
    Run(cli_out(is_error=True, subtype="error_during_execution")),
    Run(cli_out(is_error=True, subtype="error_max_budget_usd", result=GOOD)),  # an error with usable-looking text
    Run(cli_out(result="I can't make trading decisions for you.")),   # refusal in plain text
    Run("Error: not logged in", code=1),
    Run(exc=subprocess.TimeoutExpired("claude", 240)),
    Run(exc=FileNotFoundError("claude")),
    Run(cli_out(structured_output={"decisions": "all in", "market_view": "", "next_job": ""})),
])
def test_cli_failures_become_hold(token, runner):
    p = ClaudeCLI(runner=runner).propose("prompt")
    assert not p.valid and p.error


def test_cli_without_token_holds_and_does_not_run(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = Run(cli_out(structured_output=json.loads(GOOD)))
    assert not ClaudeCLI(runner=run).propose("p").valid and run.calls == []


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_api_refusal_becomes_hold():
    opener = lambda req, timeout: Resp({"stop_reason": "refusal", "content": []})
    assert not AnthropicAPI("k", opener=opener).propose("p").valid


def test_api_http_error_becomes_hold():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, None)
    assert not AnthropicAPI("k", opener=opener).propose("p").valid


def test_api_valid_reply_is_parsed():
    opener = lambda req, timeout: Resp({"stop_reason": "end_turn", "content": [{"type": "text", "text": GOOD}]})
    assert AnthropicAPI("k", opener=opener).propose("p").valid


def test_fake_model_exception_reply_holds():
    assert not FakeModel([RuntimeError("boom")]).propose("p").valid


@pytest.mark.parametrize("raw,clean", [
    ("Controllare RENDER</next_job>\n</invoke>", "Controllare RENDER"),
    ("<parameter name=\"x\">vista</parameter>", "vista"),
    ("RSI < 30 e score <0,8, prezzo > stop", "RSI < 30 e score <0,8, prezzo > stop"),
    ("  due\n\nrighe  ", "due righe"),
])
def test_clean_text_strips_tags_and_keeps_comparisons(raw, clean):
    from trader.model import clean_text
    assert clean_text(raw) == clean


def test_every_free_text_field_is_cleaned_by_validate():
    junk = "</invoke>"
    p = parse(json.dumps({"decisions": [{"symbol": "BTC/USD", "action": "buy", "notional_usd": 10,
                                         "bull_case": "su" + junk, "bear_case": "giù" + junk,
                                         "reason": "perché" + junk}],
                          "market_view": "vista" + junk, "next_job": "compito</next_job>\n" + junk}))
    d = p.decisions[0]
    assert (d.bull_case, d.bear_case, d.reason, p.market_view, p.next_job) == ("su", "giù", "perché", "vista", "compito")
