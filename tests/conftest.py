"""Shared fixtures. Everything runs offline against FakeAlpaca, in throwaway directories.

All values here are invented: no real keys, chat ids, names or balances.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trader.broker import Alpaca
from trader.config import load_limits
from trader.fake_alpaca import FakeAlpaca
from trader.model import FakeModel
from trader.records import Records
from trader.wake import Deps

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 26, 8, 17, tzinfo=UTC)
SLOT = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)

FAKE_ENV = {
    "ALPACA_API_KEY": "PKFAKEKEY0000000000",
    "ALPACA_SECRET_KEY": "fakesecret-aaaaaaaaaaaaaaaaaaaaaaaa",
    "TELEGRAM_BOT_TOKEN": "123456:FAKE-telegram-token-bbbbbbbbbb",
    "TELEGRAM_CHAT_ID": "-100999999",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-fake-cccccccccccccccc",
}


def reply(*decisions, market_view="vista di prova", next_job="compito di prova") -> str:
    return json.dumps({"decisions": [
        {"symbol": s, "action": a, "notional_usd": n, "bull_case": f"a favore di {s}",
         "bear_case": f"contro {s}", "reason": f"motivo di prova per {s}"} for s, a, n in decisions],
        "market_view": market_view, "next_job": next_job})


class Outbox:
    """Collects messages instead of sending them; can be told to fail."""

    def __init__(self):
        self.sent: list[str] = []
        self.fail = False

    def __call__(self, text: str):
        if self.fail:
            raise ConnectionError("telegram unreachable")
        self.sent.append(text)
        return True, "inviato"


# The shipped parameters, with a lower entry bar so the invented price paths of FakeAlpaca
# give buy candidates (BTC, SOL, DOGE, ETH score about 0.66 to 0.69 on them). The weights are
# pinned: the reflection rewrites config/params.json (weights.volume 0.25 -> 0.5 on
# 2026-09-28), and the tests must not change with what the agent learned.
TEST_PARAMS = {"entry_threshold": 0.6, "min_edge_mult": 10.0,
               "weights": {"adx": 0.5, "bollinger": 0.0, "macd": 0.5, "momentum": 1.0, "rsi": 0.5,
                           "trend_1h": 1.0, "trend_4h": 1.0, "volume": 0.25}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs offline: a real HTTP call fails loudly instead of reaching out."""
    import urllib.request

    def blocked(*a, **k):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def write_params(root: Path, **changes) -> dict:
    params = json.loads((REPO / "config/params.json").read_text())
    params.update({**TEST_PARAMS, **changes})
    (root / "config/params.json").write_text(json.dumps(params))
    return params


@pytest.fixture
def root(tmp_path) -> Path:
    """A throwaway repo root with the real prompts and limits, and the test parameters."""
    r = tmp_path / "repo"
    (r / "prompts").mkdir(parents=True)
    (r / "config").mkdir()
    for rel in ("prompts/decide.md", "prompts/reflect.md", "config/limits.toml", "config/jev-thresholds.json"):
        shutil.copy(REPO / rel, r / rel)
    write_params(r)
    return r


@pytest.fixture
def fake():
    return FakeAlpaca(now=lambda: NOW)


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def make_deps(root, fake, outbox):
    def make(model=None, records_dir=None, env=None, now=NOW, broker=None, secrets=None, **kw):
        broker = broker or fake
        broker.now = lambda: now  # the fake account lives at the wake's time: bars end there
        client = Alpaca(FAKE_ENV["ALPACA_API_KEY"], FAKE_ENV["ALPACA_SECRET_KEY"], transport=broker,
                        sleep=lambda s: None)
        mdl = model or FakeModel([reply(("BTC/USD", "buy", 50))])
        return Deps(root=root, records=Records(records_dir or root, secrets or list(FAKE_ENV.values())),
                    limits=load_limits(root / "config/limits.toml"), send=outbox,
                    build=lambda: (client, mdl), env=env or {}, now=lambda: now,
                    run_url="https://github.com/example/trader/actions/runs/1",
                    **{"reflect": False, **kw})
    return make
