"""Scenari end-to-end contro un conto Alpaca finto: le regole della v2 e i guasti.

    uv run python scripts/simulate.py

Ogni scenario gira un risveglio vero (lo stesso codice di GitHub Actions) con il
broker finto, il modello finto e i record in una cartella temporanea, poi verifica
cosa è successo. Nessuna rete, nessuna chiave vera. I prezzi finti salgono per
BTC, ETH, SOL, LINK, DOGE e AVAX e scendono per XRP e ADA; la soglia d'ingresso è
abbassata a 0,6 perché quei prezzi producano dei candidati.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import backtest as bt
from trader.broker import Alpaca
from trader.config import load_limits
from trader.fake_alpaca import FakeAlpaca
from trader.jev import Regime
from trader.model import FakeModel
from trader.orders import client_order_id
from trader.records import Records
from trader.trace import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    Trace,
    paint,
    use_color,
)
from trader.wake import Deps, wake

REPO = Path(__file__).resolve().parent.parent
SLOT = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
SECRETS = {"ALPACA_SECRET_KEY": "segreto-finto-alpaca-123456", "TELEGRAM_BOT_TOKEN": "999:token-finto-telegram"}

COLOR = use_color(sys.stdout)
def c(code, s): return paint(code, s, COLOR)


def reply(*ds, view="mercato in salita", job="ricontrollare BTC"):
    return json.dumps({"decisions": [{"symbol": s, "action": a, "notional_usd": n, "bull_case": "pro",
                                      "bear_case": "contro", "reason": "scenario"}
                                     for s, a, n in ds], "market_view": view, "next_job": job})


def reflection(*changes):
    return json.dumps({"lessons": ["lezione dello scenario"], "param_changes": [
        {"name": n, "new_value": v, "reason": "scenario"} for n, v in changes]})


class World:
    """Un repo temporaneo, un conto finto e una casella Telegram finta."""

    def __init__(self, explore: bool = True, **params):
        self.dir = Path(tempfile.mkdtemp(prefix="trader-sim-"))
        for rel in ("prompts/decide.md", "prompts/reflect.md", "config/limits.toml", "config/jev-thresholds.json"):
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.dir / rel)
        if not explore:
            lim = self.dir / "config/limits.toml"
            lim.write_text(lim.read_text().replace("explore_total_pct = 5.0", "explore_total_pct = 0.0"))
        p = json.loads((REPO / "config/params.json").read_text())
        p.update({"entry_threshold": 0.6, "min_edge_mult": 10.0, **params})
        (self.dir / "config/params.json").write_text(json.dumps(p))
        self.broker = FakeAlpaca(now=lambda: SLOT + timedelta(minutes=8))
        self.sent: list[str] = []
        self.telegram_down = False
        self.model: FakeModel | None = None

    def send(self, text):
        if self.telegram_down:
            raise ConnectionError("Telegram non raggiungibile")
        self.sent.append(text)
        return True, "inviato"

    def wake(self, *replies, slot=SLOT, env=None, records=None, **kw):
        now = slot + timedelta(minutes=8)
        self.broker.now = lambda: now
        client = Alpaca("chiave-finta", SECRETS["ALPACA_SECRET_KEY"], transport=self.broker, sleep=lambda s: None)
        self.model = FakeModel(list(replies) or [reply(("BTC/USD", "buy", 50))])
        deps = Deps(root=self.dir, records=Records(records or self.dir, list(SECRETS.values())),
                    limits=load_limits(self.dir / "config/limits.toml"), send=self.send,
                    build=lambda: (client, self.model), env=env or {}, now=lambda: now,
                    run_url="https://github.com/esempio/trader/actions/runs/42", trace=Trace(io.StringIO()),
                    **{"reflect": False, **kw})
        with contextlib.redirect_stderr(io.StringIO()):
            return wake(deps, slot)

    def handoff(self, root=None):
        return json.loads(((root or self.dir) / "state/last_handoff.json").read_text())

    def journal(self):
        return [json.loads(x) for x in (self.dir / "journal/decisions.jsonl").read_text().splitlines()]

    def text(self):
        return "".join(p.read_text() for d in ("state", "journal", "evidence")
                       for p in (self.dir / d).glob("*") if p.is_file())

    def trades(self):
        return [m for m in self.sent if "Trader crypto" in m]


results: list[bool] = []


def step(ok: bool, text: str) -> None:
    results.append(ok)
    print(f"   {c(GREEN, '✓') if ok else c(RED, '✗')} {text}")


def info(text: str) -> None:
    print(f"   {c(DIM, '·')} {c(DIM, text)}")


def scenario(n: int, title: str) -> None:
    print(f"\n{c(CYAN, f'▶ {n:>2}.')} {c(BOLD, title)}")


def main() -> int:
    print(c(BOLD, "Simulazione · conto Alpaca finto · nessuna rete"))
    later = SLOT + timedelta(minutes=15)

    scenario(1, "Risveglio normale")
    w = World()
    w.wake()
    j = w.journal()[-1]
    info(f"candidati della strategia: {', '.join(f'{s} fino a {n:.0f} $' for s, n in j['candidates'].items())}")
    info("il modello propone: compra BTC/USD per 50 $")
    step(w.broker.posts() == 1, "il gate ammette, un ordine inviato ad Alpaca")
    lv = json.loads((w.dir / "state/positions.json").read_text())["positions"]["BTC/USD"]
    step(lv["stop"] < lv["entry_price"] < lv["take_profit"], f"stop {lv['stop']:.0f} e take-profit {lv['take_profit']:.0f} registrati")
    step("trd-20260926T0800Z-BTCUSD-buy" in w.text(), "ordine registrato in evidence con il client_order_id")
    step(len(w.trades()) == 1 and "stop" in w.trades()[0], "messaggio Telegram con l'ordine e lo stop")

    scenario(2, "Risveglio tranquillo")
    w = World(entry_threshold=0.9, explore=False)
    w.wake()
    w.sent.clear()
    w.wake(slot=later)
    info("nessuna crypto supera le regole d'ingresso, esplorazione spenta, nessuna posizione")
    step(w.model.prompts == [], "il modello non viene interpellato")
    step(w.sent == [], "nessun messaggio Telegram")

    scenario(3, "Stop colpito")
    w = World()
    w.wake()
    w.broker.prices["BTC/USD"] *= 0.9
    w.wake(reply(("BTC/USD", "hold", 0)), slot=later)
    info("BTC scende del 10% sotto lo stop; il modello vorrebbe tenerlo")
    sells = [o for o in w.broker.orders if o["side"] == "sell"]
    step(len(sells) == 1 and w.broker.positions.get("BTCUSD", 0) <= 1e-8, "il codice vende tutta la posizione")
    step(w.journal()[-1]["exits"][0]["reason"] == "stop", "motivo 'stop' nel journal, per la riflessione")
    step("stop" in w.trades()[-1], "e nel messaggio Telegram")

    scenario(4, "Tetto per le memecoin")
    w = World()
    w.wake(reply(("DOGE/USD", "buy", 500)))
    cap = w.journal()[-1]["candidates"].get("DOGE/USD")
    info(f"DOGE è una memecoin: al massimo il 3% del patrimonio ({cap:.0f} $ su 10.000 $)")
    step(w.broker.posts() == 0, "la proposta da 500 $ è respinta, non ridotta")
    step("dimensione" in w.handoff()["decisions"][0]["reason"], "il motivo è scritto")

    scenario(5, "Regime risk-off: dimensioni ridotte")
    w = World()
    off = Regime("risk_off", 0.4, "jev", "jev-finto", "risk_off", 0.93, 5, "")
    w.wake(reply(("BTC/USD", "hold", 0)), classify=lambda state: off)
    j = w.journal()[-1]
    info("Jev risponde risk_off (confidenza 0,93): x0,4 invece di x0,7")
    step(j["candidates"]["BTC/USD"] == 320.0, "BTC al massimo 320 $ invece di 560 $")
    step(j["jev"]["regime"] == "risk_off", "la chiamata a Jev è nel journal")

    scenario(6, "Il modello risponde male")
    w = World()
    w.wake()
    w.broker.prices["BTC/USD"] *= 0.9
    w.wake("Mi dispiace, non posso aiutarti con il trading.", slot=later)
    info("risposta non JSON, come un rifiuto, mentre BTC è sotto lo stop")
    step([o["side"] for o in w.broker.orders] == ["buy", "sell"], "nessun acquisto, ma lo stop viene eseguito")
    step("JSON" in w.handoff().get("model_error", ""), "il motivo è nel handoff e nel messaggio")

    scenario(7, "Proposta fuori dalle regole")
    w = World()
    w.wake(reply(("BTC/USD", "buy", 5000), ("XRP/USD", "buy", 50), ("ETH/USD", "sell", 100)))
    reasons = [d["reason"] for d in w.handoff()["decisions"]]
    step(w.broker.posts() == 0, "nessun ordine inviato")
    for r in reasons:
        info(f"respinto: {r}")
    step(all(r != "ammesso" for r in reasons), "ogni rifiuto ha il suo motivo")

    scenario(8, "Kill switch")
    w = World()
    (w.dir / "KILL").touch()
    w.wake()
    info("file KILL presente nella root del repo")
    step(w.broker.posts() == 0, "nessun ordine")
    step(w.handoff()["status"] == "killed", "il handoff dice 'kill switch attivo'")
    step(w.model.prompts == [], "il modello non viene interpellato: niente da decidere")
    step(any("Kill switch" in m for m in w.sent if "Stato" in m), "lo stato delle 6 ore lo dice")

    scenario(9, "Perdita giornaliera oltre il limite")
    w = World()
    w.broker.positions["ETHUSD"] = 0.05
    w.broker.last_equity, w.broker.equity_override = 10000, 9400
    w.wake(reply(("BTC/USD", "buy", 50), ("ETH/USD", "sell", 150)))
    info("equity -6% rispetto alla chiusura precedente")
    sides = [o["side"] for o in w.broker.orders]
    step("buy" not in sides, "acquisto bloccato")
    step(sides == ["sell"], "vendita ammessa: ridurre il rischio è sempre possibile")

    scenario(10, "Run interrotto dopo l'invio")
    w = World()
    w.broker.add_order(client_order_id(SLOT - timedelta(minutes=15), "ETH/USD", "buy"), "ETH/USD", "buy", notional=40)
    info("il run delle 07:45 ha inviato un ordine ed è morto prima di scriverlo")
    w.wake(reply(("BTC/USD", "hold", 0)))
    step("trd-20260926T0745Z-ETHUSD-buy" in w.text(), "il risveglio successivo lo trova su Alpaca e lo registra")
    step(any("recuperati 1" in m for m in w.sent), "e lo segnala su Telegram")

    scenario(11, "Due run nello stesso slot")
    w = World()
    w.wake(records=w.dir / "a")
    w.wake(reply(("SOL/USD", "buy", 50), ("DOGE/USD", "buy", 50)), records=w.dir / "b")
    info("il secondo run non vede i record del primo e vuole altri ordini")
    step(w.broker.posts() == 1, "un solo ordine su Alpaca")
    step(w.handoff(w.dir / "b")["status"] == "already_done", "il secondo si ferma: slot già eseguito")

    scenario(12, "Risposta persa dopo l'invio dell'ordine")
    w = World()
    w.broker.post_fault = "lost_reply"
    w.wake()
    info("Alpaca accetta l'ordine, la risposta non arriva")
    step(w.broker.posts() == 1, "nessun reinvio alla cieca")
    step(w.handoff()["decisions"][0]["outcome"] == "adopted", "ritrovato per client_order_id e adottato")

    scenario(13, "Slot saltati")
    w = World()
    w.wake(reply(("BTC/USD", "hold", 0)), slot=SLOT - timedelta(hours=1))
    w.wake(reply(("BTC/USD", "buy", 50), ("SOL/USD", "buy", 50), ("DOGE/USD", "buy", 50)))
    info("GitHub ha saltato i risvegli delle 07:15, 07:30 e 07:45")
    step(w.handoff().get("missed_slots") == 3, "il buco è registrato")
    step(w.broker.posts() <= 3, "nessun recupero: solo i limiti di questo risveglio")

    scenario(14, "Livelli d'uscita persi")
    w = World()
    w.wake()
    (w.dir / "state/positions.json").unlink()
    w.wake(reply(("SOL/USD", "hold", 0)), slot=later)
    info("state/positions.json cancellato dopo l'acquisto")
    lv = json.loads((w.dir / "state/positions.json").read_text())["positions"].get("BTC/USD", {})
    step(bool(lv.get("stop")), "stop e take-profit ricostruiti dai fill di Alpaca")
    step(any("ricostruiti (fill)" in m for m in w.sent), "e segnalati su Telegram")

    scenario(15, "Alpaca irraggiungibile")
    w = World()
    w.broker.down = True
    code = w.wake()
    step(w.broker.orders == [], "nessun ordine")
    step(w.handoff()["status"] == "failed", "handoff scritto: 'Alpaca non raggiungibile'")
    step(any("Errore" in m for m in w.sent) and code == 1, "notifica di errore inviata, run segnato come fallito")

    scenario(16, "Telegram irraggiungibile")
    w = World()
    w.telegram_down = True
    w.wake()
    h = w.handoff()
    step(h["status"] == "ok", "l'ordine e il handoff ci sono comunque")
    step(h["notified"] is False, "il handoff registra che la notifica non è partita")

    scenario(17, "Riflessione: una modifica che peggiora il backtest")
    w = World()
    before = (w.dir / "config/params.json").read_text()
    def metrics(p):
        return {"net_return_pct": 1.0 if p["stop_atr_mult"] == 4.0 else 0.2, "max_drawdown_pct": 2.0,
                "start": "finto", "end": "finto"}
    judge = lambda cur, cand: bt.verdict(metrics(cur), metrics(cand))
    w.wake(reply(("BTC/USD", "hold", 0)), reflection(("stop_atr_mult", 3.5), ("max_position_pct", 20)),
           reflect=None, judge=lambda *a: judge)
    r = next(x for x in w.journal() if x.get("kind") == "reflection")
    info("il modello propone stop a 3,5 ATR e di alzare il tetto per posizione al 20%")
    step([x["name"] for x in r["rejected"]] == ["max_position_pct", "stop_atr_mult"],
         "entrambe respinte: un limite rigido e un backtest peggiore")
    step((w.dir / "config/params.json").read_text() == before, "config/params.json non cambia")
    step("lezione dello scenario" in (w.dir / "lessons/lessons.md").read_text(), "la lezione è scritta in lessons.md")
    step(any("Riflessione" in m for m in w.sent), "e riassunta su Telegram")

    scenario(18, "Riepilogo giornaliero e stato ogni 6 ore")
    w = World()
    last = datetime(2026, 9, 26, 23, 45, tzinfo=UTC)
    w.wake(slot=last - timedelta(hours=2))
    w.broker.prices["BTC/USD"] *= 1.2
    w.wake(reply(("SOL/USD", "hold", 0)), slot=last)
    w.wake(reply(("SOL/USD", "hold", 0)), slot=last)
    w.wake(reply(("SOL/USD", "hold", 0)), slot=last + timedelta(minutes=15))
    summaries = [m for m in w.sent if "Riepilogo" in m]
    info("acquisto alle 21:45, take-profit alle 23:45, poi una ripetizione e lo slot di mezzanotte")
    step(len(summaries) == 1, "riepilogo inviato una volta sola")
    step("Operazioni chiuse: 1" in summaries[0] and "vinte 1" in summaries[0], "con operazioni, vinte e P&L")
    step(len([m for m in w.sent if "Stato" in m]) == 2, "uno stato per ogni blocco di 6 ore (18:00 e 00:00)")

    scenario(19, "Segreti")
    w = World()
    w.wake(reply(("BTC/USD", "buy", 50), view="la chiave è " + SECRETS["ALPACA_SECRET_KEY"]))
    leaked = [k for k, v in SECRETS.items() if v in w.text() or any(v in m for m in w.sent)]
    info("il modello prova a scrivere una chiave nel suo testo")
    step(not leaked, "nessun segreto nei record né nei messaggi")

    scenario(20, "Esplorazione: piccoli acquisti sotto la soglia")
    w = World(entry_threshold=0.9)
    w.wake(reply(("BTC/USD", "buy", 70)))
    j = w.journal()[-1]
    info("nessuna crypto passa la regola d'ingresso; BTC, SOL e DOGE sono in tendenza sul 4h")
    info(f"candidati di esplorazione: {j['explore_candidates']} (1% del patrimonio x 0,7 del regime neutro)")
    step(j["verdicts"][0]["approved"] and j["verdicts"][0]["explore"], "il gate ammette BTC come esplorazione")
    step(json.loads((w.dir / "state/positions.json").read_text())["positions"]["BTC/USD"]["explore"],
         "la posizione resta etichettata 'esplorazione' nei livelli d'uscita")
    step(any("esplorazione" in m for m in w.sent), "e su Telegram")
    w = World(entry_threshold=0.9)
    w.wake(reply(("BTC/USD", "buy", 150)))
    v = w.journal()[-1]["verdicts"][0]
    info("il modello chiede 150 $: più dell'1% del patrimonio")
    step(not v["approved"] and "esplorazione" in v["reason"], "respinto: " + v["reason"])

    ok = sum(results)
    colour = GREEN if ok == len(results) else RED
    print(f"\n{c(colour, c(BOLD, f'{ok}/{len(results)} verifiche superate'))}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
