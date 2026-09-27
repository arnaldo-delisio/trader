"""Alpaca REST over urllib. Reads retry with backoff; order POSTs never retry.

The HTTP layer is a plain function (`transport`) so tests swap in a fake Alpaca
and still run every line of this client.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .config import DATA_URL, PAPER_URL, assert_paper

# transport(method, url, headers, body) -> (status, text); raises TransportError
Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]


class TransportError(Exception):
    """The request did not get an HTTP answer (DNS, refused, timeout)."""


class BrokerUnavailable(Exception):
    """Alpaca did not answer usefully after the retries."""


class BrokerHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # str(e) of a URLError never carries the request headers, so no keys leak here.
        raise TransportError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class Alpaca:
    def __init__(self, key: str, secret: str, transport: Transport = urllib_transport,
                 base_url: str = PAPER_URL, retries: int = 3, sleep: Callable[[float], None] = time.sleep):
        assert_paper(base_url)
        self.base = base_url.rstrip("/")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "Accept": "application/json", "Content-Type": "application/json"}
        self.transport = transport
        self.retries = retries
        self.sleep = sleep

    # ---- plumbing -------------------------------------------------------
    def _get(self, url: str, params: dict | None = None, allow_404: bool = False):
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        last = ""
        for attempt in range(self.retries):
            if attempt:
                self.sleep(2 ** (attempt - 1))
            try:
                status, text = self.transport("GET", url, self.headers, None)
            except TransportError as e:
                last = str(e)
                continue
            if status == 200:
                return json.loads(text)
            if status == 404 and allow_404:
                return None
            if _retryable(status):
                last = f"HTTP {status}"
                continue
            raise BrokerHTTPError(status, text)
        raise BrokerUnavailable(f"GET {url.split('?')[0]} failed after {self.retries} attempts: {last}")

    # ---- trading API ----------------------------------------------------
    def account(self) -> dict:
        return self._get(f"{self.base}/v2/account")

    def positions(self) -> list[dict]:
        return self._get(f"{self.base}/v2/positions")

    def orders(self, status: str = "open", after: str | None = None, limit: int = 500) -> list[dict]:
        return self._get(f"{self.base}/v2/orders",
                         {"status": status, "after": after, "limit": limit, "direction": "desc"})

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        return self._get(f"{self.base}/v2/orders:by_client_order_id",
                         {"client_order_id": client_order_id}, allow_404=True)

    def fills(self, after: str | None = None, max_pages: int = 10) -> list[dict]:
        """FILL activities after a time, oldest first. A full page means there may be more:
        the next page starts after the last id (page_token)."""
        out: list[dict] = []
        token = None
        for _ in range(max_pages):
            page = self._get(f"{self.base}/v2/account/activities/FILL",
                             {"after": after, "direction": "asc", "page_size": 100, "page_token": token})
            out.extend(page)
            if len(page) < 100:
                return out
            token = page[-1].get("id")
        raise BrokerUnavailable(f"fills: still paging after {max_pages} pages")

    def assets(self) -> list[dict]:
        """Active crypto assets; the caller keeps the tradable ones."""
        return self._get(f"{self.base}/v2/assets", {"asset_class": "crypto", "status": "active"})

    def submit_order(self, payload: dict) -> dict:
        """One attempt, no retry. The caller decides what an error means."""
        try:
            status, text = self.transport("POST", f"{self.base}/v2/orders", self.headers,
                                          json.dumps(payload).encode())
        except TransportError as e:
            raise BrokerUnavailable(f"POST /v2/orders: {e}") from None
        if status == 200:
            return json.loads(text)
        if _retryable(status):
            raise BrokerUnavailable(f"POST /v2/orders: HTTP {status}")
        raise BrokerHTTPError(status, text)

    def cancel_order(self, order_id: str) -> None:
        """DELETE /v2/orders/{id}. Cancelling twice changes nothing, so a network error or a
        5xx is retried like a read. 204 is success; 422 means the order can no longer be
        cancelled (usually it filled): BrokerHTTPError, the caller reports it."""
        url = f"{self.base}/v2/orders/{urllib.parse.quote(order_id, safe='')}"
        last = ""
        for attempt in range(self.retries):
            if attempt:
                self.sleep(2 ** (attempt - 1))
            try:
                status, text = self.transport("DELETE", url, self.headers, None)
            except TransportError as e:
                last = str(e)
                continue
            if status in (200, 204):
                return
            if _retryable(status):
                last = f"HTTP {status}"
                continue
            raise BrokerHTTPError(status, text)
        raise BrokerUnavailable(f"DELETE /v2/orders/{{id}} failed after {self.retries} attempts: {last}")

    # ---- market data ----------------------------------------------------
    def latest_quotes(self, symbols: list[str]) -> dict:
        return self._get(f"{DATA_URL}/latest/quotes", {"symbols": ",".join(symbols)})["quotes"]

    def bars(self, symbols: list[str], timeframe: str, start: str, limit: int = 1000,
             max_pages: int = 50) -> dict:
        """All bars for every symbol, following next_page_token.

        Alpaca pages across symbols in order: the first page can hold only the first
        symbol (or part of it), so one request leaves the others empty.
        """
        out: dict[str, list] = {}
        token = None
        for _ in range(max_pages):
            data = self._get(f"{DATA_URL}/bars", {"symbols": ",".join(symbols), "timeframe": timeframe,
                                                  "start": start, "limit": limit, "sort": "asc",
                                                  "page_token": token})
            for sym, rows in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = data.get("next_page_token")
            if not token:
                return out
        raise BrokerUnavailable(f"GET {DATA_URL}/bars: still paging after {max_pages} pages")

    def bars_each(self, symbols: list[str], timeframe: str, start: str, workers: int = 8) -> dict:
        """bars() one symbol per request, a few at a time: 31 symbols x 10 days of 15m bars
        took 40 sequential pages and 30 s on 2026-09-26. Any failure fails the whole call."""
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(symbols)))) as pool:
            parts = list(pool.map(lambda s: self.bars([s], timeframe, start, limit=10000), symbols))
        out: dict[str, list] = {}
        for part in parts:
            for sym, rows in part.items():
                out.setdefault(sym, []).extend(rows)
        return out


def norm_symbol(s: str) -> str:
    """Positions may say BTCUSD where orders say BTC/USD; compare without the slash."""
    return s.replace("/", "").upper()
