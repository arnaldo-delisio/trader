"""The step-by-step trace a wake-up prints to stdout, in Italian: one line per step.

Read in the Actions log and filmed in a terminal, so it is plain text with colours
only when stdout is a terminal. Every line passes through redact() like the records.
Words come from notify, so the terminal and Telegram say the same thing.
"""

from __future__ import annotations

import contextlib
import os
import sys
import textwrap
from typing import TextIO

from .context import f
from .notify import (
    ACTION_WORD,
    DRY_RUN_WORD,
    OUTCOME_WORD,
    STATUS_WORD,
    pct,
    slot_label,
    usd,
)
from .records import redact

GREEN, RED, YELLOW, CYAN, DIM, BOLD = "32", "31", "33", "36", "2", "1"
LABEL_WIDTH = 13
WIDTH = 100  # columns: long lines wrap under the text, so the log reads on any screen
INDENT = LABEL_WIDTH + 3


def use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(code: str | None, text: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on and code else text


class Trace:
    def __init__(self, stream: TextIO | None = None, secrets: list[str] | None = None):
        self.stream = stream  # None: sys.stdout at write time, so pytest's capture still works
        self.secrets = secrets or []

    def bind(self, secrets: list[str]) -> Trace:
        return Trace(self.stream, secrets)

    def _write(self, line: str) -> None:
        out = self.stream or sys.stdout
        with contextlib.suppress(Exception):  # the trace must never break a wake
            print(redact(line, self.secrets), file=out, flush=True)

    def step(self, label: str, text: str, tone: str | None = None) -> None:
        on = use_color(self.stream or sys.stdout)
        first, *rest = _wrap(text)
        self._write(f"{paint(CYAN, '▸', on)} {paint(BOLD, label.ljust(LABEL_WIDTH), on)} {paint(tone, first, on)}")
        for line in rest:
            self._write(" " * INDENT + paint(tone, line, on))

    def detail(self, text: str) -> None:
        on = use_color(self.stream or sys.stdout)
        for line in _wrap(text):
            self._write(" " * INDENT + paint(DIM, line, on))

    # ---- the steps of a wake, in order ------------------------------------
    def slot(self, slot_iso: str, slot_id: str, dry_run: bool) -> None:
        extra = f" · {DRY_RUN_WORD}: nessun ordine verrà inviato" if dry_run else ""
        self.step("slot", f"{slot_label(slot_iso)} · {slot_id}{extra}", YELLOW if dry_run else None)

    def context(self, snap, positions: list[dict], open_orders: list[dict], fills: int) -> None:
        day = f" ({(snap.equity / snap.last_equity - 1) * 100:+.2f}% oggi)".replace(".", ",") \
            if snap.last_equity else ""
        self.step("contesto", f"da Alpaca: equity {usd(snap.equity)}{day} · liquidità {usd(snap.cash)}")
        held = ", ".join(f"{p.get('symbol')} {usd(f(p.get('market_value')))}" for p in positions) or "nessuna"
        opened = ", ".join(f"{o.get('symbol')} {o.get('side')}" for o in open_orders) or "nessuno"
        self.detail(f"posizioni: {held} · ordini aperti: {opened} · fill nuovi: {fills}")

    def reconcile(self, recovered: list[str]) -> None:
        if recovered:
            self.step("riconcilia", f"recuperati {len(recovered)} ordini non registrati: {', '.join(recovered)}", YELLOW)
        else:
            self.step("riconcilia", "Alpaca e record concordano: nessun ordine da recuperare", GREEN)

    def universe(self, n: int, gone: list[str]) -> None:
        more = f" e altre {len(gone) - 5}" if len(gone) > 5 else ""
        self.step("universo", f"{n} crypto negoziabili"
                  + (f" · {len(gone)} non negoziabili ora: {', '.join(gone[:5])}{more}" if gone else ""),
                  YELLOW if gone else None)

    def market(self, m, n: int) -> None:
        if m.basket_7d_pct is None:
            self.step("mercato", "nessun dato utilizzabile: niente acquisti", RED)
            return
        gate = "aperto" if m.regime_ok else "chiuso"
        self.step("mercato", f"indicatori su {len(m.views)}/{n} crypto · paniere 7g {pct(m.basket_7d_pct)} · "
                             f"in tendenza 4h {m.breadth_pct:.0f}% · filtro acquisti {gate}",
                  GREEN if m.regime_ok else YELLOW)
        if m.missing:
            self.detail(f"senza dati sufficienti: {', '.join(m.missing)}")

    def positions(self, tracked: dict) -> None:
        if not tracked:
            self.step("posizioni", "nessuna posizione aperta")
            return
        for s, p in tracked.items():
            lv = p.levels
            tail = f" → USCITA: {p.exit}" if p.exit else ""
            self.step("posizione", f"{s} {usd(p.value)} · prezzo {p.price:.6g} · stop {lv['stop']:.6g} · "
                                   f"take-profit {lv['take_profit']:.6g} ({p.source}){tail}",
                      RED if p.exit else None)

    def candidates(self, ranked: list, eligible: list, entries: dict, regime, shortlist: list, m) -> None:
        top = ", ".join(f"{s} {m.views[s].score:+.2f}" for s in shortlist[:5] if s in m.views)
        self.step("punteggi", f"migliori: {top or 'nessuno'}")
        if not ranked:
            self.step("candidati", "nessuno supera le regole d'ingresso", DIM)
            best = next((m.views[s] for s in shortlist if s in m.views), None)
            if best:
                self.detail(f"{best.symbol}: {best.entry_reason}")
            return
        skipped = [v.symbol for v in ranked if v not in eligible]
        if skipped:
            self.detail(f"già in portafoglio, in pausa o con ordine aperto: {', '.join(skipped)}")
        if not entries and eligible and not regime:
            self.step("candidati", f"{', '.join(v.symbol for v in eligible)}: kill switch attivo, nessun acquisto",
                      YELLOW)
        if regime:
            tone = GREEN if not regime.error else YELLOW
            self.step("regime", f"Jev ({regime.source}): {regime.regime} → dimensioni x{regime.multiplier:g}"
                                + (f" · {regime.error}" if regime.error else ""), tone)
        for s, n in entries.items():
            self.step("candidato", f"{s} score {m.views[s].score:+.2f} · al massimo {usd(n)}", GREEN)

    def proposal(self, model_name: str, proposal) -> None:
        if not proposal.valid:
            self.step("modello", f"{model_name}: {proposal.error} → tutto hold", RED)
            return
        self.step("modello", f"{model_name}: {proposal.market_view}")
        for d in proposal.decisions:
            self.step("proposta", f"{d.symbol} {_action(d.action, d.notional_usd)} · {d.reason}")
            self.detail(f"pro: {d.bull_case} · contro: {d.bear_case}")

    def verdict(self, row: dict) -> None:
        what = f"{row['symbol']} {_action(row['action'], row['requested_usd'])}"
        if row.get("explore"):
            what += " (esplorazione)"
        if row["action"] == "hold":
            self.step("gate", f"{what} → nessun ordine", DIM)
        elif row["approved"]:
            self.step("gate", f"{what} → ammesso", GREEN)
        else:
            self.step("gate", f"{what} → respinto: {row['reason']}", YELLOW)

    def order(self, row: dict) -> None:
        tone = {"placed": GREEN, "adopted": GREEN, "dry_run": YELLOW}.get(row["outcome"], RED)
        self.step("ordine", f"{row['symbol']} → {OUTCOME_WORD.get(row['outcome'], row['outcome'])} · "
                            f"{row['client_order_id']}", tone)
        if row["outcome"] not in ("placed", "dry_run") and row.get("detail"):
            self.detail(row["detail"])

    def outcome(self, h: dict) -> None:
        tone = {"ok": GREEN, "no_trade": None, "failed": RED, "killed": RED}.get(h["status"], YELLOW)
        self.step("esito", f"{STATUS_WORD.get(h['status'], h['status'])} · {h.get('outcome') or ''}", tone)
        if h.get("error"):
            self.step("errore", h["error"], RED)

    def records(self, root, written: list[str]) -> None:
        self.step("record", f"{', '.join(written) or 'nessuno'} in {root}")

    def reflection(self, r: dict) -> None:
        if r.get("error"):
            self.step("riflessione", r["error"], RED)
            return
        s = r.get("summary") or {}
        self.step("riflessione", f"{s.get('trades', 0)} operazioni chiuse · {len(r.get('lessons', []))} lezioni · "
                                 f"{len(r.get('accepted', []))} modifiche accettate, {len(r.get('rejected', []))} respinte")
        for a in r.get("accepted", []):
            self.detail(f"accettata {a['name']}: {a['old_value']} → {a['new_value']}")
        for x in r.get("rejected", []):
            self.detail(f"respinta {x['name']} → {x['new_value']}: {x['why']}")

    def telegram(self, ok: bool, detail: str, dry_run: bool, what: str = "messaggio") -> None:
        tag = f" ({DRY_RUN_WORD})" if dry_run else ""
        if ok:
            self.step("telegram", f"{what} {detail}{tag}", GREEN)
        else:
            self.step("telegram", f"{what} NON inviato: {detail}", RED)


def _action(action: str, amount: float) -> str:
    word = ACTION_WORD.get(action, action)
    return word if action == "hold" else f"{word} {usd(amount)}"


def _wrap(text: str) -> list[str]:
    return textwrap.wrap(str(text), WIDTH - INDENT, break_long_words=True, break_on_hyphens=False) or [""]
