"""The strategy's arithmetic: features, a score, a shortlist and the exit levels.

Pure functions. The wake loop and the backtest call the same ones, so what the
backtest measured is what runs. The model reads these numbers and proposes;
the gate in risk.py decides; the exits here are enforced every wake whatever
the model says.

Parameters live in config/params.json. PARAM_BOUNDS is the only place that says
what a parameter may be and how far one reflection may move it: `validate_params`
rejects a file outside the bounds, `check_change` rejects a step that is too big.
The hard risk limits are not parameters and are not here.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import indicators as ind
from .config import HARD_CEILINGS, HARD_FLOORS
from .indicators import Bar

M15, H1, H4 = 900, 3600, 14400

# Alpaca crypto fees, tier 1 (under 100k USD of 30-day volume): 0.15% maker, 0.25% taker.
# Market orders take liquidity, so every fill pays the taker fee.
# Source: https://docs.alpaca.markets/docs/crypto-fees (read 2026-09-26).
TAKER_FEE_PCT = 0.25
# Half the bid-ask spread is what a market order pays on top of the fee. When the caller
# has no spread for a symbol, this is assumed per side.
DEFAULT_HALF_SPREAD_PCT = 0.15

# A bar older than this at decision time means the symbol is not trading: no features.
STALE_SECONDS = 2 * H1

# The market filter (regime_ok). Not learnable: it is what keeps the account in cash.
REGIME_MIN_BREADTH = 0.6
REGIME_MIN_SYMBOLS = 5

COMPONENTS = ("trend_4h", "trend_1h", "momentum", "macd", "rsi", "adx", "volume", "bollinger")

# name: (lowest, highest, largest change one reflection may make)
PARAM_BOUNDS: dict[str, tuple[float, float, float]] = {
    **{f"weights.{c}": (-1.0, 2.0, 0.25) for c in COMPONENTS},
    "entry_threshold": (0.2, 0.9, 0.05),
    "exit_threshold": (-0.8, 0.1, 0.1),
    "stop_atr_mult": (1.5, 5.0, 0.5),
    "tp_atr_mult": (2.0, 15.0, 1.0),
    "min_edge_mult": (2.0, 25.0, 1.0),
    "shortlist_size": (3, 15, 2),
    "risk_per_trade_pct": (0.1, 1.5, 0.1),
    "position_pct": (0.5, 8.0, 1.0),
    "cooldown_hours": (0.0, 48.0, 4.0),
    "max_hold_hours": (6.0, 336.0, 24.0),
    "regime_filter": (0, 1, 1),
}
# What a reflection may propose to change. The rest changes only by a reviewed commit.
LEARNABLE = frozenset({*(f"weights.{c}" for c in COMPONENTS), "entry_threshold", "exit_threshold",
                       "stop_atr_mult", "tp_atr_mult", "shortlist_size"})
INTEGER_PARAMS = frozenset({"shortlist_size", "regime_filter"})
# Names that must never be learned. Being absent from PARAM_BOUNDS is what stops them;
# this set only lets the rejection say so plainly.
HARD_LIMIT_NAMES = frozenset(HARD_CEILINGS) | frozenset(HARD_FLOORS) | {
    "symbols", "max_per_coin_pct", "max_per_memecoin_pct", "kill_switch", "trading_enabled",
    "allow_short", "fee_pct"}
assert not HARD_LIMIT_NAMES & set(PARAM_BOUNDS), "a hard limit must never be learnable"

# Exploration (config.HARD_CEILINGS explore_*): the take-profit distance must still cover
# the round trip this many times. Lower than min_edge_mult on purpose, fixed in code.
EXPLORE_MIN_EDGE_MULT = 3.0


class ParamsError(ValueError):
    """The parameters are missing, malformed or outside PARAM_BOUNDS."""


# ---- parameters ------------------------------------------------------------

def _flat(params: dict) -> dict[str, float]:
    out = {k: v for k, v in params.items() if k not in ("weights", "note")}
    out.update({f"weights.{k}": v for k, v in params.get("weights", {}).items()})
    return out


def validate_params(params: dict) -> dict:
    """Return the params if every known key is present, numeric and within bounds; raise otherwise."""
    if not isinstance(params, dict) or not isinstance(params.get("weights"), dict):
        raise ParamsError("params: expected an object with a 'weights' object")
    flat = _flat(params)
    unknown = sorted(set(flat) - set(PARAM_BOUNDS))
    missing = sorted(set(PARAM_BOUNDS) - set(flat))
    if unknown or missing:
        raise ParamsError(f"params: unknown {unknown}, missing {missing}")
    for k, (lo, hi, _) in PARAM_BOUNDS.items():
        v = flat[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            raise ParamsError(f"params: {k} must be a number")
        if k in INTEGER_PARAMS and v != int(v):
            raise ParamsError(f"params: {k} must be an integer")
        if not lo <= v <= hi:
            raise ParamsError(f"params: {k}={v} outside [{lo}, {hi}]")
    if all(v == 0 for v in params["weights"].values()):
        raise ParamsError("params: at least one weight must be non-zero")
    if flat["tp_atr_mult"] <= flat["stop_atr_mult"] * 0.5:
        raise ParamsError("params: tp_atr_mult must be more than half of stop_atr_mult")
    return params


def load_params(path: Path) -> dict:
    return validate_params(json.loads(Path(path).read_text(encoding="utf-8")))


def with_changes(params: dict, changes: dict[str, float]) -> dict:
    """A copy of params with dotted-key changes applied ("weights.macd": 0.5), validated."""
    out = json.loads(json.dumps(params))
    for k, v in changes.items():
        if k.startswith("weights."):
            out["weights"][k.split(".", 1)[1]] = v
        else:
            out[k] = v
    return validate_params(out)


def check_change(current: dict, changes: dict[str, float]) -> list[str]:
    """Why a proposed change is not allowed; empty when it is. The one implementation of
    what a reflection may touch (LEARNABLE), within which bounds and by how much
    (PARAM_BOUNDS). Whether the change is also better is backtest.compare()."""
    reasons = []
    flat = _flat(current)
    for k, v in changes.items():
        if k in HARD_LIMIT_NAMES or k.split(".")[0] in HARD_LIMIT_NAMES:
            reasons.append(f"{k}: limite rigido, non si impara")
            continue
        if k not in PARAM_BOUNDS:
            reasons.append(f"{k}: parametro sconosciuto")
            continue
        if k not in LEARNABLE:
            reasons.append(f"{k}: non modificabile dalla riflessione")
            continue
        lo, hi, step = PARAM_BOUNDS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            reasons.append(f"{k}: non è un numero")
            continue
        cur = flat.get(k)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            reasons.append(f"{k}: valore attuale assente in params.json")
            continue
        if k in INTEGER_PARAMS and v != int(v):
            reasons.append(f"{k}={v}: deve essere un intero")
            continue
        if not lo <= v <= hi:
            reasons.append(f"{k}={v}: fuori dai limiti [{lo}, {hi}]")
        if abs(v - cur) > step + 1e-9:
            reasons.append(f"{k}: passo {abs(v - cur):g} oltre il massimo {step:g} per riflessione")
    if not reasons:
        try:
            with_changes(current, changes)
        except ParamsError as e:
            reasons.append(str(e))
    return reasons


# ---- costs -----------------------------------------------------------------

def side_cost_pct(spread_pct: float | None, fee_pct: float = TAKER_FEE_PCT) -> float:
    """What one market order costs, in % of notional: the taker fee plus half the spread."""
    half = DEFAULT_HALF_SPREAD_PCT if spread_pct is None or spread_pct < 0 else spread_pct / 2
    return fee_pct + half


def round_trip_cost_pct(spread_pct: float | None, fee_pct: float = TAKER_FEE_PCT) -> float:
    return 2 * side_cost_pct(spread_pct, fee_pct)


# ---- series and features ---------------------------------------------------

@dataclass(frozen=True)
class Series:
    """Indicators for one symbol on one timeframe, aligned with its bars."""
    seconds: int
    ends: list[int]  # bar end times: a bar is usable at time T when end <= T
    bars: list[Bar]
    ema9: list
    ema21: list
    ema50: list
    rsi: list
    macd_hist: list
    pct_b: list
    bandwidth: list
    atr: list
    vol_z: list
    adx: list
    di_plus: list
    di_minus: list
    ret_24h: list
    ret_7d: list


def series(bars: Sequence[Bar], seconds: int) -> Series:
    c = [b.c for b in bars]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    v = [b.v for b in bars]
    pb, bw = ind.bollinger(c)
    a, dp, dm = ind.adx(h, lo, c)
    return Series(seconds=seconds, ends=[b.t + seconds for b in bars], bars=list(bars),
                  ema9=ind.ema(c, 9), ema21=ind.ema(c, 21), ema50=ind.ema(c, 50), rsi=ind.rsi(c),
                  macd_hist=ind.macd(c)[2], pct_b=pb, bandwidth=bw, atr=ind.atr(h, lo, c),
                  vol_z=ind.volume_zscore(v), adx=a, di_plus=dp, di_minus=dm,
                  ret_24h=ind.pct_change(c, max(1, 86400 // seconds)),
                  ret_7d=ind.pct_change(c, max(1, 7 * 86400 // seconds)))


@dataclass(frozen=True)
class Frames:
    m15: Series
    h1: Series
    h4: Series


def frames(bars_15m: Sequence[Bar], bars_1h: Sequence[Bar] | None = None,
           bars_4h: Sequence[Bar] | None = None) -> Frames:
    """Series on 15m, 1h and 4h. Higher timeframes are resampled from 15m unless given."""
    return Frames(series(bars_15m, M15),
                  series(bars_1h if bars_1h is not None else ind.resample(bars_15m, H1), H1),
                  series(bars_4h if bars_4h is not None else ind.resample(bars_15m, H4), H4))


def _asof(s: Series, t: int) -> int:
    """Index of the last bar complete at time t, or -1."""
    return bisect.bisect_right(s.ends, t) - 1


def _trend(s: Series, i: int) -> float | None:
    e9, e21, e50 = s.ema9[i], s.ema21[i], s.ema50[i]
    if e50 is None:
        return None
    c = s.bars[i].c
    sgn = lambda x: (x > 0) - (x < 0)
    return (sgn(c - e50) + sgn(e9 - e21) + sgn(e21 - e50)) / 3


def features(fr: Frames, t: int) -> dict | None:
    """Everything the score needs at time t, from bars complete at t. None if data is missing or stale."""
    i15, i1, i4 = _asof(fr.m15, t), _asof(fr.h1, t), _asof(fr.h4, t)
    if min(i15, i1, i4) < 0 or t - fr.m15.ends[i15] > STALE_SECONDS:
        return None
    h1 = fr.h1
    t4, t1 = _trend(fr.h4, i4), _trend(h1, i1)
    needed = (t4, t1, h1.rsi[i1], h1.macd_hist[i1], h1.pct_b[i1], h1.atr[i1], h1.vol_z[i1],
              h1.adx[i1], h1.ret_24h[i1], h1.ret_7d[i1])
    if any(x is None for x in needed):
        return None
    price = fr.m15.bars[i15].c
    b1 = h1.bars[i1]
    return {
        "t": fr.m15.ends[i15], "price": price,
        "trend_4h": t4, "trend_1h": t1,
        "rsi_1h": h1.rsi[i1], "rsi_15m": fr.m15.rsi[i15],
        "macd_hist_1h": h1.macd_hist[i1], "pct_b_1h": h1.pct_b[i1], "bandwidth_1h": h1.bandwidth[i1],
        "atr_1h": h1.atr[i1], "atr_1h_pct": h1.atr[i1] / price * 100,
        "vol_z_1h": h1.vol_z[i1], "bar_dir_1h": (b1.c > b1.o) - (b1.c < b1.o),
        "adx_1h": h1.adx[i1], "di_plus_1h": h1.di_plus[i1], "di_minus_1h": h1.di_minus[i1],
        "ret_24h_pct": h1.ret_24h[i1], "ret_7d_pct": h1.ret_7d[i1],
    }


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _rsi_component(r: float) -> float:
    """Best between 60 and 72 (momentum, not yet stretched); worst when oversold or overbought."""
    if r < 30:
        return -1.0
    if r < 60:
        return -1 + 2 * (r - 30) / 30
    if r <= 72:
        return 1.0
    if r < 85:
        return 1 - 2 * (r - 72) / 13
    return -1.0


def components(f: dict, momentum_rank: float) -> dict[str, float]:
    """Each signal mapped to [-1, 1], positive meaning bullish."""
    di = f["di_plus_1h"] - f["di_minus_1h"]
    return {
        "trend_4h": f["trend_4h"],
        "trend_1h": f["trend_1h"],
        "momentum": 2 * momentum_rank - 1,
        "macd": _clip(2 * f["macd_hist_1h"] / f["atr_1h"]) if f["atr_1h"] else 0.0,
        "rsi": _rsi_component(f["rsi_1h"]),
        "adx": _clip((f["adx_1h"] - 15) / 20, 0.0, 1.0) * ((di > 0) - (di < 0)),
        "volume": _clip(f["vol_z_1h"] / 2) * f["bar_dir_1h"],
        "bollinger": _clip(2 * f["pct_b_1h"] - 1),
    }


def weight_vector(weights: dict[str, float]) -> tuple[float, ...]:
    return tuple(float(weights.get(k, 0.0)) for k in COMPONENTS)


def score_vector(comp: Sequence[float], wv: Sequence[float]) -> float:
    """Weighted average of the components (both in COMPONENTS order), in [-1, 1]."""
    total = sum(abs(w) for w in wv)
    if not total:
        return 0.0
    return sum(w * c for w, c in zip(wv, comp)) / total


def score(comp: dict[str, float], weights: dict[str, float]) -> float:
    return score_vector([comp[k] for k in COMPONENTS], weight_vector(weights))


def momentum_ranks(feats: dict[str, dict]) -> dict[str, float]:
    return ind.rank({s: f["ret_24h_pct"] for s, f in feats.items()})


def expected_move_pct(f: dict, params: dict) -> float:
    """The take-profit distance: the move a trade is built to catch, in %."""
    return params["tp_atr_mult"] * f["atr_1h_pct"]


def entry_ok(sc: float, f: dict, params: dict, cost_rt_pct: float, regime_ok: bool = True) -> tuple[bool, str]:
    """Would the deterministic rule buy? The same rule is the backtest's stand-in for the model."""
    if sc < params["entry_threshold"]:
        return False, f"punteggio {sc:.2f} sotto la soglia {params['entry_threshold']:.2f}"
    need = params["min_edge_mult"] * cost_rt_pct
    move = expected_move_pct(f, params)
    if move < need:
        return False, f"movimento atteso {move:.2f}% sotto {need:.2f}% ({params['min_edge_mult']:g} x costi)"
    if params["regime_filter"] and not regime_ok:
        return False, "mercato debole: paniere in calo sui 7 giorni o poche crypto in tendenza"
    return True, "ok"


def explore_ok(v: View, params: dict) -> tuple[bool, str]:
    """May this symbol be an exploration buy? It failed the entry rule, its 4h trend is
    positive, and its take-profit distance still covers the round trip EXPLORE_MIN_EDGE_MULT
    times. The market filter and the entry threshold do not apply: exploration exists to
    trade where the strict rule would not, in small size (the caps are in risk.gate)."""
    if v.entry:
        return False, "passa già la regola d'ingresso"
    if v.components["trend_4h"] <= 0:
        return False, "tendenza 4h non positiva"
    move, need = expected_move_pct(v.features, params), EXPLORE_MIN_EDGE_MULT * v.cost_rt_pct
    if move < need:
        return False, f"movimento atteso {move:.2f}% sotto {need:.2f}% ({EXPLORE_MIN_EDGE_MULT:g} x costi)"
    return True, "ok"


def regime_ok(feats: dict[str, dict]) -> bool:
    """The market filter for new buys: the equal-weight basket rose over the last 7 days and
    at least REGIME_MIN_BREADTH of the coins are in a 4h uptrend. Too few coins counts as not ok.

    On 2026-04 to 2026-09 data this halved the loss in the falling months and kept most of the
    gain in the rising ones (STRATEGY.md). It only blocks buys; exits never depend on it.
    """
    if len(feats) < REGIME_MIN_SYMBOLS:
        return False
    basket = sum(f["ret_7d_pct"] for f in feats.values()) / len(feats)
    breadth = sum(f["trend_4h"] > 0 for f in feats.values()) / len(feats)
    return basket > 0 and breadth >= REGIME_MIN_BREADTH


@dataclass(frozen=True)
class View:
    """One symbol at one moment, as the model and the wake see it."""
    symbol: str
    features: dict
    components: dict
    score: float
    cost_rt_pct: float
    entry: bool
    entry_reason: str


def analyze(frames_by_symbol: dict[str, Frames], t: int, params: dict,
            spreads_pct: dict[str, float] | None = None, fee_pct: float = TAKER_FEE_PCT) -> dict[str, View]:
    """Score every symbol with usable data at time t."""
    feats = {s: f for s, fr in frames_by_symbol.items() if (f := features(fr, t)) is not None}
    ranks = momentum_ranks(feats)
    reg = regime_ok(feats)
    out = {}
    for s, f in feats.items():
        comp = components(f, ranks[s])
        sc = score(comp, params["weights"])
        cost = round_trip_cost_pct((spreads_pct or {}).get(s), fee_pct)
        ok, why = entry_ok(sc, f, params, cost, reg)
        out[s] = View(s, f, comp, sc, cost, ok, why)
    return out


def shortlist(views: dict[str, View], held: Sequence[str], n: int) -> list[str]:
    """Top n by score, plus every held symbol (its exit must be watched), best first."""
    top = sorted(views, key=lambda s: views[s].score, reverse=True)[:n]
    extra = [s for s in held if s not in top]
    return top + sorted(extra, key=lambda s: views[s].score if s in views else -9, reverse=True)


# ---- sizing and exits -------------------------------------------------------

def size_usd(equity: float, f: dict, params: dict, regime_mult: float = 1.0) -> float:
    """Risk-based size: lose about risk_per_trade_pct of equity if the initial stop is hit,
    never more than position_pct of equity, then scaled by the regime. The gate still caps it."""
    stop_pct = params["stop_atr_mult"] * f["atr_1h_pct"]
    if stop_pct <= 0 or equity <= 0:
        return 0.0
    risk_size = equity * params["risk_per_trade_pct"] / stop_pct
    return max(0.0, min(risk_size, equity * params["position_pct"] / 100) * regime_mult)


def open_levels(entry_price: float, entry_t: int, atr_1h: float, params: dict) -> dict:
    """Exit levels for a new position: an ATR trailing stop and a fixed take-profit."""
    return {"entry_price": entry_price, "entry_t": entry_t, "atr_at_entry": atr_1h,
            "highest": entry_price,
            "stop": entry_price - params["stop_atr_mult"] * atr_1h,
            "take_profit": entry_price + params["tp_atr_mult"] * atr_1h}


def update_levels(levels: dict, price: float, atr_1h: float | None, params: dict) -> dict:
    """Raise the trailing stop under the highest 15m close seen since entry. It never moves down."""
    highest = max(levels["highest"], price)
    atr_now = atr_1h if atr_1h else levels["atr_at_entry"]
    stop = max(levels["stop"], highest - params["stop_atr_mult"] * atr_now)
    return {**levels, "highest": highest, "stop": stop}


def exit_reason(levels: dict, price: float, t: int, sc: float | None, params: dict) -> str | None:
    """Why the position must be closed now, or None. Checked every wake, whatever the model says."""
    if price <= levels["stop"]:
        return "stop"
    if price >= levels["take_profit"]:
        return "take_profit"
    if (t - levels["entry_t"]) >= params["max_hold_hours"] * 3600:
        return "tempo"
    if sc is not None and sc <= params["exit_threshold"]:
        return "segnale"
    return None


def levels_from_fills(fills: Sequence[dict], fr: Frames, params: dict) -> dict | None:
    """Rebuild exit levels when the records are lost, from Alpaca FILL activities for one symbol.

    The position's entry is the volume-weighted buy price since the last time it was flat;
    the highest 15m close since then comes from the bars. None when the fills show no position.
    """
    rows = sorted(fills, key=lambda a: a.get("transaction_time", ""))
    qty, cost, entry_t = 0.0, 0.0, None
    for a in rows:
        q, p = float(a.get("qty") or 0), float(a.get("price") or 0)
        if a.get("side") == "buy":
            if qty <= 1e-12:
                qty, cost, entry_t = 0.0, 0.0, ind.parse_time(a["transaction_time"])
            qty, cost = qty + q, cost + q * p
        elif a.get("side") == "sell" and qty > 0:
            avg = cost / qty
            qty = max(0.0, qty - q)
            cost = avg * qty
    if qty <= 1e-12 or entry_t is None:
        return None
    entry = cost / qty
    i = _asof(fr.h1, entry_t + H1)  # the bar containing the first buy
    a = fr.h1.atr[i] if i >= 0 and fr.h1.atr[i] else entry * 0.01
    lv = open_levels(entry, entry_t, a, params)
    high = max((b.c for b in fr.m15.bars if b.t >= entry_t), default=entry)
    j = len(fr.h1.bars) - 1
    return update_levels(lv, high, fr.h1.atr[j] if j >= 0 else None, params)
