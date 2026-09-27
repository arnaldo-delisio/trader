"""The strategy's pure functions: parameters and their bounds, features, score,
entry rule, shortlist, sizing and exits. Every rule has a test that it rejects."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trader import strategy as st
from trader.indicators import Bar

REPO = Path(__file__).resolve().parent.parent
T0 = 1_788_220_800  # 2026-09-01 00:00 UTC


def make_bars(n: int = 1400, drift: float = 0.001, noise: float = 0.004, seed: int = 1,
              start: float = 100.0, t0: int = T0, vol: float = 10.0) -> list[Bar]:
    """Invented 15m bars: a geometric walk with a drift. No market data."""
    rnd = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        p = max(1e-6, p * math.exp(drift + rnd.gauss(0, noise)))
        hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, noise / 2))), min(o, p) * (1 - abs(rnd.gauss(0, noise / 2)))
        out.append(Bar(t0 + i * 900, o, hi, lo, p, vol * (1 + rnd.random())))
    return out


def as_rows(bars: list[Bar]) -> list[dict]:
    """Bars in Alpaca's JSON shape."""
    return [{"t": datetime.fromtimestamp(b.t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v} for b in bars]


@pytest.fixture
def params():
    return st.load_params(REPO / "config" / "params.json")


def end_of(bars: list[Bar]) -> int:
    return bars[-1].t + st.M15


# ---- parameters -------------------------------------------------------------

def test_the_shipped_params_file_is_valid(params):
    assert st.validate_params(params) is params


@pytest.mark.parametrize("change", [
    {"entry_threshold": 0.95},        # above the bound
    {"stop_atr_mult": 0.5},           # below the bound
    {"shortlist_size": 7.5},          # not an integer
    {"weights.macd": 3.0},            # weight above the bound
    {"regime_filter": 2},
])
def test_params_outside_the_bounds_are_rejected(params, change):
    with pytest.raises(st.ParamsError):
        st.with_changes(params, change)


def test_params_with_a_missing_or_unknown_key_are_rejected(params):
    missing = {k: v for k, v in params.items() if k != "tp_atr_mult"}
    with pytest.raises(st.ParamsError, match="missing"):
        st.validate_params(missing)
    with pytest.raises(st.ParamsError, match="unknown"):
        st.validate_params({**params, "leverage": 3})


@pytest.mark.parametrize("bad", [float("nan"), True, "0.5", None])
def test_params_that_are_not_numbers_are_rejected(params, bad):
    with pytest.raises(st.ParamsError):
        st.validate_params({**params, "entry_threshold": bad})


def test_all_zero_weights_are_rejected(params):
    with pytest.raises(st.ParamsError, match="non-zero"):
        st.validate_params({**params, "weights": {k: 0 for k in st.COMPONENTS}})


def test_a_take_profit_much_closer_than_the_stop_is_rejected(params):
    with pytest.raises(st.ParamsError, match="tp_atr_mult"):
        st.with_changes(params, {"stop_atr_mult": 5.0, "tp_atr_mult": 2.0})


def test_with_changes_does_not_touch_the_original(params):
    before = json.dumps(params, sort_keys=True)
    st.with_changes(params, {"weights.rsi": 0.25})
    assert json.dumps(params, sort_keys=True) == before


def test_a_small_learnable_change_is_allowed(params):
    assert st.check_change(params, {"entry_threshold": params["entry_threshold"] + 0.05,
                                    "weights.macd": params["weights"]["macd"] - 0.25}) == []


def test_a_step_larger_than_allowed_is_rejected(params):
    reasons = st.check_change(params, {"entry_threshold": params["entry_threshold"] + 0.1})
    assert reasons and "passo" in reasons[0]


def test_a_change_to_a_non_learnable_parameter_is_rejected(params):
    for k in ("min_edge_mult", "risk_per_trade_pct", "position_pct", "regime_filter"):
        assert "non modificabile" in st.check_change(params, {k: params[k]})[0]


def test_a_change_outside_the_bounds_is_rejected_even_if_the_step_is_small(params):
    p = st.with_changes(params, {"entry_threshold": 0.9})
    assert "fuori dai limiti" in st.check_change(p, {"entry_threshold": 0.94})[0]


def test_unknown_or_non_numeric_changes_are_rejected(params):
    assert "sconosciuto" in st.check_change(params, {"leverage": 2})[0]
    assert "limite rigido" in st.check_change(params, {"max_invested_pct": 90})[0]
    assert "limite rigido" in st.check_change(params, {"explore_total_pct": 6})[0]
    assert "non è un numero" in st.check_change(params, {"tp_atr_mult": "7"})[0]


# ---- costs ------------------------------------------------------------------

def test_costs_are_the_taker_fee_plus_half_the_spread():
    assert st.side_cost_pct(0.4) == pytest.approx(0.25 + 0.2)
    assert st.round_trip_cost_pct(0.4) == pytest.approx(0.9)
    assert st.side_cost_pct(None) == pytest.approx(st.TAKER_FEE_PCT + st.DEFAULT_HALF_SPREAD_PCT)


# ---- features and score -----------------------------------------------------

def test_features_are_none_with_too_few_bars():
    bars = make_bars(100)  # 4h EMA 50 needs 200 hours
    assert st.features(st.frames(bars), end_of(bars)) is None


def test_features_are_none_when_the_data_is_stale():
    bars = make_bars()
    fr = st.frames(bars)
    assert st.features(fr, end_of(bars)) is not None
    assert st.features(fr, end_of(bars) + st.STALE_SECONDS + 1) is None


def test_features_never_use_a_bar_that_has_not_closed():
    bars = make_bars()
    t = end_of(bars) - 1  # the last bar ends one second later
    f = st.features(st.frames(bars), t)
    assert f["price"] == bars[-2].c
    # a wild last bar changes nothing before it closes
    wild = bars[:-1] + [bars[-1]._replace(c=bars[-1].c * 3, h=bars[-1].c * 3)]
    assert st.features(st.frames(wild), t) == f


def test_components_stay_in_range_and_an_uptrend_outscores_a_downtrend(params):
    up, down = make_bars(drift=0.002, seed=2), make_bars(drift=-0.002, seed=3)
    t = end_of(up)
    views = st.analyze({"UP/USD": st.frames(up), "DOWN/USD": st.frames(down)}, t, params)
    for v in views.values():
        assert all(-1 <= c <= 1 for c in v.components.values())
        assert -1 <= v.score <= 1
    assert views["UP/USD"].score > 0.5 > views["DOWN/USD"].score
    assert views["UP/USD"].components["momentum"] == 1 and views["DOWN/USD"].components["momentum"] == -1


def test_score_is_the_weighted_average():
    comp = {k: 0.0 for k in st.COMPONENTS} | {"trend_4h": 1.0, "rsi": -1.0}
    w = {k: 0.0 for k in st.COMPONENTS} | {"trend_4h": 3.0, "rsi": 1.0}
    assert st.score(comp, w) == pytest.approx((3 - 1) / 4)


def test_rsi_component_rewards_momentum_and_punishes_extremes():
    assert st._rsi_component(20) == -1 and st._rsi_component(90) == -1
    assert st._rsi_component(65) == 1
    assert st._rsi_component(45) == pytest.approx(0)


# ---- entry rule -------------------------------------------------------------

def test_entry_needs_the_score_above_the_threshold(params):
    f = {"atr_1h_pct": 10.0}
    ok, why = st.entry_ok(params["entry_threshold"] - 0.01, f, params, 1.0)
    assert not ok and "soglia" in why
    assert st.entry_ok(params["entry_threshold"], f, params, 1.0)[0]


def test_entry_is_rejected_when_the_expected_move_does_not_beat_the_costs(params):
    cost = 1.0
    need = params["min_edge_mult"] * cost
    f_small = {"atr_1h_pct": need / params["tp_atr_mult"] * 0.99}
    ok, why = st.entry_ok(0.99, f_small, params, cost)
    assert not ok and "costi" in why
    assert st.entry_ok(0.99, {"atr_1h_pct": need / params["tp_atr_mult"] * 1.01}, params, cost)[0]


def test_entry_is_rejected_in_a_weak_market_when_the_filter_is_on(params):
    f = {"atr_1h_pct": 10.0}
    on = st.with_changes(params, {"regime_filter": 1})
    ok, why = st.entry_ok(0.99, f, on, 1.0, regime_ok=False)
    assert not ok and "mercato debole" in why
    assert st.entry_ok(0.99, f, st.with_changes(params, {"regime_filter": 0}), 1.0, regime_ok=False)[0]


def _market(n, up, ret7):
    return {f"S{i}": {"trend_4h": 1.0 if i < up else -1.0, "ret_7d_pct": ret7} for i in range(n)}


def test_regime_needs_a_rising_basket_and_enough_coins_in_an_uptrend():
    assert st.regime_ok(_market(10, 6, 2.0)) is True
    assert st.regime_ok(_market(10, 5, 2.0)) is False   # breadth 50% < 60%
    assert st.regime_ok(_market(10, 10, -0.1)) is False  # basket down over 7 days
    assert st.regime_ok(_market(10, 10, 0.0)) is False   # flat is not rising


def test_regime_is_not_ok_with_too_few_coins():
    assert st.regime_ok({}) is False
    assert st.regime_ok(_market(st.REGIME_MIN_SYMBOLS - 1, 4, 5.0)) is False


# ---- shortlist --------------------------------------------------------------

def _view(sym, sc):
    return st.View(sym, {}, {}, sc, 1.0, False, "")


def test_shortlist_is_top_n_plus_every_holding():
    views = {s: _view(s, sc) for s, sc in (("A", 0.9), ("B", 0.5), ("C", 0.1), ("D", -0.4))}
    assert st.shortlist(views, held=["D"], n=2) == ["A", "B", "D"]
    assert st.shortlist(views, held=["A"], n=2) == ["A", "B"]


def test_a_holding_without_data_stays_on_the_shortlist():
    views = {"A": _view("A", 0.9)}
    assert st.shortlist(views, held=["GONE/USD"], n=3) == ["A", "GONE/USD"]


# ---- sizing -----------------------------------------------------------------

def test_size_is_risk_based_and_capped_by_position_pct(params):
    p = st.with_changes(params, {"risk_per_trade_pct": 0.5, "stop_atr_mult": 3.0, "position_pct": 8.0})
    # stop 3 x 2% = 6% away: risking 0.5% of 10k means 833 $, capped at 8% = 800 $
    assert st.size_usd(10_000, {"atr_1h_pct": 2.0}, p) == pytest.approx(800)
    # stop 3 x 4% = 12% away: 416.67 $, under the cap
    assert st.size_usd(10_000, {"atr_1h_pct": 4.0}, p) == pytest.approx(416.6667, rel=1e-4)
    assert st.size_usd(10_000, {"atr_1h_pct": 4.0}, p, regime_mult=0.4) == pytest.approx(166.6667, rel=1e-4)


def test_size_is_zero_without_equity_or_volatility(params):
    assert st.size_usd(0, {"atr_1h_pct": 2.0}, params) == 0
    assert st.size_usd(10_000, {"atr_1h_pct": 0.0}, params) == 0


# ---- exits ------------------------------------------------------------------

def _levels(params):
    p = st.with_changes(params, {"stop_atr_mult": 2.0, "tp_atr_mult": 6.0})
    return p, st.open_levels(100.0, T0, 1.0, p)


def test_new_levels_by_hand(params):
    _, lv = _levels(params)
    assert lv["stop"] == 98.0 and lv["take_profit"] == 106.0 and lv["highest"] == 100.0


def test_trailing_stop_follows_the_high_and_never_moves_down(params):
    p, lv = _levels(params)
    lv = st.update_levels(lv, 104.0, 1.0, p)
    assert lv["stop"] == 102.0 and lv["highest"] == 104.0
    lv = st.update_levels(lv, 101.0, 3.0, p)  # price falls and volatility triples
    assert lv["stop"] == 102.0 and lv["highest"] == 104.0


def test_exit_reasons(params):
    p, lv = _levels(params)
    assert st.exit_reason(lv, 98.0, T0 + 900, 0.9, p) == "stop"
    assert st.exit_reason(lv, 106.0, T0 + 900, 0.9, p) == "take_profit"
    assert st.exit_reason(lv, 101.0, T0 + int(p["max_hold_hours"] * 3600), 0.9, p) == "tempo"
    assert st.exit_reason(lv, 101.0, T0 + 900, p["exit_threshold"], p) == "segnale"


def test_no_exit_while_inside_the_levels(params):
    p, lv = _levels(params)
    assert st.exit_reason(lv, 101.0, T0 + 900, 0.0, p) is None
    assert st.exit_reason(lv, 101.0, T0 + 900, None, p) is None


def _fill(side, qty, price, minute):
    return {"side": side, "qty": str(qty), "price": str(price),
            "transaction_time": f"2026-09-10T00:{minute:02d}:00Z"}


def test_levels_are_rebuilt_from_fills(params):
    bars = make_bars(n=1500)
    fr = st.frames(bars)
    fills = [_fill("buy", 1, 100, 0), _fill("buy", 1, 110, 5), _fill("sell", 1, 120, 10)]
    lv = st.levels_from_fills(fills, fr, params)
    assert lv["entry_price"] == pytest.approx(105)  # the average cost of what is still held
    assert lv["entry_t"] == int(datetime(2026, 9, 10, tzinfo=UTC).timestamp())
    assert lv["stop"] >= lv["entry_price"] - params["stop_atr_mult"] * lv["atr_at_entry"]


def test_no_levels_when_the_fills_end_flat_and_a_new_buy_resets_the_entry(params):
    fr = st.frames(make_bars(n=1500))
    flat = [_fill("buy", 1, 100, 0), _fill("sell", 1, 120, 10)]
    assert st.levels_from_fills(flat, fr, params) is None
    again = flat + [_fill("buy", 2, 90, 20)]
    assert st.levels_from_fills(again, fr, params)["entry_price"] == pytest.approx(90)


def test_the_stop_is_checked_at_the_bid_not_at_the_entry_price():
    # Real Alpaca positions carry avg_entry_price; a check at that price would never fire.
    from trader import context
    params = st.load_params(REPO / "config" / "params.json")
    t = int(datetime(2026, 9, 26, tzinfo=UTC).timestamp())
    m = context.market({}, [], t, params, {})
    stored = {"BTC/USD": {"entry_price": 100.0, "entry_t": t - 3600, "atr_at_entry": 1.0, "highest": 100.0,
                          "stop": 95.0, "take_profit": 110.0}}
    pos = [{"symbol": "BTCUSD", "asset_class": "crypto", "qty": "1", "qty_available": "1",
            "market_value": "90", "avg_entry_price": "100", "current_price": "90"}]
    tracked, _ = context.track(pos, stored, m, [], {"BTC/USD": 90.0}, params, 10.0)
    assert tracked["BTC/USD"].exit == "stop" and tracked["BTC/USD"].price == 90.0


# ---- exploration --------------------------------------------------------------------------

def _view(entry=False, trend_4h=0.5, atr_pct=1.0, cost=0.6):
    return st.View("SOL/USD", {"atr_1h_pct": atr_pct}, {"trend_4h": trend_4h}, 0.4, cost, entry, "sotto la soglia")


def test_explore_ok_takes_a_coin_below_the_rule_in_a_4h_uptrend(params):
    assert st.explore_ok(_view(), params) == (True, "ok")


def test_explore_ok_rejects_what_it_must(params):
    assert "già" in st.explore_ok(_view(entry=True), params)[1]           # the strict rule handles it
    assert "tendenza 4h" in st.explore_ok(_view(trend_4h=0.0), params)[1]  # flat or falling 4h trend
    assert "tendenza 4h" in st.explore_ok(_view(trend_4h=-1 / 3), params)[1]
    # take-profit distance 6 x 0.2% = 1.2% < 3 x 0.6% round trip: cannot pay for itself
    assert "movimento atteso" in st.explore_ok(_view(atr_pct=0.2), params)[1]


def test_exploration_parameters_are_not_learnable():
    assert not {k for k in st.LEARNABLE if "explore" in k}
