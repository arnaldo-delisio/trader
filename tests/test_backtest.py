"""The backtest on invented bars: costs are charged, caps hold, no step sees the future,
the walk-forward comparison rejects a worse candidate, and it scores like the live code."""

from __future__ import annotations

import pytest

from tests.test_strategy import REPO, as_rows, make_bars
from trader import backtest as bt
from trader import strategy as st

MEME = "MEME/USD"


@pytest.fixture(scope="module")
def params():
    return st.with_changes(st.load_params(REPO / "config" / "params.json"),
                           {"min_edge_mult": 2.0, "entry_threshold": 0.4, "regime_filter": 0})


@pytest.fixture(scope="module")
def market():
    """Twelve invented symbols trending up with noise, one of them a memecoin, plus BTC."""
    raw = {f"C{i}/USD": as_rows(make_bars(n=1600, drift=0.0012, noise=0.006, seed=i, start=10 + i))
           for i in range(11)}
    raw[MEME] = as_rows(make_bars(n=1600, drift=0.0015, noise=0.008, seed=99))
    raw["BTC/USD"] = as_rows(make_bars(n=1600, drift=0.0008, noise=0.003, seed=100, start=60_000))
    return raw


@pytest.fixture(scope="module")
def prep(market):
    return bt.prepare(market)


def run(prep, params, **kw):
    kw.setdefault("caps", bt.Caps(memecoins=frozenset({MEME})))
    return bt.run(prep, params, prep.first_ready(), **kw)


def test_it_trades_on_a_trending_market(prep, params):
    r = run(prep, params)
    assert r.metrics["trades"] > 5


def test_fees_and_spread_are_charged_on_every_fill(prep, params):
    free = run(prep, params, costs=bt.Costs(fee_pct=0.0, slippage_pct=0.0)).metrics
    paid = run(prep, params, costs=bt.Costs(fee_pct=0.25, slippage_pct=0.1)).metrics
    assert free["fees_usd"] == 0 and paid["fees_usd"] > 0 and paid["slippage_usd"] > 0
    assert paid["net_return_pct"] < free["net_return_pct"]


def test_no_trades_means_no_change(prep, params):
    idle = st.with_changes(params, {"entry_threshold": 0.9, "min_edge_mult": 25.0})
    m = run(prep, idle).metrics
    assert m["trades"] == 0 and m["final_equity"] == bt.START_EQUITY and m["fees_usd"] == 0


def test_every_trade_is_closed_and_accounted(prep, params):
    r = run(prep, params)
    assert sum(t["pnl_usd"] for t in r.trades) == pytest.approx(r.metrics["final_equity"] - bt.START_EQUITY)


# ---- caps ---------------------------------------------------------------------

CAPS = bt.Caps(memecoins=frozenset({MEME}))


def test_buy_is_capped_per_coin_and_tighter_for_memecoins():
    kw = {"equity": 10_000, "invested": 0, "cash": 10_000, "positions": 0, "caps": CAPS}
    assert bt.buy_usd(5_000, "C1/USD", **kw) == 800
    assert bt.buy_usd(5_000, MEME, **kw) == 300


def test_buy_is_capped_by_total_invested_and_cash():
    kw = {"equity": 10_000, "positions": 0, "caps": CAPS}
    assert bt.buy_usd(800, "C1/USD", invested=5_700, cash=4_300, **kw) == 300
    assert bt.buy_usd(800, "C1/USD", invested=0, cash=50, **kw) == 50


def test_buy_is_rejected_at_the_position_limit_under_the_minimum_or_when_fully_invested():
    kw = {"equity": 10_000, "cash": 10_000, "caps": CAPS}
    assert bt.buy_usd(800, "C1/USD", invested=0, positions=CAPS.max_positions, **kw) == 0
    assert bt.buy_usd(9.99, "C1/USD", invested=0, positions=0, **kw) == 0
    assert bt.buy_usd(800, "C1/USD", invested=6_000, positions=0, **kw) == 0
    assert bt.buy_usd(800, "C1/USD", invested=0, positions=0, equity=0, cash=0, caps=CAPS) == 0


def test_daily_loss_blocks_new_buys_only_past_the_limit():
    assert bt.daily_loss_blocks(9_499, 10_000, CAPS)
    assert not bt.daily_loss_blocks(9_500, 10_000, CAPS)


def test_caps_hold_during_a_whole_run(prep, params):
    """Replay the run's entries and check each against the caps at the moment of entry."""
    big = st.with_changes(params, {"risk_per_trade_pct": 1.5})
    r = run(prep, big, regime_mult=1.0)
    by_entry = {}
    for tr in r.trades:
        by_entry.setdefault(tr["entry_t"], []).append(tr)
    eq = dict(r.equity)
    open_at = lambda t: [x for x in r.trades if x["entry_t"] <= t < x["exit_t"]]
    for t, new in by_entry.items():
        assert len(new) <= CAPS.max_orders_per_wake
        assert len(open_at(t)) <= CAPS.max_positions
    assert max(len(open_at(tr["entry_t"])) for tr in r.trades) >= 2  # the check had something to check
    assert eq  # an equity point per step


def test_memecoin_and_coin_positions_never_exceed_their_caps(prep, params, monkeypatch):
    seen = []
    real = bt.buy_usd

    def spy(want, symbol, **kw):
        n = real(want, symbol, **kw)
        if n:
            cap = CAPS.max_meme_pct if symbol == MEME else CAPS.max_position_pct
            seen.append(n <= kw["equity"] * cap / 100 + 1e-9)
        return n

    monkeypatch.setattr(bt, "buy_usd", spy)
    run(prep, st.with_changes(params, {"risk_per_trade_pct": 1.5}), regime_mult=1.0)
    assert seen and all(seen)


def test_daily_loss_stops_buys_inside_a_run_but_not_exits(prep, params, monkeypatch):
    """From a moment on the day is over the loss limit: no buy after it, exits go on."""
    start = prep.first_ready()
    t_block = prep.grid[start + (len(prep.grid) - start) // 2]
    real = bt.daily_loss_blocks
    seen = {"after": 0}

    # daily_loss_blocks has no clock: the spy counts its calls, one per step
    calls = {"n": 0}

    def spy(equity, day_start, caps):
        calls["n"] += 1
        if prep.grid[start + calls["n"] - 1] >= t_block:
            seen["after"] += 1
            return True
        return real(equity, day_start, caps)

    monkeypatch.setattr(bt, "daily_loss_blocks", spy)
    r = run(prep, params)
    iso = bt._iso(t_block)
    assert seen["after"] > 0
    assert r.trades and all(t["entry_t"] < iso for t in r.trades)
    assert any(t["exit_t"] >= iso and t["reason"] != "fine" for t in r.trades)


def test_a_crash_is_cut_by_the_stop(params):
    up = make_bars(n=1400, drift=0.0015, noise=0.004, seed=5)
    crash = make_bars(n=200, drift=-0.01, noise=0.004, seed=6, start=up[-1].c, t0=up[-1].t + 900)
    raw = {"X/USD": as_rows(up + crash),
           "BTC/USD": as_rows(make_bars(n=1600, drift=0.0005, seed=7, start=60_000))}
    p = bt.prepare(raw)
    exits_only_by_stop = {"max_hold_hours": 336.0, "exit_threshold": -0.8, "tp_atr_mult": 15.0,
                          "stop_atr_mult": 3.0, "cooldown_hours": 0.0}
    r = bt.run(p, st.with_changes(params, exits_only_by_stop), p.first_ready())
    x = [t for t in r.trades if t["symbol"] == "X/USD"]
    assert x and x[-1]["reason"] == "stop"
    assert x[-1]["exit_price"] > crash[-1].c * 1.5  # out long before the bottom


# ---- no look-ahead and one implementation ----------------------------------------

def test_no_step_sees_a_bar_that_closes_after_it(market):
    cut = 1300
    short = {s: rows[:cut] for s, rows in market.items()}
    a, b = bt.prepare(short), bt.prepare(market)
    n = len(a.grid)
    assert a.grid == b.grid[:n]
    assert a.rows == b.rows[:n]


def test_backtest_scores_match_the_live_analysis(market, prep, params):
    frames = {s: st.frames(st.ind.parse_bars(rows)) for s, rows in market.items()}
    g = len(prep.grid) - 5
    views = st.analyze(frames, prep.grid[g], params)
    wv = st.weight_vector(params["weights"])
    for s, v in views.items():
        assert st.score_vector(prep.rows[g][s][3], wv) == pytest.approx(v.score)


# ---- walk-forward comparison ------------------------------------------------------

def test_compare_rejects_a_candidate_that_does_worse(prep, params):
    base = run(prep, params).metrics["net_return_pct"]
    assert base > 0  # the rejection below is only meaningful if the current params earn
    idle = st.with_changes(params, {"entry_threshold": 0.9, "min_edge_mult": 25.0})
    ok, why = bt.compare(prep, params, idle, prep.first_ready())
    assert not ok and "peggiore" in why


def test_compare_accepts_a_candidate_that_is_not_worse(prep, params):
    ok, _ = bt.compare(prep, params, params, prep.first_ready())
    assert ok


def test_judge_compares_on_the_recent_window_net_of_costs(market, params):
    judge = bt.judge(market, days=3)
    idle = st.with_changes(params, {"entry_threshold": 0.9, "min_edge_mult": 25.0})
    ok, why = judge(params, idle)
    assert not ok and "+0.00%" in why.split("proposti")[1]  # the idle params trade nothing
    assert judge(params, params)[0]


def m(net, dd):
    return {"net_return_pct": net, "max_drawdown_pct": dd, "start": "a", "end": "b"}


def test_verdict_rejects_a_lower_net_return():
    ok, why = bt.verdict(m(1.0, 2.0), m(0.99, 1.0))
    assert not ok and "rendimento netto peggiore" in why


def test_verdict_rejects_a_higher_return_with_a_drawdown_deeper_than_the_tolerance():
    ok, why = bt.verdict(m(1.0, 2.0), m(5.0, 2.0 + bt.DRAWDOWN_TOLERANCE_PCT + 0.01))
    assert not ok and "drawdown peggiore" in why


def test_verdict_accepts_an_equal_return_within_the_drawdown_tolerance():
    assert bt.verdict(m(1.0, 2.0), m(1.0, 2.0 + bt.DRAWDOWN_TOLERANCE_PCT))[0]


def test_each_trade_pays_the_fee_on_the_buy_and_on_the_sell(prep, params):
    # Without slippage: pnl = proceeds * (1 - r) - cost and fees = r * (cost + proceeds).
    r = 0.25 / 100
    trades = run(prep, params, costs=bt.Costs(fee_pct=0.25, slippage_pct=0.0)).trades
    checked = 0
    for t in trades:
        if abs(t["pnl_pct"]) < 1e-6:
            continue
        cost = t["pnl_usd"] / (t["pnl_pct"] / 100)
        proceeds = (t["pnl_usd"] + cost) / (1 - r)
        assert t["fees_usd"] == pytest.approx(r * (cost + proceeds), rel=1e-6)
        checked += 1
    assert checked > 5
