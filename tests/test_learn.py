"""trader/learn.py: outcomes, attribution, the reflection and the acceptance rule.

Every rejection rule has a test that shows it rejecting. All data here is invented.
"""

import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from tests.conftest import REPO
from trader import backtest as bt
from trader import learn
from trader.model import ClaudeCLI, FakeModel
from trader.records import Records

NOW = datetime(2026, 9, 26, 12, 5, tzinfo=UTC)
SLOT = "20260926T1200Z"
# The shipped parameters with a round stop, so the steps below are easy to read.
PARAMS = {**json.loads((REPO / "config/params.json").read_text()), "stop_atr_mult": 2.5, "tp_atr_mult": 6.0,
          "entry_threshold": 0.5}


def fill(fid, side, qty, price, t, order_id, symbol="BTC/USD"):
    return {"id": fid, "activity_type": "FILL", "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": t, "order_id": order_id, "symbol": symbol}


def order(oid, cid):
    return {"id": oid, "client_order_id": cid}


def reflection(lessons=("lezione di prova",), changes=()):
    return json.dumps({"lessons": list(lessons), "param_changes": [
        {"name": n, "new_value": v, "reason": f"motivo per {n}"} for n, v in changes]})


# ---- closed trades --------------------------------------------------------------

def test_round_trip_is_net_of_fees_on_both_sides():
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 1, 110, "2026-09-25T16:00:00Z", "o2")]
    [t] = learn.closed_trades(fills, [], [], fee_pct=0.25)
    assert t.fees_usd == pytest.approx(0.0025 * (100 + 110))
    assert t.pnl_usd == pytest.approx(10 - 0.525)
    assert t.pnl_pct == pytest.approx(100 * 9.475 / 100)
    assert t.hold_hours == 6


def test_fees_can_turn_a_small_gain_into_a_loss():
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 1, 100.4, "2026-09-25T10:15:00Z", "o2")]
    [t] = learn.closed_trades(fills, [], [], fee_pct=0.25)
    assert t.pnl_usd < 0


def test_partial_fills_of_one_order_make_one_trade_closed_fifo():
    fills = [fill("f1", "buy", 0.5, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "buy", 0.5, 102, "2026-09-25T10:00:01Z", "o1"),
             fill("f3", "buy", 1, 200, "2026-09-25T11:00:00Z", "o3"),
             fill("f4", "sell", 1.5, 150, "2026-09-25T12:00:00Z", "o4")]
    trades = learn.closed_trades(fills, [], [], fee_pct=0)
    assert len(trades) == 1  # o3 is still half open
    assert trades[0].entry_order_id == "o1" and trades[0].qty == pytest.approx(1)
    assert trades[0].entry_price == pytest.approx(101)


def test_duplicate_fills_count_once_and_orphan_sells_are_skipped():
    f = fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1")
    fills = [fill("f0", "sell", 1, 90, "2026-09-25T09:00:00Z", "o0"), f, dict(f),
             fill("f2", "sell", 1, 110, "2026-09-25T16:00:00Z", "o2")]
    [t] = learn.closed_trades(fills, [], [], fee_pct=0)
    assert t.pnl_usd == pytest.approx(10)


def test_signals_and_exit_reason_come_from_the_journal():
    orders = [order("o1", "trd-20260925T1000Z-BTCUSD-buy"), order("o2", "trd-20260925T1600Z-BTCUSD-sell")]
    journal = [{"slot": "20260925T1000Z", "signals": {"BTC/USD": {"trend": 0.8, "rsi": -0.2, "label": "x"}}},
               {"slot": "20260925T1600Z", "exits": [{"client_order_id": "trd-20260925T1600Z-BTCUSD-sell",
                                                     "reason": "stop ATR"}]}]
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 1, 95, "2026-09-25T16:00:00Z", "o2")]
    [t] = learn.closed_trades(fills, orders, journal)
    assert t.signals == {"trend": 0.8, "rsi": -0.2}
    assert t.exit_reason == "stop ATR"
    assert t.entry_client_order_id == "trd-20260925T1000Z-BTCUSD-buy"


def test_attribution_separates_signals_that_led_to_winners_from_losers():
    trades = [learn.ClosedTrade("BTCUSD", f"o{i}", "", [], "", "", 1, 100, 100, 0, pnl, pnl, 1,
                                signals={"trend": s, "rsi": 0.5})
              for i, (s, pnl) in enumerate([(1, 2.0), (0.8, 1.0), (-0.5, -1.0), (-1, -3.0)])]
    a = learn.attribution(trades)
    assert a["trend"]["corr_with_pnl"] > 0.9
    assert a["trend"]["when_positive"]["win_rate_pct"] == 100
    assert a["trend"]["when_not_positive"]["avg_pnl_pct"] == -2.0
    assert a["rsi"]["corr_with_pnl"] is None  # constant signal: no correlation, no crash
    s = learn.summarize(trades)
    assert s["trades"] == 4 and s["win_rate_pct"] == 50


# ---- validation of the model's answer ----------------------------------------------

@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    json.dumps({"lessons": ["x"]}),
    json.dumps({"lessons": ["x"], "param_changes": [], "extra": 1}),
    json.dumps({"lessons": "x", "param_changes": []}),
    json.dumps({"lessons": [""], "param_changes": []}),
    json.dumps({"lessons": ["x"] * 6, "param_changes": []}),
    json.dumps({"lessons": [], "param_changes": [{"name": "tp_atr_mult", "new_value": 6.5}]}),
    json.dumps({"lessons": [], "param_changes": [{"name": "tp_atr_mult", "new_value": "6.5", "reason": "r"}]}),
    json.dumps({"lessons": [], "param_changes": [{"name": "tp_atr_mult", "new_value": True, "reason": "r"}]}),
    '{"lessons": [], "param_changes": [{"name": "tp_atr_mult", "new_value": NaN, "reason": "r"}]}',
    reflection(changes=[("tp_atr_mult", 6.5), ("tp_atr_mult", 5.5)]),
])
def test_invalid_model_output_is_rejected(raw):
    with pytest.raises(learn.InvalidReflection):
        learn.parse_reflection(raw)


def test_valid_output_parses_even_inside_a_code_fence():
    lessons, changes = learn.parse_reflection("```json\n" + reflection(changes=[("tp_atr_mult", 6.5)]) + "\n```")
    assert lessons == ["lezione di prova"] and changes[0]["name"] == "tp_atr_mult"


# ---- the acceptance rule -----------------------------------------------------------

def metrics(net, dd=1.0):
    return {"net_return_pct": net, "max_drawdown_pct": dd, "start": "a", "end": "b"}


def by(score, dd=lambda p: 1.0):
    """A judge from a score function, through the real acceptance rule (backtest.verdict)."""
    return lambda cur, cand: bt.verdict(metrics(score(cur), dd(cur)), metrics(score(cand), dd(cand)))


def flat(score=1.0):
    return by(lambda p: score)


def change(name, value):
    return {"name": name, "new_value": value, "reason": "r"}


def test_a_small_step_within_bounds_that_does_not_hurt_is_accepted():
    new, acc, rej = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5)], flat())
    assert [a["name"] for a in acc] == ["tp_atr_mult"] and rej == []
    assert new["tp_atr_mult"] == 6.5 and PARAMS["tp_atr_mult"] == 6.0  # the input is not mutated


def test_nested_weight_change_is_accepted():
    new, acc, _ = learn.decide_changes(PARAMS, [change("weights.rsi", 0.75)], flat())
    assert new["weights"]["rsi"] == 0.75 and acc[0]["old_value"] == 0.5


@pytest.mark.parametrize("name,value,why", [
    ("stop_atr_mult", 1.4, "fuori dai limiti"),       # below the minimum
    ("entry_threshold", 0.95, "fuori dai limiti"),
    ("min_edge_mult", 11.0, "non modificabile"),      # the fee margin is not learnable
    ("regime_filter", 0, "non modificabile"),         # nor is the market filter
    ("position_pct", 7.0, "non modificabile"),
    ("tp_atr_mult", 7.5, "oltre il massimo"),         # inside bounds, step 1.5 > 1.0
    ("weights.trend_4h", 1.5, "oltre il massimo"),
    ("shortlist_size", 9.5, "intero"),
    ("tp_atr_mult", 6.0, "nessun cambiamento"),
    ("tp_atr_mult", 2.0, "oltre il massimo"),
])
def test_out_of_bounds_and_too_large_steps_are_rejected(name, value, why):
    evaluated = []
    new, acc, rej = learn.decide_changes(PARAMS, [change(name, value)],
                                         lambda a, b: evaluated.append(b) or (True, "ok"))
    assert acc == [] and new == PARAMS and why in rej[0]["why"]
    assert evaluated == []  # rejected before any backtest


@pytest.mark.parametrize("name", ["max_position_pct", "max_invested_pct", "max_memecoin_pct", "daily_loss_limit_pct",
                                  "min_order_usd", "max_per_memecoin_pct", "max_open_positions", "kill_switch",
                                  "symbols", "fee_pct"])
def test_hard_limits_are_never_learnable(name):
    params = {**PARAMS, name: 1.0}
    new, acc, rej = learn.decide_changes(params, [change(name, 1.0 + 1e-3)], flat())
    assert acc == [] and new == params and rej[0]["why"].startswith("limite rigido")


def test_no_hard_limit_is_in_the_learnable_table():
    from trader import strategy as st
    from trader.config import HARD_CEILINGS, HARD_FLOORS
    assert not (set(HARD_CEILINGS) | set(HARD_FLOORS)) & set(st.PARAM_BOUNDS)
    assert not hasattr(learn, "PARAM_BOUNDS")  # one table, in strategy.py
    for name in st.LEARNABLE:
        lo, hi, step = st.PARAM_BOUNDS[name]
        assert lo < hi and 0 < step <= (hi - lo) / 2, name
        assert learn.get_param(PARAMS, name) is not None, name  # every learnable name exists in params.json


@pytest.mark.parametrize("name", ["leverage", "weights.secret_sauce", "entry_threshold.x", "", "weights.trend",
                                  "trail_atr_mult"])
def test_unknown_params_are_rejected(name):
    new, acc, rej = learn.decide_changes(PARAMS, [change(name, 1.0)], flat())
    assert acc == [] and new == PARAMS and rej[0]["why"] == "parametro sconosciuto"


def test_param_missing_from_current_params_is_rejected():
    params = {k: v for k, v in PARAMS.items() if k != "exit_threshold"}
    _, acc, rej = learn.decide_changes(params, [change("exit_threshold", -0.35)], flat())
    assert acc == [] and "assente" in rej[0]["why"]


def test_a_change_the_backtest_scores_worse_is_rejected():
    judge = by(lambda p: 1.0 if p["stop_atr_mult"] == 2.5 else 0.9)
    new, acc, rej = learn.decide_changes(PARAMS, [change("stop_atr_mult", 3.0)], judge)
    assert acc == [] and new == PARAMS
    assert "rendimento netto peggiore" in rej[0]["why"] and "+0.90%" in rej[0]["why"]


def test_a_better_return_with_a_much_deeper_drawdown_is_rejected():
    # The reflection's rule is backtest.compare: net return AND drawdown, not return alone.
    judge = by(lambda p: 1.0 if p["stop_atr_mult"] == 2.5 else 2.0,
               dd=lambda p: 1.0 if p["stop_atr_mult"] == 2.5 else 1.0 + bt.DRAWDOWN_TOLERANCE_PCT + 0.5)
    new, acc, rej = learn.decide_changes(PARAMS, [change("stop_atr_mult", 3.0)], judge)
    assert acc == [] and new == PARAMS and "drawdown peggiore" in rej[0]["why"]


def test_a_slightly_deeper_drawdown_within_the_tolerance_is_accepted():
    judge = by(lambda p: 1.0, dd=lambda p: 1.0 if p["stop_atr_mult"] == 2.5 else 1.5)
    _, acc, _ = learn.decide_changes(PARAMS, [change("stop_atr_mult", 3.0)], judge)
    assert [a["name"] for a in acc] == ["stop_atr_mult"] and "drawdown" in acc[0]["backtest"]


def test_greedy_acceptance_keeps_the_good_change_and_drops_the_bad_one():
    judge = by(lambda p: (p["tp_atr_mult"] - 6.0) - (p["stop_atr_mult"] - 2.5))  # tp helps, stop hurts
    new, acc, rej = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5), change("stop_atr_mult", 3.0)],
                                         judge)
    assert [a["name"] for a in acc] == ["tp_atr_mult"] and [r["name"] for r in rej] == ["stop_atr_mult"]
    assert new["tp_atr_mult"] == 6.5 and new["stop_atr_mult"] == 2.5


@pytest.mark.parametrize("judge", [None, lambda a, b: 1 / 0, lambda a, b: 1.0, lambda a, b: None,
                                   lambda a, b: ("yes", "x"), lambda a, b: (True,)])
def test_no_or_broken_judge_accepts_nothing(judge):
    new, acc, rej = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5)], judge)
    assert acc == [] and new == PARAMS and len(rej) == 1
    assert ("nessun backtest" if judge is None else "fallito") in rej[0]["why"]


def test_a_judge_that_crashes_on_the_candidate_rejects_it():
    def judge(cur, cand):
        raise RuntimeError("backtest crashed")
    _, acc, rej = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5)], judge)
    assert acc == [] and rej[0]["why"] == "backtest fallito"


def test_learn_asks_strategy_check_change_and_keeps_no_rule_of_its_own(monkeypatch):
    # One implementation of what may change: strategy.check_change. Whatever it says, learn obeys.
    from trader import strategy as st
    monkeypatch.setattr(st, "check_change", lambda cur, ch: [f"{next(iter(ch))}: vietato dal test"])
    _, acc, rej = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5)], flat())
    assert acc == [] and rej[0]["why"] == "vietato dal test"
    monkeypatch.setattr(st, "check_change", lambda cur, ch: [])
    _, acc, _ = learn.decide_changes(PARAMS, [change("tp_atr_mult", 6.5)], flat())
    assert [a["name"] for a in acc] == ["tp_atr_mult"]
    assert not any(hasattr(learn, n) for n in ("PARAM_BOUNDS", "LEARNABLE", "HARD_LIMIT_NAMES"))


def test_more_than_the_allowed_changes_per_reflection_are_rejected():
    names = ["entry_threshold", "stop_atr_mult", "tp_atr_mult", "exit_threshold"]
    values = [0.55, 3.0, 6.5, -0.35]
    _, acc, rej = learn.decide_changes(PARAMS, [change(n, v) for n, v in zip(names, values)], flat())
    assert len(acc) == learn.MAX_CHANGES and "oltre" in rej[0]["why"] and rej[0]["name"] == "exit_threshold"


# ---- the whole reflection ------------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    r = tmp_path / "repo"
    (r / "prompts").mkdir(parents=True)
    (r / "config").mkdir()
    shutil.copy(REPO / "prompts/reflect.md", r / "prompts/reflect.md")
    (r / "config/params.json").write_text(json.dumps(PARAMS))
    return r


def seed(records: Records):
    t0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    records.journal({"slot": "20260925T1000Z", "signals": {"BTC/USD": {"trend": 0.9}},
                     "proposal": {"market_view": "vista"}, "verdicts": []})
    records.evidence("order", order("o1", "trd-20260925T1000Z-BTCUSD-buy"), t0)
    records.evidence("fill", fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"), t0)
    records.evidence("order", order("o2", "trd-20260925T1600Z-BTCUSD-sell"), t0)
    records.evidence("fill", fill("f2", "sell", 1, 108, "2026-09-25T16:00:00Z", "o2"), t0)


def run(root, reply, judge=None, **kw):
    rec = Records(root, ["secret-value-123456"])
    seed(rec)
    model = reply if not isinstance(reply, str) else FakeModel([reply])
    return rec, model, learn.run_reflection(root=root, records=rec, model=model, judge=judge or flat(),
                                            now=NOW, slot=SLOT, **kw)


def test_reflection_writes_lessons_and_accepted_params(root):
    _, model, out = run(root, reflection(["prima lezione"], [("tp_atr_mult", 6.5), ("max_order_usd", 900)]))
    assert out["error"] == "" and out["params_written"]
    assert json.loads((root / "config/params.json").read_text())["tp_atr_mult"] == 6.5
    assert [r["name"] for r in out["rejected"]] == ["max_order_usd"]
    assert "tp_atr_mult 6.0 -> 6.5" in out["commit_reason"]
    lessons = (root / "lessons/lessons.md").read_text()
    assert lessons.startswith("# Lezioni") and "prima lezione" in lessons and SLOT in lessons
    assert out["summary"]["trades"] == 1 and out["attribution"]["trend"]["n"] == 1
    prompt = model.prompts[0]
    assert "{{" not in prompt and '"trend": 0.9' in prompt and "tp_atr_mult" in prompt
    json.dumps(out)  # the journal record serializes


def test_newest_lessons_go_on_top(root):
    run(root, reflection(["vecchia"]))
    rec = Records(root, [])
    learn.run_reflection(root=root, records=rec, model=FakeModel([reflection(["nuova"])]), judge=flat(),
                         now=NOW, slot="20260926T1800Z")
    text = (root / "lessons/lessons.md").read_text()
    assert text.index("nuova") < text.index("vecchia") and text.count("# Lezioni") == 1
    assert learn.recent_lessons(root, entries=1).count("nuova") == 1 and "vecchia" not in learn.recent_lessons(root, 1)


@pytest.mark.parametrize("reply", ["I'd rather not.", reflection(changes=[("tp_atr_mult", "big")]),
                                   FakeModel([RuntimeError("down")])])
def test_invalid_model_output_changes_nothing(root, reply):
    before = (root / "config/params.json").read_text()
    _, _, out = run(root, reply)
    assert out["error"] and out["lessons"] == [] and not out["params_written"]
    assert (root / "config/params.json").read_text() == before
    assert not (root / "lessons/lessons.md").exists()


def test_worse_backtest_keeps_params_but_keeps_the_lessons(root):
    before = (root / "config/params.json").read_text()
    _, _, out = run(root, reflection(["lezione"], [("stop_atr_mult", 3.0)]),
                    judge=by(lambda p: 1.0 if p["stop_atr_mult"] == 2.5 else 0.5))
    assert not out["params_written"] and (root / "config/params.json").read_text() == before
    assert out["rejected"][0]["name"] == "stop_atr_mult" and (root / "lessons/lessons.md").exists()


def test_missing_params_file_is_an_error_not_a_crash(root):
    (root / "config/params.json").unlink()
    _, _, out = run(root, reflection())
    assert "preparazione" in out["error"] and not out["params_written"]


def test_outcomes_can_be_rebuilt_from_alpaca_fills_without_evidence(root):
    rec = Records(root, [])
    out = learn.run_reflection(root=root, records=rec, model=FakeModel([reflection()]), judge=flat(), now=NOW,
                               slot=SLOT, fills=[fill("a", "buy", 2, 50, "2026-09-25T10:00:00Z", "x"),
                                                 fill("b", "sell", 2, 55, "2026-09-25T11:00:00Z", "y")], orders=[])
    assert out["summary"]["trades"] == 1 and out["summary"]["pnl_usd"] > 0


def test_claude_cli_is_asked_with_the_reflection_schema(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token-000000")
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        out = {"subtype": "success", "is_error": False,
               "structured_output": {"lessons": ["ok"], "param_changes": []}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
    raw = learn.ask_model(ClaudeCLI(runner=runner), "prompt")
    assert learn.parse_reflection(raw) == (["ok"], [])
    cmd = seen["cmd"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == learn.REFLECT_SCHEMA
    assert cmd[cmd.index("--system-prompt") + 1] == learn.REFLECT_SYSTEM


@pytest.mark.parametrize("stdout", ["not json", json.dumps({"subtype": "error_max_budget_usd", "is_error": True})])
def test_claude_cli_failures_raise_reflect_error(monkeypatch, stdout):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token-000000")
    cli = ClaudeCLI(runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout, ""))
    with pytest.raises(learn.ReflectError):
        learn.ask_model(cli, "prompt")


def test_claude_cli_timeout_raises_reflect_error(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token-000000")

    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    with pytest.raises(learn.ReflectError):
        learn.ask_model(ClaudeCLI(runner=runner), "prompt")


def test_selling_all_but_the_fee_kept_in_the_coin_closes_the_trade_and_counts_the_fee_once():
    # Alpaca keeps the buy fee in the coin: 1 bought, 0.9975 held and sold (paper fills, 2026-09-26).
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 0.9975, 110, "2026-09-25T16:00:00Z", "o2"),
             fill("f3", "buy", 1, 120, "2026-09-26T10:00:00Z", "o3")]
    trades = learn.closed_trades(fills, [], [], fee_pct=0.25)
    assert len(trades) == 1 and trades[0].exit_price == 110
    proceeds = 0.9975 * 110
    assert trades[0].pnl_usd == pytest.approx(proceeds * (1 - 0.0025) - 100)  # the cash that really came back


def test_a_real_partial_sell_does_not_close_the_trade():
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 0.5, 110, "2026-09-25T16:00:00Z", "o2")]
    assert learn.closed_trades(fills, [], [], fee_pct=0.25) == []


def test_unsupported_adapter_raises_reflect_error():
    with pytest.raises(learn.ReflectError):
        learn.ask_model(object(), "prompt")


def test_trade_outcomes_charge_the_same_fee_as_the_strategy_by_default():
    # One fee for the backtest, the entry rule and the outcomes: two constants would drift.
    from trader import strategy as st
    assert learn.FEE_PCT == st.TAKER_FEE_PCT > 0
    fills = [fill("f1", "buy", 1, 100, "2026-09-25T10:00:00Z", "o1"),
             fill("f2", "sell", 1, 110, "2026-09-25T16:00:00Z", "o2")]
    [t] = learn.closed_trades(fills, [], [])
    assert t.fees_usd == pytest.approx(st.TAKER_FEE_PCT / 100 * (100 + 110))
