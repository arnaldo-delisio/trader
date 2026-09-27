"""Indicators against values computed by hand, plus the degenerate inputs: too few bars,
flat prices, zero volume."""

from __future__ import annotations

import pytest

from trader import indicators as ind
from trader.indicators import Bar


def approx(xs, rel=1e-6):
    return [None if x is None else pytest.approx(x, rel=rel) for x in xs]


def test_sma_by_hand():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_ema_seeds_with_the_average_then_smooths():
    # period 3: seed (1+2+3)/3 = 2, k = 0.5: 4*.5 + 2*.5 = 3, 5*.5 + 3*.5 = 4
    assert ind.ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_too_few_bars_give_no_value_instead_of_a_wrong_one():
    assert ind.ema([1, 2], 3) == [None, None]
    assert ind.rsi([1, 2], 14) == [None, None]
    assert ind.atr([1], [1], [1], 14) == [None]
    assert ind.adx([1, 2], [1, 2], [1, 2], 14)[0] == [None, None]
    assert ind.bollinger([1, 2], 20) == ([None, None], [None, None])
    assert ind.volume_zscore([1, 2], 20) == [None, None]
    assert ind.macd([1, 2, 3]) == ([None] * 3, [None] * 3, [None] * 3)
    assert ind.pct_change([1, 2], 5) == [None, None]
    assert ind.ema([], 3) == []


def test_rsi_by_hand():
    # changes +1, +1, -1 with period 2: seed gains 1, losses 0 -> 100;
    # then gains (1 + 0) / 2 = .5, losses (0 + 1) / 2 = .5 -> 50
    assert ind.rsi([1, 2, 3, 2], 2) == [None, None, 100, 50]


def test_rsi_of_flat_prices_is_50_not_a_division_by_zero():
    assert ind.rsi([5, 5, 5, 5], 2) == [None, None, 50, 50]


def test_rsi_only_falling_is_zero():
    assert ind.rsi([3, 2, 1], 2)[-1] == 0


def test_macd_by_hand():
    # fast 2, slow 3, signal 2 on 1, 2, 4, 8, 16 (worked out on paper)
    line, sig, hist = ind.macd([1, 2, 4, 8, 16], 2, 3, 2)
    assert line == approx([None, None, 5 / 6, 1.2222222, 2.2129630])
    assert sig == approx([None, None, None, 1.0277778, 1.8179012])
    assert hist == approx([None, None, None, 0.1944444, 0.3950617])


def test_bollinger_by_hand():
    # period 2, width 1 on 1, 3: mean 2, sd 1, bands 1..3; 3 sits on the upper band
    pct_b, bw = ind.bollinger([1, 3], 2, 1.0)
    assert pct_b == [None, 1.0]
    assert bw == [None, 1.0]


def test_bollinger_of_flat_prices():
    pct_b, bw = ind.bollinger([4, 4, 4], 2)
    assert pct_b == [None, 0.5, 0.5]
    assert bw == [None, 0.0, 0.0]


def test_true_range_uses_the_previous_close():
    assert ind.true_range([2, 3], [1, 2], [1.5, 2.8]) == [1, 1.5]


def test_atr_by_hand():
    # true ranges 1, 1.5, 2; the first has no previous close and is skipped; (1.5 + 2) / 2
    assert ind.atr([2, 3, 4], [1, 2, 2], [1.5, 2.8, 3], 2) == [None, None, 1.75]


def test_adx_by_hand():
    h = [10, 12, 13, 12, 14]
    lo = [9, 10, 11, 10, 12]
    c = [9.5, 11.5, 12.5, 10.5, 13.5]
    a, dp, dm = ind.adx(h, lo, c, 2)
    assert dp == approx([None, None, 200 / 3, 31.578947, 46.808511])
    assert dm == approx([None, None, 0.0, 21.052632, 8.510638])
    assert a == approx([None, None, None, 60.0, 64.615385])


def test_adx_of_flat_prices_is_zero():
    a, dp, dm = ind.adx([5] * 6, [5] * 6, [5] * 6, 2)
    assert a[-1] == 0 and dp[-1] == 0 and dm[-1] == 0


def test_volume_zscore_by_hand():
    assert ind.volume_zscore([1, 3, 5], 2) == [None, None, 3.0]
    assert ind.volume_zscore([1, 3, 2], 2) == [None, None, 0.0]


def test_zero_volume_gives_zero_not_a_division_by_zero():
    assert ind.volume_zscore([0, 0, 0, 0], 2) == [None, None, 0.0, 0.0]


def test_pct_change():
    assert ind.pct_change([100, 110, 121], 1) == approx([None, 10.0, 10.0])
    assert ind.pct_change([0, 5], 1) == [None, None]


def test_rank_is_cross_sectional_and_shares_ties():
    assert ind.rank({"a": 1, "b": 2, "c": 3}) == {"a": 0, "b": 0.5, "c": 1}
    assert ind.rank({"a": 1, "b": 1, "c": 2}) == {"a": 0.25, "b": 0.25, "c": 1}
    assert ind.rank({"a": 7}) == {"a": 0.5}
    assert ind.rank({}) == {}


def test_parse_bars_sorts_and_drops_bad_rows():
    rows = [{"t": "2026-09-01T00:15:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3},
            {"t": "2026-09-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1},
            {"t": "2026-09-01T00:30:00Z", "o": 1, "h": 1, "l": 2, "c": 1, "v": 1},  # high below low
            {"t": "2026-09-01T00:45:00Z", "o": 1, "h": 1, "l": 1, "c": 0, "v": 1},  # zero price
            {"t": "garbage"}]
    bars = ind.parse_bars(rows)
    assert [b.c for b in bars] == [1, 2]
    assert bars[0].v == 0.0
    assert bars[1].t - bars[0].t == 900


def test_resample_aggregates_on_aligned_buckets():
    base = 1_788_220_800  # a multiple of 3600
    m15 = [Bar(base + i * 900, 10 + i, 11 + i, 9 + i, 10.5 + i, 1.0) for i in range(5)]
    h1 = ind.resample(m15, 3600)
    assert h1[0] == Bar(base, 10, 14, 9, 13.5, 4.0)
    assert h1[1] == Bar(base + 3600, 14, 15, 13, 14.5, 1.0)


def test_completed_drops_the_bar_still_in_progress():
    bars = [Bar(0, 1, 1, 1, 1, 1), Bar(3600, 1, 1, 1, 1, 1)]
    assert ind.completed(bars, 3600, now=7199) == bars[:1]
    assert ind.completed(bars, 3600, now=7200) == bars


def test_parse_time_accepts_z_offsets_and_nanoseconds():
    assert ind.parse_time("2026-09-01T00:00:00Z") == 1_788_220_800
    assert ind.parse_time("2026-09-01T00:00:00.123456789Z") == 1_788_220_800
    assert ind.parse_time("2026-09-01T02:00:00+02:00") == 1_788_220_800
