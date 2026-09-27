"""The dashboard page: one self-contained HTML file, inline CSS, inline SVG, a little inline JS.

Everything from the records passes through `e()` before it reaches the page.
"""

from __future__ import annotations

import html
import json
import math
from datetime import UTC, datetime

from trader import notify

from .data import parse_time

STATUS_WORD = {**notify.STATUS_WORD, "reflection": "Riflessione", "unknown": "Sconosciuto"}
STATUS_TONE = {"ok": "good", "no_trade": "calm", "failed": "bad", "killed": "warn", "already_done": "dim",
               "paused": "warn", "liquidating": "warn", "liquidated": "calm",
               "stale_slot": "dim", "reflection": "violet", "unknown": "dim"}
REGIME_WORD = {"risk_on": "Propenso al rischio", "neutral": "Neutrale", "risk_off": "Avverso al rischio"}
REGIME_TONE = {"risk_on": "good", "neutral": "calm", "risk_off": "bad"}
EXIT_WORD = {"stop": "stop", "take_profit": "take profit", "segnale": "segnale", "tempo": "tempo massimo"}
PARAM_WORD = {
    "entry_threshold": "Soglia di entrata", "exit_threshold": "Soglia di uscita",
    "stop_atr_mult": "Stop (multipli di ATR)", "tp_atr_mult": "Take profit (multipli di ATR)",
    "trail_atr_mult": "Trailing stop (multipli di ATR)", "min_edge_mult": "Margine minimo sulle commissioni",
    "shortlist_size": "Simboli in lista corta", "risk_per_trade_pct": "Rischio per operazione %",
    "position_pct": "Peso per posizione %", "cooldown_hours": "Pausa dopo un'uscita (ore)",
    "max_hold_hours": "Durata massima (ore)", "regime_filter": "Filtro di mercato",
}
LIMIT_WORD = {
    "max_invested_pct": ("Investito al massimo", "%"), "max_exposure_pct": ("Investito al massimo", "%"),
    "max_position_pct": ("Massimo per moneta", "%"), "max_coin_pct": ("Massimo per moneta", "%"),
    "max_memecoin_pct": ("Massimo per memecoin", "%"), "max_meme_pct": ("Massimo per memecoin", "%"),
    "max_open_positions": ("Posizioni aperte al massimo", ""), "max_positions": ("Posizioni aperte al massimo", ""),
    "daily_loss_limit_pct": ("Perdita giornaliera: stop agli acquisti", "%"),
    "max_orders_per_wake": ("Ordini per risveglio", ""), "min_order_usd": ("Ordine minimo", "$"),
    "max_order_usd": ("Ordine massimo", "$"), "max_position_usd": ("Massimo per simbolo", "$"),
    "max_total_exposure_usd": ("Esposizione totale massima", "$"),
    "explore_position_pct": ("Esplorazione: per posizione", "%"),
    "explore_total_pct": ("Esplorazione: in totale", "%"),
}
EXPLORE_TAG = '<span class="tag explore" title="sotto la soglia d\'ingresso, piccola e con tetti propri">esplorazione</span>'



MAX_TRADE_CARDS = 6
MAX_CHANGE_ROWS = 6


def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


# ---- numbers, Italian style ----------------------------------------------------------

def fnum(x: float | None, dec: int = 2) -> str:
    if x is None:
        return "n/d"
    s = f"{abs(x):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s


def usd(x: float | None, dec: int = 2, sign: bool = False) -> str:
    if x is None:
        return "n/d"
    s = fnum(x, dec)
    return f"{'+' if sign and x > 0 else ''}{s}\u00a0$"


def usd_big(x: float | None) -> str:
    """For the KPI tiles: the unit smaller than the figure."""
    return "n/d" if x is None else f'{fnum(x, 2)}<small class="unit">$</small>'



def pct(x: float | None, dec: int = 2, sign: bool = True) -> str:
    if x is None:
        return "n/d"
    return f"{'+' if sign and x > 0 else ''}{fnum(x, dec)}%"


def price(x: float | None) -> str:
    if x is None:
        return "n/d"
    a = abs(x)
    if a >= 1000:
        return fnum(x, 2)
    if a >= 1:
        return fnum(x, 3 if a < 100 else 2)
    if a == 0:
        return "0"
    dec = min(10, 3 - math.floor(math.log10(a)))
    return fnum(x, dec)


def value(x) -> str:
    if isinstance(x, bool) or x is None:
        return e(x if x is not None else "n/d")
    if isinstance(x, (int, float)):
        return fnum(x, 0 if float(x).is_integer() else (2 if abs(x) >= 1 else 3))
    return e(x)


def amount(x: float | None, unit: str) -> str:
    return "n/d" if x is None else f"{fnum(x, 0 if float(x).is_integer() else 1)}{' ' + unit if unit else ''}"


def tone(x: float | None) -> str:
    return "" if x is None or abs(x) < 1e-9 else ("up" if x > 0 else "down")


def when(iso_str: str, fmt: str = "%d/%m %H:%M") -> str:
    d = parse_time(iso_str)
    return d.strftime(fmt) if d else "n/d"


def ago(iso_str: str) -> str:
    """A span the page's script keeps current; the text is the build-time value."""
    d = parse_time(iso_str)
    if not d:
        return ""
    return f'<span class="ago" data-ts="{e(d.isoformat())}">{e(when(iso_str))} UTC</span>'


# ---- pieces -----------------------------------------------------------------------------

def pill(text: str, t: str, dot: bool = True) -> str:
    return f'<span class="pill {t}">{"<i></i>" if dot else ""}{e(text)}</span>'


def panel(title: str, body: str, cls: str = "", aside: str = "") -> str:
    return (f'<section class="panel {cls}"><header><h2>{e(title)}</h2>{aside}</header>'
            f'<div class="body">{body}</div></section>')


def empty(text: str) -> str:
    return f'<p class="empty">{e(text)}</p>'


def kpi(label: str, main: str, sub: str = "", t: str = "", extra: str = "") -> str:
    return (f'<div class="kpi"><div class="label">{e(label)}</div><div class="num {t}">{main}</div>'
            f'<div class="sub">{sub}</div>{extra}</div>')


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = hi - lo if hi > lo else abs(hi) or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.001:
        if v >= lo - step * 0.001:
            ticks.append(round(v, 10))
        v += step
    return ticks


def equity_chart(series: list[dict], base: float | None) -> str:
    pts = [(parse_time(p["t"]), p["equity"]) for p in series]
    pts = [(t, v) for t, v in pts if t and v is not None]
    if len(pts) < 2:
        one = f" Ultimo valore: {usd(pts[0][1])}." if pts else ""
        return empty("La curva comparirà dopo i primi risvegli." + one)
    W, H, L, R, T, B = 1200, 470, 96, 28, 24, 48
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    vals = [v for _, v in pts] + ([base] if base else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    ticks = nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def x(t: float) -> float:
        return L + (t - t0) / ((t1 - t0) or 1) * (W - L - R)

    def y(v: float) -> float:
        return T + (hi - v) / ((hi - lo) or 1) * (H - T - B)

    xy = [(x(t.timestamp()), y(v)) for t, v in pts]
    line = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in xy)
    area = f"{line} L{xy[-1][0]:.1f},{H - B} L{xy[0][0]:.1f},{H - B} Z"
    grid = "".join(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="tick" x="{L - 14}" y="{y(v) + 6:.1f}" text-anchor="end">{fnum(v, 0)}</text>'
                   for v in ticks)
    days = (t1 - t0) / 86400
    fmt = "%d/%m" if days > 8 else "%d/%m %H:%M"
    n = 6
    xt = "".join(
        f'<text class="tick" x="{x(t0 + (t1 - t0) * i / (n - 1)):.1f}" y="{H - 14}" '
        f'text-anchor="{"start" if i == 0 else "end" if i == n - 1 else "middle"}">'
        f'{datetime.fromtimestamp(t0 + (t1 - t0) * i / (n - 1), UTC).strftime(fmt)}</text>'
        for i in range(n))
    baseline = ""
    if base and lo <= base <= hi:
        baseline = (f'<line class="base" x1="{L}" x2="{W - R}" y1="{y(base):.1f}" y2="{y(base):.1f}"/>'
                    f'<text class="baselabel" x="{L + 10}" y="{y(base) + (24 if y(base) < T + 40 else -10):.1f}">'
                    f'valore iniziale {usd(base, 0)}</text>')
    lx, ly = xy[-1]
    data = json.dumps([[round(a, 1), round(b, 1), when(t.isoformat()), usd(v)] for (a, b), (t, v) in zip(xy, pts)],
                      separators=(",", ":"))
    return (f'<div class="chart" data-points="{e(data)}">'
            f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Andamento del patrimonio">'
            '<defs><linearGradient id="eqfill" x1="0" x2="0" y1="0" y2="1">'
            '<stop offset="0" stop-color="var(--accent)" stop-opacity=".32"/>'
            '<stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>'
            f'{grid}{baseline}<path class="area" d="{area}"/><path class="line" d="{line}"/>'
            f'<circle class="halo" cx="{lx:.1f}" cy="{ly:.1f}" r="12"/><circle class="dot" cx="{lx:.1f}" cy="{ly:.1f}" r="6"/>'
            f'{xt}<line class="cross" x1="0" x2="0" y1="{T}" y2="{H - B}"/>'
            f'<circle class="hover" r="7" cx="-20" cy="-20"/></svg><div class="tip"></div></div>')


def range_bar(p: dict) -> str:
    stop, tp, px, entry = p["stop"], p["take_profit"], p["price"], p["entry_price"]
    if not (stop and tp and tp > stop):
        return '<span class="muted">livelli non registrati</span>'

    def at(v: float) -> float:
        return max(0.0, min(100.0, (v - stop) / (tp - stop) * 100))

    cur = at(px) if px else None
    ent = at(entry) if entry else None
    fill = "" if cur is None else f'<b class="fill {"up" if px >= (entry or px) else "down"}" style="width:{cur:.1f}%"></b>'
    return ('<div class="range">' + fill
            + (f'<i class="entry" style="left:{ent:.1f}%" title="ingresso"></i>' if ent is not None else "")
            + (f'<i class="now" style="left:{cur:.1f}%" title="prezzo"></i>' if cur is not None else "")
            + '</div>')


def positions_table(pos: list[dict], max_positions: float | None) -> str:
    if not pos:
        return empty("Nessuna posizione aperta: il conto è tutto in contanti.")
    rows = []
    for p in pos:
        meme = ('<span class="tag">meme</span>' if p["meme"] else "") + (EXPLORE_TAG if p.get("explore") else "")
        dist = (p["price"] / p["stop"] - 1) * 100 if p["price"] and p["stop"] else None
        rows.append(
            f'<tr><td class="sym">{e(p["symbol"].split("/")[0])}<small>/USD</small>{meme}</td>'
            f'<td class="r">{usd(p["market_value"])}</td>'
            f'<td class="r">{price(p["entry_price"])}</td><td class="r strong">{price(p["price"])}</td>'
            f'<td class="r {tone(p["pnl_pct"])}">{pct(p["pnl_pct"])}</td>'
            f'<td class="r stop">{price(p["stop"])}<small>{pct(-dist if dist is not None else None, 1)}</small></td>'
            f'<td class="bar">{range_bar(p)}</td>'
            f'<td class="r tp">{price(p["take_profit"])}</td></tr>')
    return ('<table class="positions"><thead><tr><th>Moneta</th><th class="r">Valore</th><th class="r">Ingresso</th>'
            '<th class="r">Prezzo</th><th class="r">P&amp;L</th><th class="r">Stop</th>'
            '<th class="bar">stop → take profit</th><th class="r">Take profit</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>")


def trade_cards(trades: list[dict]) -> str:
    if not trades:
        return empty("Ancora nessuna operazione. Ogni ordine comparirà qui con il motivo, i pro e i contro.")
    out = []
    for t in trades[:MAX_TRADE_CARDS]:
        side = t["side"] or "hold"
        word = notify.ACTION_WORD.get(side, side)
        head = (f'<div class="thead"><span class="side {side}">{e(word)}</span>'
                f'<span class="tsym">{e(t["symbol"])}</span>'
                + ('<span class="tag">meme</span>' if t["meme"] else "")
                + ('<span class="tag prova">prova</span>' if t["dry_run"] else "")
                + (EXPLORE_TAG if t.get("explore") else "")
                + f'<span class="tmeta">{usd(t["notional"]) if t["notional"] else ""}'
                  f'{" @ " + price(t["price"]) if t["price"] else ""}</span>'
                f'<span class="twhen">{e(when(t["at"]))}</span></div>')
        body = ""
        if t["origin"] == "exit":
            r = EXIT_WORD.get(t["exit_reason"], t["exit_reason"]) or "regola di uscita"
            body += f'<p class="why"><b>Uscita decisa dal codice:</b> {e(r)}' + \
                (f', stop a {price(t["stop"])}' if t["stop"] else "") + "</p>"
        elif t["reason"]:
            body += f'<p class="why">{e(t["reason"])}</p>'
        if t["bull"] or t["bear"]:
            body += ('<div class="probe">'
                     + (f'<div class="pro"><span>pro</span>{e(t["bull"])}</div>' if t["bull"] else "")
                     + (f'<div class="con"><span>contro</span>{e(t["bear"])}</div>' if t["bear"] else "")
                     + '</div>')
        foot = []
        if t["stop"] and t["origin"] != "exit":
            foot.append(f"stop {price(t['stop'])}")
        if t["take_profit"]:
            foot.append(f"take profit {price(t['take_profit'])}")
        if t["gate"]:
            foot.append(f"gate: {e(t['gate'])}")
        link = f' · <a href="{e(t["run_url"])}" target="_blank" rel="noopener">run</a>' if t["run_url"] else ""
        if foot or link:
            body += f'<p class="tfoot">{" · ".join(foot)}{link}</p>'
        out.append(f'<article class="trade">{head}{body}</article>')
    return '<div class="trades">' + "".join(out) + "</div>"


def timeline(wakes: list[dict]) -> str:
    if not wakes:
        return empty("Nessun risveglio registrato. Il primo arriverà con il cron di GitHub Actions.")
    bars = []
    for w in wakes:
        t = STATUS_TONE.get(w["status"], "dim")
        h = 100 if w["orders"] else 62 if w["status"] != "reflection" else 80
        title = f'{when(w["t"])} · {STATUS_WORD.get(w["status"], w["status"])}' + \
            (f' · {w["orders"]} ordini' if w["orders"] else "")
        tag = "a" if w["run_url"] else "span"
        href = f' href="{e(w["run_url"])}" target="_blank" rel="noopener"' if w["run_url"] else ""
        bars.append(f'<{tag} class="tick {t}" style="height:{h}%" title="{e(title)}"{href}></{tag}>')
    first, last = wakes[0]["t"], wakes[-1]["t"]
    counts: dict[str, int] = {}
    for w in wakes:
        counts[w["status"]] = counts.get(w["status"], 0) + 1
    legend = "".join(f'<span class="lg {STATUS_TONE.get(k, "dim")}"><i></i>{e(STATUS_WORD.get(k, k))} '
                     f'<b>{n}</b></span>' for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    rows = []
    for w in reversed(wakes[-8:]):
        t = STATUS_TONE.get(w["status"], "dim")
        note = w["error"] or w["note"]
        link = (f'<a href="{e(w["run_url"])}" target="_blank" rel="noopener">run ↗</a>' if w["run_url"]
                else '<span class="muted">locale</span>')
        rows.append(f'<li><span class="slot">{e(when(w["t"]))}</span>{pill(STATUS_WORD.get(w["status"], w["status"]), t)}'
                    f'<span class="orders">{w["orders"] or ""}{" ordini" if w["orders"] > 1 else " ordine" if w["orders"] else ""}</span>'
                    f'<span class="note">{e(note)}</span>{link}</li>')
    return (f'<div class="strip">{"".join(bars)}</div>'
            f'<div class="stripaxis"><span>{e(when(first))}</span><div class="legend">{legend}</div>'
            f'<span>{e(when(last))}</span></div><ul class="wakes">{"".join(rows)}</ul>')


def handoff_block(m: dict) -> str:
    h = m["handoff"]
    if not h and not m["progress"]:
        return empty("Nessun passaggio di consegne: l'agente non si è ancora svegliato.")
    if not h:
        return f'<pre class="progress">{e(m["progress"])}</pre>'
    st = str(h.get("status") or "unknown")
    items = [("Esito", h.get("outcome")), ("Cosa è cambiato", h.get("changed")),
             ("Visione del mercato", h.get("market_view")), ("Rischio residuo", h.get("remaining_risk")),
             ("Prossimo compito", h.get("next_job")), ("Errore", h.get("error"))]
    dl = "".join(f'<dt>{e(k)}</dt><dd>{e(v)}</dd>' for k, v in items if v)
    warns = "".join(f"<li>{e(w)}</li>" for w in m["handoff_warnings"])
    link = (f'<a href="{e(m["handoff_run_url"])}" target="_blank" rel="noopener">apri il run ↗</a>'
            if m["handoff_run_url"] else "")
    return (f'<div class="hohead">{pill(STATUS_WORD.get(st, st), STATUS_TONE.get(st, "dim"))}'
            f'<span class="slot">slot {e(when(str(h.get("slot") or "")))}</span>'
            + ('<span class="tag prova">prova</span>' if h.get("dry_run") else "") + f'{link}</div>'
            f'<dl class="handoff">{dl}</dl>' + (f'<ul class="warns">{warns}</ul>' if warns else ""))


def lessons_block(lessons: list[dict]) -> str:
    if not lessons:
        return empty("Nessuna lezione ancora. La prima riflessione arriva dopo sei ore di operatività.")
    out = []
    for i, s in enumerate(lessons[:4]):
        items = "".join(f"<li>{e(x)}</li>" for x in s["items"])
        out.append(f'<div class="lesson{" newest" if i == 0 else ""}"><h3>{e(s["title"])}</h3><ul>{items}</ul></div>')
    return "".join(out)


def changes_block(changes: list[dict]) -> str:
    if not changes:
        return empty("Nessuna modifica proposta finora. Ogni proposta passa da limiti nel codice e da un backtest.")
    rows = []
    for c in changes[:MAX_CHANGE_ROWS]:
        name = PARAM_WORD.get(c["name"], c["name"])
        verdict = pill("accettata", "good") if c["accepted"] else pill("respinta", "bad")
        score = ""
        if c["score_before"] is not None and c["score_after"] is not None:
            d = c["score_after"] - c["score_before"]
            score = f'<span class="score {tone(d)}">backtest {fnum(c["score_before"], 3)} → {fnum(c["score_after"], 3)}</span>'
        why = "" if c["accepted"] else f'<p class="rej">{e(c["why"])}</p>'
        rows.append(
            f'<div class="change{"" if c["accepted"] else " rejected"}"><div class="chead">{verdict}'
            f'<span class="pname">{e(name)}</span><span class="mono">{e(c["name"]) if name != c["name"] else ""}</span>'
            f'<span class="twhen">{e(when(c["at"]))}</span></div>'
            f'<div class="delta"><span class="old">{value(c["old"])}</span><span class="arrow">→</span>'
            f'<span class="new">{value(c["new"])}</span>{score}</div>'
            f'<p class="reason">{e(c["reason"])}</p>{why}</div>')
    return "".join(rows)


def limits_block(lim: dict, params: dict) -> str:
    rows = []
    for r in lim["rows"]:
        label, unit = LIMIT_WORD.get(r["name"], (r["name"], "%" if r["name"].endswith("_pct") else
                                                 "$" if r["name"].endswith("_usd") else ""))
        v = r["value"] if r["value"] is not None else r["ceiling"] if r["ceiling"] is not None else r["floor"]
        hard = r["ceiling"] if r["ceiling"] is not None else r["floor"]
        word = "tetto" if r["ceiling"] is not None else "minimo" if r["floor"] is not None else ""

        hard_txt = f'<small>{word} nel codice {amount(hard, unit)}</small>' if hard is not None and word else ""
        rows.append(f'<li><span class="lname">{e(label)}</span><span class="lval">{amount(v, unit)}{hard_txt}</span></li>')
    kill = (pill(lim["kill_reason"], "bad") if not lim["trading_enabled"]
            else pill("kill switch spento", "good"))
    syms = lim["symbols"]
    sym_txt = (f'<p class="syms"><b>{len(syms)}</b> simboli ammessi: '
               + e(", ".join(s.split("/")[0] for s in syms)) + "</p>") if syms else ""
    fixed = ('<ul class="fixed"><li>Niente vendite allo scoperto</li><li>Solo conto paper</li>'
             '<li>Controllo della liquidità prima di ogni acquisto</li>'
             '<li>Le uscite (stop e take profit) valgono a ogni risveglio, qualunque cosa dica il modello</li></ul>')
    return (f'<div class="killrow">{kill}</div><ul class="limits">{"".join(rows)}</ul>{sym_txt}{fixed}'
            if rows else f'<div class="killrow">{kill}</div>' + empty("Limiti non leggibili.") + fixed)


def params_mini(params: dict) -> str:
    keys = [k for k in ("entry_threshold", "stop_atr_mult", "tp_atr_mult", "shortlist_size", "min_edge_mult")
            if k in params]
    if not keys:
        return ""
    return '<div class="pmini">' + "".join(
        f'<div><span>{e(PARAM_WORD.get(k, k))}</span><b>{value(params[k])}</b></div>' for k in keys) + "</div>"


# ---- page -----------------------------------------------------------------------------------

def page(m: dict) -> str:
    a, c = m["account"], m["closed"]
    last = m["wakes"][-1] if m["wakes"] else None
    lim_by = {r["name"]: r for r in m["limits"]["rows"]}
    max_inv = next((lim_by[k]["value"] or lim_by[k]["ceiling"] for k in ("max_invested_pct", "max_exposure_pct")
                    if k in lim_by), None)
    max_pos = next((lim_by[k]["value"] or lim_by[k]["ceiling"] for k in ("max_open_positions", "max_positions")
                    if k in lim_by), None)
    reg = m["regime"]

    status = (pill(f"ultimo risveglio: {STATUS_WORD.get(last['status'], last['status'])}",
                   STATUS_TONE.get(last["status"], "dim")) + f'<span class="since">{ago(last["t"])}</span>'
              if last else pill("in attesa del primo risveglio", "dim"))
    regime_pill = (pill(f"Jev: {REGIME_WORD.get(reg['regime'], reg['regime'])} ×{fnum(reg['multiplier'], 1)}",
                        REGIME_TONE.get(reg["regime"], "calm")) if reg else pill("Jev: nessun dato", "dim"))
    kill_pill = ("" if m["limits"]["trading_enabled"] else
                 pill("KILL SWITCH", "bad") if m["limits"]["kill_reason"].startswith("kill") else
                 pill(m["limits"]["kill_reason"], "warn"))

    base = m["equity_series"][0]["equity"] if m["equity_series"] else None
    inv_bar = ""
    if a["invested_pct"] is not None and max_inv:
        w = min(100.0, a["invested_pct"] / max_inv * 100)
        inv_bar = f'<div class="meter"><b style="width:{w:.1f}%"></b></div>'
    win = c.get("win_rate_pct")
    kpis = "".join([
        kpi("Patrimonio", usd_big(a["equity"]), "conto paper, soldi finti"),
        kpi("Oggi", pct(a["day_pnl_pct"]), usd(a["day_pnl_usd"], sign=True), tone(a["day_pnl_pct"])),
        kpi("Dall'inizio", pct(a["total_pnl_pct"]), usd(a["total_pnl_usd"], sign=True), tone(a["total_pnl_pct"])),
        kpi("Investito", pct(a["invested_pct"], 1, sign=False),
            f"{usd(a['invested_usd'], 0)}" + (f" · tetto {fnum(max_inv, 0)}%" if max_inv else ""), "", inv_bar),
        kpi("Posizioni", f"{len(m['positions'])}" + (f'<small> / {fnum(max_pos, 0)}</small>' if max_pos else ""),
            "aperte, ognuna con stop"),
        kpi("Operazioni chiuse", str(c.get("trades", 0)),
            (f"vinte {fnum(win, 0)}% · netto {usd(c.get('pnl_usd'), sign=True)}" if win is not None
             else "nessuna ancora")
            + (f"<br>esplorazione: {c['explore']['trades']} · netto {usd(c['explore'].get('pnl_usd'), sign=True)}"
               if (c.get("explore") or {}).get("trades") else "")),
    ])
    chart_aside = f'<span class="src">fonte: {e(m["equity_source"])}</span>' if m["equity_source"] else ""
    closed_aside = ""
    if c.get("trades"):
        closed_aside = (f'<span class="src">migliore {pct(c.get("best_pct"), 1)} {e(c.get("best_symbol", ""))} · '
                        f'peggiore {pct(c.get("worst_pct"), 1)} {e(c.get("worst_symbol", ""))}</span>')
    problems = "".join(f"<li>{e(p)}</li>" for p in m["problems"])

    body = f"""
<div class="wrap">
  <header class="top">
    <div class="brand">
      <svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="11" fill="var(--panel-2)"/>
        <path d="M8 27 L16 19 L22 24 L32 12" fill="none" stroke="var(--accent)" stroke-width="3.2"
          stroke-linecap="round" stroke-linejoin="round"/><circle cx="32" cy="12" r="3" fill="var(--good)"/></svg>
      <div><h1>trader</h1><p>Agente autonomo di trading crypto · conto Alpaca <b>paper</b>, soldi finti</p></div>
    </div>
    <div class="status">{status}{regime_pill}{kill_pill}</div>
  </header>
  <div class="kpis">{kpis}</div>
  <div class="grid">
    {panel("Patrimonio", equity_chart(m["equity_series"], base), "span8", chart_aside)}
    {panel("Ultimo passaggio di consegne", handoff_block(m), "span4")}
    {panel("Posizioni aperte", positions_table(m["positions"], max_pos), "span12")}
    {panel("Risvegli", timeline(m["wakes"]), "span12",
           '<span class="src">ogni 15 minuti, riflessione ogni 6 ore</span>')}
    <div class="col span7">
      {panel("Ultime operazioni", trade_cards(m["trades"]), "", closed_aside)}
      {panel("Parametri: modifiche proposte", params_mini(m["params"]) + changes_block(m["param_changes"]))}
    </div>
    <div class="col span5">
      {panel("Lezioni imparate", lessons_block(m["lessons"]))}
      {panel("Limiti rigidi in vigore", limits_block(m["limits"], m["params"]))}
    </div>
  </div>
  <footer class="foot">
    {f'<ul class="problems">{problems}</ul>' if problems else ""}
    <p>Generata dai record del repo il {e(when(m["built_at"], "%d/%m/%Y alle %H:%M"))} UTC ·
    si aggiorna da sola ogni 60 secondi · versione dati {e(m["version"])}</p>
  </footer>
</div>"""
    return TEMPLATE.replace("{{VERSION}}", e(m["version"])).replace("{{BODY}}", body)


TEMPLATE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="data-version" content="{{VERSION}}">
<title>trader · cruscotto</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 40 40%27%3E%3Crect width=%2740%27 height=%2740%27 rx=%2711%27 fill=%27%23181d29%27/%3E%3Cpath d=%27M8 27 L16 19 L22 24 L32 12%27 fill=%27none%27 stroke=%27%235b9cf5%27 stroke-width=%273.2%27 stroke-linecap=%27round%27/%3E%3C/svg%3E">
<style>
:root{
  color-scheme: dark;
  --bg:#0a0c11; --bg-2:#0e1118; --panel:#121620; --panel-2:#181d29; --line:#232a38; --line-2:#2c3445;
  --ink:#eef1f7; --ink-2:#b4bccb; --muted:#7c8699; --accent:#5b9cf5;
  --good:#3fcf8e; --bad:#f2616f; --warn:#f2b347; --violet:#a295f0; --calm:#7fa6d8;
  --sans:"Inter","IBM Plex Sans","Segoe UI",system-ui,-apple-system,sans-serif;
  --mono:"IBM Plex Mono","JetBrains Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html{font-size:18px}
body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#15203a 0%,transparent 60%),
  radial-gradient(900px 500px at 100% 0%,#1a1530 0%,transparent 55%),var(--bg);
  color:var(--ink);font-family:var(--sans);line-height:1.45;-webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums;min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1840px;margin:0 auto;padding:34px 40px 40px}
.top{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:26px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:18px}
.brand svg{width:54px;height:54px;flex:none}
.brand h1{margin:0;font-size:2.1rem;font-weight:700;letter-spacing:-.02em;line-height:1}
.brand p{margin:6px 0 0;color:var(--ink-2);font-size:1.02rem}
.brand p b{color:var(--warn);font-weight:600}
.status{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.since{color:var(--muted);font-size:.95rem;margin-right:8px}
.pill{display:inline-flex;align-items:center;gap:9px;padding:7px 15px;border-radius:999px;font-size:.95rem;
  font-weight:600;background:var(--panel-2);border:1px solid var(--line-2);color:var(--ink);white-space:nowrap}
.pill i{width:9px;height:9px;border-radius:50%;background:currentColor;flex:none}
.pill.good{color:var(--good);background:rgba(63,207,142,.09);border-color:rgba(63,207,142,.28)}
.pill.bad{color:var(--bad);background:rgba(242,97,111,.1);border-color:rgba(242,97,111,.3)}
.pill.warn{color:var(--warn);background:rgba(242,179,71,.1);border-color:rgba(242,179,71,.3)}
.pill.violet{color:var(--violet);background:rgba(162,149,240,.1);border-color:rgba(162,149,240,.3)}
.pill.calm{color:var(--calm);background:rgba(127,166,216,.09);border-color:rgba(127,166,216,.26)}
.pill.dim{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:18px;margin-bottom:18px}
.kpi{background:linear-gradient(180deg,var(--panel-2),var(--panel));border:1px solid var(--line);border-radius:18px;
  padding:20px 22px 18px;position:relative;overflow:hidden}
.kpi .label{color:var(--ink-2);font-size:.9rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.kpi .num{font-size:2.35rem;font-weight:700;letter-spacing:-.02em;margin:6px 0 2px;line-height:1.15;white-space:nowrap}
.kpi .num small{font-size:1.3rem;color:var(--muted);font-weight:600}
.kpi .num small.unit{margin-left:6px}
.kpi .sub{color:var(--muted);font-size:.95rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.up{color:var(--good)} .down{color:var(--bad)}
.meter{height:6px;background:var(--line);border-radius:6px;margin-top:10px;overflow:hidden}
.meter b{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#8ab8ff);border-radius:6px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}
.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
.col{display:flex;flex-direction:column;gap:18px;min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:22px 26px 24px;min-width:0}
.panel>header{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:16px}
.panel h2{margin:0;font-size:1rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2)}
.src{color:var(--muted);font-size:.9rem}
.empty{color:var(--muted);font-size:1.05rem;margin:18px 0;padding:26px;border:1px dashed var(--line-2);border-radius:14px;text-align:center}
.muted{color:var(--muted)}
.chart{position:relative}
.chart svg{width:100%;height:auto;display:block;overflow:visible}
.chart .grid{stroke:var(--line);stroke-width:1}
.chart .tick{fill:var(--muted);font-size:17px;font-family:var(--sans)}
.chart .base{stroke:var(--muted);stroke-dasharray:6 7;stroke-width:1.4;opacity:.7}
.chart .baselabel{fill:var(--ink-2);font-size:16px;paint-order:stroke;stroke:var(--panel);stroke-width:6px;stroke-linejoin:round}
.chart .area{fill:url(#eqfill)}
.chart .line{fill:none;stroke:var(--accent);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.chart .dot{fill:var(--accent);stroke:var(--panel);stroke-width:3}
.chart .halo{fill:var(--accent);opacity:.18}
.chart .cross{stroke:var(--ink-2);stroke-width:1;opacity:0}
.chart .hover{fill:var(--ink);stroke:var(--accent);stroke-width:3;opacity:0}
.chart .tip{position:absolute;pointer-events:none;background:var(--panel-2);border:1px solid var(--line-2);
  border-radius:10px;padding:8px 12px;font-size:.95rem;opacity:0;transform:translate(-50%,-120%);white-space:nowrap}
.chart .tip b{display:block;font-size:1.1rem}
.hohead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.hohead .slot{color:var(--ink-2);font-size:.95rem}
.hohead a{margin-left:auto;font-size:.95rem}
dl.handoff{margin:0;display:grid;gap:12px}
dl.handoff dt{color:var(--muted);font-size:.82rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600}
dl.handoff dd{margin:2px 0 0;font-size:1.05rem;color:var(--ink)}
.warns{margin:14px 0 0;padding:12px 14px 12px 32px;background:rgba(242,179,71,.07);border-radius:12px;color:var(--warn);font-size:.95rem}
pre.progress{white-space:pre-wrap;font-family:var(--mono);font-size:.9rem;color:var(--ink-2);margin:0}
table{width:100%;border-collapse:collapse}
th{font-size:.82rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;text-align:left;
  padding:0 12px 12px;border-bottom:1px solid var(--line)}
td{padding:15px 12px;border-bottom:1px solid var(--line);font-size:1.12rem;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
.r{text-align:right}
td.sym{font-weight:700;font-size:1.2rem}
td.sym small{color:var(--muted);font-weight:500;font-size:.9rem;margin-right:10px}
td.strong{font-weight:600}
td small{display:block;color:var(--muted);font-size:.85rem}
td.sym small{display:inline}
td.stop{color:#f59aa3} td.tp{color:#7fe0b4}
th.bar,td.bar{width:30%;text-align:center}
.tag{display:inline-block;font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  padding:3px 8px;border-radius:6px;background:rgba(242,179,71,.12);color:var(--warn);vertical-align:middle;margin-left:6px}
.tag.prova{background:rgba(162,149,240,.14);color:var(--violet)}
.tag.explore{background:rgba(92,184,230,.14);color:#7cc8ee}
.range{position:relative;height:12px;border-radius:12px;margin:0 8px;
  background:linear-gradient(90deg,rgba(242,97,111,.35),rgba(255,255,255,.07) 45%,rgba(63,207,142,.35))}
.range .fill{position:absolute;left:0;top:0;bottom:0;border-radius:12px;opacity:.0}
.range .entry{position:absolute;top:-6px;bottom:-6px;width:2px;background:var(--ink-2);margin-left:-1px;border-radius:2px}
.range .now{position:absolute;top:50%;width:20px;height:20px;border-radius:50%;background:var(--ink);
  border:4px solid var(--accent);transform:translate(-50%,-50%);box-shadow:0 0 0 4px rgba(91,156,245,.18)}
.strip{display:flex;align-items:flex-end;gap:3px;overflow:hidden;height:74px;padding:4px 0}
.strip .tick{flex:1;min-width:0;border-radius:4px 4px 2px 2px;background:var(--line-2);display:block;transition:opacity .15s}
.strip .tick:hover{opacity:.7}
.tick.good{background:var(--good)} .tick.bad{background:var(--bad)} .tick.warn{background:var(--warn)}
.tick.calm{background:#3a4d6b} .tick.violet{background:var(--violet)} .tick.dim{background:#2b3140}
.stripaxis{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:.9rem;margin-top:10px;gap:16px}
.legend{display:flex;gap:18px;flex-wrap:wrap;justify-content:center}
.lg{display:inline-flex;align-items:center;gap:7px;color:var(--ink-2)}
.lg i{width:12px;height:12px;border-radius:3px;background:#2b3140}
.lg.good i{background:var(--good)} .lg.bad i{background:var(--bad)} .lg.warn i{background:var(--warn)}
.lg.calm i{background:#3a4d6b} .lg.violet i{background:var(--violet)}
.lg b{color:var(--ink);font-weight:600}
ul.wakes{list-style:none;margin:18px 0 0;padding:0;display:grid;grid-template-columns:1fr 1fr;column-gap:36px}
ul.wakes li{display:grid;grid-template-columns:128px 210px 90px 1fr auto;align-items:center;gap:12px;padding:10px 0;
  border-top:1px solid var(--line);font-size:1rem}
ul.wakes .slot{color:var(--ink-2);font-family:var(--mono);font-size:.95rem}
ul.wakes .pill{font-size:.85rem;padding:4px 11px;justify-self:start}
ul.wakes .orders{color:var(--ink-2);font-size:.92rem}
ul.wakes .note{color:var(--muted);font-size:.92rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trades{display:grid;gap:14px}
.trade{background:var(--bg-2);border:1px solid var(--line);border-radius:16px;padding:16px 20px}
.thead{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.side{font-size:.8rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;padding:4px 10px;border-radius:7px}
.side.buy{background:rgba(63,207,142,.14);color:var(--good)} .side.sell{background:rgba(242,97,111,.14);color:var(--bad)}
.side.hold{background:var(--panel-2);color:var(--muted)}
.tsym{font-weight:700;font-size:1.2rem}
.tmeta{color:var(--ink-2);font-size:1rem}
.twhen{margin-left:auto;color:var(--muted);font-size:.92rem;font-family:var(--mono)}
.why{margin:10px 0 0;font-size:1.05rem;color:var(--ink)}
.why b{color:var(--ink-2);font-weight:600}
.probe{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
.probe>div{border-radius:12px;padding:10px 14px;font-size:.98rem;color:var(--ink-2)}
.probe span{display:block;font-size:.75rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin-bottom:3px}
.pro{background:rgba(63,207,142,.07);border-left:3px solid var(--good)} .pro span{color:var(--good)}
.con{background:rgba(242,97,111,.07);border-left:3px solid var(--bad)} .con span{color:var(--bad)}
.tfoot{margin:10px 0 0;color:var(--muted);font-size:.9rem}
.lesson{padding:14px 0;border-top:1px solid var(--line)}
.lesson:first-child{border-top:0;padding-top:0}
.lesson h3{margin:0 0 8px;font-size:.9rem;font-weight:600;color:var(--muted);font-family:var(--mono)}
.lesson ul{margin:0;padding-left:22px}
.lesson li{margin:5px 0;font-size:1.04rem;color:var(--ink-2)}
.lesson.newest li{color:var(--ink);font-size:1.1rem}
.lesson.newest h3{color:var(--violet)}
.pmini{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px}
.pmini div{background:var(--bg-2);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.pmini span{display:block;color:var(--muted);font-size:.78rem;line-height:1.25;min-height:2em}
.pmini b{font-size:1.35rem}
.change{padding:14px 0;border-top:1px solid var(--line)}
.chead{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.chead .pill{font-size:.82rem;padding:4px 11px}
.pname{font-weight:700;font-size:1.08rem}
.mono{font-family:var(--mono);color:var(--muted);font-size:.85rem}
.delta{display:flex;align-items:baseline;gap:12px;margin:8px 0 2px;font-size:1.3rem}
.delta .old{color:var(--muted);text-decoration:line-through;text-decoration-thickness:1px}
.delta .arrow{color:var(--muted)}
.delta .new{font-weight:700}
.score{font-size:.92rem;margin-left:10px}
.reason{margin:4px 0 0;color:var(--ink-2);font-size:1rem}
.rej{margin:4px 0 0;color:#f59aa3;font-size:.95rem}
.change.rejected .new{color:var(--ink-2)}
.killrow{margin-bottom:12px}
ul.limits{list-style:none;margin:0;padding:0}
ul.limits li{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:11px 0;border-top:1px solid var(--line)}
.lname{color:var(--ink-2);font-size:1.02rem}
.lval{font-weight:700;font-size:1.2rem;text-align:right}
.lval small{display:block;font-weight:500;font-size:.8rem;color:var(--muted)}
.syms{color:var(--ink-2);font-size:.95rem;margin:14px 0 0}
ul.fixed{margin:14px 0 0;padding-left:20px;color:var(--muted);font-size:.95rem}
.foot{margin-top:26px;color:var(--muted);font-size:.9rem;text-align:center}
.problems{display:inline-block;text-align:left;color:var(--warn);background:rgba(242,179,71,.07);border-radius:12px;padding:10px 16px 10px 34px}
@media (max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.span4,.span5,.span7,.span8{grid-column:span 12}
  ul.wakes{grid-template-columns:1fr}}
@media (max-width:700px){html{font-size:15px}.strip{gap:1px}.top{align-items:flex-start}.stripaxis{flex-wrap:wrap}.wrap{padding:20px 16px}.kpis{grid-template-columns:1fr 1fr}
  ul.wakes li{grid-template-columns:1fr auto}ul.wakes .note,ul.wakes .orders{display:none}
  .probe{grid-template-columns:1fr}.pmini{grid-template-columns:1fr 1fr}.positions{display:block;overflow-x:auto}}
</style>
</head>
<body>
{{BODY}}
<script>
(function(){
  // Relative times ("12 min fa"), kept current without reloading.
  function rel(ts){
    var s=(Date.now()-Date.parse(ts))/1000;
    if(!isFinite(s))return "";
    if(s<60)return "adesso";
    if(s<3600)return Math.round(s/60)+" min fa";
    if(s<86400)return Math.round(s/3600)+" ore fa";
    return Math.round(s/86400)+" giorni fa";
  }
  function tick(){document.querySelectorAll(".ago").forEach(function(el){
    var r=rel(el.dataset.ts); if(r){el.textContent=r;}});}
  tick(); setInterval(tick,30000);

  // Crosshair and tooltip on the equity curve.
  document.querySelectorAll(".chart[data-points]").forEach(function(box){
    var pts=JSON.parse(box.dataset.points), svg=box.querySelector("svg"), tip=box.querySelector(".tip"),
        cross=svg.querySelector(".cross"), dot=svg.querySelector(".hover");
    function hide(){tip.style.opacity=0;cross.style.opacity=0;dot.style.opacity=0;}
    svg.addEventListener("mouseleave",hide);
    svg.addEventListener("mousemove",function(ev){
      var r=svg.getBoundingClientRect(), vb=svg.viewBox.baseVal, x=(ev.clientX-r.left)/r.width*vb.width;
      var lo=0,hi=pts.length-1;
      while(hi-lo>1){var mid=(lo+hi)>>1; if(pts[mid][0]<x)lo=mid;else hi=mid;}
      var p=Math.abs(pts[lo][0]-x)<Math.abs(pts[hi][0]-x)?pts[lo]:pts[hi];
      cross.setAttribute("x1",p[0]);cross.setAttribute("x2",p[0]);cross.style.opacity=.5;
      dot.setAttribute("cx",p[0]);dot.setAttribute("cy",p[1]);dot.style.opacity=1;
      tip.innerHTML="<b></b><span></span>";tip.firstChild.textContent=p[3];tip.lastChild.textContent=p[2]+" UTC";
      tip.style.left=(p[0]/vb.width*r.width)+"px";tip.style.top=(p[1]/vb.height*r.height)+"px";tip.style.opacity=1;
    });
  });

  // Every 60 s: reload only if the records changed (the build writes a new version).
  var mine=document.querySelector('meta[name="data-version"]').content;
  setInterval(function(){
    fetch("data.json?t="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();})
      .then(function(d){if(d.version&&d.version!==mine){location.reload();}}).catch(function(){});
  },60000);
})();
</script>
</body>
</html>
"""
