"""End-to-end wake-ups against the fake Alpaca, with records in a temp dir."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import FAKE_ENV, NOW, SLOT, reply
from trader import backtest as bt
from trader.model import FakeModel
from trader.orders import client_order_id
from trader.records import Records
from trader.wake import summary_day, wake


def no_exploration(root):
    p = root / "config/limits.toml"
    p.write_text(p.read_text().replace("explore_total_pct = 5.0", "explore_total_pct = 0.0"))


def judge_by(score):
    """A judge from a score function, through the real acceptance rule (backtest.verdict)."""
    def metrics(p):
        return {"net_return_pct": score(p), "max_drawdown_pct": 1.0, "start": "a", "end": "b"}
    return lambda cur, cand: bt.verdict(metrics(cur), metrics(cand))


def handoff(root):
    return json.loads((root / "state/last_handoff.json").read_text())


def journal(root):
    return [json.loads(x) for x in (root / "journal/decisions.jsonl").read_text().splitlines()]


def positions_state(root):
    return json.loads((root / "state/positions.json").read_text())["positions"]


def trade_messages(outbox):
    return [m for m in outbox.sent if "Trader crypto" in m]


def later(minutes):
    return {"now": NOW + timedelta(minutes=minutes)}, SLOT + timedelta(minutes=minutes)


def hold_all(*symbols):
    return FakeModel([reply(*[(s, "hold", 0) for s in symbols or ("BTC/USD",)])])


def evidence(root, day="2026-09-26"):
    p = root / f"evidence/{day}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_normal_wake_places_one_order_and_writes_every_record(make_deps, fake, outbox, root):
    assert wake(make_deps(), SLOT) == 0
    assert fake.posts() == 1
    h = handoff(root)
    assert h["status"] == "ok" and h["notified"] is True
    assert "trd-20260926T0800Z-BTCUSD-buy" in (root / "state/progress.md").read_text()
    rows = evidence(root)
    order = next(r for r in rows if r["kind"] == "order")
    assert order["alpaca"]["client_order_id"] == "trd-20260926T0800Z-BTCUSD-buy" and order["fetched_at"]
    j = journal(root)[-1]
    assert j["proposal"]["decisions"][0]["symbol"] == "BTC/USD"
    assert j["verdicts"][0]["approved"] and j["verdicts"][0]["outcome"] == "placed"
    assert set(j["candidates"]) == {"BTC/USD", "SOL/USD", "DOGE/USD"} and "trend_4h" in j["signals"]["BTC/USD"]
    assert j["jev"]["regime"] == "neutral" and j["jev"]["multiplier"] == 0.7  # no keys: neutral, logged
    lv = positions_state(root)["BTC/USD"]
    assert lv["stop"] < lv["entry_price"] < lv["take_profit"] and lv["client_order_id"].endswith("BTCUSD-buy")
    trade = trade_messages(outbox)
    assert len(trade) == 1 and "BTC/USD" in trade[0] and "stop" in trade[0] and "actions/runs/1" in trade[0]


def test_invalid_model_output_holds_everything_and_says_so(make_deps, fake, outbox, root):
    assert wake(make_deps(model=FakeModel(["{not json"])), SLOT) == 0
    assert fake.posts() == 0
    h = handoff(root)
    assert h["status"] == "no_trade" and "JSON" in h["model_error"]
    assert h["decisions"] == []
    assert "nessun acquisto" in trade_messages(outbox)[0] and "JSON" in trade_messages(outbox)[0]


def test_model_refusal_holds(make_deps, fake, root):
    wake(make_deps(model=FakeModel(["I won't help with trading."])), SLOT)
    assert fake.posts() == 0 and handoff(root)["model_error"]


def test_kill_file_stops_every_order(make_deps, fake, root, outbox):
    (root / "KILL").touch()
    wake(make_deps(), SLOT)
    assert fake.posts() == 0 and handoff(root)["status"] == "killed"
    assert trade_messages(outbox) == []  # nothing happened: quiet
    assert "Kill switch" in next(m for m in outbox.sent if "Stato" in m)


def test_kill_variable_stops_every_order(make_deps, fake, root):
    wake(make_deps(env={"TRADING_ENABLED": "false"}), SLOT)
    assert fake.posts() == 0 and handoff(root)["status"] == "killed"


def test_interrupted_run_is_recovered_on_next_wake(make_deps, fake, root, outbox):
    # A run of the previous slot sent an order and died before writing anything.
    prev = SLOT - timedelta(minutes=15)
    fake.add_order(client_order_id(prev, "ETH/USD", "buy"), "ETH/USD", "buy", notional=40)
    wake(make_deps(model=hold_all()), SLOT)
    rows = [r for r in evidence(root) if r["kind"] == "order"]
    assert [r["alpaca"]["client_order_id"] for r in rows] == ["trd-20260926T0745Z-ETHUSD-buy"]
    assert "recuperato" in rows[0]["note"]
    assert journal(root)[-1]["recovered_orders"] == ["trd-20260926T0745Z-ETHUSD-buy"]
    assert "recuperati 1" in trade_messages(outbox)[0]
    # and it is not recovered twice
    kw, nxt = later(15)
    wake(make_deps(model=hold_all(), **kw), nxt)
    assert len([r for r in evidence(root) if r["kind"] == "order"]) == 1


def test_two_wakes_in_the_same_slot_place_one_order(make_deps, fake, root):
    wake(make_deps(model=FakeModel([reply(("BTC/USD", "buy", 50))])), SLOT)
    # The second run wants something else entirely; the slot is already taken.
    wake(make_deps(model=FakeModel([reply(("ETH/USD", "buy", 50), ("SOL/USD", "buy", 50))])), SLOT)
    assert fake.posts() == 1 and len(fake.orders) == 1
    assert handoff(root)["status"] == "already_done"


def test_overlapping_runs_that_do_not_share_records_still_place_one_order(make_deps, fake, tmp_path):
    # Two runners, each with its own checkout (the first one has not pushed yet).
    a, b = tmp_path / "a", tmp_path / "b"
    wake(make_deps(records_dir=a), SLOT)
    wake(make_deps(records_dir=b), SLOT)
    assert fake.posts() == 1
    assert json.loads((b / "state/last_handoff.json").read_text())["status"] == "already_done"


def test_missed_slot_is_recorded_and_not_caught_up(make_deps, fake, root, outbox):
    wake(make_deps(model=hold_all(), now=NOW - timedelta(hours=1)), SLOT - timedelta(hours=1))
    wake(make_deps(model=FakeModel([reply(("BTC/USD", "buy", 50), ("SOL/USD", "buy", 50),
                                         ("DOGE/USD", "buy", 50))])), SLOT)
    h = handoff(root)
    assert h["missed_slots"] == 3
    assert journal(root)[-1]["missed_slots"] == 3
    assert fake.posts() <= 3  # this wake's own limit, nothing extra for the missed ones
    assert "saltati 3 slot" in " ".join(h["warnings"])


def test_alpaca_down_means_no_order_a_failure_notice_and_a_handoff(make_deps, fake, root, outbox):
    fake.down = True
    code = wake(make_deps(), SLOT)
    assert code == 1
    assert fake.orders == []
    h = handoff(root)
    assert h["status"] == "failed" and "Alpaca non raggiungibile" in h["error"]
    assert "Errore" in outbox.sent[0] and "Alpaca" in outbox.sent[0] and len(outbox.sent) == 1
    assert journal(root)[-1]["status"] == "failed"


def test_notify_failure_does_not_lose_the_handoff(make_deps, fake, root, outbox):
    outbox.fail = True
    assert wake(make_deps(), SLOT) == 0
    h = handoff(root)
    assert h["status"] == "ok" and h["notified"] is False
    assert "## 2026-09-26T08:00:00+00:00" in (root / "state/progress.md").read_text()


def test_unexpected_crash_still_writes_handoff_and_notifies(make_deps, root, outbox):
    deps = make_deps()

    def broken():
        raise RuntimeError("something odd")
    deps.build = broken
    assert wake(deps, SLOT) == 1
    assert handoff(root)["status"] == "failed" and outbox.sent


def test_records_and_messages_never_contain_secrets(make_deps, fake, root, outbox):
    leaky = FakeModel([reply(("BTC/USD", "buy", 50),
                             market_view="chiave " + FAKE_ENV["ALPACA_SECRET_KEY"],
                             next_job="token " + FAKE_ENV["TELEGRAM_BOT_TOKEN"])])
    wake(make_deps(model=leaky), SLOT)
    fake.down = True  # and a failure path too
    kw, nxt = later(15)
    wake(make_deps(**kw), nxt)
    written = "".join(p.read_text() for p in root.rglob("*") if p.is_file()
                      and p.parent.name in ("state", "journal", "evidence"))
    assert written  # the check below must have something to look at
    for secret in FAKE_ENV.values():
        assert secret not in written, secret
        assert all(secret not in m for m in outbox.sent)
    assert "[redatto]" in written


LAST = datetime(2026, 9, 26, 23, 45, tzinfo=UTC)


def at(slot):
    return {"now": slot + timedelta(minutes=8)}


def summaries(outbox):
    return [m for m in outbox.sent if "Riepilogo" in m]


def test_daily_summary_is_sent_exactly_once(make_deps, fake, root, outbox):
    wake(make_deps(**at(LAST)), LAST)
    assert len(summaries(outbox)) == 1 and "2026-09-26" in summaries(outbox)[0]
    # a re-run of the same slot and the next day's first slots do not resend it
    wake(make_deps(now=LAST + timedelta(minutes=12)), LAST)
    wake(make_deps(**at(LAST + timedelta(minutes=15))), LAST + timedelta(minutes=15))
    assert len(summaries(outbox)) == 1
    assert json.loads((root / "state/daily_summary.json").read_text())["last_sent_date"] == "2026-09-26"


def test_daily_summary_is_caught_up_when_the_last_slot_was_missed(make_deps, root, outbox):
    wake(make_deps(model=hold_all()), SLOT)  # 08:00, no summary yet
    assert not summaries(outbox)
    nxt = datetime(2026, 9, 27, 0, 30, tzinfo=UTC)  # 23:45 and more were missed
    wake(make_deps(model=hold_all(), **at(nxt)), nxt)
    assert len(summaries(outbox)) == 1


def test_daily_summary_failure_is_retried_later(make_deps, root, outbox):
    outbox.fail = True
    wake(make_deps(**at(LAST)), LAST)
    assert Records(root, []).summary_sent_for() == ""
    outbox.fail = False
    nxt = LAST + timedelta(minutes=15)
    wake(make_deps(**at(nxt)), nxt)
    assert len(summaries(outbox)) == 1


def test_daily_summary_reports_pnl_trades_and_win_rate(make_deps, fake, root, outbox):
    wake(make_deps(**at(LAST - timedelta(hours=2))), LAST - timedelta(hours=2))  # buys BTC
    fake.prices["BTC/USD"] *= 1.2  # far above the take-profit
    wake(make_deps(model=hold_all(), **at(LAST)), LAST)
    text = summaries(outbox)[0]
    assert "Operazioni chiuse: 1" in text and "vinte 1" in text and "Migliore BTCUSD" in text
    assert "dall'inizio" in text


def test_summary_day_rule():
    assert summary_day(LAST) == "2026-09-26"
    assert summary_day(datetime(2026, 9, 27, 0, tzinfo=UTC)) == "2026-09-26"
    assert summary_day(datetime(2026, 9, 26, 23, 30, tzinfo=UTC)) == "2026-09-25"


def test_daily_loss_blocks_buys_end_to_end(make_deps, fake, root):
    fake.last_equity, fake.equity_override = 10000, 9490
    wake(make_deps(), SLOT)
    assert fake.posts() == 0
    assert "perdita giornaliera" in handoff(root)["decisions"][0]["reason"]


def test_prompt_carries_the_previous_handoff(make_deps, root):
    wake(make_deps(model=FakeModel([reply(("BTC/USD", "hold", 0), next_job="guardare ETH per primo")])), SLOT)
    m = hold_all()
    kw, nxt = later(15)
    wake(make_deps(model=m, **kw), nxt)
    assert "guardare ETH per primo" in m.prompts[0] and "{{" not in m.prompts[0]


def test_model_adapter_that_raises_holds(make_deps, fake, root):
    class Boom:
        name = "boom"

        def propose(self, prompt):
            raise RuntimeError("adapter bug")
    assert wake(make_deps(model=Boom()), SLOT) == 0
    h = handoff(root)
    assert fake.posts() == 0 and h["status"] == "no_trade" and "errore del modello" in h["model_error"]


def test_rerun_of_a_slot_that_traded_nothing_stays_read_only(make_deps, fake, root):
    wake(make_deps(model=hold_all()), SLOT)
    m = FakeModel([reply(("BTC/USD", "buy", 50))])
    wake(make_deps(model=m), SLOT)
    assert fake.posts() == 0 and m.prompts == [] and handoff(root)["status"] == "already_done"


def test_dry_run_does_not_mark_the_slot_done(make_deps, fake, root):
    wake(make_deps(), SLOT, dry_run=True)
    assert fake.posts() == 0
    wake(make_deps(), SLOT)
    assert fake.posts() == 1 and handoff(root)["status"] == "ok"


def test_slot_older_than_the_records_is_read_only(make_deps, fake, root):
    wake(make_deps(model=hold_all()), SLOT)
    m = FakeModel([reply(("BTC/USD", "buy", 50))])
    wake(make_deps(model=m), SLOT - timedelta(minutes=15))
    assert fake.posts() == 0 and m.prompts == [] and handoff(root)["status"] == "stale_slot"


def test_open_order_on_a_symbol_blocks_a_new_one_end_to_end(make_deps, fake, root):
    fake.fill_orders = False
    fake.add_order("manual-order-1", "BTC/USD", "buy", notional=20)  # someone else's, still open
    m = FakeModel([reply(("BTC/USD", "buy", 50))])
    wake(make_deps(model=m), SLOT)
    assert fake.posts() == 0
    assert "BTC/USD" not in journal(root)[-1]["candidates"]  # not a candidate with an order open
    assert "ordine aperto" in handoff(root)["decisions"][0]["reason"]


def test_fills_are_recorded_once(make_deps, fake, root):
    fake.add_order("manual-order-1", "BTC/USD", "buy", notional=20)  # fills immediately
    wake(make_deps(model=hold_all()), SLOT)
    kw, nxt = later(15)
    wake(make_deps(model=hold_all(), **kw), nxt)
    assert len([r for r in evidence(root) if r["kind"] == "fill"]) == 1


def test_records_write_failure_still_notifies(make_deps, outbox):
    deps = make_deps()

    def broken(*a, **k):
        raise OSError("disk full")
    deps.records.journal = broken
    deps.records.write_handoff = broken
    wake(deps, SLOT)
    assert len(outbox.sent) >= 1


def test_message_build_failure_falls_back_to_a_minimal_notice(make_deps, outbox, monkeypatch):
    from trader import notify

    def broken(h):
        raise KeyError("x")
    monkeypatch.setattr(notify, "wake_message", broken)
    wake(make_deps(), SLOT)
    assert outbox.sent and "errore" in outbox.sent[0] and "20260926T0800Z" in outbox.sent[0]


def test_dry_run_never_sends_the_daily_summary_or_the_status(make_deps, outbox):
    wake(make_deps(**at(LAST - timedelta(hours=1))), LAST - timedelta(hours=1))  # a real wake earlier that day
    outbox.sent.clear()
    wake(make_deps(**at(LAST)), LAST, dry_run=True)
    assert not summaries(outbox) and not [m for m in outbox.sent if "Stato" in m]
    assert len(outbox.sent) == 1 and outbox.sent[0].startswith("🧪")


def traced(make_deps, **kw):
    import io

    from trader.trace import Trace
    deps = make_deps(**kw)
    deps.trace = Trace(io.StringIO())
    return deps, deps.trace.stream


def test_trace_prints_every_step_in_order_in_italian(make_deps, fake, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    deps, out = traced(make_deps, model=FakeModel([reply(("BTC/USD", "buy", 50), ("SOL/USD", "buy", 5000),
                                                         ("DOGE/USD", "hold", 0))]))
    wake(deps, SLOT)
    text = out.getvalue()
    labels = [line.split()[1] for line in text.splitlines() if line.startswith("▸")]
    assert labels == ["slot", "contesto", "riconcilia", "universo", "mercato", "posizioni", "punteggi", "regime",
                      "candidato", "candidato", "candidato", "modello", "proposta", "proposta", "proposta",
                      "gate", "ordine", "gate", "gate", "esito", "record", "telegram", "telegram"]
    assert "pro: a favore di SOL/USD · contro: contro SOL/USD" in text
    assert "SOL/USD compra 5.000,00 $ → respinto:" in text
    assert "inviato · trd-20260926T0800Z-BTCUSD-buy" in text
    assert "journal/decisions.jsonl" in text and "state/positions.json" in text
    assert "\033[" not in text  # not a terminal: no colours
    assert max(len(line) for line in text.splitlines()) <= 100


def test_trace_is_coloured_only_on_a_terminal(make_deps, monkeypatch):
    import io

    from trader.trace import Trace
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    class Tty(io.StringIO):
        def isatty(self):
            return True
    deps = make_deps()
    deps.trace = Trace(Tty())
    wake(deps, SLOT)
    assert "\033[32m" in deps.trace.stream.getvalue()


def test_dry_run_trace_and_telegram_say_prova(make_deps, fake, outbox):
    deps, out = traced(make_deps)
    wake(deps, SLOT, dry_run=True)
    assert fake.posts() == 0
    assert "PROVA" in out.getvalue().splitlines()[0]
    assert "prova, non inviato · trd-20260926T0800Z-BTCUSD-buy" in out.getvalue()
    assert "messaggio inviato (PROVA)" in out.getvalue()
    assert len(outbox.sent) == 1 and outbox.sent[0].startswith("🧪 <b>PROVA</b>")
    assert "BTC/USD" not in positions_state(deps.records.root)  # nothing bought, no exit levels


def test_a_real_wake_message_is_not_marked_prova(make_deps, outbox):
    wake(make_deps(), SLOT)
    assert outbox.sent and not [m for m in outbox.sent if "PROVA" in m]


def test_bull_and_bear_cases_reach_journal_and_telegram(make_deps, root, outbox):
    wake(make_deps(), SLOT)
    d = journal(root)[-1]["proposal"]["decisions"][0]
    assert (d["bull_case"], d["bear_case"]) == ("a favore di BTC/USD", "contro BTC/USD")
    assert "📈 a favore di BTC/USD · 📉 contro BTC/USD" in trade_messages(outbox)[0]


def test_trace_never_contains_secrets(make_deps):
    leaky = FakeModel([reply(("BTC/USD", "buy", 50), market_view="chiave " + FAKE_ENV["ALPACA_SECRET_KEY"])])
    deps, out = traced(make_deps, model=leaky)
    wake(deps, SLOT)
    assert FAKE_ENV["ALPACA_SECRET_KEY"] not in out.getvalue() and "[redatto]" in out.getvalue()


# ---- v2: quiet wakes, code exits, strategy candidates, regime, reflection, cadence -------------

def test_quiet_wake_sends_nothing_and_does_not_ask_the_model(make_deps, fake, root, outbox):
    from tests.conftest import write_params
    write_params(root, entry_threshold=0.9)  # nothing passes: no candidates
    no_exploration(root)
    (root / "state").mkdir(exist_ok=True)
    (root / "state/cadence.json").write_text(json.dumps({"status": "20260926T0600Z", "reflection": "20260926T0600Z"}))
    m = FakeModel([reply(("BTC/USD", "buy", 50))])
    assert wake(make_deps(model=m), SLOT) == 0
    assert m.prompts == [] and fake.posts() == 0 and outbox.sent == []
    h = handoff(root)
    assert h["status"] == "no_trade" and h["notified"] is True and "tranquillo" in h["notify_detail"]
    assert "jev" not in journal(root)[-1]  # no candidate, no regime call


def buy_then(make_deps, fake, root, price_factor=None, minutes=15, model=None):
    """Buy BTC in SLOT, move the price, run the next wake. Returns the next slot."""
    wake(make_deps(), SLOT)
    assert fake.posts() == 1
    if price_factor:
        fake.prices["BTC/USD"] *= price_factor
    kw, nxt = later(minutes)
    wake(make_deps(model=model or hold_all("SOL/USD"), **kw), nxt)
    return nxt


def test_stop_hit_is_sold_by_code_whatever_the_model_says(make_deps, fake, root, outbox):
    model = FakeModel([reply(("BTC/USD", "hold", 0))])  # the model wants to keep it
    buy_then(make_deps, fake, root, price_factor=0.9, model=model)
    sells = [o for o in fake.orders if o["side"] == "sell"]
    assert [o["client_order_id"] for o in sells] == ["trd-20260926T0815Z-BTCUSD-sell"]
    assert fake.positions.get("BTCUSD", 0) <= 1e-8  # the whole position, at most 1e-9 of dust left
    j = journal(root)[-1]
    assert j["exits"] == [{"client_order_id": "trd-20260926T0815Z-BTCUSD-sell", "symbol": "BTC/USD", "reason": "stop"}]
    msg = trade_messages(outbox)[-1]
    assert "BTC/USD" in msg and "stop" in msg and "vendi" in msg


def test_stop_is_enforced_even_when_the_model_output_is_unusable(make_deps, fake, root):
    buy_then(make_deps, fake, root, price_factor=0.9, model=FakeModel(["{broken"]))
    assert [o["side"] for o in fake.orders] == ["buy", "sell"]


def test_take_profit_is_sold_by_code(make_deps, fake, root):
    buy_then(make_deps, fake, root, price_factor=1.2)
    assert journal(root)[-1]["exits"][0]["reason"] == "take-profit"


def test_no_exit_while_the_price_sits_between_stop_and_take_profit(make_deps, fake, root):
    buy_then(make_deps, fake, root, price_factor=1.01)
    assert [o["side"] for o in fake.orders] == ["buy"]
    assert "BTC/USD" in positions_state(root)


def test_kill_switch_blocks_the_code_exits_too_and_says_so(make_deps, fake, root, outbox):
    wake(make_deps(), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    (root / "KILL").touch()
    kw, nxt = later(15)
    wake(make_deps(**kw), nxt)
    assert [o["side"] for o in fake.orders] == ["buy"]
    assert "uscita stop su BTC/USD respinta" in trade_messages(outbox)[-1]


def test_the_trailing_stop_only_moves_up_across_wakes(make_deps, fake, root):
    wake(make_deps(), SLOT)
    first = positions_state(root)["BTC/USD"]["stop"]
    fake.prices["BTC/USD"] *= 1.02
    kw, nxt = later(15)
    wake(make_deps(model=hold_all("SOL/USD"), **kw), nxt)
    raised = positions_state(root)["BTC/USD"]["stop"]
    fake.prices["BTC/USD"] /= 1.02 * 1.01
    kw, nxt = later(30)
    wake(make_deps(model=hold_all("SOL/USD"), **kw), nxt)
    assert first < raised <= positions_state(root)["BTC/USD"]["stop"]  # the price fell back: the stop did not


def test_exit_levels_are_rebuilt_from_alpaca_fills_when_the_records_are_lost(make_deps, fake, root, outbox):
    wake(make_deps(), SLOT)
    stop = positions_state(root)["BTC/USD"]["stop"]
    (root / "state/positions.json").unlink()
    kw, nxt = later(15)
    wake(make_deps(model=hold_all("SOL/USD"), **kw), nxt)
    rebuilt = positions_state(root)["BTC/USD"]
    assert rebuilt["stop"] == pytest.approx(stop, rel=0.02)
    assert any("ricostruiti (fill)" in m for m in outbox.sent)


def test_a_buy_the_strategy_did_not_select_is_rejected(make_deps, fake, root):
    wake(make_deps(model=FakeModel([reply(("XRP/USD", "buy", 50), ("ETH/USD", "buy", 50))])), SLOT)
    assert fake.posts() == 0
    reasons = [d["reason"] for d in handoff(root)["decisions"]]
    assert all("candidato" in r for r in reasons)


def test_memecoin_is_sized_to_its_cap_and_a_bigger_buy_is_rejected(make_deps, fake, root):
    wake(make_deps(model=FakeModel([reply(("DOGE/USD", "buy", 301))])), SLOT)
    assert journal(root)[-1]["candidates"]["DOGE/USD"] == 300.0  # 3% of 10.000 $
    assert fake.posts() == 0 and "dimensione della strategia" in handoff(root)["decisions"][0]["reason"]


def test_risk_off_regime_shrinks_the_size_and_is_journaled(make_deps, fake, root):
    from trader.jev import Regime
    off = Regime("risk_off", 0.4, "jev", "jev-test", "risk_off", 0.9, 5, "")
    wake(make_deps(model=FakeModel([reply(("BTC/USD", "buy", 400))]), classify=lambda state: off), SLOT)
    j = journal(root)[-1]
    assert j["jev"]["regime"] == "risk_off" and j["candidates"]["BTC/USD"] == 320.0  # 8% x 0.4
    assert fake.posts() == 0 and "dimensione della strategia" in j["verdicts"][0]["reason"]


def test_jev_bug_is_reported_and_sizes_as_neutral(make_deps, fake, root, outbox):
    from trader.jev import Regime
    bug = Regime("neutral", 0.7, "jev", None, None, None, 5, "bug nella richiesta a Jev: HTTP 422")
    wake(make_deps(classify=lambda state: bug), SLOT)
    assert fake.posts() == 1 and "HTTP 422" in trade_messages(outbox)[0]


def test_no_new_buy_during_the_cooldown_after_a_sale(make_deps, fake, root):
    buy_then(make_deps, fake, root, price_factor=0.9)  # stopped out at 08:15
    kw, nxt = later(30)
    wake(make_deps(**kw), nxt)
    assert "BTC/USD" not in journal(root)[-1]["candidates"]


def test_symbols_alpaca_does_not_list_as_tradable_are_left_out(make_deps, fake, root):
    fake.untradable = {"BTC/USD"}
    wake(make_deps(), SLOT)
    j = journal(root)[-1]
    assert "BTC/USD" not in j["candidates"] and fake.posts() == 0


def test_status_message_goes_out_once_per_six_hours(make_deps, fake, root, outbox):
    wake(make_deps(model=hold_all()), SLOT)
    kw, nxt = later(15)
    wake(make_deps(model=hold_all(), **kw), nxt)
    assert len([m for m in outbox.sent if "Stato" in m]) == 1
    noon = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    wake(make_deps(model=hold_all(), now=noon + timedelta(minutes=9)), noon)
    assert len([m for m in outbox.sent if "Stato" in m]) == 2


def reflection_reply(changes):
    return json.dumps({"lessons": ["lezione di prova"], "param_changes": [
        {"name": n, "new_value": v, "reason": "motivo"} for n, v in changes]})


def test_reflection_runs_once_per_six_hours_and_rejects_a_bad_change(make_deps, fake, root, outbox):
    before = (root / "config/params.json").read_text()
    replies = [reply(("BTC/USD", "hold", 0)), reflection_reply([("stop_atr_mult", 3.5)])]
    judge = judge_by(lambda p: 1.0 if p["stop_atr_mult"] == 4.0 else 0.5)
    deps = make_deps(model=FakeModel(replies), reflect=None, judge=lambda *a: judge)
    wake(deps, SLOT)
    rows = [r for r in journal(root) if r.get("kind") == "reflection"]
    assert len(rows) == 1 and rows[0]["rejected"][0]["name"] == "stop_atr_mult"
    assert "peggiore" in rows[0]["rejected"][0]["why"]
    assert (root / "config/params.json").read_text() == before
    assert "lezione di prova" in (root / "lessons/lessons.md").read_text()
    msg = next(m for m in outbox.sent if "Riflessione" in m)
    assert "stop_atr_mult" in msg and "lezione di prova" in msg
    kw, nxt = later(15)
    wake(make_deps(model=FakeModel(replies), reflect=None, judge=lambda *a: judge, **kw), nxt)
    assert len([r for r in journal(root) if r.get("kind") == "reflection"]) == 1  # same 6-hour bucket


def test_reflection_writes_an_accepted_change_and_says_why_in_the_commit(make_deps, fake, root):
    replies = [reply(("BTC/USD", "hold", 0)), reflection_reply([("tp_atr_mult", 6.5), ("max_position_pct", 20)])]
    wake(make_deps(model=FakeModel(replies), reflect=True, judge=lambda *a: judge_by(lambda p: 1.0)), SLOT)
    assert json.loads((root / "config/params.json").read_text())["tp_atr_mult"] == 6.5
    r = next(r for r in journal(root) if r.get("kind") == "reflection")
    assert r["rejected"][0]["why"].startswith("limite rigido")
    assert "tp_atr_mult 6.0 -> 6.5" in handoff(root)["commit_message"]


def test_reflection_without_a_backtest_accepts_nothing(make_deps, fake, root):
    def broken(*a):
        raise RuntimeError("no bars")
    replies = [reply(("BTC/USD", "hold", 0)), reflection_reply([("tp_atr_mult", 6.5)])]
    wake(make_deps(model=FakeModel(replies), reflect=True, judge=broken), SLOT)
    r = next(r for r in journal(root) if r.get("kind") == "reflection")
    assert r["accepted"] == [] and "backtest non disponibile" in r["evaluator_error"]


def test_the_default_judge_is_backtest_compare_on_fresh_bars(make_deps, fake, root):
    from trader.broker import Alpaca
    from trader.wake import _backtest_judge
    deps = make_deps()
    client = Alpaca("k", "s", transport=fake, sleep=lambda s: None)
    judge = _backtest_judge(client, list(fake.prices), {}, deps, NOW)
    params = json.loads((root / "config/params.json").read_text())
    ok, detail = judge(params, params)
    assert ok is True and "drawdown" in detail


def test_orders_that_are_not_the_agents_do_not_count_as_trades(make_deps, fake, root):
    fake.add_order("verify-1-buy", "BTC/USD", "buy", notional=20)  # a manual check on the same account
    fake.add_order("verify-1-sell", "BTC/USD", "sell", qty=fake.positions["BTCUSD"])
    replies = [reply(("BTC/USD", "hold", 0)), reflection_reply([])]
    wake(make_deps(model=FakeModel(replies), reflect=True, judge=lambda *a: judge_by(lambda p: 1.0)), SLOT)
    r = next(r for r in journal(root) if r.get("kind") == "reflection")
    assert r["summary"] == {"trades": 0}


def test_without_bars_the_stop_is_still_enforced(make_deps, fake, root, outbox):
    wake(make_deps(), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    real = fake._data
    fake._data = lambda path, q: (503, "") if path == "/bars" else real(path, q)
    kw, nxt = later(15)
    assert wake(make_deps(**kw), nxt) == 0
    assert [o["side"] for o in fake.orders] == ["buy", "sell"]
    assert "barre non disponibili" in trade_messages(outbox)[-1]


def test_kill_switch_asks_neither_the_model_nor_jev(make_deps, fake, root):
    asked = []
    m = FakeModel([reply(("BTC/USD", "buy", 50))])
    wake(make_deps(model=m, env={"TRADING_ENABLED": "false"}, classify=lambda s: asked.append(s)), SLOT)
    j = journal(root)[-1]
    assert m.prompts == [] and asked == [] and "BTC/USD" in j["blocked_candidates"]


def test_code_exits_go_before_the_models_buys_when_orders_are_scarce(make_deps, fake, root):
    # One order per wake: the stop must take it, not a buy the model asked for.
    lim = root / "config/limits.toml"
    lim.write_text(lim.read_text().replace("max_orders_per_wake = 3", "max_orders_per_wake = 1"))
    wake(make_deps(), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    kw, nxt = later(15)
    wake(make_deps(model=FakeModel([reply(("ETH/USD", "buy", 50))]), **kw), nxt)
    j = journal(root)[-1]
    assert "ETH/USD" in j["candidates"]  # the buy was allowed, only the order count stops it
    assert [(o["symbol"], o["side"]) for o in fake.orders] == [("BTC/USD", "buy"), ("BTC/USD", "sell")]


def test_the_model_cannot_add_its_own_order_on_a_symbol_the_code_is_exiting(make_deps, fake, root):
    model = FakeModel([reply(("BTC/USD", "sell", 10))])  # a partial sell of its own
    buy_then(make_deps, fake, root, price_factor=0.9, model=model)
    rows = [v for v in journal(root)[-1]["verdicts"] if v["symbol"] == "BTC/USD"]
    assert len(rows) == 1 and rows[0]["auto"] and rows[0]["outcome"] == "placed"


def test_a_coin_already_held_is_not_a_buy_candidate(make_deps, fake, root):
    wake(make_deps(), SLOT)
    kw, nxt = later(15)
    wake(make_deps(model=FakeModel([reply(("BTC/USD", "buy", 50))]), always_ask=True, **kw), nxt)
    j = journal(root)[-1]
    assert "BTC/USD" not in j["candidates"]
    assert [o["side"] for o in fake.orders] == ["buy"]
    assert "non è un candidato" in next(v["reason"] for v in j["verdicts"] if v["symbol"] == "BTC/USD")


def test_without_quotes_the_stop_is_still_enforced(make_deps, fake, root, outbox):
    wake(make_deps(), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    real = fake._data
    fake._data = lambda path, q: (503, "") if path == "/latest/quotes" else real(path, q)
    kw, nxt = later(15)
    assert wake(make_deps(**kw), nxt) == 0
    assert [o["side"] for o in fake.orders] == ["buy", "sell"]
    assert "prezzi non disponibili" in trade_messages(outbox)[-1]


# ---- exploration --------------------------------------------------------------------------
# With entry_threshold 0.9 nothing passes the strict rule on the fake market; BTC, SOL and DOGE
# are on the shortlist with a positive 4h trend, so they are exploration candidates. Equity is
# 10000$: 1% is 100$, times 0.7 for Jev's neutral regime = 70$.

def explore_deps(make_deps, root, model, **kw):
    from tests.conftest import write_params
    write_params(root, entry_threshold=0.9)
    return make_deps(model=model, **kw)


def test_an_exploration_buy_is_asked_placed_and_labelled_everywhere(make_deps, fake, root, outbox):
    model = FakeModel([reply(("BTC/USD", "buy", 70))])
    assert wake(explore_deps(make_deps, root, model), SLOT) == 0
    assert "Exploration candidates" in model.prompts[0] and "esplorazione, massimo 70.00$" in model.prompts[0]
    assert fake.posts() == 1
    j = journal(root)[-1]
    assert j["candidates"] == {} and j["explore_candidates"]["BTC/USD"] == 70.0
    v = j["verdicts"][0]
    assert v["approved"] and v["explore"] is True and v["outcome"] == "placed"
    assert positions_state(root)["BTC/USD"]["explore"] is True
    assert "esplorazione" in trade_messages(outbox)[0]


def test_an_exploration_buy_over_its_size_is_rejected_end_to_end(make_deps, fake, root):
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 100))])), SLOT)
    v = journal(root)[-1]["verdicts"][0]
    assert fake.posts() == 0 and not v["approved"] and v["explore"] and "dimensione ammessa" in v["reason"]


def test_an_exploration_position_obeys_the_code_stop(make_deps, fake, root):
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 70))])), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    kw, nxt = later(15)
    wake(explore_deps(make_deps, root, hold_all("BTC/USD"), **kw), nxt)
    assert [o["side"] for o in fake.orders] == ["buy", "sell"]
    assert journal(root)[-1]["exits"][0]["reason"] == "stop"


def test_held_exploration_counts_against_the_total_so_a_full_budget_asks_nobody(make_deps, fake, root):
    p = root / "config/limits.toml"
    p.write_text(p.read_text().replace("explore_total_pct = 5.0", "explore_total_pct = 1.0"))
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 70))])), SLOT)
    model = FakeModel([reply(*[(s, "buy", 30) for s in ("SOL/USD", "ETH/USD", "DOGE/USD")])])
    kw, nxt = later(15)
    wake(explore_deps(make_deps, root, model, **kw), nxt)
    # about 70$ of 100$ used: about 30$ left for one more, and the gate knows BTC is exploration
    j = journal(root)[-1]
    (usd,) = j["explore_candidates"].values()
    assert 29 < usd < 31 and sum(v["approved"] for v in j["verdicts"]) == 1
    kw, nxt = later(30)
    model = FakeModel([reply(("DOGE/USD", "buy", 10))])
    wake(explore_deps(make_deps, root, model, **kw), nxt)
    assert model.prompts == [] and journal(root)[-1]["explore_candidates"] == {}


def test_exploration_trades_are_reported_apart_by_the_reflection(make_deps, fake, root):
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 70))])), SLOT)
    fake.prices["BTC/USD"] *= 0.9
    kw, nxt = later(15)
    replies = [reply(("BTC/USD", "hold", 0)), reflection_reply([])]
    wake(explore_deps(make_deps, root, FakeModel(replies), reflect=True, judge=lambda *a: judge_by(lambda p: 1.0),
                      **kw), nxt)
    r = next(r for r in journal(root) if r.get("kind") == "reflection")
    assert r["summary_by_kind"]["esplorazione"]["trades"] == 1 and r["summary_by_kind"]["regola"] == {"trades": 0}


def test_a_position_worth_less_than_the_order_minimum_is_still_exited(make_deps, fake, root):
    # 50$ of BTC shrinks to about 5$ (a partial fill, or a crash): its stop must still sell it.
    wake(make_deps(), SLOT)
    fake.positions["BTCUSD"] *= 0.1
    fake.prices["BTC/USD"] *= 0.9
    kw, nxt = later(15)
    wake(make_deps(model=hold_all("SOL/USD"), **kw), nxt)
    sells = [o for o in fake.orders if o["side"] == "sell"]
    assert len(sells) == 1 and journal(root)[-1]["exits"][0]["reason"] == "stop"


def test_the_exploration_label_survives_lost_exit_levels(make_deps, fake, root):
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 70))])), SLOT)
    (root / "state/positions.json").unlink()  # the levels are lost; the journal is not
    kw, nxt = later(15)
    wake(explore_deps(make_deps, root, hold_all("SOL/USD"), **kw), nxt)
    assert positions_state(root)["BTC/USD"]["explore"] is True


def test_an_unfilled_exploration_buy_still_uses_the_budget(make_deps, fake, root):
    p = root / "config/limits.toml"
    p.write_text(p.read_text().replace("explore_total_pct = 5.0", "explore_total_pct = 1.0"))
    fake.fill_orders = False  # the paper POL/USD buy of 2026-09-26 sat open for over 30 minutes
    wake(explore_deps(make_deps, root, FakeModel([reply(("BTC/USD", "buy", 70))])), SLOT)
    model = FakeModel([reply(*[(s, "buy", 30) for s in ("SOL/USD", "ETH/USD", "DOGE/USD")])])
    kw, nxt = later(15)
    wake(explore_deps(make_deps, root, model, **kw), nxt)
    (usd,) = journal(root)[-1]["explore_candidates"].values()
    assert usd == 30.0  # 100$ budget less the 70$ still waiting to fill
