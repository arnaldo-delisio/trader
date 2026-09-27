"""Technical indicators as pure functions over plain lists.

Every function returns a full series aligned with its input, with None where the
indicator is not defined yet (too few bars). The backtest computes each series
once and reads it at every step; a live wake reads the last value. Same code,
same numbers.

Conventions for degenerate input, chosen so nothing divides by zero:
- flat prices: RSI 50, Bollinger %B 0.5 and bandwidth 0, ADX 0;
- zero or constant volume: volume z-score 0.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

Num = float | None


class Bar(NamedTuple):
    t: int  # bar start, epoch seconds UTC
    o: float
    h: float
    l: float
    c: float
    v: float


def parse_time(s: str) -> int:
    """RFC 3339 to epoch seconds. Fractions beyond microseconds (Alpaca can send nanoseconds) are cut."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return int(datetime.fromisoformat(s).timestamp())


def parse_bars(rows: Sequence[dict]) -> list[Bar]:
    """Alpaca bar dicts ({"t","o","h","l","c","v"}) to Bars sorted by time, bad rows dropped."""
    out = []
    for r in rows:
        try:
            b = Bar(parse_time(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                    float(r.get("v") or 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if b.c > 0 and b.h >= b.l:
            out.append(b)
    out.sort(key=lambda b: b.t)
    return out


def resample(bars: Sequence[Bar], seconds: int) -> list[Bar]:
    """Aggregate bars into buckets aligned to multiples of `seconds` since the epoch."""
    out: list[Bar] = []
    for b in bars:
        start = b.t - b.t % seconds
        if out and out[-1].t == start:
            p = out[-1]
            out[-1] = Bar(start, p.o, max(p.h, b.h), min(p.l, b.l), b.c, p.v + b.v)
        else:
            out.append(Bar(start, b.o, b.h, b.l, b.c, b.v))
    return out


def completed(bars: Sequence[Bar], seconds: int, now: int) -> list[Bar]:
    """Drop bars still in progress at `now` (a bar is complete once its end has passed)."""
    return [b for b in bars if b.t + seconds <= now]


def sma(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: Sequence[float], period: int) -> list[Num]:
    """Exponential moving average, seeded with the simple average of the first `period` values."""
    out: list[Num] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def _wilder(values: Sequence[float], period: int, first: int) -> list[Num]:
    """Wilder smoothing of values[first:], seeded with their simple average over `period`."""
    out: list[Num] = [None] * len(values)
    if len(values) - first < period:
        return out
    a = sum(values[first:first + period]) / period
    out[first + period - 1] = a
    for i in range(first + period, len(values)):
        a = (a * (period - 1) + values[i]) / period
        out[i] = a
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder's RSI. Defined from index `period` (it needs `period` changes)."""
    n = len(closes)
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    ag, al = _wilder(gains, period, 1), _wilder(losses, period, 1)
    out: list[Num] = [None] * n
    for i in range(n):
        if ag[i] is None:
            continue
        if al[i] == 0:
            out[i] = 50.0 if ag[i] == 0 else 100.0
        else:
            out[i] = 100 - 100 / (1 + ag[i] / al[i])
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[list[Num], list[Num], list[Num]]:
    """MACD line, signal line and histogram."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line: list[Num] = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_tail = ema([v for v in line[first:]], signal)  # type: ignore[misc]
    sig: list[Num] = [None] * first + sig_tail
    hist: list[Num] = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: Sequence[float], period: int = 20, width: float = 2.0) -> tuple[list[Num], list[Num]]:
    """%B (0 at the lower band, 1 at the upper) and bandwidth ((upper - lower) / middle)."""
    n = len(closes)
    pct_b: list[Num] = [None] * n
    bw: list[Num] = [None] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        lo, hi = m - width * sd, m + width * sd
        pct_b[i] = 0.5 if hi == lo else (closes[i] - lo) / (hi - lo)
        bw[i] = (hi - lo) / m if m else 0.0
    return pct_b, bw


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[0] - lows[0])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder's average true range. The first true range has no previous close, so it is skipped."""
    return _wilder(true_range(highs, lows, closes), period, 1)


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
        ) -> tuple[list[Num], list[Num], list[Num]]:
    """Wilder's ADX with +DI and -DI. ADX is defined from index 2 * period - 1."""
    n = len(closes)
    plus_dm, minus_dm = [0.0] * n, [0.0] * n
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr = true_range(highs, lows, closes)
    s_tr, s_p, s_m = _wilder(tr, period, 1), _wilder(plus_dm, period, 1), _wilder(minus_dm, period, 1)
    di_p: list[Num] = [None] * n
    di_m: list[Num] = [None] * n
    dx = [0.0] * n
    for i in range(n):
        if s_tr[i] is None:
            continue
        di_p[i] = 100 * s_p[i] / s_tr[i] if s_tr[i] else 0.0
        di_m[i] = 100 * s_m[i] / s_tr[i] if s_tr[i] else 0.0
        tot = di_p[i] + di_m[i]
        dx[i] = 100 * abs(di_p[i] - di_m[i]) / tot if tot else 0.0
    return _wilder(dx, period, period), di_p, di_m


def volume_zscore(volumes: Sequence[float], period: int = 20) -> list[Num]:
    """How unusual this bar's volume is against the `period` bars before it."""
    out: list[Num] = [None] * len(volumes)
    for i in range(period, len(volumes)):
        w = volumes[i - period:i]
        m = sum(w) / period
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        out[i] = 0.0 if sd == 0 else (volumes[i] - m) / sd
    return out


def pct_change(values: Sequence[float], n: int) -> list[Num]:
    return [None if i < n or not values[i - n] else (values[i] / values[i - n] - 1) * 100
            for i in range(len(values))]


def rank(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional rank in [0, 1]: 1 for the highest value, 0 for the lowest, ties share the average."""
    if not values:
        return {}
    if len(values) == 1:
        return {k: 0.5 for k in values}
    ordered = sorted(values, key=values.get)
    out: dict[str, float] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and values[ordered[j + 1]] == values[ordered[i]]:
            j += 1
        r = (i + j) / 2 / (len(ordered) - 1)
        for k in ordered[i:j + 1]:
            out[k] = r
        i = j + 1
    return out
