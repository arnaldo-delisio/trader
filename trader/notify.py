"""Telegram messages, in Italian, HTML formatted. Sending never raises."""

from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime

from .records import redact

Sender = Callable[[str], tuple[bool, str]]

# The words are shared with the terminal trace (trader/trace.py); Telegram adds the icons.
ACTION_WORD = {"buy": "compra", "sell": "vendi", "hold": "tieni"}
OUTCOME_WORD = {"placed": "inviato", "adopted": "già presente, adottato", "rejected": "rifiutato da Alpaca",
                "unconfirmed": "non confermato", "dry_run": "prova, non inviato"}
STATUS_WORD = {"ok": "Completato", "no_trade": "Nessun ordine", "failed": "Errore",
               "killed": "Kill switch attivo", "already_done": "Slot già eseguito",
               "stale_slot": "Slot vecchio, solo lettura", "paused": "Acquisti sospesi, uscite attive",
               "liquidating": "Liquidazione in corso", "liquidated": "Liquidazione completata"}
DRY_RUN_WORD = "PROVA"

ACTION_ICON = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}
OUTCOME_ICON = {"placed": "📨", "adopted": "♻️", "rejected": "❌", "unconfirmed": "❓", "dry_run": "🧪"}
STATUS_ICON = {"ok": "✅", "no_trade": "⏸️", "failed": "🚨", "killed": "🛑", "already_done": "♻️", "stale_slot": "⏪",
               "paused": "⏯️", "liquidating": "🧹", "liquidated": "🏁"}
ACTION_IT = {k: f"{ACTION_ICON[k]} {w}" for k, w in ACTION_WORD.items()}
OUTCOME_IT = {k: f"{OUTCOME_ICON[k]} {w}" for k, w in OUTCOME_WORD.items()}
STATUS_IT = {k: f"{STATUS_ICON[k]} {w}" for k, w in STATUS_WORD.items()}


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def usd(x: float | None) -> str:
    if x is None:
        return "n/d"
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} $"


def slot_label(slot_iso: str) -> str:
    try:
        d = datetime.fromisoformat(slot_iso)
        return d.strftime("%d/%m %H:%M UTC")
    except ValueError:
        return slot_iso


def telegram_sender(token: str, chat_id: str, secrets: list[str], opener=urllib.request.urlopen,
                    sleep=time.sleep, attempts: int = 2) -> Sender:
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> tuple[bool, str]:
        body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        last = ""
        for i in range(attempts):
            if i:
                sleep(2)
            try:
                req = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                with opener(req, timeout=15) as r:
                    if json.loads(r.read().decode()).get("ok"):
                        return True, "inviato"
                    last = "Telegram ha risposto ok=false"
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code < 500 and e.code != 429:
                    break
            except Exception as e:  # noqa: BLE001 - a notification must never crash the wake
                last = type(e).__name__
        return False, redact(last, secrets)

    return send


def print_sender(text: str) -> tuple[bool, str]:
    print("---- messaggio Telegram (non inviato) ----")
    print(text)
    print("------------------------------------------")
    return True, "stampato"


def safe_send(send: Sender, text: str, secrets: list[str]) -> tuple[bool, str]:
    """The one path every message takes: strip secrets, never raise."""
    try:
        return send(redact(text, secrets))
    except Exception as e:  # noqa: BLE001
        return False, f"invio fallito: {type(e).__name__}"


# ---- messages -------------------------------------------------------------

def pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "n/d"
    return (f"{x:+.2f}%" if signed else f"{x:.2f}%").replace(".", ",")


def price(x: float | None) -> str:
    return "n/d" if x is None else f"{x:.6g}".replace(".", ",")


EXPLORE_WORD = "🧭 <i>esplorazione</i>"
EXIT_ICON = {"stop": "🛑", "take_profit": "🎯", "tempo": "⌛", "segnale": "📉"}


def _decision_line(d: dict) -> str:
    line = f"• <b>{esc(d['symbol'])}</b> {ACTION_IT.get(d['action'], esc(d['action']))}"
    if d.get("explore"):
        line += f" {EXPLORE_WORD}"
    if d["action"] == "hold":
        return line
    line += f" {esc(usd(d['requested_usd']))}"
    if d.get("auto"):
        line += f" {EXIT_ICON.get(d.get('exit'), '')} <i>{esc(d.get('reason_model', ''))}</i>"
    line += " → ✅ ammesso" if d["approved"] else f"\n   ⛔ {esc(d['reason'])}"
    if d.get("outcome"):
        line += f" → {OUTCOME_IT.get(d['outcome'], esc(d['outcome']))}"
    if d["action"] == "buy" and d.get("stop"):
        line += f"\n   stop {esc(price(d['stop']))} · take-profit {esc(price(d.get('take_profit')))}"
    if not d.get("auto") and (d.get("bull_case") or d.get("bear_case")):
        line += f"\n   <i>📈 {esc(d.get('bull_case', ''))} · 📉 {esc(d.get('bear_case', ''))}</i>"
    return line


def wake_message(h: dict) -> str:
    """A wake with something to say: trades (entries, exits with their reason and stop),
    rejected orders, failures and alerts. Quiet wakes send nothing (wake.notable)."""
    lines = [f"🧪 <b>{DRY_RUN_WORD}</b> · nessun ordine inviato"] if h.get("dry_run") else []
    lines += [f"🤖 <b>Trader crypto</b> · <code>{esc(slot_label(h['slot']))}</code>",
              f"<b>{esc(STATUS_IT.get(h['status'], h['status']))}</b>"]
    if h.get("equity") is not None:
        day = h.get("day_change_pct")
        lines.append(f"💰 Equity: <b>{esc(usd(h['equity']))}</b>" + (f" ({esc(pct(day))} oggi)" if day is not None else ""))
    if h.get("regime"):
        lines.append(f"🌡️ Regime: {esc(h['regime'])}")
    if h.get("market_view"):
        lines.append(f"\n🧠 <i>{esc(h['market_view'])}</i>")
    if h.get("model_error"):
        lines.append(f"\n⚠️ Modello: {esc(h['model_error'])}\n→ nessun acquisto, le uscite restano")
    rows = [d for d in h.get("decisions", []) if d["action"] != "hold" or h.get("dry_run")]
    if rows:
        lines.append("")
    lines += [_decision_line(d) for d in rows]
    if h.get("liquidation"):
        lines += ["", *liquidation_lines(h["liquidation"])]
    for w in h.get("alerts", []):
        lines.append(f"⚠️ {esc(w)}")
    if h.get("error"):
        lines.append(f"\n🚨 <b>Errore:</b> <code>{esc(h['error'][:300])}</code>")
    if h.get("run_url"):
        lines.append(f'🔗 <a href="{esc(h["run_url"])}">Run su GitHub Actions</a>')
    return "\n".join(lines)


def liquidation_lines(liq: dict) -> list[str]:
    """The result of a liquidation: what is left, and the P&L since the start, realised
    when nothing is left."""
    out = []
    if liq.get("left"):
        out.append(f"⏳ Ancora da vendere: {esc(', '.join(liq['left']))} · il prossimo risveglio riprova")
    else:
        out.append("🏁 <b>Tutto venduto</b>: il conto è in liquidità")
    word = "realizzato" if not liq.get("left") else "a valore di mercato"
    out.append(f"💰 Patrimonio {esc(usd(liq.get('equity')))} · partenza {esc(usd(liq.get('baseline_equity')))}")
    out.append(f"📊 Risultato {word}: <b>{esc(usd(liq.get('pnl_usd')))}</b> ({esc(pct(liq.get('pnl_pct')))})")
    if liq.get("trades"):
        out.append(f"🧾 Operazioni chiuse: {liq['trades']} · vinte {esc(pct(liq.get('win_rate_pct'), False))}"
                   f" · commissioni {esc(usd(liq.get('fees_usd')))}")
    return out


def failure_message(slot: str, error: str, run_url: str = "") -> str:
    """Minimal text for when building the normal message itself failed."""
    msg = f"🚨 <b>Trader crypto: errore</b>\nSlot <code>{esc(slot)}</code>\n<code>{esc(error[:300])}</code>"
    if run_url:
        msg += f'\n🔗 <a href="{esc(run_url)}">Run su GitHub Actions</a>'
    return msg


def reflection_message(r: dict, dry_run: bool = False, run_url: str = "") -> str:
    lines = [f"🧪 <b>{DRY_RUN_WORD}</b>"] if dry_run else []
    lines.append(f"🪞 <b>Riflessione</b> · <code>{esc(r.get('slot', ''))}</code>")
    s = r.get("summary") or {}
    if s.get("trades"):
        lines.append(f"Operazioni chiuse (14 giorni): {s['trades']} · vinte {esc(pct(s.get('win_rate_pct'), False))}"
                     f" · P&amp;L {esc(usd(s.get('pnl_usd')))} · commissioni {esc(usd(s.get('fees_usd')))}")
    else:
        lines.append("Nessuna operazione chiusa negli ultimi 14 giorni.")
    for kind, x in (r.get("summary_by_kind") or {}).items():
        if x.get("trades"):
            lines.append(f"• {esc(kind)}: {x['trades']} · vinte {esc(pct(x.get('win_rate_pct'), False))}"
                         f" · P&amp;L {esc(usd(x.get('pnl_usd')))}")
    if r.get("error"):
        lines.append(f"⚠️ {esc(r['error'])}")
    if r.get("lessons"):
        lines.append("\n📚 <b>Lezioni</b>")
        lines += [f"• {esc(x)}" for x in r["lessons"]]
    for a in r.get("accepted", []):
        lines.append(f"✅ {esc(a['name'])}: {esc(a['old_value'])} → {esc(a['new_value'])} · <i>{esc(a['reason'])}</i>")
    for x in r.get("rejected", []):
        lines.append(f"⛔ {esc(x['name'])} → {esc(x['new_value'])}: {esc(x['why'])}")
    if not r.get("accepted") and not r.get("rejected") and not r.get("error"):
        lines.append("Parametri invariati: nessuna modifica proposta.")
    if r.get("evaluator_error"):
        lines.append(f"⚠️ {esc(r['evaluator_error'])}")
    if run_url:
        lines.append(f'🔗 <a href="{esc(run_url)}">Run su GitHub Actions</a>')
    return "\n".join(lines)


def _positions(rows: list[dict]) -> list[str]:
    out = []
    for p in rows:
        chg = (p["price"] / p["entry_price"] - 1) * 100 if p.get("entry_price") else None
        tag = f" {EXPLORE_WORD}" if p.get("explore") else ""
        out.append(f"• {esc(p['symbol'])}{tag} {esc(usd(p['value']))} ({esc(pct(chg))}) · stop {esc(price(p['stop']))}")
    return out


def status_message(h: dict, baseline: float | None, today: list[dict], run_url: str = "") -> str:
    """The compact status every 6 hours."""
    total = (h["equity"] / baseline - 1) * 100 if baseline and h.get("equity") is not None else None
    wakes = [w for w in today if w.get("kind", "wake") == "wake"]
    failed = sum(1 for w in wakes if w.get("status") == "failed")
    m = h.get("market") or {}
    lines = [f"🩺 <b>Stato</b> · <code>{esc(slot_label(h['slot']))}</code>",
             (f"💰 Equity {esc(usd(h['equity']))} · oggi {esc(pct(h.get('day_change_pct')))} · "
              f"dall'inizio {esc(pct(total))}")]
    if h.get("status") == "killed":
        lines.append("🛑 Kill switch attivo: nessun ordine, nemmeno le uscite")
    elif h.get("status") == "paused":
        lines.append("⏯️ Acquisti sospesi: stop e take-profit restano attivi")
    if m:
        gate_open = "aperto" if m.get("regime_ok") else "chiuso"
        lines.append(f"🌍 Paniere 7g {esc(pct(m.get('basket_7d_pct')))} · in tendenza "
                     f"{esc(pct(m.get('breadth_pct'), False))} · filtro acquisti {gate_open}")
    lines.append(f"⏰ Risvegli oggi: {len(wakes)} · falliti: {failed}")
    pos = _positions(h.get("positions") or [])
    lines += (["\n📦 <b>Posizioni</b>"] + pos) if pos else ["📦 Nessuna posizione aperta."]
    if run_url:
        lines.append(f'🔗 <a href="{esc(run_url)}">Run su GitHub Actions</a>')
    return "\n".join(lines)


def daily_message(day: str, wakes: list[dict], closed: list, start_equity: float | None,
                  end_equity: float | None, baseline: float | None, positions: list[dict], run_url: str = "") -> str:
    """The daily summary: equity, P&L of the day and since the start, trades, win rate, best and worst."""
    orders = [d for w in wakes for d in w.get("verdicts", []) if d.get("outcome") in ("placed", "adopted")]
    rejected = sum(1 for w in wakes for d in w.get("verdicts", []) if d["action"] != "hold" and not d["approved"])
    failures = sum(1 for w in wakes if w.get("status") == "failed")
    day_pl = end_equity - start_equity if end_equity is not None and start_equity is not None else None
    total = (end_equity / baseline - 1) * 100 if end_equity is not None and baseline else None
    lines = [f"📊 <b>Riepilogo del {esc(day)}</b>",
             f"💰 Equity {esc(usd(end_equity))} · giorno {esc(usd(day_pl))} · dall'inizio {esc(pct(total))}",
             f"⏰ Risvegli: {len(wakes)} · 📨 Ordini: {len(orders)} · ⛔ Respinti: {rejected} · 🚨 Errori: {failures}"]
    if closed:
        wins = sum(1 for t in closed if t.pnl_usd > 0)
        best = max(closed, key=lambda t: t.pnl_pct)
        worst = min(closed, key=lambda t: t.pnl_pct)
        lines.append(f"🔁 Operazioni chiuse: {len(closed)} · vinte {wins} ({esc(pct(100 * wins / len(closed), False))})"
                     f" · P&amp;L {esc(usd(sum(t.pnl_usd for t in closed)))}")
        ex = [t for t in closed if getattr(t, "explore", False)]
        if ex:
            lines.append(f"🧭 di cui esplorazione: {len(ex)} · P&amp;L {esc(usd(sum(t.pnl_usd for t in ex)))}")
        lines.append(f"🏆 Migliore {esc(best.symbol)} {esc(pct(best.pnl_pct))} · "
                     f"peggiore {esc(worst.symbol)} {esc(pct(worst.pnl_pct))}")
    else:
        lines.append("😴 Nessuna operazione chiusa oggi.")
    pos = _positions(positions)
    if pos:
        lines += ["\n📦 <b>Posizioni</b>"] + pos
    if run_url:
        lines.append(f'\n🔗 <a href="{esc(run_url)}">Run su GitHub Actions</a>')
    return "\n".join(lines)
