"""The risk gate: a pure function from (proposal, market snapshot, limits) to verdicts.

No I/O, no clock, no randomness: the same inputs always give the same verdicts,
and every rejection carries a reason in plain Italian that ends up in the journal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .broker import norm_symbol
from .config import EXIT_MIN_ORDER_USD, Limits
from .model import Decision


@dataclass(frozen=True)
class Holding:
    qty_available: float
    market_value: float


@dataclass(frozen=True)
class Snapshot:
    equity: float
    last_equity: float
    cash: float  # non_marginable_buying_power: crypto cannot use margin
    trading_blocked: bool
    bids: dict[str, float]  # by allowlist symbol, e.g. "BTC/USD"
    asks: dict[str, float]
    holdings: dict[str, Holding] = field(default_factory=dict)  # by norm_symbol
    open_order_symbols: frozenset[str] = frozenset()  # norm_symbol
    min_qty: dict[str, float] = field(default_factory=dict)  # Alpaca's min_order_size, by allowlist symbol
    # Open buy orders not filled yet, norm_symbol -> notional: they spend when they fill, so
    # they count toward the invested and exploration caps (a paper POL/USD market buy sat
    # unfilled for over 30 minutes on 2026-09-26).
    pending_buys: dict[str, float] = field(default_factory=dict)


@dataclass
class Verdict:
    symbol: str
    action: str
    requested_usd: float
    approved: bool
    reason: str
    order: dict | None = None  # {"symbol","side","notional"} or {"symbol","side","qty"}
    explore: bool = False      # an exploration buy (strategy.explore_ok), under its own caps

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def daily_loss_pct(s: Snapshot) -> float:
    if s.last_equity <= 0:
        return 0.0
    return max(0.0, (s.last_equity - s.equity) / s.last_equity * 100)


def _floor(x: float, decimals: int = 9) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def open_positions(s: Snapshot) -> int:
    """Positions worth at least a dollar. Smaller ones are fee dust Alpaca leaves behind."""
    return sum(1 for h in s.holdings.values() if h.market_value >= 1.0)


def exit_min_usd(s: Snapshot, symbol: str) -> float:
    """The smallest sale of a whole holding Alpaca takes: its min_order_size when known,
    and never under EXIT_MIN_ORDER_USD."""
    bid = s.bids.get(symbol, 0.0)
    return max(EXIT_MIN_ORDER_USD, s.min_qty.get(symbol, 0.0) * bid)


def gate(decisions: list[Decision], s: Snapshot, limits: Limits, enabled: bool = True,
         disabled_reason: str = "", entries: dict[str, float] | None = None,
         explore: dict[str, float] | None = None, explore_held: frozenset[str] = frozenset()) -> list[Verdict]:
    """entries: the strategy's buy candidates this wake, symbol -> the most it may buy.
    explore: the exploration candidates, symbol -> the most it may buy; explore_held: the
    held symbols (norm_symbol) that were exploration buys. A buy of anything else is
    rejected, so the model can be more careful than the strategy, never more aggressive.
    Sells only need something to sell."""
    entries = entries or {}
    explore = explore or {}
    out: list[Verdict] = []
    cash = s.cash
    held_value = {k: h.market_value for k, h in s.holdings.items()}
    exposure = sum(held_value.values()) + sum(s.pending_buys.values())
    explore_exposure = (sum(v for k, v in held_value.items() if k in explore_held)
                        + sum(v for k, v in s.pending_buys.items() if k in explore_held))
    positions = open_positions(s) + sum(1 for k in s.pending_buys if held_value.get(k, 0.0) < 1.0)
    placed = 0
    loss = daily_loss_pct(s)

    for d in decisions:
        v = Verdict(d.symbol, d.action, d.notional_usd, False, "",
                    explore=d.action == "buy" and d.symbol in explore and d.symbol not in entries)
        out.append(v)
        key = norm_symbol(d.symbol)

        def no(reason: str, v: Verdict = v) -> None:
            v.reason = reason

        if d.action == "hold":
            no("hold: nessun ordine")
            continue
        if not enabled:
            no(f"kill switch attivo ({disabled_reason}): nessun ordine")
            continue
        if s.trading_blocked:
            no("Alpaca segnala il conto bloccato al trading")
            continue
        if key in s.open_order_symbols:
            no("c'è già un ordine aperto su questo simbolo")
            continue
        if placed >= limits.max_orders_per_wake:
            no(f"superato il massimo di {limits.max_orders_per_wake} ordini per risveglio")
            continue

        if d.action == "buy":
            n = d.notional_usd
            ask = s.asks.get(d.symbol, 0.0)
            cap_pct = limits.position_cap_pct(d.symbol)
            if d.symbol not in limits.symbols:
                no(f"{d.symbol} non è nella lista dei simboli ammessi")
            elif n < limits.min_order_usd:
                no(f"{n:.2f}$ sotto il minimo per ordine di {limits.min_order_usd:.2f}$")
            elif loss >= limits.daily_loss_limit_pct:
                no(f"perdita giornaliera {loss:.2f}% oltre il limite {limits.daily_loss_limit_pct:.2f}%: niente acquisti")
            elif d.symbol not in entries and d.symbol not in explore:
                no("non è un candidato d'ingresso della strategia in questo risveglio")
            elif d.symbol in entries and n > entries[d.symbol] + 0.01:
                no(f"{n:.2f}$ oltre la dimensione della strategia ({entries[d.symbol]:.2f}$)")
            elif d.symbol not in entries and (why := _explore_no(d.symbol, n, s, limits, explore, held_value,
                                                                 explore_exposure)):
                no(why)
            elif ask <= 0:
                no("nessun prezzo ask disponibile")
            elif s.equity <= 0:
                no("equity non disponibile")
            elif held_value.get(key, 0.0) < 1.0 and positions >= limits.max_open_positions:
                no(f"già {positions} posizioni aperte, il massimo è {limits.max_open_positions}")
            elif held_value.get(key, 0.0) + n > s.equity * cap_pct / 100:
                no(f"la posizione salirebbe a {held_value.get(key, 0.0) + n:.2f}$, oltre il {cap_pct:g}% "
                   f"del patrimonio ({s.equity * cap_pct / 100:.2f}$)")
            elif exposure + n > s.equity * limits.max_invested_pct / 100:
                no(f"l'investito salirebbe a {exposure + n:.2f}$, oltre il {limits.max_invested_pct:g}% "
                   f"del patrimonio ({s.equity * limits.max_invested_pct / 100:.2f}$)")
            elif n > cash:
                no(f"liquidità insufficiente: {cash:.2f}$ disponibili")
            else:
                notional = round(n, 2)
                v.approved, v.reason = True, "ammesso"
                v.order = {"symbol": d.symbol, "side": "buy", "notional": notional}
                if v.explore:
                    explore_exposure += notional
                cash -= notional
                if held_value.get(key, 0.0) < 1.0:
                    positions += 1
                held_value[key] = held_value.get(key, 0.0) + notional
                exposure += notional
                placed += 1
            continue

        # sell: never more than what is held (crypto cannot be shorted). Allowed whatever
        # the allowlist or the daily loss say: reducing risk is always possible.
        h = s.holdings.get(key)
        bid = s.bids.get(d.symbol, 0.0)
        if not h or h.qty_available <= 0:
            no("nessuna quantità detenuta: vendere sarebbe uno short")
            continue
        if bid <= 0:
            no("nessun prezzo bid disponibile")
            continue
        held_usd = h.qty_available * bid
        if d.notional_usd > held_usd * 1.01:
            no(f"vendita di {d.notional_usd:.2f}$ oltre il detenuto ({held_usd:.2f}$): niente short")
            continue
        qty = h.qty_available if d.notional_usd >= held_usd * 0.99 else _floor(d.notional_usd / bid)
        qty = min(qty, h.qty_available)
        # Selling the whole holding (every exit) only needs Alpaca's own minimum, so a position
        # that shrank under min_order_usd can still be closed; a partial sell needs min_order_usd.
        whole = qty >= h.qty_available
        floor = exit_min_usd(s, d.symbol) if whole else limits.min_order_usd
        if qty * bid < floor:
            no(f"{qty * bid:.2f}$ sotto il minimo per {'la vendita di tutto' if whole else 'ordine'} di {floor:.2f}$")
            continue
        v.approved, v.reason = True, "ammesso"
        v.order = {"symbol": d.symbol, "side": "sell", "qty": qty}
        held_value[key] = max(0.0, held_value.get(key, 0.0) - qty * bid)
        exposure = max(0.0, exposure - qty * bid)
        placed += 1
    return out


def _explore_no(symbol: str, n: float, s: Snapshot, limits: Limits, explore: dict[str, float],
                held_value: dict[str, float], explore_exposure: float) -> str:
    """Why an exploration buy is refused, or "" when its own caps allow it. The usual caps
    (per coin, invested, positions, cash) are checked after this, as for every buy."""
    key = norm_symbol(symbol)
    one, total = s.equity * limits.explore_position_pct / 100, s.equity * limits.explore_total_pct / 100
    if n > explore[symbol] + 0.01:
        return f"esplorazione: {n:.2f}$ oltre la dimensione ammessa ({explore[symbol]:.2f}$)"
    if held_value.get(key, 0.0) >= 1.0:
        return "esplorazione: solo posizioni nuove, questa crypto è già in portafoglio"
    if n > one + 1e-9:
        return (f"esplorazione: {n:.2f}$ oltre l'{limits.explore_position_pct:g}% del patrimonio "
                f"per posizione ({one:.2f}$)")
    if explore_exposure + n > total + 1e-9:
        return (f"esplorazione: il totale salirebbe a {explore_exposure + n:.2f}$, oltre il "
                f"{limits.explore_total_pct:g}% del patrimonio ({total:.2f}$)")
    return ""
