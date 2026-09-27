"""An in-memory Alpaca paper account, served through the same transport the real client uses.

Tests, scripts/simulate.py and local dry runs plug this in instead of urllib, so
the real Alpaca client code (URLs, retries, error handling) still runs.
Numbers here are invented; nothing comes from a real account.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import uuid
from datetime import UTC, datetime

from .broker import TransportError, norm_symbol
from .config import DATA_URL, PAPER_URL

START_PRICES = {"BTC/USD": 60000.0, "ETH/USD": 3000.0, "SOL/USD": 150.0, "LINK/USD": 15.0,
                "DOGE/USD": 0.2, "XRP/USD": 0.6, "ADA/USD": 0.4, "AVAX/USD": 30.0}
# Drift per 15-minute bar of the invented price paths: most coins rise, two fall.
DRIFT = {"BTC/USD": 0.0004, "ETH/USD": 0.0003, "SOL/USD": 0.0005, "LINK/USD": 0.0002,
         "DOGE/USD": 0.0006, "XRP/USD": -0.0004, "ADA/USD": -0.0003, "AVAX/USD": 0.0001}
FEE_RATE = 0.0025  # Alpaca keeps the buy fee in the coin and the sell fee in dollars (paper, 2026-09-26)
TIMEFRAMES = {"15Min": 900, "1Hour": 3600}


class FakeAlpaca:
    def __init__(self, cash: float = 10000.0, last_equity: float | None = None, now=None):
        self.cash = cash
        self.last_equity = cash if last_equity is None else last_equity
        self.prices = dict(START_PRICES)
        self.positions: dict[str, float] = {}  # norm symbol -> qty
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.trading_blocked = False
        self.down = False            # every request fails at the network level
        self.post_fault = None       # "lost_reply" (order lands, reply lost) | "lost_request" | "http_500"
        self.fill_orders = True      # False keeps new orders open
        self.equity_override = None  # force equity (to trigger the daily loss limit)
        self.bars_page_size = 1000   # the real API returned about 750 15m bars per page (2026-09-26)
        self.drift = dict(DRIFT)
        self.untradable: set[str] = set()  # symbols /v2/assets lists as not tradable
        self.calls: list[tuple[str, str]] = []
        self.now = now or (lambda: datetime.now(UTC))

    # ---- helpers for tests --------------------------------------------
    def posts(self) -> int:
        return sum(1 for m, _ in self.calls if m == "POST")

    def symbol_of(self, key: str) -> str:
        return next(s for s in self.prices if norm_symbol(s) == key)

    def equity(self) -> float:
        if self.equity_override is not None:
            return self.equity_override
        return self.cash + sum(q * self.prices[self.symbol_of(k)] for k, q in self.positions.items())

    def add_order(self, cid: str, symbol: str, side: str, notional: float | None = None,
                  qty: float | None = None) -> dict:
        """Put an order into the account as if some earlier run had sent it."""
        return self._accept({"symbol": symbol, "side": side, "client_order_id": cid,
                             "notional": None if notional is None else str(notional),
                             "qty": None if qty is None else str(qty), "type": "market", "time_in_force": "gtc"})

    def path(self, symbol: str, start: int, end: int, step: int) -> list[dict]:
        """Invented bars from start to end: a drift and a wave, ending at the current price."""
        first = start + (-start) % step
        n = (end - first) // step
        price, drift = self.prices[symbol], self.drift.get(symbol, 0.0) * step / 900
        rows, prev = [], None
        for k in range(n):
            back = n - k  # bars before now
            c = price * math.exp(-drift * back) * (1 + 0.012 * math.sin(back / 9.0))
            c = price if back == 1 else c
            o = prev if prev is not None else c
            t = datetime.fromtimestamp(first + k * step, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append({"t": t, "o": o, "h": max(o, c) * 1.004, "l": min(o, c) * 0.996, "c": c,
                         "v": 100 + 40 * math.sin(back / 5.0), "n": 10, "vw": c})
            prev = c
        return rows

    # ---- transport ----------------------------------------------------
    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
        self.calls.append((method, url))
        if self.down:
            raise TransportError("URLError: [Errno 111] Connection refused")
        if not headers.get("APCA-API-KEY-ID") or not headers.get("APCA-API-SECRET-KEY"):
            return 401, json.dumps({"message": "unauthorized"})
        parsed = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path
        if base == PAPER_URL:
            return self._trading(method, path, q, body)
        if url.startswith(DATA_URL):
            return self._data(path.removeprefix(urllib.parse.urlparse(DATA_URL).path), q)
        return 404, "{}"

    def _ok(self, obj) -> tuple[int, str]:
        return 200, json.dumps(obj)

    def _trading(self, method, path, q, body):
        if method == "GET" and path == "/v2/account":
            eq = self.equity()
            return self._ok({"id": "fake-account", "status": "ACTIVE", "crypto_status": "ACTIVE",
                             "currency": "USD", "cash": f"{self.cash:.2f}",
                             "non_marginable_buying_power": f"{self.cash:.2f}", "buying_power": f"{self.cash:.2f}",
                             "equity": f"{eq:.2f}", "last_equity": f"{self.last_equity:.2f}",
                             "trading_blocked": self.trading_blocked})
        if method == "GET" and path == "/v2/positions":
            out = []
            for k, qty in self.positions.items():
                px = self.prices[self.symbol_of(k)]
                out.append({"symbol": k, "asset_class": "crypto", "qty": f"{qty:.9f}",
                            "qty_available": f"{qty:.9f}", "market_value": f"{qty * px:.2f}",
                            "current_price": f"{px:.2f}"})
            return self._ok(out)
        if method == "GET" and path == "/v2/orders":
            status = q.get("status", "open")
            rows = [o for o in self.orders if status == "all"
                    or (status == "open") == (o["status"] not in ("filled", "canceled", "rejected", "expired"))]
            return self._ok(list(reversed(rows)))
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            for o in self.orders:
                if o["client_order_id"] == q.get("client_order_id"):
                    return self._ok(o)
            return 404, json.dumps({"code": 40410000, "message": "order not found"})
        if method == "GET" and path == "/v2/account/activities/FILL":
            return self._ok(self.fills)
        if method == "GET" and path == "/v2/assets":
            return self._ok([{"symbol": s, "class": "crypto", "status": "active", "tradable": s not in self.untradable}
                             for s in [*self.prices, "USDT/USD", "BTC/USDT"]])
        if method == "POST" and path == "/v2/orders":
            req = json.loads(body)
            if self.post_fault == "http_500":
                return 500, json.dumps({"message": "internal server error"})
            if self.post_fault == "lost_request":
                raise TransportError("TimeoutError: timed out")
            if any(o["client_order_id"] == req.get("client_order_id") for o in self.orders):
                return 422, json.dumps({"code": 40010001, "message": "client_order_id must be unique"})
            if req.get("time_in_force") not in ("gtc", "ioc"):
                return 422, json.dumps({"code": 42210000, "message": "invalid crypto time_in_force"})
            key = norm_symbol(req["symbol"])
            if req["side"] == "sell":
                want = float(req.get("qty") or 0) or float(req["notional"]) / self.prices[req["symbol"]]
                if want > self.positions.get(key, 0) + 1e-12:
                    return 403, json.dumps({"code": 40310000, "message": "insufficient balance"})
            order = self._accept(req)
            if self.post_fault == "lost_reply":
                raise TransportError("TimeoutError: timed out")
            return self._ok(order)
        return 404, json.dumps({"message": "not found"})

    def _accept(self, req: dict) -> dict:
        now = self.now().isoformat()
        o = {"id": str(uuid.uuid4()), "client_order_id": req["client_order_id"], "symbol": req["symbol"],
             "asset_class": "crypto", "side": req["side"], "type": req.get("type", "market"),
             "time_in_force": req.get("time_in_force", "gtc"), "notional": req.get("notional"),
             "qty": req.get("qty"), "filled_qty": "0", "filled_avg_price": None, "status": "new",
             "created_at": now, "submitted_at": now, "filled_at": None}
        self.orders.append(o)
        if self.fill_orders:
            self._fill(o)
        return o

    def _fill(self, o: dict) -> None:
        px = self.prices[o["symbol"]]
        key = norm_symbol(o["symbol"])
        qty = float(o["qty"]) if o.get("qty") else float(o["notional"]) / px
        if o["side"] == "buy":
            self.cash -= qty * px
            self.positions[key] = self.positions.get(key, 0) + qty * (1 - FEE_RATE)
        else:
            self.cash += qty * px * (1 - FEE_RATE)
            self.positions[key] = self.positions.get(key, 0) - qty
            if self.positions[key] <= 1e-12:
                del self.positions[key]
        o.update(status="filled", filled_qty=f"{qty:.9f}", filled_avg_price=f"{px:.2f}", filled_at=o["created_at"])
        self.fills.append({"id": f"{o['created_at']}::{uuid.uuid4()}", "activity_type": "FILL", "type": "fill",
                           "symbol": o["symbol"], "side": o["side"], "qty": o["filled_qty"], "price": f"{px:.2f}",
                           "order_id": o["id"], "order_status": "filled", "transaction_time": o["created_at"]})

    def _data(self, path, q):
        symbols = q.get("symbols", "").split(",")
        if path == "/latest/quotes":
            return self._ok({"quotes": {s: {"ap": self.prices[s] * 1.0005, "bp": self.prices[s] * 0.9995,
                                            "as": 1.0, "bs": 1.0, "t": self.now().isoformat()}
                                        for s in symbols if s in self.prices}})
        if path == "/bars":
            # Pages run across symbols in order, like the real API: a page can end in the
            # middle of one symbol, and the next page continues from there.
            step = TIMEFRAMES[q.get("timeframe", "15Min")]
            now = int(self.now().timestamp())
            end = now - now % step  # the bar in progress is not served
            start = int(datetime.fromisoformat(q["start"]).timestamp())
            flat = []
            for s in symbols:
                if s in self.prices:
                    flat += [(s, r) for r in self.path(s, start, end, step)]
            offset = int(q.get("page_token") or 0)
            size = min(int(q.get("limit") or 1000), self.bars_page_size)
            bars: dict[str, list] = {}
            for s, r in flat[offset:offset + size]:
                bars.setdefault(s, []).append(r)
            nxt = offset + size
            return self._ok({"bars": bars, "next_page_token": str(nxt) if nxt < len(flat) else None})
        return 404, "{}"
