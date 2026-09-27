"""Download 15-minute crypto bars from Alpaca and backtest the deterministic strategy on them.

    uv run python scripts/backtest.py fetch --days 60          # cache under data/ (gitignored)
    uv run python scripts/backtest.py run                       # config/params.json, train and test
    uv run python scripts/backtest.py run --params other.json
    uv run python scripts/backtest.py sweep                     # grid on train, report the best on test

Crypto market data does not need keys; ALPACA_API_KEY and ALPACA_SECRET_KEY are used if set
(they raise the rate limit). The live asset list comes from the paper trading API, which does
need them: without keys the configured universe is used as is.
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import multiprocessing
import os
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader import backtest as bt
from trader import strategy
from trader.broker import Alpaca, urllib_transport
from trader.config import EXCLUDED_BASES, HARD_SYMBOLS, MEMECOIN_BASES

DATA = ROOT / "data"
CACHE = DATA / "bars_15m.json"
SPREADS = DATA / "spreads.json"

# The universe and the memecoins are the live wake's (trader/config.py): one list.
UNIVERSE = [s.split("/")[0] for s in HARD_SYMBOLS]
EXCLUDED = EXCLUDED_BASES
MEMECOINS = MEMECOIN_BASES


def _transport(method, url, headers, body):
    """Drop empty key headers, so the public data endpoint works without an account."""
    return urllib_transport(method, url, {k: v for k, v in headers.items() if v}, body)


def client() -> Alpaca:
    return Alpaca(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", ""),
                  transport=_transport)


def live_universe(api: Alpaca) -> list[str]:
    wanted = [f"{b}/USD" for b in UNIVERSE if b not in EXCLUDED]
    if not os.environ.get("ALPACA_API_KEY"):
        print("nessuna chiave: uso l'universo configurato senza controllare /v2/assets")
        return wanted
    assets = api._get(f"{api.base}/v2/assets", {"asset_class": "crypto", "status": "active"})
    live = {a["symbol"] for a in assets if a.get("tradable")}
    missing = sorted(set(wanted) - live)
    if missing:
        print("non negoziabili ora, esclusi:", ", ".join(missing))
    return [s for s in wanted if s in live]


def fetch(days: int) -> None:
    api = client()
    symbols = live_universe(api)
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    bars = {}
    for s in symbols:  # one symbol at a time: a page holds at most about 1000 rows in practice
        bars.update(api.bars([s], "15Min", start.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=10000, max_pages=100))
        print(f"{s:12} {len(bars.get(s, [])):6} barre", flush=True)
    DATA.mkdir(exist_ok=True)
    ignore = DATA / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")  # the cache never reaches git, this file included
    CACHE.write_text(json.dumps({"fetched_at": end.isoformat(), "start": start.isoformat(),
                                 "timeframe": "15Min", "bars": bars}))
    SPREADS.write_text(json.dumps(sample_spreads(api, symbols), indent=1))
    print(f"salvato {CACHE.relative_to(ROOT)}: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")


def sample_spreads(api: Alpaca, symbols: list[str], samples: int = 5, pause: float = 20) -> dict[str, float]:
    """Median bid-ask spread in % of mid over a few quotes: what a market order pays on top of the fee."""
    seen: dict[str, list[float]] = {s: [] for s in symbols}
    for k in range(samples):
        if k:
            time.sleep(pause)
        for s, q in api.latest_quotes(symbols).items():
            ap, bp = float(q.get("ap") or 0), float(q.get("bp") or 0)
            if ap > 0 and bp > 0:
                seen[s].append((ap - bp) / ((ap + bp) / 2) * 100)
    return {s: round(statistics.median(v), 4) for s, v in seen.items() if v}


def load_cache() -> dict[str, list[dict]]:
    if not CACHE.exists():
        sys.exit("nessun dato: lancia prima `scripts/backtest.py fetch`")
    return json.loads(CACHE.read_text())["bars"]


def caps_for(args) -> bt.Caps:
    return bt.Caps(memecoins=frozenset(f"{m}/USD" for m in MEMECOINS))


def cost_for(args, spreads: dict) -> bt.Costs:
    return bt.Costs(fee_pct=args.fee, slippage_pct=args.slippage, spreads_pct=spreads)


def print_report(title: str, r: bt.Result) -> None:
    m = r.metrics
    print(f"\n== {title}: {m['start']} -> {m['end']} UTC ==")
    print(f"rendimento netto {m['net_return_pct']:+.2f}%   max drawdown {m['max_drawdown_pct']:.2f}%   "
          f"operazioni {m['trades']}   vinte {m['win_rate_pct']:.0f}%   commissioni {m['fees_usd']:.2f} $   "
          f"lordo prima delle commissioni {m['gross_return_pct']:+.2f}%   buy&hold paniere {m['benchmark_pct']:+.2f}%")
    per = r.per_symbol()
    for s in sorted(per, key=lambda k: per[k]["pnl_usd"]):
        p = per[s]
        print(f"  {s:12} op {p['trades']:3}  vinte {p['wins']:3}  P&L netto {p['pnl_usd']:+9.2f} $  "
              f"commissioni {p['fees_usd']:7.2f} $")


def prepared() -> tuple[bt.Prepared, dict]:
    raw = load_cache()
    spreads = json.loads(SPREADS.read_text()) if SPREADS.exists() else {}
    return bt.prepare(raw), spreads


def split(prep: bt.Prepared, train_frac: float, cut: str | None = None) -> tuple[int, int, int]:
    """Train is the first part of the usable period, test the rest: the test never sees the future.
    `cut` (YYYY-MM-DD, UTC) fixes the boundary by date instead of by fraction."""
    first = prep.first_ready()
    if cut:
        t = int(datetime.strptime(cut, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        return first, max(first, bisect.bisect_left(prep.grid, t)), len(prep.grid)
    return first, first + int((len(prep.grid) - first) * train_frac), len(prep.grid)


def cmd_run(args) -> None:
    prep, spreads = prepared()
    params = strategy.load_params(Path(args.params))
    first, cut, last = split(prep, args.train, args.cut)
    for title, a, b in (("train", first, cut), ("test", cut, last), ("tutto", first, last)):
        r = bt.run(prep, params, a, b, caps=caps_for(args), costs=cost_for(args, spreads))
        print_report(title, r)
        if args.trades and title != "tutto":
            for tr in r.trades:
                print(f"    {tr['entry_t']} -> {tr['exit_t']}  {tr['symbol']:10} {tr['reason']:12} "
                      f"{tr['pnl_pct']:+6.2f}%")


GRID = {  # the grid behind config/params.json (STRATEGY.md), weight presets aside
    "entry_threshold": [0.6, 0.7, 0.8],
    "stop_atr_mult": [3.0, 4.0, 5.0],
    "tp_atr_mult": [6.0, 9.0, 12.0],
    "min_edge_mult": [6.0, 8.0, 10.0, 12.0],
    "cooldown_hours": [12.0, 24.0, 48.0],
}


_SWEEP: dict = {}  # shared with forked workers: the prepared data is built once


def _sweep_one(c: dict):
    w = _SWEEP
    try:
        p = strategy.with_changes(w["base"], c)
    except strategy.ParamsError:
        return None
    train = bt.run(w["prep"], p, w["first"], w["cut"], **w["kw"]).metrics
    test = bt.run(w["prep"], p, w["cut"], w["last"], **w["kw"]).metrics
    return c, train, test


def cmd_sweep(args) -> None:
    """Rank every grid point on the train period only; the test column is shown, never used."""
    prep, spreads = prepared()
    first, cut, last = split(prep, args.train, args.cut)
    grid = json.loads(args.grid) if args.grid else GRID
    _SWEEP.update(prep=prep, base=strategy.load_params(Path(args.params)), first=first, cut=cut, last=last,
                  kw={"caps": caps_for(args), "costs": cost_for(args, spreads)})
    combos = [dict(zip(grid, combo)) for combo in itertools.product(*grid.values())]
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(args.jobs) as pool:
        rows = [r for r in pool.map(_sweep_one, combos) if r]
    rows.sort(key=lambda x: x[1]["net_return_pct"] - 0.5 * x[1]["max_drawdown_pct"], reverse=True)
    print(f"train {bt._iso(prep.grid[first])} -> {bt._iso(prep.grid[cut])}, test fino a {bt._iso(prep.grid[-1])}; "
          f"migliori {args.top} su {len(rows)} (ordinati per rendimento - 0,5 x drawdown sul train)")
    for c, m, t in rows[: args.top]:
        print(f"{json.dumps(c)}\n   train {m['net_return_pct']:+6.2f}% dd {m['max_drawdown_pct']:5.2f}% "
              f"op {m['trades']:3}  |  test {t['net_return_pct']:+6.2f}% dd {t['max_drawdown_pct']:5.2f}% "
              f"op {t['trades']:3}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="scarica le barre da 15 minuti")
    f.add_argument("--days", type=int, default=60)
    for name in ("run", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--params", default=str(ROOT / "config" / "params.json"))
        p.add_argument("--train", type=float, default=0.6, help="quota iniziale del periodo usata come train")
        p.add_argument("--fee", type=float, default=bt.Costs().fee_pct, help="commissione per lato, in %%")
        p.add_argument("--slippage", type=float, default=None,
                       help="slippage per lato in %%; senza, metà dello spread misurato per simbolo")
        p.add_argument("--cut", help="data di confine train/test, AAAA-MM-GG (UTC)")
        p.add_argument("--top", type=int, default=8)
        p.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="processi per sweep")
        p.add_argument("--trades", action="store_true", help="elenca le operazioni")
        p.add_argument("--grid", help="griglia JSON per sweep, es. '{\"tp_atr_mult\": [4, 6]}'")
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        fetch(args.days)
    elif args.cmd == "run":
        cmd_run(args)
    else:
        cmd_sweep(args)


if __name__ == "__main__":
    main()
