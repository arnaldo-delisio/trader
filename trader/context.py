"""From what Alpaca returned to what the wake needs: the gate's Snapshot, the market views,
the open positions with their exit levels, and the model's prompt."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import strategy as st
from .broker import norm_symbol
from .config import EXCLUDED_BASES, Limits, is_memecoin, is_position
from .indicators import parse_bars, parse_time
from .model import clean_text
from .risk import Holding, Snapshot, daily_loss_pct


def f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def snapshot(account: dict, positions: list[dict], open_orders: list[dict], quotes: dict,
             assets: list[dict] | None = None) -> Snapshot:
    return Snapshot(
        equity=f(account.get("equity")),
        last_equity=f(account.get("last_equity")),
        cash=f(account.get("non_marginable_buying_power", account.get("cash"))),
        trading_blocked=bool(account.get("trading_blocked")) or account.get("crypto_status", "ACTIVE") != "ACTIVE",
        bids={s: f(q.get("bp")) for s, q in quotes.items()},
        asks={s: f(q.get("ap")) for s, q in quotes.items()},
        holdings={norm_symbol(p["symbol"]): Holding(f(p.get("qty_available", p.get("qty"))), f(p.get("market_value")))
                  for p in positions if p.get("asset_class", "crypto") == "crypto"},
        open_order_symbols=frozenset(norm_symbol(o["symbol"]) for o in open_orders),
        pending_buys=_pending_buys(open_orders),
        min_qty={a["symbol"]: f(a.get("min_order_size")) for a in assets or [] if a.get("symbol")},
    )


def _pending_buys(open_orders: list[dict]) -> dict[str, float]:
    """norm_symbol -> what the open buy orders may still spend: the unfilled notional, or the
    unfilled quantity at the fill price so far. A partial fill is already in the position."""
    out: dict[str, float] = {}
    for o in open_orders:
        if o.get("side") != "buy":
            continue
        filled, px = f(o.get("filled_qty")), f(o.get("filled_avg_price"))
        if o.get("notional"):
            left = max(0.0, f(o["notional"]) - filled * px)
        else:
            left = max(0.0, f(o.get("qty")) - filled) * px
        k = norm_symbol(o["symbol"])
        out[k] = out.get(k, 0.0) + left
    return out


def pair(symbol: str) -> str:
    """'BTCUSD' or 'BTC/USD' -> 'BTC/USD'. Positions may come without the slash."""
    s = symbol.upper()
    return s if "/" in s else f"{s.removesuffix('USD')}/USD"


# ---- universe and market -----------------------------------------------------------

def universe(assets: list[dict], limits: Limits) -> tuple[list[str], list[str]]:
    """(symbols to watch, allowed symbols Alpaca does not list as tradable now)."""
    live = {a.get("symbol") for a in assets if a.get("tradable") and a.get("status", "active") == "active"}
    allowed = [s for s in limits.symbols if s.split("/")[0] not in EXCLUDED_BASES]
    return [s for s in allowed if s in live], [s for s in allowed if s not in live]


def spreads_pct(quotes: dict) -> dict[str, float]:
    out = {}
    for s, q in quotes.items():
        ap, bp = f(q.get("ap")), f(q.get("bp"))
        if ap > 0 and bp > 0 and ap >= bp:
            out[s] = (ap - bp) / ((ap + bp) / 2) * 100
    return out


@dataclass
class Market:
    t: int                                  # decision time, epoch seconds
    frames: dict[str, st.Frames]
    views: dict[str, st.View]
    regime_ok: bool
    basket_7d_pct: float | None
    breadth_pct: float | None
    missing: list[str] = field(default_factory=list)  # symbols without usable data


def market(raw_bars: dict[str, list[dict]], symbols: list[str], t: int, params: dict,
           spreads: dict[str, float]) -> Market:
    frames = {s: st.frames(b) for s in symbols if (b := parse_bars(raw_bars.get(s, [])))}
    views = st.analyze(frames, t, params, spreads)
    feats = {s: v.features for s, v in views.items()}
    basket = statistics.fmean(x["ret_7d_pct"] for x in feats.values()) if feats else None
    breadth = 100 * sum(x["trend_4h"] > 0 for x in feats.values()) / len(feats) if feats else None
    return Market(t, frames, views, st.regime_ok(feats), basket, breadth,
                  sorted(s for s in symbols if s not in views))


def regime_state(m: Market) -> dict:
    """What Jev reads to classify the regime: the whole market in a few numbers."""
    feats = {s: v.features for s, v in m.views.items()}

    def coin(s):
        x = feats.get(s)
        return None if not x else {k: round(x[k], 3) for k in (
            "ret_24h_pct", "ret_7d_pct", "trend_4h", "trend_1h", "rsi_1h", "atr_1h_pct")}
    med = (lambda k: round(statistics.median(x[k] for x in feats.values()), 3)) if feats else (lambda k: None)
    return {"coins": len(feats),
            "basket_return_7d_pct": None if m.basket_7d_pct is None else round(m.basket_7d_pct, 3),
            "coins_in_4h_uptrend_pct": None if m.breadth_pct is None else round(m.breadth_pct, 1),
            "median_return_24h_pct": med("ret_24h_pct"), "median_rsi_1h": med("rsi_1h"),
            "median_atr_1h_pct": med("atr_1h_pct"), "BTC": coin("BTC/USD"), "ETH": coin("ETH/USD")}


def cooldowns(orders: list[dict], params: dict) -> dict[str, int]:
    """symbol -> until when (epoch) a new buy waits after the last filled sell, from Alpaca's orders."""
    out: dict[str, int] = {}
    for o in orders:
        if o.get("side") == "sell" and o.get("status") == "filled" and o.get("filled_at"):
            until = parse_time(o["filled_at"]) + int(params["cooldown_hours"] * 3600)
            s = pair(o["symbol"])
            out[s] = max(out.get(s, 0), until)
    return out


# ---- open positions and their exits -------------------------------------------------

@dataclass
class Position:
    symbol: str
    qty: float
    value: float
    avg_entry: float
    price: float            # the bid now: what a sell would get
    levels: dict
    source: str             # where the levels came from: registro | fill | posizione
    exit: str | None = None  # stop | take_profit | tempo | segnale


def track(positions: list[dict], stored: dict, m: Market, fills: list[dict], bids: dict, params: dict,
          min_value: float) -> tuple[dict[str, Position], list[str]]:
    """Every position worth selling, with exit levels updated and checked. (positions, notes)

    Levels come from the records; when they are missing, from Alpaca's fills (the entry
    since the position was last flat); when the fills are too old, from the position's own
    average price with the entry time set to now. The stop only moves up, following the
    highest 15m close since entry; the check uses the bid, the price a sell would get now.
    """
    out, notes = {}, []
    for p in positions:
        if p.get("asset_class", "crypto") != "crypto":
            continue
        s = pair(p["symbol"])
        qty, value = f(p.get("qty_available", p.get("qty"))), f(p.get("market_value"))
        price = bids.get(s) or f(p.get("current_price"))
        if value < min_value or qty <= 0 or price <= 0:
            if is_position(value):
                notes.append(f"{s}: {value:.2f}$ sotto l'ordine minimo, uscita impossibile")
            continue
        fr, view = m.frames.get(s), m.views.get(s)
        levels, source = stored.get(s), "registro"
        if not _valid_levels(levels):
            mine = _since_last_exit([x for x in fills if pair(x.get("symbol", "")) == s])
            levels = st.levels_from_fills(mine, fr, params) if fr and mine else None
            source = "fill"
            if levels is None:
                entry = f(p.get("avg_entry_price")) or price
                atr = view.features["atr_1h"] if view else entry * 0.01
                levels = st.open_levels(entry, m.t, atr, params)
                source = "posizione"
            notes.append(f"{s}: livelli d'uscita ricostruiti ({source})")
        high = max([b.c for b in fr.m15.bars if b.t + st.M15 > levels["entry_t"]] if fr else [], default=levels["highest"])
        levels = st.update_levels(levels, high, view.features["atr_1h"] if view else None, params)
        why = st.exit_reason(levels, price, m.t, view.score if view else None, params)
        out[s] = Position(s, qty, value, f(p.get("avg_entry_price")), price, levels, source, why)
    return out, notes


def _since_last_exit(fills: list[dict]) -> list[dict]:
    """The fills of the position held now: the buys after the last sell, when there are any.
    Alpaca keeps the buy fee in the coin, so a full exit leaves dust and the fills alone
    never show the position flat."""
    rows = sorted(fills, key=lambda a: a.get("transaction_time", ""))
    last_sell = max((i for i, a in enumerate(rows) if a.get("side") == "sell"), default=-1)
    after = rows[last_sell + 1:]
    return after if any(a.get("side") == "buy" for a in after) else rows


def _valid_levels(x) -> bool:
    keys = ("entry_price", "entry_t", "atr_at_entry", "highest", "stop", "take_profit")
    return isinstance(x, dict) and all(isinstance(x.get(k), (int, float)) for k in keys)


# ---- the model's prompt -------------------------------------------------------------

def _row(v: st.View) -> str:
    x, c = v.features, v.components
    return (f"{v.symbol:<11} score {v.score:+.2f} | prezzo {x['price']:.6g} | 24h {x['ret_24h_pct']:+.1f}% "
            f"7g {x['ret_7d_pct']:+.1f}% | trend 4h {c['trend_4h']:+.2f} 1h {c['trend_1h']:+.2f} | "
            f"RSI {x['rsi_1h']:.0f} | ADX {x['adx_1h']:.0f} | ATR {x['atr_1h_pct']:.2f}% | "
            f"costo giro {v.cost_rt_pct:.2f}%")


def render_prompt(template: str, *, slot: str, snap: Snapshot, m: Market, candidates: dict[str, float],
                  watch: list[str], explore: dict[str, float] | None = None, positions: dict[str, Position], params: dict, regime,
                  trades: list[dict], lessons: str, prev: dict | None, enabled: bool) -> str:
    cand = []
    for s, usd in candidates.items():
        v = m.views[s]
        lv = st.open_levels(v.features["price"], m.t, v.features["atr_1h"], params)
        meme = " (memecoin)" if is_memecoin(s) else ""
        cand.append(_row(v) + f"\n  -> massimo {usd:.2f}${meme}; stop iniziale {lv['stop']:.6g}, "
                    f"take-profit {lv['take_profit']:.6g}")
    expl = []
    for s, usd in (explore or {}).items():
        v = m.views[s]
        lv = st.open_levels(v.features["price"], m.t, v.features["atr_1h"], params)
        expl.append(_row(v) + f"\n  -> esplorazione, massimo {usd:.2f}$; sotto la regola: {v.entry_reason}; "
                    f"stop iniziale {lv['stop']:.6g}, take-profit {lv['take_profit']:.6g}")
    held = []
    for s, p in positions.items():
        lv = p.levels
        hours = (m.t - lv["entry_t"]) / 3600
        pnl = (p.price / lv["entry_price"] - 1) * 100 if lv["entry_price"] else 0.0
        score = f"{m.views[s].score:+.2f}" if s in m.views else "n/d"
        tag = " [esplorazione]" if lv.get("explore") else ""
        held.append(f"{s:<11}{tag} valore {p.value:.2f}$ | entrata {lv['entry_price']:.6g} | ora {p.price:.6g} "
                    f"({pnl:+.1f}%) | stop {lv['stop']:.6g} | take-profit {lv['take_profit']:.6g} | "
                    f"da {hours:.0f}h | score {score}")
    invested = sum(h.market_value for h in snap.holdings.values())
    account = (f"equity {snap.equity:.2f}$ | liquidità {snap.cash:.2f}$ | investito {invested:.2f}$ "
               f"({invested / snap.equity * 100 if snap.equity else 0:.1f}%) | perdita da ieri {daily_loss_pct(snap):.2f}%")
    reg = (f"{regime.regime} (x{regime.multiplier:g} sulla dimensione)"
           + (f", {regime.error}" if regime.error else ""))
    mk = (f"paniere 7g {m.basket_7d_pct:+.2f}% | crypto in tendenza 4h {m.breadth_pct:.0f}% | "
          f"filtro di mercato {'ok' if m.regime_ok else 'CHIUSO'}") if m.basket_7d_pct is not None else "dati insufficienti"
    # The previous handoff is text a model wrote: cleaned again, so junk written before the
    # cleaning existed does not copy itself forward.
    previous = ({k: clean_text(prev.get(k)) if k in ("market_view", "next_job") else prev.get(k)
                 for k in ("slot", "status", "market_view", "next_job")} if prev else None)
    values = {
        "SLOT": slot,
        "TRADING": "abilitato" if enabled else "acquisti DISABILITATI: ogni acquisto sarà respinto",
        "ACCOUNT": account, "REGIME": reg, "MARKET": mk,
        "CANDIDATES": "\n".join(cand) or "(nessuno)",
        "EXPLORE": "\n".join(expl) or "(nessuno)",
        "WATCH": "\n".join(_row(m.views[s]) for s in watch if s in m.views) or "(nessuna)",
        "POSITIONS": "\n".join(held) or "(nessuna)",
        "TRADES": "\n".join(json.dumps(t, ensure_ascii=False) for t in trades) or "(nessuna)",
        "LESSONS": lessons or "(nessuna lezione ancora)",
        "PREVIOUS": json.dumps(previous, ensure_ascii=False),
        "PARAMS": (f"soglia d'ingresso {params['entry_threshold']}, stop {params['stop_atr_mult']} ATR, "
                   f"take-profit {params['tp_atr_mult']} ATR, uscita sotto score {params['exit_threshold']}, "
                   f"tenuta massima {params['max_hold_hours']:g}h"),
    }
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")
