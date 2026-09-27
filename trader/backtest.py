"""Event-driven backtest of the deterministic part of the strategy, on 15-minute bars.

At every 15-minute step (the end of a bar) the simulation does what a wake does:
it updates and checks the exit levels of every position (strategy.update_levels,
strategy.exit_reason), then buys the best-scoring symbols that pass
strategy.entry_ok, as a stand-in for the model, within the hard caps.

What it models: the taker fee on every fill, half the bid-ask spread per side,
a fill one bar after the decision (the wake runs late and the order is market),
the portfolio caps, the daily loss stop and a cooldown after each exit.
What it does not: the model's judgment, partial fills, missed wakes, Jev's
regime (a fixed multiplier stands in for it), liquidity beyond the spread.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import strategy as st
from .indicators import parse_bars

START_EQUITY = 10_000.0


@dataclass(frozen=True)
class Costs:
    fee_pct: float = st.TAKER_FEE_PCT
    slippage_pct: float | None = None  # per side; None: half of each symbol's spread
    spreads_pct: dict = field(default_factory=dict)  # symbol -> full bid-ask spread in %

    def fee(self) -> float:
        return self.fee_pct / 100

    def slip(self, symbol: str) -> float:
        if self.slippage_pct is not None:
            return self.slippage_pct / 100
        return (st.side_cost_pct(self.spreads_pct.get(symbol), 0.0)) / 100

    def round_trip_pct(self, symbol: str) -> float:
        return 2 * (self.fee_pct + self.slip(symbol) * 100)


@dataclass(frozen=True)
class Caps:
    """The design's hard limits, as the simulation applies them. The live gate is risk.py."""
    max_invested_pct: float = 60.0
    max_position_pct: float = 8.0
    max_meme_pct: float = 3.0
    max_positions: int = 10
    daily_loss_pct: float = 5.0
    max_orders_per_wake: int = 3
    min_order_usd: float = 10.0
    memecoins: frozenset = frozenset()


@dataclass
class Prepared:
    """Features computed once, reused by every run over the same data."""
    grid: list[int]  # decision times: 15m bar ends
    symbols: list[str]
    # per step: symbol -> (price, atr_1h, atr_1h_pct, components tuple in st.COMPONENTS order)
    rows: list[dict[str, tuple]]
    regime: list[bool]
    bar_t: dict[str, list[int]]  # 15m bar starts per symbol, for fills
    bar_c: dict[str, list[float]]

    def first_ready(self) -> int:
        """First step with features for at least half the symbols (the indicators need warm-up)."""
        need = max(1, len(self.symbols) // 2)
        return next((g for g, r in enumerate(self.rows) if len(r) >= need), len(self.grid))


def prepare(raw: dict[str, list[dict]]) -> Prepared:
    """From Alpaca 15m bar dicts per symbol to per-step features for every symbol."""
    bars = {s: parse_bars(rows) for s, rows in raw.items()}
    bars = {s: b for s, b in bars.items() if b}
    fr = {s: st.frames(b) for s, b in bars.items()}
    grid = sorted({b.t + st.M15 for bs in bars.values() for b in bs})
    rows, regime = [], []
    for t in grid:
        feats = {s: f for s, x in fr.items() if (f := st.features(x, t)) is not None}
        ranks = st.momentum_ranks(feats)
        row = {}
        for s, f in feats.items():
            c = st.components(f, ranks[s])
            row[s] = (f["price"], f["atr_1h"], f["atr_1h_pct"], tuple(c[k] for k in st.COMPONENTS))
        rows.append(row)
        regime.append(st.regime_ok(feats))
    return Prepared(grid, sorted(bars), rows, regime,
                    {s: [x.t for x in b] for s, b in bars.items()},
                    {s: [x.c for x in b] for s, b in bars.items()})


@dataclass
class Result:
    trades: list[dict]
    equity: list[tuple[int, float]]
    metrics: dict

    def per_symbol(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for tr in self.trades:
            p = out.setdefault(tr["symbol"], {"trades": 0, "wins": 0, "pnl_usd": 0.0, "fees_usd": 0.0})
            p["trades"] += 1
            p["wins"] += tr["pnl_usd"] > 0
            p["pnl_usd"] += tr["pnl_usd"]
            p["fees_usd"] += tr["fees_usd"]
        return out


def daily_loss_blocks(equity: float, day_start: float, caps: Caps) -> bool:
    """New buys stop once the day's loss passes the limit; exits go on."""
    return day_start > 0 and equity < day_start * (1 - caps.daily_loss_pct / 100)


def buy_usd(want: float, symbol: str, *, equity: float, invested: float, cash: float,
            positions: int, caps: Caps) -> float:
    """How much of a wanted buy the caps allow; 0 when the buy must not happen."""
    if positions >= caps.max_positions or equity <= 0:
        return 0.0
    cap_pct = caps.max_meme_pct if symbol in caps.memecoins else caps.max_position_pct
    n = min(want, equity * cap_pct / 100, equity * caps.max_invested_pct / 100 - invested, cash)
    return n if n >= caps.min_order_usd else 0.0


def _fill_price(p: Prepared, symbol: str, t: int, fallback: float) -> float:
    """The close of the first bar that starts at or after t: the order lands during that bar."""
    i = bisect.bisect_left(p.bar_t[symbol], t)
    return p.bar_c[symbol][i] if i < len(p.bar_t[symbol]) else fallback


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M")


def run(prep: Prepared, params: dict, start: int = 0, end: int | None = None, *,
        caps: Caps | None = None, costs: Costs | None = None, regime_mult: float = 0.7,
        equity0: float = START_EQUITY) -> Result:
    """Simulate steps [start, end) of the prepared grid with these parameters."""
    st.validate_params(params)
    caps = caps or Caps()
    costs = costs or Costs()
    end = len(prep.grid) if end is None else end
    wv = st.weight_vector(params["weights"])
    fee = costs.fee()
    cash = equity0
    pos: dict[str, dict] = {}
    last_price: dict[str, float] = {}
    cooldown: dict[str, int] = {}
    trades: list[dict] = []
    curve: list[tuple[int, float]] = []
    fees_paid = slip_paid = 0.0
    day, day_start = None, equity0
    peak, max_dd = equity0, 0.0
    first_price: dict[str, float] = {}

    def sell(sym: str, t: int, reason: str, mark: float) -> None:
        nonlocal cash, fees_paid, slip_paid
        ps = pos.pop(sym)
        px = _fill_price(prep, sym, t, mark)
        s = costs.slip(sym)
        gross = ps["qty"] * px
        proceeds = gross * (1 - s)
        f = proceeds * fee
        cash += proceeds - f
        fees_paid += f
        slip_paid += gross * s
        pnl = proceeds - f - ps["cost_usd"]
        trades.append({"symbol": sym, "entry_t": _iso(ps["levels"]["entry_t"]), "exit_t": _iso(t),
                       "entry_price": ps["levels"]["entry_price"], "exit_price": px, "reason": reason,
                       "pnl_usd": pnl, "pnl_pct": pnl / ps["cost_usd"] * 100,
                       "fees_usd": ps["fee_usd"] + f})
        cooldown[sym] = t + int(params["cooldown_hours"] * 3600)

    for g in range(start, end):
        t = prep.grid[g]
        row = prep.rows[g]
        for s, r in row.items():
            last_price[s] = r[0]
            first_price.setdefault(s, r[0])
        equity = cash + sum(ps["qty"] * last_price.get(s, ps["levels"]["entry_price"]) for s, ps in pos.items())
        d = t // 86400
        if d != day:
            day, day_start = d, equity
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        curve.append((t, equity))

        # exits first, every step, whatever else happens
        for s in list(pos):
            r = row.get(s)
            if r is None:
                continue
            sc = st.score_vector(r[3], wv)
            pos[s]["levels"] = st.update_levels(pos[s]["levels"], r[0], r[1], params)
            why = st.exit_reason(pos[s]["levels"], r[0], t, sc, params)
            if why:
                sell(s, t, why, r[0])

        if daily_loss_blocks(equity, day_start, caps):
            continue
        invested = sum(ps["qty"] * last_price.get(s, 0.0) for s, ps in pos.items())
        cands = []
        for s, r in row.items():
            if s in pos or cooldown.get(s, 0) > t:
                continue
            sc = st.score_vector(r[3], wv)
            ok, _ = st.entry_ok(sc, {"atr_1h_pct": r[2]}, params, costs.round_trip_pct(s), prep.regime[g])
            if ok:
                cands.append((sc, s))
        cands.sort(reverse=True)
        for sc, s in cands[: caps.max_orders_per_wake]:
            price, atr_abs, atr_pct, _ = row[s]
            want = st.size_usd(equity, {"atr_1h_pct": atr_pct}, params, regime_mult)
            n = buy_usd(want, s, equity=equity, invested=invested, cash=cash, positions=len(pos), caps=caps)
            if n <= 0:
                continue
            px = _fill_price(prep, s, t, price)
            sl = costs.slip(s)
            f = n * fee
            qty = (n - f) / (px * (1 + sl))
            cash -= n
            invested += n
            fees_paid += f
            slip_paid += (n - f) * sl
            lv = st.open_levels(px, t, atr_abs, params)
            pos[s] = {"qty": qty, "cost_usd": n, "fee_usd": f, "levels": lv}

    t_end = prep.grid[end - 1] if end > start else 0
    for s in list(pos):
        sell(s, t_end, "fine", last_price.get(s, pos[s]["levels"]["entry_price"]))
    final = cash
    wins = sum(1 for tr in trades if tr["pnl_usd"] > 0)
    bench = [last_price[s] / first_price[s] - 1 for s in first_price if s in last_price]
    net = (final / equity0 - 1) * 100
    metrics = {
        "start": _iso(prep.grid[start]) if end > start else "", "end": _iso(t_end),
        "net_return_pct": net, "max_drawdown_pct": max_dd, "trades": len(trades),
        "win_rate_pct": wins / len(trades) * 100 if trades else 0.0,
        "fees_usd": fees_paid, "slippage_usd": slip_paid,
        "gross_return_pct": net + (fees_paid + slip_paid) / equity0 * 100,
        "benchmark_pct": sum(bench) / len(bench) * 100 if bench else 0.0,
        "final_equity": final,
    }
    return Result(trades, curve, metrics)


# A candidate may deepen the max drawdown by at most this many points and still pass.
DRAWDOWN_TOLERANCE_PCT = 1.0


def verdict(current: dict, candidate: dict) -> tuple[bool, str]:
    """The acceptance rule on two runs' metrics: the candidate must not have a lower net
    return, and its max drawdown must not be more than DRAWDOWN_TOLERANCE_PCT points deeper."""
    a, b = current, candidate
    detail = (f"attuali {a['net_return_pct']:+.2f}% (drawdown {a['max_drawdown_pct']:.2f}%), "
              f"proposti {b['net_return_pct']:+.2f}% (drawdown {b['max_drawdown_pct']:.2f}%), "
              f"{a['start']} -> {a['end']} UTC")
    if b["net_return_pct"] < a["net_return_pct"]:
        return False, "rendimento netto peggiore: " + detail
    if b["max_drawdown_pct"] > a["max_drawdown_pct"] + DRAWDOWN_TOLERANCE_PCT:
        return False, "drawdown peggiore: " + detail
    return True, detail


def compare(prep: Prepared, current: dict, candidate: dict, start: int, end: int | None = None,
            **kw) -> tuple[bool, str]:
    """Walk-forward check for a reflection: both parameter sets on the same recent window,
    net of costs, judged by verdict()."""
    return verdict(run(prep, current, start, end, **kw).metrics, run(prep, candidate, start, end, **kw).metrics)


def judge(raw: dict[str, list[dict]], days: float = 14.0, **kw):
    """judge(current, candidate) -> (ok, detail): compare() over the last `days` of these
    15m bars. The reflection's acceptance rule (learn.decide_changes).

    Features are prepared once, each call only replays the trades. Needs about 9 days of
    history before the window for the 4h indicators to warm up.
    """
    prep = prepare(raw)
    first = prep.first_ready()
    start = max(first, len(prep.grid) - int(days * 86400 / st.M15))

    def judge_(current: dict, candidate: dict) -> tuple[bool, str]:
        return compare(prep, current, candidate, start, **kw)

    return judge_
