from dataclasses import replace

import pytest

from trader.config import HARD_CEILINGS, HARD_FLOORS, clamp_limits
from trader.model import Decision
from trader.risk import Holding, Snapshot, gate

RAW = {"symbols": ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LINK/USD"], "max_invested_pct": 60,
       "max_position_pct": 8, "max_memecoin_pct": 3, "max_open_positions": 10, "max_orders_per_wake": 3,
       "daily_loss_limit_pct": 5, "min_order_usd": 10,
       "explore_position_pct": 1, "explore_total_pct": 5}
LIMITS = clamp_limits(RAW)
PX = {"BTC/USD": 60000.0, "ETH/USD": 3000.0, "SOL/USD": 150.0, "DOGE/USD": 0.2, "LINK/USD": 15.0}
SNAP = Snapshot(equity=10000, last_equity=10000, cash=5000, trading_blocked=False, bids=PX, asks=PX)
# The strategy allows every symbol up to 800$ in these tests unless a test says otherwise.
ENTRIES = {s: 800.0 for s in PX}


def buy(sym="BTC/USD", n=100.0):
    return Decision(sym, "buy", n, "r")


def sell(sym="BTC/USD", n=100.0):
    return Decision(sym, "sell", n, "r")


def one(decision, snap=SNAP, limits=LIMITS, **kw):
    kw.setdefault("entries", ENTRIES)
    return gate([decision], snap, limits, **kw)[0]


def many(decisions, snap=SNAP, limits=LIMITS, **kw):
    kw.setdefault("entries", ENTRIES)
    return gate(decisions, snap, limits, **kw)


def test_a_buy_inside_every_limit_is_approved():
    v = one(buy())
    assert v.approved and v.order == {"symbol": "BTC/USD", "side": "buy", "notional": 100.0}


def test_rejects_symbol_outside_allowlist():
    v = one(buy("PEPE/USD"), entries={"PEPE/USD": 800.0})
    assert not v.approved and "lista" in v.reason and v.order is None


def test_rejects_a_buy_the_strategy_did_not_select():
    v = one(buy("ETH/USD"), entries={"BTC/USD": 800.0})
    assert not v.approved and "candidato" in v.reason and v.order is None
    assert not one(buy("ETH/USD"), entries=None).approved  # no candidates given: no buys at all


def test_rejects_a_buy_over_the_strategy_size():
    v = one(buy(n=300.02), entries={"BTC/USD": 300.0})
    assert not v.approved and "dimensione della strategia" in v.reason
    assert one(buy(n=300.0), entries={"BTC/USD": 300.0}).approved


def test_rejects_position_over_max_per_coin_of_equity():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.012, 720.0)})  # 7.2% of 10k held, cap 8%
    v = one(buy(n=100), snap)
    assert not v.approved and "8% del patrimonio" in v.reason
    assert one(buy(n=80), snap).approved


def test_memecoin_cap_is_lower_than_the_coin_cap():
    v = one(buy("DOGE/USD", 301))  # 3% of 10k is 300$
    assert not v.approved and "3% del patrimonio" in v.reason
    assert one(buy("DOGE/USD", 300)).approved and one(buy("LINK/USD", 301)).approved


def test_rejects_invested_over_max_pct_of_equity():
    held = {k: Holding(1, 740.0) for k in ("ETHUSD", "SOLUSD", "LINKUSD", "AAVEUSD", "UNIUSD", "DOTUSD",
                                          "ADAUSD", "XRPUSD")}  # 5920$ invested, cap 60% = 6000$
    v = one(buy(n=100), replace(SNAP, holdings=held, cash=4000))
    assert not v.approved and "investito" in v.reason
    assert one(buy(n=80), replace(SNAP, holdings=held, cash=4000)).approved


def test_rejects_a_new_position_beyond_the_maximum_count():
    limits = replace(LIMITS, max_open_positions=2)
    snap = replace(SNAP, holdings={"ETHUSD": Holding(0.01, 30.0), "SOLUSD": Holding(0.2, 30.0),
                                   "BTCUSD": Holding(1e-8, 0.0006)})  # BTC is fee dust, not a position
    v = one(buy("BTC/USD", 50), snap, limits)
    assert not v.approved and "posizioni aperte" in v.reason
    vs = many([buy("LINK/USD", 50)], replace(SNAP, holdings={"ETHUSD": Holding(0.01, 30.0)}), limits)
    assert vs[0].approved


def test_new_positions_in_the_same_wake_count_toward_the_maximum():
    limits = replace(LIMITS, max_open_positions=1)
    vs = many([buy("BTC/USD", 50), buy("ETH/USD", 50)], limits=limits)
    assert vs[0].approved and not vs[1].approved and "posizioni aperte" in vs[1].reason


def test_rejects_order_under_min_size():
    v = one(buy(n=9.99))
    assert not v.approved and "minimo" in v.reason


def test_rejects_more_orders_than_the_per_wake_maximum():
    decisions = [buy("BTC/USD", 20), buy("ETH/USD", 20), buy("SOL/USD", 20)]
    limits = replace(LIMITS, max_orders_per_wake=2)
    vs = many(decisions, SNAP, limits)
    assert [v.approved for v in vs] == [True, True, False]
    assert "massimo di 2 ordini" in vs[2].reason


def test_daily_loss_blocks_buys_but_allows_sells():
    snap = replace(SNAP, equity=9490, last_equity=10000, holdings={"BTCUSD": Holding(0.005, 300.0)})
    vs = many([buy("ETH/USD", 50), sell("BTC/USD", 100)], snap)
    assert not vs[0].approved and "perdita giornaliera" in vs[0].reason
    assert vs[1].approved and vs[1].order["side"] == "sell"


def test_kill_switch_rejects_every_order_even_sells():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.005, 300.0)})
    vs = many([buy("ETH/USD", 50), sell("BTC/USD", 100)], snap, enabled=False, disabled_reason="KILL")
    assert not any(v.approved for v in vs)
    assert all("kill switch" in v.reason for v in vs)


def test_rejects_sell_with_nothing_held_no_shorting():
    v = one(sell("ETH/USD", 50))
    assert not v.approved and "short" in v.reason


def test_rejects_sell_larger_than_holding():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.001, 60.0)})  # 60$ held
    v = one(sell("BTC/USD", 100), snap)
    assert not v.approved and "oltre il detenuto" in v.reason


def test_sell_quantity_never_exceeds_quantity_held():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.001, 60.0)})
    v = one(sell("BTC/USD", 60.5), snap)  # within rounding of the full position
    assert v.approved and v.order["qty"] <= 0.001


def test_rejects_buy_without_enough_cash():
    snap = replace(SNAP, cash=40)
    v = one(buy(n=50), snap)
    assert not v.approved and "liquidità" in v.reason


def test_cash_is_consumed_by_earlier_buys_in_the_same_wake():
    snap = replace(SNAP, cash=120)
    vs = many([buy("BTC/USD", 100), buy("ETH/USD", 50)], snap)
    assert vs[0].approved and not vs[1].approved


def test_rejects_when_an_order_is_already_open_on_the_symbol():
    snap = replace(SNAP, open_order_symbols=frozenset({"BTCUSD"}))
    v = one(buy(), snap)
    assert not v.approved and "ordine aperto" in v.reason


def test_rejects_when_the_account_is_blocked():
    v = one(buy(), replace(SNAP, trading_blocked=True))
    assert not v.approved and "bloccato" in v.reason


def test_hold_never_produces_an_order():
    v = one(Decision("BTC/USD", "hold", 100, "r"))
    assert not v.approved and v.order is None


def test_gate_is_deterministic():
    ds = [buy("BTC/USD", 100), sell("ETH/USD", 10), buy("SOL/USD", 30)]
    assert [v.as_dict() for v in many(ds)] == [v.as_dict() for v in many(ds)]


@pytest.mark.parametrize("key", sorted(HARD_CEILINGS))
def test_config_cannot_raise_a_hard_ceiling(key):
    raw = {**RAW, key: 10 ** 9}
    assert getattr(clamp_limits(raw), key) == HARD_CEILINGS[key]


def test_config_can_lower_a_ceiling():
    assert clamp_limits({**RAW, "max_invested_pct": 20}).max_invested_pct == 20


def test_config_cannot_lower_the_minimum_order_or_add_symbols():
    lim = clamp_limits({**RAW, "symbols": ["BTC/USD", "PEPE/USD", "USDT/USD"], "min_order_usd": 0.5})
    assert lim.min_order_usd == HARD_FLOORS["min_order_usd"] and lim.symbols == ("BTC/USD",)


def test_the_shipped_limits_file_sits_at_the_ceilings():
    from pathlib import Path

    from trader.config import HARD_SYMBOLS, load_limits
    lim = load_limits(Path(__file__).resolve().parent.parent / "config/limits.toml")
    assert lim.symbols == HARD_SYMBOLS and lim.max_invested_pct == 60 and lim.max_memecoin_pct == 3
    assert "PAXG/USD" not in HARD_SYMBOLS and not [s for s in HARD_SYMBOLS if s.startswith("USD")]


def test_sells_are_allowed_outside_the_allowlist_and_the_strategy():
    snap = replace(SNAP, holdings={"PEPEUSD": Holding(1000, 50.0)}, bids={**PX, "PEPE/USD": 0.05})
    v = one(sell("PEPE/USD", 50), snap, entries={})
    assert v.approved and v.order["qty"] == 1000


def test_sells_count_toward_the_per_wake_maximum():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.005, 300.0), "ETHUSD": Holding(0.1, 300.0)})
    limits = replace(LIMITS, max_orders_per_wake=2)
    vs = many([sell("BTC/USD", 50), sell("ETH/USD", 50), buy("SOL/USD", 20)], snap, limits)
    assert [v.approved for v in vs] == [True, True, False]
    assert "massimo di 2 ordini" in vs[2].reason


def test_rejects_sell_under_min_size():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.005, 300.0)})
    v = one(sell("BTC/USD", 5), snap)
    assert not v.approved and "minimo" in v.reason and v.order is None


def test_rejects_buy_without_an_ask_price():
    v = one(buy(), replace(SNAP, asks={}))
    assert not v.approved and "ask" in v.reason


def test_invested_is_consumed_by_earlier_buys_in_the_same_wake():
    # 5800$ held; each buy alone fits under 6000$ (60%), the second one on top of the first does not.
    held = {k: Holding(1, 725.0) for k in ("AAVEUSD", "UNIUSD", "DOTUSD", "ADAUSD", "XRPUSD", "FILUSD",
                                          "GRTUSD", "LDOUSD")}
    vs = many([buy("BTC/USD", 150), buy("ETH/USD", 100)], replace(SNAP, holdings=held))
    assert vs[0].approved and not vs[1].approved and "investito" in vs[1].reason


@pytest.mark.parametrize("bad", [float("nan"), -1])
def test_config_rejects_nan_or_negative_limits(bad):
    from trader.config import ConfigError
    with pytest.raises(ConfigError):
        clamp_limits({**RAW, "max_position_pct": bad})


def test_hard_ceilings_are_the_approved_numbers():
    # The ceilings are the owner's, not the config's: raising one in code must fail a test.
    assert {k: HARD_CEILINGS[k] for k in ("max_invested_pct", "max_position_pct", "max_memecoin_pct",
                                          "max_open_positions", "daily_loss_limit_pct")} == {
        "max_invested_pct": 60.0, "max_position_pct": 8.0, "max_memecoin_pct": 3.0,
        "max_open_positions": 25, "daily_loss_limit_pct": 5.0}
    assert HARD_CEILINGS["max_orders_per_wake"] <= 5 and HARD_FLOORS["min_order_usd"] >= 10.0
    huge = clamp_limits({**RAW, **{k: 10 ** 9 for k in HARD_CEILINGS}, "min_order_usd": 0})
    assert (huge.max_invested_pct, huge.max_position_pct, huge.max_memecoin_pct, huge.max_open_positions,
            huge.daily_loss_limit_pct, huge.min_order_usd) == (60.0, 8.0, 3.0, 25, 5.0, 10.0)
    assert clamp_limits({**RAW, "max_open_positions": 26}).max_open_positions == 25


# ---- exploration: small buys below the entry threshold, under their own caps -----------
# SNAP: equity 10000$, so one exploration buy may be 100$ (1%) and all of them 500$ (5%).
EXPLORE = {"SOL/USD": 100.0, "LINK/USD": 100.0}


def explore_one(decision, snap=SNAP, limits=LIMITS, explore=EXPLORE, **kw):
    return gate([decision], snap, limits, entries={}, explore=explore, **kw)[0]


def test_an_exploration_buy_inside_its_caps_is_approved_and_labelled():
    v = explore_one(buy("SOL/USD", 100))
    assert v.approved and v.explore and v.order == {"symbol": "SOL/USD", "side": "buy", "notional": 100.0}
    assert not one(buy("BTC/USD", 100)).explore  # a strategy entry is not exploration


def test_rejects_an_exploration_buy_over_its_candidate_size():
    v = explore_one(buy("SOL/USD", 100), explore={"SOL/USD": 60.0})
    assert not v.approved and "dimensione ammessa" in v.reason


def test_rejects_an_exploration_buy_over_one_percent_of_equity_whatever_the_size_says():
    v = explore_one(buy("SOL/USD", 150), explore={"SOL/USD": 150.0})
    assert not v.approved and "1% del patrimonio per posizione" in v.reason


def test_rejects_exploration_beyond_five_percent_of_equity_counting_what_is_held():
    held = {k: Holding(1, 100.0) for k in ("AAVEUSD", "UNIUSD", "DOTUSD", "ADAUSD")}  # 400$ of exploration
    snap = replace(SNAP, holdings=held)
    vs = gate([buy("SOL/USD", 100), buy("LINK/USD", 100)], snap, LIMITS, entries={}, explore=EXPLORE,
              explore_held=frozenset(held))
    assert vs[0].approved and not vs[1].approved and "5% del patrimonio" in vs[1].reason
    # the same holdings not labelled exploration leave the whole exploration budget free
    vs = gate([buy("SOL/USD", 100), buy("LINK/USD", 100)], snap, LIMITS, entries={}, explore=EXPLORE)
    assert all(v.approved for v in vs)


def test_rejects_an_exploration_buy_on_a_coin_already_held():
    snap = replace(SNAP, holdings={"SOLUSD": Holding(1, 50.0)})
    v = explore_one(buy("SOL/USD", 40), snap)
    assert not v.approved and "già in portafoglio" in v.reason


def test_exploration_turned_off_in_the_config_rejects_every_exploration_buy():
    v = explore_one(buy("SOL/USD", 10), limits=clamp_limits({**RAW, "explore_total_pct": 0}))
    assert not v.approved and "esplorazione" in v.reason
    v = explore_one(buy("SOL/USD", 10), limits=clamp_limits({**RAW, "explore_position_pct": 0}))
    assert not v.approved and "esplorazione" in v.reason


def test_config_cannot_raise_the_exploration_caps():
    lim = clamp_limits({**RAW, "explore_position_pct": 8, "explore_total_pct": 60})
    assert (lim.explore_position_pct, lim.explore_total_pct) == (1.5, 15.0)
    assert (HARD_CEILINGS["explore_position_pct"], HARD_CEILINGS["explore_total_pct"]) == (1.5, 15.0)


def test_the_hard_exploration_caps_reject_a_buy_over_them():
    # The config at the ceilings: 1.5% of 10000$ per buy is 150$, 15% in total is 1500$.
    top = clamp_limits({**RAW, "explore_position_pct": 1.5, "explore_total_pct": 15})
    wide = {"SOL/USD": 1000.0, "LINK/USD": 1000.0}
    assert explore_one(buy("SOL/USD", 150), limits=top, explore=wide).approved
    v = explore_one(buy("SOL/USD", 151), limits=top, explore=wide)
    assert not v.approved and "1.5% del patrimonio per posizione" in v.reason
    held = {f"C{i}USD": Holding(1, 150.0) for i in range(9)}  # 1350$ of exploration already
    v = explore_one(buy("SOL/USD", 151 - 1), replace(SNAP, holdings=held), limits=top, explore=wide,
                    explore_held=frozenset(held))
    assert v.approved
    held["C9USD"] = Holding(1, 100.0)  # 1450$: 150$ more would make 1600$
    v = explore_one(buy("SOL/USD", 150), replace(SNAP, holdings=held), limits=top, explore=wide,
                    explore_held=frozenset(held))
    assert not v.approved and "15% del patrimonio" in v.reason


def test_explore_exposure_counts_held_and_pending_exploration_only():
    from trader.risk import explore_exposure
    snap = replace(SNAP, holdings={"SOLUSD": Holding(1, 120.0), "BTCUSD": Holding(1, 700.0)},
                   pending_buys={"LINKUSD": 80.0, "ETHUSD": 300.0})
    assert explore_exposure(snap, frozenset({"SOLUSD", "LINKUSD"})) == 200.0
    assert explore_exposure(snap, frozenset()) == 0.0


# ---- TRADING_ENABLED off: exits only -------------------------------------------------------

def test_buys_switched_off_reject_every_buy_but_let_sells_through():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.01, 600.0)})
    vs = many([buy("ETH/USD", 50), sell("BTC/USD", 600)], snap, buys_enabled=False,
              disabled_reason="TRADING_ENABLED=false")
    assert not vs[0].approved and "acquisti sospesi" in vs[0].reason
    assert vs[1].approved and vs[1].order == {"symbol": "BTC/USD", "side": "sell", "qty": 0.01}
    assert "acquisti sospesi" in explore_one(buy("SOL/USD", 100), buys_enabled=False).reason


def test_exploration_buys_obey_every_other_limit_too():
    assert "perdita giornaliera" in explore_one(buy("SOL/USD", 100), replace(SNAP, equity=9400)).reason
    assert "kill switch" in explore_one(buy("SOL/USD", 100), enabled=False).reason
    assert "liquidità" in explore_one(buy("SOL/USD", 100), replace(SNAP, cash=50)).reason
    full = {f"C{i}USD": Holding(1, 50.0) for i in range(LIMITS.max_open_positions)}
    assert "posizioni aperte" in explore_one(buy("SOL/USD", 100), replace(SNAP, holdings=full)).reason
    assert "non è un candidato" in explore_one(buy("ETH/USD", 100)).reason


# ---- exits below the order minimum ------------------------------------------------------

def test_a_whole_position_under_the_order_minimum_can_still_be_sold():
    # 5$ of BTC: under the 10$ minimum, over Alpaca's own (about 1$). An exit must go through.
    snap = replace(SNAP, holdings={"BTCUSD": Holding(5 / 60000, 5.0)})
    v = one(sell("BTC/USD", 5), snap)
    assert v.approved and v.order["qty"] == pytest.approx(5 / 60000)


def test_a_partial_sell_under_the_order_minimum_is_still_rejected():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.005, 300.0)})
    v = one(sell("BTC/USD", 5), snap)
    assert not v.approved and "minimo per ordine" in v.reason


def test_a_whole_position_under_alpacas_minimum_is_rejected():
    snap = replace(SNAP, holdings={"BTCUSD": Holding(0.5 / 60000, 0.5)})
    v = one(sell("BTC/USD", 0.5), snap)
    assert not v.approved and "vendita di tutto" in v.reason
    # and Alpaca's min_order_size for the coin wins when it is higher than the 1$ floor
    snap = replace(SNAP, holdings={"BTCUSD": Holding(2 / 60000, 2.0)}, min_qty={"BTC/USD": 3 / 60000})
    v = one(sell("BTC/USD", 2), snap)
    assert not v.approved and "3.00$" in v.reason


# ---- buy orders still open count as spent ------------------------------------------------

def test_an_unfilled_buy_counts_toward_the_invested_cap():
    held = {k: Holding(1, 725.0) for k in ("AAVEUSD", "UNIUSD", "DOTUSD", "ADAUSD", "XRPUSD", "FILUSD", "GRTUSD")}
    snap = replace(SNAP, holdings=held)  # 5075$ held: a 150$ buy fits under 6000$
    assert one(buy("BTC/USD", 150), snap).approved
    v = one(buy("BTC/USD", 150), replace(snap, pending_buys={"LDOUSD": 800.0}))
    assert not v.approved and "investito" in v.reason


def test_an_unfilled_exploration_buy_counts_toward_the_exploration_total():
    snap = replace(SNAP, pending_buys={"AAVEUSD": 450.0})
    v = explore_one(buy("SOL/USD", 100), snap, explore_held=frozenset({"AAVEUSD"}))
    assert not v.approved and "5% del patrimonio" in v.reason
    assert explore_one(buy("SOL/USD", 50), snap, explore_held=frozenset({"AAVEUSD"})).approved


def test_an_unfilled_buy_on_a_new_coin_counts_as_a_position():
    full = {f"C{i}USD": Holding(1, 50.0) for i in range(LIMITS.max_open_positions - 1)}
    snap = replace(SNAP, holdings=full, pending_buys={"LDOUSD": 50.0})
    v = one(buy("BTC/USD", 100), snap)
    assert not v.approved and "posizioni aperte" in v.reason


def test_pending_buys_are_what_open_buy_orders_may_still_spend():
    from trader.context import _pending_buys
    orders = [{"symbol": "POL/USD", "side": "buy", "notional": "300", "filled_qty": "0", "filled_avg_price": None},
              {"symbol": "BTC/USD", "side": "buy", "notional": "100", "filled_qty": "0.0005",
               "filled_avg_price": "60000"},
              {"symbol": "ETH/USD", "side": "buy", "qty": "2", "filled_qty": "0.5", "filled_avg_price": "3000"},
              {"symbol": "SOL/USD", "side": "sell", "qty": "1", "filled_qty": "0"}]
    assert _pending_buys(orders) == {"POLUSD": 300.0, "BTCUSD": 70.0, "ETHUSD": 4500.0}
