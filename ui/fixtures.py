"""Synthetic records for tests and screenshots. Every value is invented; nothing is real data.

    python -m ui.fixtures DIR [--wakes N] [--seed S]

writes a throwaway repo tree (state/, journal/, evidence/, lessons/, config/) in DIR
through the same writers the agent uses (trader.records, trader.learn), so the
shapes match what a wake would leave behind.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader import learn
from trader.records import Records

REPO = Path(__file__).resolve().parent.parent
START_EQUITY = 100_000.0
# Invented prices, only to keep the numbers plausible in shape.
PRICES = {"BTC/USD": 64000.0, "ETH/USD": 3100.0, "SOL/USD": 150.0, "LINK/USD": 14.0, "AVAX/USD": 27.0,
          "DOGE/USD": 0.12, "UNI/USD": 7.5, "AAVE/USD": 160.0, "SHIB/USD": 0.0000175, "XRP/USD": 0.55}
STATUSES = ["no_trade"] * 7 + ["ok"] * 3 + ["failed", "already_done"]
BULL = ["Trend 4h sopra la EMA 50 e momentum tra i primi tre del paniere",
        "MACD appena girato positivo con volume sopra la media",
        "RSI a 58: spazio per salire senza essere ipercomprato",
        "Rompe la banda alta di Bollinger con ampiezza in espansione"]
BEAR = ["Movimento atteso di poco sopra le commissioni di andata e ritorno",
        "ADX debole: il trend potrebbe non tenere",
        "Volume in calo nelle ultime quattro barre",
        "Correlato a BTC, che è sotto la EMA 21 a 1h"]
LESSONS = ["Le entrate con momentum debole hanno perso più delle commissioni: alzare la soglia aiuta.",
           "Gli stop a 4 ATR hanno lasciato correre i vincenti senza farsi buttare fuori dal rumore.",
           "Sui memecoin il take profit arriva raramente: meglio posizioni piccole e uscita per segnale.",
           "Nessuna operazione in regime risk-off: il filtro di mercato ha evitato due falsi segnali."]


def _slot_id(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%MZ")


def generate(root: Path, wakes: int = 400, seed: int = 7, now: datetime | None = None) -> Path:
    """Write `wakes` 15-minute wakes of synthetic history ending at `now`. wakes=0 writes only config."""
    rnd = random.Random(seed)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    for name in ("limits.toml", "params.json"):
        if (REPO / "config" / name).exists():
            shutil.copy(REPO / "config" / name, root / "config" / name)
    if wakes <= 0:
        return root
    rec = Records(root, [])
    now = now or datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=15 * wakes)
    prices = dict(PRICES)
    equity = START_EQUITY
    history_t, history_e = [], []
    open_pos: dict[str, dict] = {}
    run = 1000
    for i in range(wakes):
        t = start + timedelta(minutes=15 * (i + 1))
        slot = _slot_id(t)
        for s in prices:
            prices[s] *= 1 + rnd.gauss(0.0002, 0.006)
        equity *= 1 + rnd.gauss(0.00008, 0.0012)
        history_t.append(int(t.timestamp()))
        history_e.append(round(equity, 2))
        run += 1
        run_url = f"https://github.com/example/trader/actions/runs/{run}"
        status = rnd.choice(STATUSES) if i < wakes - 1 else "ok"
        regime = rnd.choice(["risk_on", "neutral", "neutral", "risk_off"])
        row = {"slot": slot, "dry_run": False, "status": status, "run_url": run_url, "equity": round(equity, 2),
               "finished_at": (t + timedelta(minutes=rnd.randint(6, 14))).isoformat(), "warnings": [],
               "jev": {"regime": regime, "multiplier": {"risk_on": 1.0, "neutral": 0.7, "risk_off": 0.4}[regime],
                       "source": "jev", "model": "jev-test", "choice": regime,
                       "confidence": round(rnd.uniform(0.55, 0.97), 2), "latency_ms": rnd.randint(300, 900),
                       "error": ""},
               "verdicts": [], "exits": [], "error": ""}
        if status == "failed":
            row["error"] = "Alpaca non raggiungibile: timeout dopo 3 tentativi (esempio sintetico)"
        if status == "already_done":
            row["skipped"] = "slot già eseguito (1 ordini con questo slot): nessun nuovo ordine"
        for s, lv in open_pos.items():  # trailing stop only moves up
            lv["highest"] = max(lv["highest"], prices[s])
            lv["stop"] = max(lv["stop"], lv["highest"] - 4 * lv["atr_at_entry"])
        # exits are enforced by code every wake, as in the agent
        hit = [(s, "stop" if prices[s] <= lv["stop"] else "take_profit") for s, lv in open_pos.items()
               if prices[s] <= lv["stop"] or prices[s] >= lv["take_profit"]]
        if status in ("failed", "already_done"):
            hit = []
        for s, reason in hit:
            lv = open_pos.pop(s)
            cid = f"trd-{slot}-{s.replace('/', '')}-sell"
            row["exits"].append({"symbol": s, "action": "sell", "reason": reason, "outcome": "placed",
                                 "client_order_id": cid, "qty": lv["qty"], "stop": round(lv["stop"], 10),
                                 "price": prices[s]})
            _order(rec, t, cid, s, "sell", lv["qty"], prices[s], rnd)
        if hit and status == "no_trade":
            row["status"] = status = "ok"
        if status == "ok" and not hit:
            s = rnd.choice(list(prices))
            if s in open_pos and rnd.random() < 0.3:
                lv = open_pos.pop(s)
                cid = f"trd-{slot}-{s.replace('/', '')}-sell"
                reason = "segnale"
                row["exits"].append({"symbol": s, "action": "sell", "reason": reason, "outcome": "placed",
                                     "client_order_id": cid, "qty": lv["qty"], "stop": round(lv["stop"], 8),
                                     "price": prices[s]})
                _order(rec, t, cid, s, "sell", lv["qty"], prices[s], rnd)
            elif s not in open_pos:
                notional = round(rnd.uniform(300, 4000 if not s.startswith(("DOGE", "SHIB")) else 1500), 2)
                qty = notional / prices[s]
                cid = f"trd-{slot}-{s.replace('/', '')}-buy"
                atr = prices[s] * 0.012
                lv = {"entry_price": prices[s], "stop": prices[s] - 4 * atr, "take_profit": prices[s] + 6 * atr,
                      "highest": prices[s], "qty": qty, "opened_at": t.isoformat(), "atr_at_entry": atr}
                open_pos[s] = lv
                row["verdicts"].append({"symbol": s, "action": "buy", "requested_usd": notional, "approved": True,
                                        "reason": "entro i limiti", "order": {"symbol": s, "side": "buy",
                                                                              "notional": notional},
                                        "reason_model": "Punteggio alto e movimento atteso ben sopra le commissioni",
                                        "bull_case": rnd.choice(BULL), "bear_case": rnd.choice(BEAR),
                                        "outcome": "placed", "client_order_id": cid,
                                        "stop": round(lv["stop"], 8), "take_profit": round(lv["take_profit"], 8)})
                _order(rec, t, cid, s, "buy", qty, prices[s], rnd)
            else:
                row["status"] = "no_trade"
        if i == wakes - 1:
            row["status"] = "ok" if row["verdicts"] or row["exits"] else "no_trade"
        if row["status"] == "no_trade":
            s = rnd.choice(list(prices))
            row["verdicts"].append({"symbol": s, "action": "buy", "requested_usd": 900.0, "approved": False,
                                    "reason": "esposizione totale oltre il massimo", "reason_model": "Segnale buono",
                                    "bull_case": rnd.choice(BULL), "bear_case": rnd.choice(BEAR)})
            row["proposal"] = {"market_view": "Mercato laterale, nessun vantaggio netto dopo le commissioni."}
        rec.journal(row)
        if i % 24 == 23:
            _reflection(rec, root, t, slot, rnd, run_url)
    _handoff(rec, now, equity, history_e, open_pos, prices, run_url)
    rec._write("state/portfolio_history.json", json.dumps(
        {"timestamp": history_t, "equity": history_e, "base_value": START_EQUITY, "timeframe": "15Min"}) + "\n")
    rec._write("state/positions.json", json.dumps(
        {s: {k: v for k, v in lv.items() if k != "qty"} for s, lv in open_pos.items()}, indent=2) + "\n")
    return root


def _order(rec: Records, t: datetime, cid: str, s: str, side: str, qty: float, price: float, rnd) -> None:
    oid = f"{rnd.getrandbits(64):016x}"
    order = {"id": oid, "client_order_id": cid, "symbol": s, "side": side, "qty": f"{qty:.9f}",
             "status": "filled", "filled_qty": f"{qty:.9f}", "filled_avg_price": f"{price:.8f}",
             "type": "market", "time_in_force": "gtc", "submitted_at": t.isoformat()}
    rec.evidence("order", order, t)
    rec.evidence("fill", {"id": f"{rnd.getrandbits(64):016x}", "order_id": oid, "symbol": s.replace("/", ""),
                          "side": side, "qty": f"{qty:.9f}", "price": f"{price:.8f}",
                          "transaction_time": (t + timedelta(seconds=2)).isoformat(),
                          "activity_type": "FILL"}, t + timedelta(seconds=3))


def _reflection(rec: Records, root: Path, t: datetime, slot: str, rnd, run_url: str) -> None:
    lessons = rnd.sample(LESSONS, 2)
    learn.write_lessons(rec, lessons, slot, t)
    old = round(rnd.choice([0.55, 0.6, 0.65, 0.7]), 2)
    stop = rnd.choice([3.5, 4.0, 4.5])
    accepted = [{"name": "entry_threshold", "old_value": old, "new_value": round(old + 0.05, 2),
                 "reason": "Le entrate deboli non pagavano le commissioni", "score_before": 0.412,
                 "score_after": 0.436}] if rnd.random() < 0.5 else []
    rejected = [{"name": "stop_atr_mult", "new_value": stop - 1.0, "old_value": stop,
                 "reason": "Stop più stretti per perdere meno sui falsi segnali",
                 "why": "backtest peggiore: 0.3810 contro 0.4120", "score_before": 0.412, "score_after": 0.381},
                {"name": "max_invested_pct", "new_value": 80,
                 "reason": "Più capitale investito nei trend forti", "why": "limite rigido: non si impara"}]
    rec.journal({"kind": "reflection", "slot": slot, "at": t.isoformat(), "lessons": lessons,
                 "accepted": accepted, "rejected": rejected, "params_written": bool(accepted), "error": "",
                 "run_url": run_url})


def _handoff(rec: Records, now: datetime, equity: float, hist: list, open_pos: dict, prices: dict, run_url: str):
    last = hist[-97] if len(hist) > 96 else hist[0]
    positions = []
    for s, lv in open_pos.items():
        p = prices[s]
        positions.append({"symbol": s.replace("/", ""), "qty": f"{lv['qty']:.9f}",
                          "avg_entry_price": f"{lv['entry_price']:.8f}", "current_price": f"{p:.8f}",
                          "market_value": f"{lv['qty'] * p:.2f}", "unrealized_pl": f"{lv['qty'] * (p - lv['entry_price']):.2f}",
                          "unrealized_plpc": f"{p / lv['entry_price'] - 1:.6f}"})
    rec.write_handoff({
        "slot": now.replace(minute=now.minute - now.minute % 15).isoformat(), "status": "ok",
        "started_at": now.isoformat(), "finished_at": (now + timedelta(minutes=8)).isoformat(),
        "run_url": run_url, "dry_run": False, "equity": round(equity, 2), "last_equity": last,
        "day_change_pct": (equity / last - 1) * 100,
        "changed": "trd-esempio-ETHUSD-buy (placed)", "outcome": "compra ETH/USD: inviato",
        "remaining_risk": f"esposizione su {len(positions)} posizioni, tutte con stop attivo",
        "next_job": "controllare se il trend a 4h di ETH regge prima di aggiungere",
        "market_view": "Rialzo moderato guidato da BTC ed ETH; altcoin miste.",
        "warnings": ["dati sintetici per il test della dashboard"], "positions": positions, "decisions": []})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scrive record sintetici per provare la dashboard.")
    ap.add_argument("dir", type=Path)
    ap.add_argument("--wakes", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    generate(a.dir, a.wakes, a.seed)
    print(a.dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
