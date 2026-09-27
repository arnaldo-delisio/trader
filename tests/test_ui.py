"""The dashboard: built from synthetic records only, in throwaway directories."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ui import build, data, fixtures, render

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class Tags(HTMLParser):
    """Every start tag with its attributes, to check the built page rather than its text."""

    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(html: str) -> list[tuple[str, dict]]:
    p = Tags()
    p.feed(html)
    return p.tags


def write_journal(root: Path, rows: list) -> None:
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "journal/decisions.jsonl").write_text(
        "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")


def site(tmp_path: Path, root: Path) -> tuple[dict, str, dict]:
    out = tmp_path / "site"
    m = build.build(root, out, NOW)
    return m, (out / "index.html").read_text(encoding="utf-8"), json.loads((out / "data.json").read_text())


# ---- shapes -------------------------------------------------------------------------------

def test_empty_records_build_a_complete_page(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    m, html, j = site(tmp_path, root)
    assert m["wakes"] == [] and m["trades"] == [] and m["positions"] == [] and m["lessons"] == []
    assert m["equity_series"] == [] and m["problems"] == []
    assert j["version"] == m["version"]
    assert html.count('class="empty"') >= 6  # every section says it has nothing yet
    assert "In attesa del primo risveglio".lower() in html.lower()


def test_many_records_are_capped_and_every_section_filled(tmp_path):
    root = fixtures.generate(tmp_path / "repo", wakes=600, seed=3, now=NOW)
    m, html, _ = site(tmp_path, root)
    assert len(m["wakes"]) == data.MAX_WAKES
    assert 0 < len(m["trades"]) <= data.MAX_TRADES
    assert m["positions"] and all(p["stop"] and p["take_profit"] for p in m["positions"])
    assert m["closed"]["trades"] > 0 and m["lessons"] and m["regime"]
    assert {c["accepted"] for c in m["param_changes"]} == {True, False}
    assert m["equity_source"].startswith("Alpaca")
    assert m["problems"] == []
    assert sum(1 for t, a in tags(html) if t == "article" and "trade" in a.get("class", "")) == render.MAX_TRADE_CARDS
    assert any(t == "svg" and a.get("role") == "img" for t, a in tags(html))


def test_fixtures_are_deterministic_and_synthetic(tmp_path):
    a = fixtures.generate(tmp_path / "a", wakes=50, seed=1, now=NOW)
    b = fixtures.generate(tmp_path / "b", wakes=50, seed=1, now=NOW)
    ja = (a / "journal/decisions.jsonl").read_text()
    assert ja == (b / "journal/decisions.jsonl").read_text()
    urls = set(re.findall(r'https?://[^"\s]+', ja))
    assert urls and all(u.startswith("https://github.com/example/") for u in urls)


def test_portfolio_history_wins_over_wake_equity(tmp_path):
    root = tmp_path / "repo"
    write_journal(root, [{"slot": "20260926T0800Z", "status": "ok", "equity": 5.0},
                         {"slot": "20260926T0815Z", "status": "ok", "equity": 6.0}])
    (root / "state").mkdir()
    (root / "state/portfolio_history.json").write_text(json.dumps(
        {"timestamp": [1790000000, 1790000900], "equity": [100000, 100500]}))
    m, _, _ = site(tmp_path, root)
    assert [p["equity"] for p in m["equity_series"]] == [100000, 100500]
    (root / "state/portfolio_history.json").unlink()
    m, _, _ = site(tmp_path, root)
    assert [p["equity"] for p in m["equity_series"]] == [5.0, 6.0]


def test_version_changes_only_when_records_change(tmp_path):
    root = fixtures.generate(tmp_path / "repo", wakes=30, seed=2, now=NOW)
    v1 = data.load(root, NOW)["version"]
    assert data.load(root, datetime(2027, 1, 1, tzinfo=UTC))["version"] == v1
    with open(root / "journal/decisions.jsonl", "a") as f:
        f.write(json.dumps({"slot": "20260926T1200Z", "status": "no_trade"}) + "\n")
    assert data.load(root, NOW)["version"] != v1


# ---- rejections ---------------------------------------------------------------------------

def test_rejects_markup_from_the_records(tmp_path):
    root = tmp_path / "repo"
    evil = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    write_journal(root, [{"slot": "20260926T0800Z", "status": "ok", "verdicts": [
        {"symbol": "BTC/USD", "action": "buy", "outcome": "placed", "client_order_id": "trd-1",
         "reason_model": evil, "bull_case": evil, "bear_case": evil}]}])
    (root / "lessons").mkdir()
    (root / "lessons/lessons.md").write_text(f"# Lezioni\n\n## oggi\n\n- {evil}\n")
    _, html, _ = site(tmp_path, root)
    assert not any(t == "img" for t, _ in tags(html))
    assert sum(1 for t, a in tags(html) if t == "script") == 1  # only the page's own script
    assert "&lt;script&gt;" in html


def test_rejects_run_links_that_are_not_github(tmp_path):
    root = tmp_path / "repo"
    bad = ["javascript:alert(1)", "https://evil.example/run", 'https://github.com/x" onmouseover="alert(1)']
    write_journal(root, [{"slot": f"20260926T08{i}0Z", "status": "ok", "run_url": u} for i, u in enumerate(bad)])
    (root / "state").mkdir()
    (root / "state/last_handoff.json").write_text(json.dumps({"slot": "2026-09-26T08:00:00+00:00",
                                                              "status": "ok", "run_url": bad[0]}))
    m, html, _ = site(tmp_path, root)
    hrefs = [a.get("href", "") for _, a in tags(html)]
    assert not any("evil" in h or h.startswith("javascript") or "onmouseover" in h for h in hrefs)
    assert all(w["run_url"] == "" for w in m["wakes"]) and m["handoff_run_url"] == ""
    assert data.safe_url("https://github.com/o/r/actions/runs/1") == "https://github.com/o/r/actions/runs/1"


def test_page_loads_nothing_from_outside(tmp_path):
    root = fixtures.generate(tmp_path / "repo", wakes=40, seed=5, now=NOW)
    _, html, _ = site(tmp_path, root)
    for t, a in tags(html):
        assert not a.get("src"), f"<{t}> loads {a.get('src')}"
        if t == "link":
            assert a.get("href", "").startswith("data:"), a


def test_malformed_records_are_skipped_and_reported(tmp_path):
    root = tmp_path / "repo"
    write_journal(root, ["{not json", "[1, 2]", {"slot": "20260926T0800Z"},
                         {"slot": "20260926T0815Z", "status": "ok", "verdicts": [{"outcome": "placed"}, "x", None],
                          "exits": [{"symbol": "ETHUSD", "outcome": "placed"}], "jev": "broken"},
                         {"kind": "reflection", "accepted": [{"name": "entry_threshold"}], "rejected": ["x"]}])
    (root / "state").mkdir()
    (root / "state/last_handoff.json").write_text("{truncated")
    (root / "state/positions.json").write_text(json.dumps({"BTCUSD": {"stop": "abc", "take_profit": None}}))
    m, html, _ = site(tmp_path, root)
    assert any("2 righe illeggibili" in p for p in m["problems"])
    assert any("last_handoff.json" in p for p in m["problems"])
    assert [w["slot"] for w in m["wakes"]] == ["20260926T0800Z", "20260926T0815Z"]
    assert m["positions"][0]["symbol"] == "BTC/USD" and m["positions"][0]["stop"] is None
    assert "livelli non registrati" in html


def test_kill_switch_is_shown_when_the_kill_file_exists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert data.load(root, NOW)["limits"]["trading_enabled"] is True
    (root / "KILL").write_text("")
    m, html, _ = site(tmp_path, root)
    assert m["limits"]["trading_enabled"] is False
    assert "KILL SWITCH" in html


@pytest.mark.parametrize("x,expected", [(71964.6, "71.964,60"), (0.0000175, "0,00001750"), (0.5274, "0,5274"),
                                        (-3.5, "−3,500"), (None, "n/d")])
def test_prices_keep_significant_digits(x, expected):
    assert render.price(x) == expected


def test_symbols_normalize_and_memecoins_are_tagged():
    assert data.symbol("btcusd") == "BTC/USD" and data.symbol("ETH/USD") == "ETH/USD"
    assert data.is_meme("DOGEUSD") and data.is_meme("SHIB/USD") and not data.is_meme("BTC/USD")


def test_exploration_positions_and_trades_are_labelled(tmp_path):
    root = tmp_path / "repo"
    (root / "state").mkdir(parents=True)
    (root / "state/positions.json").write_text(json.dumps({"positions": {"SOL/USD": {
        "entry_price": 150.0, "entry_t": 1790000000, "atr_at_entry": 1.0, "highest": 151.0, "stop": 146.0,
        "take_profit": 156.0, "explore": True}}}))
    write_journal(root, [{"kind": "wake", "slot": "20260926T1100Z", "finished_at": "2026-09-26T11:10:00+00:00",
                          "status": "ok", "verdicts": [{"symbol": "SOL/USD", "action": "buy", "requested_usd": 70.0,
                                                        "approved": True, "reason": "ammesso", "explore": True,
                                                        "outcome": "placed",
                                                        "client_order_id": "trd-20260926T1100Z-SOLUSD-buy"}]}])
    m, html, _ = site(tmp_path, root)
    assert m["positions"][0]["explore"] is True and m["trades"][0]["explore"] is True
    assert html.count('class="tag explore"') == 2


def test_invested_comes_from_the_handoffs_position_values(tmp_path):
    # The handoff's rows say "value", not Alpaca's "market_value": the page showed 0% invested
    # next to six open positions on 2026-09-27.
    root = tmp_path / "repo"
    (root / "state").mkdir(parents=True)
    (root / "state/last_handoff.json").write_text(json.dumps({
        "slot": "2026-09-26T11:45:00+00:00", "status": "no_trade", "equity": 10000.0,
        "positions": [{"symbol": "BTC/USD", "value": 600.0, "stop": 1.0, "take_profit": 2.0, "entry_price": 1.5,
                       "price": 1.6, "exit": None, "explore": False},
                      {"symbol": "SOL/USD", "value": 400.0, "stop": 1.0, "take_profit": 2.0, "entry_price": 1.5,
                       "price": 1.4, "exit": None, "explore": True}]}))
    m = data.load(root, NOW)
    assert m["account"]["invested_usd"] == 1000.0 and m["account"]["invested_pct"] == pytest.approx(10.0)
    assert [p["market_value"] for p in m["positions"]] == [600.0, 400.0]


def test_the_page_shows_buys_paused_from_the_handoff_status(tmp_path):
    root = tmp_path / "repo"
    (root / "state").mkdir(parents=True)
    (root / "state/last_handoff.json").write_text(json.dumps({"slot": "2026-09-26T11:45:00+00:00",
                                                              "status": "paused", "equity": 10000.0}))
    lim = data.load(root, NOW)["limits"]
    assert lim["trading_enabled"] is False and "Acquisti sospesi" in lim["kill_reason"]
