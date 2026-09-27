"""Idempotent order placement. The client_order_id is the only key that matters.

The id comes from slot + symbol + side, so any run of the same slot computes the
same id. Before sending we look for it; after an ambiguous error we look again
instead of resending.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from .broker import Alpaca, BrokerHTTPError, BrokerUnavailable, norm_symbol
from .config import ORDER_PREFIX
from .slot import slot_id

OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "pending_cancel",
                 "pending_replace", "partially_filled"}


def client_order_id(slot: datetime, symbol: str, side: str) -> str:
    cid = f"{ORDER_PREFIX}{slot_id(slot)}-{norm_symbol(symbol)}-{side}"
    assert len(cid) <= 128  # Alpaca's documented maximum
    return cid


def slot_prefix(slot: datetime) -> str:
    return f"{ORDER_PREFIX}{slot_id(slot)}-"


@dataclass
class Placement:
    client_order_id: str
    outcome: str  # placed | adopted | rejected | unconfirmed | dry_run
    detail: str
    order: dict | None = None  # the order exactly as Alpaca returned it


def format_qty(qty: float) -> str:
    """A sell quantity at Alpaca's precision (min_trade_increment 1e-9 on every crypto pair,
    checked 2026-09-27), rounded down. Rounding to nearest could ask for 1e-9 more than is
    held: a memecoin holding of millions of units has about 16 significant digits, beyond
    what a float keeps, and the sell of the whole position would be rejected. Rounding the
    float's exact value down leaves at most 1e-9 of the coin behind."""
    d = Decimal(float(qty)).quantize(Decimal("1e-9"), rounding=ROUND_DOWN)  # the exact binary value
    return format(d.normalize(), "f") if d else "0"


def _is_duplicate(e: BrokerHTTPError) -> bool:
    return e.status == 422 and "client_order_id" in e.body and "unique" in e.body


def place(alpaca: Alpaca, slot: datetime, intent: dict, open_orders: list[dict], dry_run: bool = False) -> Placement:
    cid = client_order_id(slot, intent["symbol"], intent["side"])

    for o in open_orders:
        if o.get("client_order_id") == cid:
            return Placement(cid, "adopted", "ordine già aperto con lo stesso id", o)
    try:
        existing = alpaca.order_by_client_id(cid)
    except (BrokerUnavailable, BrokerHTTPError) as e:
        return Placement(cid, "unconfirmed", f"verifica dell'id non riuscita, ordine NON inviato: {e}")
    if existing:
        return Placement(cid, "adopted", "ordine già presente su Alpaca con lo stesso id", existing)
    if dry_run:
        return Placement(cid, "dry_run", "prova: ordine non inviato")

    payload = {"symbol": intent["symbol"], "side": intent["side"], "type": "market",
               "time_in_force": "gtc", "client_order_id": cid}
    if "notional" in intent:
        payload["notional"] = f"{intent['notional']:.2f}"
    else:
        payload["qty"] = format_qty(intent["qty"])
    try:
        return Placement(cid, "placed", "inviato", alpaca.submit_order(payload))
    except BrokerHTTPError as e:
        if _is_duplicate(e):
            found = _lookup(alpaca, cid)
            if found:
                return Placement(cid, "adopted", "Alpaca ha risposto 'id duplicato': ordine adottato", found)
            return Placement(cid, "unconfirmed", "Alpaca ha risposto 'id duplicato' ma l'ordine non si trova")
        return Placement(cid, "rejected", f"rifiutato da Alpaca: HTTP {e.status} {e.body[:200]}")
    except BrokerUnavailable as e:
        # The order may or may not have landed. Look, never resend blindly.
        found = _lookup(alpaca, cid)
        if found:
            return Placement(cid, "adopted", f"errore di rete dopo l'invio, ma l'ordine esiste: {e}", found)
        return Placement(cid, "unconfirmed", f"errore di rete, ordine non trovato, NON reinviato: {e}")


def _lookup(alpaca: Alpaca, cid: str) -> dict | None:
    try:
        return alpaca.order_by_client_id(cid)
    except (BrokerUnavailable, BrokerHTTPError):
        return None
