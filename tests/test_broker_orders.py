import json

import pytest

from tests.conftest import NOW, SLOT
from trader.broker import Alpaca, BrokerHTTPError, BrokerUnavailable, TransportError
from trader.fake_alpaca import FakeAlpaca
from trader.orders import client_order_id, format_qty, place

INTENT = {"symbol": "BTC/USD", "side": "buy", "notional": 50.0}


class Scripted:
    """Answers each call with the next scripted (status, body) or exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(method)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def client(transport):
    return Alpaca("k", "s", transport=transport, sleep=lambda s: None)


def test_reads_retry_then_succeed():
    t = Scripted((503, ""), TransportError("reset"), (200, json.dumps({"equity": "1"})))
    assert client(t).account() == {"equity": "1"} and len(t.calls) == 3


def test_reads_give_up_after_bounded_retries():
    t = Scripted(*[(500, "")] * 3)
    with pytest.raises(BrokerUnavailable):
        client(t).account()
    assert len(t.calls) == 3


def test_client_errors_are_not_retried():
    t = Scripted((403, "forbidden"))
    with pytest.raises(BrokerHTTPError):
        client(t).account()
    assert len(t.calls) == 1


def test_order_post_is_never_retried():
    t = Scripted((500, ""))
    with pytest.raises(BrokerUnavailable):
        client(t).submit_order({"symbol": "BTC/USD"})
    assert t.calls == ["POST"]


def test_client_order_id_is_deterministic_and_short():
    cid = client_order_id(SLOT, "BTC/USD", "buy")
    assert cid == "trd-20260926T0800Z-BTCUSD-buy" == client_order_id(SLOT, "BTCUSD", "buy")
    assert len(cid) <= 128


@pytest.fixture
def fake():
    return FakeAlpaca(now=lambda: NOW)


def test_first_placement_sends_once(fake):
    p = place(client(fake), SLOT, INTENT, [])
    assert p.outcome == "placed" and fake.posts() == 1
    sent = fake.orders[0]
    assert sent["time_in_force"] == "gtc" and sent["type"] == "market" and sent["notional"] == "50.00"


def test_existing_open_order_is_adopted_not_resent(fake):
    fake.fill_orders = False
    first = place(client(fake), SLOT, INTENT, [])
    open_orders = client(fake).orders("open")
    again = place(client(fake), SLOT, INTENT, open_orders)
    assert again.outcome == "adopted" and again.order["id"] == first.order["id"] and fake.posts() == 1


def test_existing_order_found_by_client_id_is_adopted_not_resent(fake):
    fake.add_order(client_order_id(SLOT, "BTC/USD", "buy"), "BTC/USD", "buy", notional=50)
    p = place(client(fake), SLOT, INTENT, [])
    assert p.outcome == "adopted" and fake.posts() == 0


def test_duplicate_422_is_adopted(fake):
    c = client(fake)
    fake.add_order(client_order_id(SLOT, "BTC/USD", "buy"), "BTC/USD", "buy", notional=50)
    real_lookup = c.order_by_client_id
    seen = []

    def lookup(cid):  # the pre-check misses it (a race), the lookup after the 422 finds it
        seen.append(cid)
        return None if len(seen) == 1 else real_lookup(cid)
    c.order_by_client_id = lookup
    p = place(c, SLOT, INTENT, [])
    assert p.outcome == "adopted" and fake.posts() == 1 and len(fake.orders) == 1


def test_lost_reply_is_found_by_client_id_and_not_resent(fake):
    fake.post_fault = "lost_reply"  # Alpaca took the order, the answer never arrived
    p = place(client(fake), SLOT, INTENT, [])
    assert p.outcome == "adopted" and fake.posts() == 1 and len(fake.orders) == 1


def test_lost_request_is_reported_and_not_resent(fake):
    fake.post_fault = "lost_request"
    p = place(client(fake), SLOT, INTENT, [])
    assert p.outcome == "unconfirmed" and fake.posts() == 1 and fake.orders == []


def test_no_send_when_the_id_check_itself_fails(fake):
    c = client(fake)

    def broken(cid):
        raise BrokerUnavailable("down")
    c.order_by_client_id = broken
    p = place(c, SLOT, INTENT, [])
    assert p.outcome == "unconfirmed" and fake.posts() == 0


def test_broker_rejection_is_recorded(fake):
    p = place(client(fake), SLOT, {"symbol": "ETH/USD", "side": "sell", "qty": 1.0}, [])
    assert p.outcome == "rejected" and "403" in p.detail


def test_dry_run_never_posts(fake):
    p = place(client(fake), SLOT, INTENT, [], dry_run=True)
    assert p.outcome == "dry_run" and fake.posts() == 0


def test_duplicate_422_adopts_the_order_alpaca_has(fake):
    c = client(fake)
    cid = client_order_id(SLOT, "BTC/USD", "buy")
    fake.add_order(cid, "BTC/USD", "buy", notional=50)
    real_lookup = c.order_by_client_id
    seen = []

    def lookup(x):
        seen.append(x)
        return None if len(seen) == 1 else real_lookup(x)
    c.order_by_client_id = lookup
    p = place(c, SLOT, INTENT, [])
    assert p.outcome == "adopted" and p.order and p.order["client_order_id"] == cid and len(seen) == 2


def test_duplicate_422_without_a_match_is_unconfirmed_and_not_resent(fake):
    c = client(fake)
    fake.add_order(client_order_id(SLOT, "BTC/USD", "buy"), "BTC/USD", "buy", notional=50)
    c.order_by_client_id = lambda cid: None  # both lookups miss
    p = place(c, SLOT, INTENT, [])
    assert p.outcome == "unconfirmed" and fake.posts() == 1 and len(fake.orders) == 1


def test_bars_follow_the_page_token_so_every_symbol_gets_its_bars():
    # Alpaca pages across symbols: the first page held only BTC/USD on 2026-09-26.
    fake = FakeAlpaca(now=lambda: NOW)
    fake.bars_page_size = 100
    bars = client(fake).bars(["BTC/USD", "ETH/USD", "SOL/USD"], "1Hour", "2026-09-19T08:00:00Z")
    assert {s: len(r) for s, r in bars.items()} == {"BTC/USD": 168, "ETH/USD": 168, "SOL/USD": 168}
    ts = [b["t"] for b in bars["ETH/USD"]]
    assert ts == sorted(ts) and len(set(ts)) == 168  # a symbol split across pages is joined in order
    assert sum(1 for _, url in fake.calls if "/bars" in url) == 6


def test_bars_each_asks_one_symbol_per_request_and_joins_them():
    fake = FakeAlpaca(now=lambda: NOW)
    bars = client(fake).bars_each(["BTC/USD", "ETH/USD", "DOGE/USD"], "15Min", "2026-09-25T08:00:00Z")
    assert {s: len(r) for s, r in bars.items()} == {"BTC/USD": 97, "ETH/USD": 97, "DOGE/USD": 97}
    assert sorted(url.split("symbols=")[1].split("&")[0] for _, url in fake.calls) == ["BTC%2FUSD", "DOGE%2FUSD",
                                                                                     "ETH%2FUSD"]


def test_bars_each_fails_whole_when_one_symbol_fails():
    fake = FakeAlpaca(now=lambda: NOW)
    real = fake._data

    def flaky(path, q):
        return (500, "") if q.get("symbols") == "ETH/USD" else real(path, q)
    fake._data = flaky
    with pytest.raises(BrokerUnavailable):
        client(fake).bars_each(["BTC/USD", "ETH/USD"], "15Min", "2026-09-25T08:00:00Z")


def test_fills_follow_the_page_token():
    pages = [[{"id": f"a{i}"} for i in range(100)], [{"id": "b0"}]]
    t = Scripted(*[(200, json.dumps(p)) for p in pages])
    assert len(client(t).fills(after="2026-09-18T00:00:00Z")) == 101 and len(t.calls) == 2


def test_bars_stop_on_a_page_token_that_never_ends():
    endless = Scripted(*[(200, json.dumps({"bars": {"BTC/USD": [{"c": 1}]}, "next_page_token": "x"}))] * 3)
    with pytest.raises(BrokerUnavailable):
        client(endless).bars(["BTC/USD"], "1Hour", "2026-09-18T08:00:00Z", max_pages=3)


@pytest.mark.parametrize("held", ["9183394.009883727", "2739726.027397261", "0.000255999", "166666.666666666",
                                  "1", "0.000000001"])
def test_a_sell_quantity_is_never_more_than_what_alpaca_says_is_held(held):
    # Rounding a float of 16 significant digits to nearest gave 9183394.009883728 for this holding.
    from decimal import Decimal
    out = format_qty(float(held))
    assert Decimal(out) <= Decimal(held) and Decimal(held) - Decimal(out) <= Decimal("1e-9")
    assert "e" not in out.lower() and len(out.split(".")[-1]) <= 9 if "." in out else True


def test_a_whole_position_sell_sends_exactly_what_alpaca_says_is_available():
    # 2026-09-28: Alpaca refused the BONK liquidation (HTTP 403 'insufficient balance'). The
    # float of 156921866.806502359, even rounded down, was 156921866.806502372.
    from decimal import Decimal

    from tests.test_risk import LIMITS
    from trader.context import snapshot
    from trader.model import Decision
    from trader.risk import gate

    held = "156921866.806502359"
    position = {"symbol": "BONKUSD", "asset_class": "crypto", "qty": held, "qty_available": held,
                "market_value": "3100.12"}
    quote = {"BONK/USD": {"bp": "0.00001975", "ap": "0.00001976"}}
    snap = snapshot({"equity": "100000", "last_equity": "100000", "cash": "90000"}, [position], [], quote)
    v = gate([Decision("BONK/USD", "sell", 3099.2, "liquidazione")], snap, LIMITS, enabled=True,
             buys_enabled=False)[0]
    assert v.approved, v.reason

    sent = []

    def alpaca(method, url, headers, body):
        if method == "GET":
            return 404, json.dumps({"message": "order not found"})
        sent.append(json.loads(body))
        if Decimal(sent[-1]["qty"]) > Decimal(held):
            return 403, json.dumps({"code": 40310000, "message": "insufficient balance"})
        return 200, json.dumps({"id": "o1", "client_order_id": sent[-1]["client_order_id"], "status": "new"})

    p = place(client(alpaca), SLOT, v.order, [])
    assert p.outcome == "placed", p.detail
    assert sent[-1]["qty"] == held
