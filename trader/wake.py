"""One wake-up, start to finish. It always writes a handoff, and it notifies when there is
something to say: a trade, a failure, a reflection, the 6-hour status, the daily summary.

Order inside a wake:
  1. continuity with the previous handoff, then Alpaca (the source of truth), reconcile
  2. universe, quotes, 15m bars for 10 days, indicators and scores (strategy.py)
  3. open positions: exit levels updated and checked; exits come first, whatever the model says
  4. buy candidates (the strategy's rule), sized; Jev's regime scales the size
  5. the model, only when there is a candidate; it proposes, the gate decides
  6. orders (idempotent), exit levels for new positions, records
  7. every 6 hours: the reflection (learn.py) and the status message; once a day: the summary
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import backtest as bt
from . import context, jev, learn, notify
from . import strategy as st
from .broker import Alpaca, BrokerHTTPError, BrokerUnavailable
from .config import (
    EXIT_MIN_ORDER_USD,
    HARD_SYMBOLS,
    ORDER_PREFIX,
    ConfigError,
    Limits,
    is_memecoin,
    trading_enabled,
)
from .model import Decision, Proposal
from .orders import OPEN_STATUSES, place, slot_prefix
from .records import Records
from .risk import gate, open_positions
from .slot import (
    cadence_bucket,
    is_last_slot_of_day,
    missed_count,
    previous_slot,
    slot_id,
)
from .trace import RED, YELLOW, Trace

HISTORY_DAYS = 10     # 15m bars per wake: the 4h indicators need about 9 days to warm up
REFLECT_DAYS = 14     # the walk-forward window of the reflection's backtest
EXIT_WORD = {"stop": "stop", "take_profit": "take-profit", "tempo": "tenuta massima",
             "segnale": "segnale in calo"}


@dataclass
class Deps:
    root: Path                       # repo root: prompts/, config/, KILL
    records: Records
    limits: Limits
    send: notify.Sender
    build: Callable[[], tuple[Alpaca, object]]  # -> (alpaca, model); may raise ConfigError
    env: dict = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    run_url: str = ""
    trace: Trace = field(default_factory=Trace)
    classify: Callable | None = None  # state -> jev.Regime; default jev.classify with env's keys
    reflect: bool | None = None       # None: once per 6-hour bucket; True: now; False: never
    always_ask: bool = False          # ask the model even without buy candidates (local checks)
    judge: Callable | None = None  # (alpaca, symbols, spreads, deps, now) -> judge(current, candidate)


def wake(deps: Deps, slot: datetime, dry_run: bool = False) -> int:
    """Returns the process exit code: 0 ok, 1 failure, 2 configuration error."""
    h = {"slot": slot.isoformat(), "slot_id": slot_id(slot), "started_at": deps.now().isoformat(),
         "run_url": deps.run_url, "dry_run": dry_run, "status": "failed", "decisions": [],
         "warnings": [], "alerts": [], "changed": "", "outcome": "",
         "remaining_risk": "sconosciuto: stato non letto",
         "next_job": "ricostruire lo stato da Alpaca e verificare l'errore del risveglio precedente",
         "equity": None, "error": "", "positions": []}
    journal = {"kind": "wake", "slot": slot_id(slot), "dry_run": dry_run}
    trace = deps.trace.bind(deps.records.secrets)
    trace.slot(h["slot"], h["slot_id"], dry_run)
    ctx: dict = {}
    code = 1
    try:
        _run(deps, slot, dry_run, h, journal, trace, ctx)
        code = 0
    except ConfigError as e:
        h["error"], code = f"configurazione: {e}", 2
        h["outcome"] = "il programma non è partito: configurazione incompleta"
    except BrokerUnavailable as e:
        h["error"] = f"Alpaca non raggiungibile: {e}"
        h["outcome"] = "nessun ordine: Alpaca non raggiungibile"
    except Exception as e:  # noqa: BLE001 - any failure must still end in a handoff and a notice
        h["error"] = f"{type(e).__name__}: {e}"
        h["outcome"] = "interrotto da un errore inatteso, nessun altro ordine inviato"
        traceback.print_exc()
    finally:
        h["finished_at"] = deps.now().isoformat()
        _finish(deps, slot, h, journal, trace, ctx)
    if code == 0 and ctx.get("alpaca") and h["status"] not in ("already_done", "stale_slot"):
        try:
            _maybe_reflect(deps, slot, h, trace, ctx)
        except Exception as e:  # noqa: BLE001 - a reflection never fails the wake
            print(f"ERRORE riflessione: {type(e).__name__}: {e}", file=sys.stderr)
    _cadence(deps, slot, h, trace, ctx)
    if h["error"]:
        print(f"ERRORE: {h['error']}", file=sys.stderr)
    return code


def _alert(h: dict, text: str) -> None:
    """A warning worth a Telegram message."""
    h["warnings"].append(text)
    h["alerts"].append(text)


def _run(deps: Deps, slot: datetime, dry_run: bool, h: dict, journal: dict, trace: Trace, ctx: dict) -> None:
    rec, limits = deps.records, deps.limits
    now = deps.now()
    prev = rec.last_handoff()
    stale = _check_continuity(prev, slot, h, journal)

    alpaca, model = deps.build()
    ctx["alpaca"], ctx["model"] = alpaca, model
    journal["model"] = getattr(model, "name", type(model).__name__)

    # 1. Alpaca, the source of truth
    account = alpaca.account()
    positions = alpaca.positions()
    open_orders = alpaca.orders("open")
    recent = alpaca.orders("all", after=(now - timedelta(days=7)).isoformat())
    fills = alpaca.fills(after=(now - timedelta(days=8)).isoformat())

    # reconcile: our orders Alpaca knows about and the records do not
    known = rec.known_client_ids(now)
    ours = {o["client_order_id"]: o for o in recent + open_orders
            if str(o.get("client_order_id", "")).startswith(ORDER_PREFIX)}
    recovered = [o for cid, o in ours.items() if cid not in known]
    for o in recovered:
        rec.evidence("order", o, now, note="recuperato: inviato da un risveglio che non l'ha registrato")
    if recovered:
        _alert(h, f"recuperati {len(recovered)} ordini non registrati: "
                  + ", ".join(o["client_order_id"] for o in recovered))
    known_fills = rec.known_fill_ids(now)
    new_fills = [x for x in fills if x.get("id") not in known_fills]
    for x in new_fills:
        rec.evidence("fill", x, now)
    journal["recovered_orders"] = [o["client_order_id"] for o in recovered]
    journal["new_fills"] = len(new_fills)

    enabled, why = trading_enabled(deps.root, deps.env)
    assets = alpaca.assets()
    symbols, gone = context.universe(assets, limits)
    held_syms = sorted({context.pair(p["symbol"]) for p in positions if p.get("asset_class", "crypto") == "crypto"})
    watch = sorted(set(symbols) | set(held_syms))
    try:
        quotes = alpaca.latest_quotes(watch)
    except (BrokerUnavailable, BrokerHTTPError) as e:
        # Without quotes nothing is bought (no ask); exits still go at the position's own price.
        quotes = {context.pair(p["symbol"]): {"bp": p.get("current_price"), "ap": 0}
                  for p in positions if p.get("asset_class", "crypto") == "crypto"}
        _alert(h, f"prezzi non disponibili ({type(e).__name__}): niente acquisti, uscite al prezzo della posizione")
    snap = context.snapshot(account, positions, open_orders, quotes, assets)
    ctx["snap"] = snap
    h["equity"] = snap.equity
    h["day_change_pct"] = (snap.equity / snap.last_equity - 1) * 100 if snap.last_equity else None
    journal["equity"] = snap.equity
    trace.context(snap, positions, open_orders, len(new_fills))
    trace.reconcile(journal["recovered_orders"])
    trace.universe(len(symbols), gone)
    if not enabled:
        trace.step("kill switch", f"attivo ({why}): ogni ordine sarà respinto", RED)

    slot_orders = [cid for cid in ours if cid.startswith(slot_prefix(slot))]
    same_slot_done = (prev and prev.get("slot") == slot.isoformat() and not prev.get("dry_run")
                      and prev.get("status") in ("ok", "no_trade"))
    if slot_orders or same_slot_done or stale:
        # Someone already acted in this slot (or the slot is older than the records): read only.
        h["status"] = "stale_slot" if stale else "already_done"
        h["outcome"] = ("slot più vecchio dell'ultimo handoff: nessuna decisione" if stale else
                        f"slot già eseguito ({len(slot_orders)} ordini con questo slot): nessun nuovo ordine")
        journal["skipped"] = h["outcome"]
        trace.step("sola lettura", h["outcome"], YELLOW)
        _summarise_state(h, snap, alpaca, prev)
        return

    # 2. market: indicators and scores for the whole universe
    params = st.load_params(deps.root / "config/params.json")
    t = int(now.timestamp())
    try:
        bars = alpaca.bars_each(watch, "15Min", (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except (BrokerUnavailable, BrokerHTTPError) as e:
        # Without bars there are no scores and no buys; stops and take-profits still hold.
        bars = {}
        _alert(h, f"barre non disponibili ({type(e).__name__}): niente acquisti, stop e take-profit restano")
    spreads = context.spreads_pct(quotes)
    m = context.market(bars, symbols, t, params, spreads)
    held_frames = context.market(bars, [s for s in held_syms if s not in m.frames], t, params, spreads)
    m.frames.update(held_frames.frames)
    ctx.update(symbols=symbols, spreads=spreads, market=m)
    journal["market"] = {"coins": len(m.views), "missing": m.missing, "regime_ok": m.regime_ok,
                         "basket_7d_pct": m.basket_7d_pct, "breadth_pct": m.breadth_pct}
    h["market"] = journal["market"]
    trace.market(m, len(symbols))

    # 3. open positions: exit levels, then the code's own exits
    stored = rec.read_state("positions").get("positions", {})
    # Every position Alpaca would let us sell whole is tracked, even under min_order_usd:
    # an exit only needs Alpaca's own minimum (risk.exit_min_usd).
    tracked, notes = context.track(positions, stored, m, fills, snap.bids, params, EXIT_MIN_ORDER_USD)
    for n in notes:
        if "ricostruiti" in n:
            _alert(h, n)
        else:
            h["warnings"].append(n)
    exits = [Decision(s, "sell", round(p.qty * p.price, 2),
                      f"uscita automatica ({EXIT_WORD[p.exit]}): prezzo {p.price:.6g}, stop {p.levels['stop']:.6g}, "
                      f"take-profit {p.levels['take_profit']:.6g}")
             for s, p in tracked.items() if p.exit]
    trace.positions(tracked)
    ctx["positions"] = tracked

    # 4. buy candidates: the strategy's rule, sized, capped like the gate will cap them
    cool = context.cooldowns(recent, params)
    held_keys = {k for k, x in snap.holdings.items() if x.market_value >= 1.0}
    ranked = sorted((v for v in m.views.values() if v.entry), key=lambda v: v.score, reverse=True)
    eligible = [v for v in ranked if v.symbol.replace("/", "") not in held_keys
                and cool.get(v.symbol, 0) <= t and v.symbol.replace("/", "") not in snap.open_order_symbols]
    shortlist = st.shortlist(m.views, list(tracked), int(params["shortlist_size"]))
    journal["shortlist"] = shortlist
    journal["signals"] = {s: {k: round(c, 3) for k, c in m.views[s].components.items()}
                          for s in shortlist if s in m.views}
    # exploration: shortlisted coins below the entry rule with a positive 4h trend, small and capped
    _relabel_exploration(tracked, recent, rec, now)
    # held exploration positions, and exploration buys still waiting to fill (their levels are stored)
    explore_held = frozenset(s.replace("/", "") for s, p in tracked.items() if p.levels.get("explore")) | frozenset(
        s.replace("/", "") for s, lv in stored.items()
        if isinstance(lv, dict) and lv.get("explore") and s.replace("/", "") in snap.pending_buys)
    explore_room = min(snap.equity * limits.explore_total_pct / 100
                       - sum(p.value for s, p in tracked.items() if p.levels.get("explore"))
                       - sum(v for k, v in snap.pending_buys.items() if k in explore_held),
                       snap.equity * limits.explore_position_pct / 100)
    explorable = [m.views[s] for s in shortlist if s in m.views and st.explore_ok(m.views[s], params)[0]
                  and s.replace("/", "") not in held_keys and cool.get(s, 0) <= t
                  and s.replace("/", "") not in snap.open_order_symbols] if explore_room >= limits.min_order_usd else []
    regime = None
    entries: dict[str, float] = {}
    explore: dict[str, float] = {}
    if (eligible or explorable) and not enabled:
        # nothing may be bought: ask nobody
        journal["blocked_candidates"] = [v.symbol for v in eligible + explorable]
    elif eligible or explorable:
        regime = _classify(deps, context.regime_state(m))
        journal["jev"] = regime.as_dict()
        if regime.error.startswith("bug"):
            _alert(h, f"Jev: {regime.error}")
        entries = _size(eligible, snap, limits, params, regime.multiplier)
        explore = _size_explore(explorable, snap, limits, regime.multiplier, entries, tracked, explore_held)
    ctx["regime"] = regime
    h["regime"] = regime.regime if regime else None
    journal["candidates"] = entries
    journal["explore_candidates"] = explore
    trace.candidates(ranked, eligible, entries, regime, shortlist, m)
    if explore:
        trace.step("esplorazione", ", ".join(f"{k} fino a {v:.2f}$" for k, v in explore.items()))

    # 5. the model: only when there is something to decide
    proposal = None
    if entries or explore or deps.always_ask:
        watch_only = [s for s in shortlist if s not in entries and s not in explore and s not in tracked]
        prompt = context.render_prompt(
            context.load_text(deps.root / "prompts/decide.md"), slot=context.iso(int(slot.timestamp())), snap=snap, m=m,
            candidates=entries, explore=explore, watch=watch_only, positions=tracked, params=params,
            regime=regime or jev.Regime("neutral", jev.multiplier_for("neutral"), "none", None, None, None, 0,
                                        "non interpellato"),
            trades=_recent_trades(fills, recent, rec, now), lessons=learn.recent_lessons(deps.root, 2),
            prev=prev, enabled=enabled)
        try:
            proposal = model.propose(prompt)
        except Exception as e:  # noqa: BLE001 - adapters should not raise; if one does, hold
            proposal = Proposal.hold(f"errore del modello: {type(e).__name__}")
        trace.proposal(journal["model"], proposal)
        journal["proposal"] = ({"decisions": [d.__dict__ for d in proposal.decisions],
                                "market_view": proposal.market_view, "next_job": proposal.next_job}
                               if proposal.valid else {"error": proposal.error})
        if not proposal.valid:
            h["model_error"] = proposal.error
            _alert(h, f"modello non utilizzabile: {proposal.error}")
        h["market_view"] = proposal.market_view
    else:
        journal["proposal"] = {"skipped": "nessun candidato d'ingresso né di esplorazione: modello non interpellato"}

    # the code's exits first; the model cannot override them
    exiting = {d.symbol for d in exits}
    decisions = exits + [d for d in (proposal.decisions if proposal and proposal.valid else [])
                         if d.symbol not in exiting]

    # 6. deterministic gate, then orders
    verdicts = gate(decisions, snap, limits, enabled=enabled, disabled_reason=why, entries=entries,
                    explore=explore, explore_held=explore_held)
    rows, new_levels, exit_rows = [], {}, []
    for v, d in zip(verdicts, decisions):
        row = v.as_dict()
        row.update(reason_model=d.reason, bull_case=d.bull_case, bear_case=d.bear_case,
                   auto=d.symbol in exiting and d.action == "sell")
        if row["auto"]:
            row["exit"] = tracked[d.symbol].exit
            row["stop"] = tracked[d.symbol].levels["stop"]
        trace.verdict(row)
        if v.approved:
            p = place(alpaca, slot, v.order, open_orders, dry_run=dry_run)
            row.update(outcome=p.outcome, client_order_id=p.client_order_id, detail=p.detail)
            trace.order(row)
            if p.order:
                rec.evidence("order", p.order, deps.now(), note=p.outcome)
            if p.outcome in ("unconfirmed", "rejected"):
                _alert(h, f"{p.client_order_id}: {p.detail}")
            if v.action == "buy" and p.outcome in ("placed", "adopted"):
                view = m.views[v.symbol]
                price = context.f((p.order or {}).get("filled_avg_price")) or snap.asks.get(v.symbol) or view.features["price"]
                lv = st.open_levels(price, t, view.features["atr_1h"], params)
                new_levels[v.symbol] = {**lv, "entry_at": context.iso(t), "client_order_id": p.client_order_id,
                                        "explore": v.explore}
                row.update(stop=lv["stop"], take_profit=lv["take_profit"])
            if row["auto"] and p.outcome in ("placed", "adopted", "dry_run"):
                exit_rows.append({"client_order_id": p.client_order_id, "symbol": v.symbol,
                                  "reason": EXIT_WORD[row["exit"]]})
        elif v.action != "hold" and row["auto"]:
            _alert(h, f"uscita {EXIT_WORD[row['exit']]} su {v.symbol} respinta: {v.reason}")
        rows.append(row)
    h["decisions"] = rows
    journal["verdicts"] = rows
    journal["exits"] = exit_rows

    # 7. exit levels of what is held now, plus what was just bought or is still being bought
    pending = {context.pair(o["symbol"]) for o in open_orders if o.get("side") == "buy"}
    state = {s: p.levels for s, p in tracked.items()}
    state.update({s: lv for s, lv in stored.items() if s in pending and s not in state})
    if not dry_run:
        state.update(new_levels)
    rec.write_state("positions", {"updated_at": now.isoformat(), "positions": state})
    h["positions"] = [{"symbol": s, "value": round(p.value, 2), "stop": p.levels["stop"],
                       "take_profit": p.levels["take_profit"], "entry_price": p.levels["entry_price"],
                       "price": p.price, "exit": p.exit, "explore": bool(p.levels.get("explore"))}
                      for s, p in tracked.items()]

    acted = [r for r in rows if r.get("outcome") in ("placed", "adopted", "dry_run")]
    if not enabled:
        h["status"] = "killed"
        h["outcome"] = f"kill switch attivo ({why}): nessun ordine"
    elif acted:
        h["status"] = "ok"
        h["outcome"] = "; ".join(f"{notify.ACTION_WORD[r['action']]} {r['symbol']}: "
                                 f"{notify.OUTCOME_WORD[r['outcome']]}" for r in acted)
    else:
        h["status"] = "no_trade"
        h["outcome"] = "nessun ordine: " + (
            "modello non utilizzabile" if proposal and not proposal.valid else
            "tutto hold o respinto dal gate" if decisions else
            "il modello non ha proposto acquisti" if entries or explore else
            "nessun candidato d'ingresso e nessuna uscita")
    h["changed"] = ", ".join(f"{r['client_order_id']} ({r['outcome']})" for r in rows if r.get("outcome")) or "niente"
    h["next_job"] = (proposal.next_job if proposal and proposal.valid and proposal.next_job
                     else "ricontrollare stop e candidati con le nuove barre")
    _summarise_state(h, snap, alpaca, prev)


def _classify(deps: Deps, state: dict):
    if deps.classify:
        return deps.classify(state)
    thresholds, problem = jev.load_thresholds(deps.root)
    r = jev.classify(state, env=deps.env, thresholds=thresholds)
    if problem:
        print(f"Jev: {problem}", file=sys.stderr)
    return r


def _size(eligible: list, snap, limits: Limits, params: dict, regime_mult: float) -> dict[str, float]:
    """The most the strategy buys per candidate, best score first, each one leaving less room
    for the next. The same caps the backtest applies (backtest.buy_usd); the gate checks again."""
    caps = caps_from(limits)
    invested = sum(x.market_value for x in snap.holdings.values()) + sum(snap.pending_buys.values())
    cash, n_pos, out = snap.cash, open_positions(snap), {}
    for v in eligible[: limits.max_orders_per_wake]:
        want = st.size_usd(snap.equity, v.features, params, regime_mult)
        usd = bt.buy_usd(want, v.symbol, equity=snap.equity, invested=invested, cash=cash, positions=n_pos, caps=caps)
        usd = int(usd * 100) / 100
        if usd >= limits.min_order_usd:
            out[v.symbol] = usd
            invested, cash, n_pos = invested + usd, cash - usd, n_pos + 1
    return out


def _relabel_exploration(tracked: dict, orders: list[dict], rec: Records, now: datetime) -> None:
    """Exit levels rebuilt from fills lose the exploration label. Put it back when the
    position's latest buy is an order the journal recorded as exploration, so the 5% budget
    keeps counting it. Positions older than the 8 days of journal read here stay unlabelled."""
    lost = [s for s, p in tracked.items() if p.source != "registro" and "explore" not in p.levels]
    if not lost:
        return
    try:
        _, _, explore_ids = learn._journal_index(rec.journal_since(f"{now - timedelta(days=8):%Y-%m-%d}"))
    except Exception:  # noqa: BLE001 - without the journal the label stays lost; the hard caps still hold
        return
    for s in lost:
        buys = sorted((o for o in orders if context.pair(o.get("symbol", "")) == s and o.get("side") == "buy"
                       and str(o.get("client_order_id", "")).startswith(ORDER_PREFIX)),
                      key=lambda o: str(o.get("created_at", "")))
        if buys and buys[-1]["client_order_id"] in explore_ids:
            tracked[s].levels["explore"] = True


def _size_explore(explorable: list, snap, limits: Limits, regime_mult: float, entries: dict[str, float],
                  tracked: dict, explore_held: frozenset[str]) -> dict[str, float]:
    """The most each exploration candidate may buy, best score first: explore_position_pct of
    equity scaled by the regime, within what is left of explore_total_pct and of the usual caps,
    after the strategy's own entries. The gate checks every cap again."""
    caps = caps_from(limits)
    invested = (sum(x.market_value for x in snap.holdings.values()) + sum(snap.pending_buys.values())
                + sum(entries.values()))
    cash, n_pos = snap.cash - sum(entries.values()), open_positions(snap) + len(entries)
    budget = snap.equity * limits.explore_total_pct / 100 - sum(
        p.value for s, p in tracked.items() if s.replace("/", "") in explore_held) - sum(
        v for k, v in snap.pending_buys.items() if k in explore_held)
    out = {}
    for v in explorable[: max(0, limits.max_orders_per_wake - len(entries))]:
        want = min(snap.equity * limits.explore_position_pct / 100 * regime_mult, budget)
        usd = bt.buy_usd(want, v.symbol, equity=snap.equity, invested=invested, cash=cash, positions=n_pos, caps=caps)
        usd = int(usd * 100) / 100
        if usd >= limits.min_order_usd:
            out[v.symbol] = usd
            invested, cash, n_pos, budget = invested + usd, cash - usd, n_pos + 1, budget - usd
    return out


def caps_from(limits: Limits) -> bt.Caps:
    """The live hard limits, for the backtest's simulation of the gate."""
    return bt.Caps(max_invested_pct=limits.max_invested_pct, max_position_pct=limits.max_position_pct,
                   max_meme_pct=limits.max_memecoin_pct, max_positions=limits.max_open_positions,
                   daily_loss_pct=limits.daily_loss_limit_pct, max_orders_per_wake=limits.max_orders_per_wake,
                   min_order_usd=limits.min_order_usd,
                   memecoins=frozenset(s for s in HARD_SYMBOLS if is_memecoin(s)))


def ours(fills: list[dict], orders: list[dict]) -> list[dict]:
    """The fills of the agent's own orders (client_order_id trd-...): a manual or test order
    on the same account is not a trade of the strategy."""
    ids = {o.get("id") for o in orders if str(o.get("client_order_id", "")).startswith(ORDER_PREFIX)}
    return [f for f in fills if f.get("order_id") in ids]


def _recent_trades(fills: list[dict], orders: list[dict], rec: Records, now: datetime, n: int = 5) -> list[dict]:
    try:
        journal = rec.journal_since(f"{now - timedelta(days=8):%Y-%m-%d}")
        trades = learn.closed_trades(ours(fills, orders), orders, journal)
    except Exception:  # noqa: BLE001 - the prompt can do without
        return []
    return [{"symbol": x.symbol, "entrata": x.entry_time[:16], "uscita": x.exit_time[:16],
             "pnl_pct": round(x.pnl_pct, 2), "motivo": x.exit_reason} for x in trades[-n:]]


def _check_continuity(prev: dict | None, slot: datetime, h: dict, journal: dict) -> bool:
    """Record a missed slot or an overlap. Returns True when this slot is older than the records."""
    if not prev or not prev.get("slot"):
        h["warnings"].append("nessun handoff precedente: primo risveglio o storico perso")
        return False
    prev_slot = datetime.fromisoformat(prev["slot"])
    if prev_slot > slot:
        h["warnings"].append(f"questo slot è precedente all'ultimo handoff ({prev['slot']})")
        return True
    if prev_slot == slot:
        h["warnings"].append("slot già visto nell'ultimo handoff: esecuzione ripetuta o sovrapposta")
    elif prev_slot < previous_slot(slot):
        h["missed_slots"] = journal["missed_slots"] = missed_count(prev_slot, slot)
        h["warnings"].append(f"saltati {h['missed_slots']} slot dall'ultimo risveglio ({prev['slot_id']}): "
                             "nessun recupero")
    if prev.get("status") == "failed":
        h["warnings"].append(f"il risveglio precedente era fallito: {prev.get('error', '')[:120]}")
    return False


def _summarise_state(h: dict, snap, alpaca: Alpaca, prev: dict | None) -> None:
    try:
        open_now = [o for o in alpaca.orders("open") if o.get("status") in OPEN_STATUSES]
    except (BrokerUnavailable, BrokerHTTPError):
        open_now = None
    exposure = sum(x.market_value for x in snap.holdings.values())
    held = ", ".join(sorted(k for k, x in snap.holdings.items() if x.market_value >= 1.0)) or "nessuna"
    h["remaining_risk"] = (f"investito {exposure:.2f}$ (prima degli ordini di questo slot) su {held}; "
                           + (f"{len(open_now)} ordini aperti" if open_now is not None else "ordini aperti non verificati"))
    if h["status"] in ("already_done", "stale_slot") and prev:
        h["next_job"] = prev.get("next_job") or h["next_job"]


def notable(h: dict) -> bool:
    """A wake worth a message: a trade or a rejected order, a failure, an alert. Quiet otherwise."""
    return (h["status"] == "failed" or bool(h.get("alerts"))
            or any(d.get("action") != "hold" for d in h.get("decisions", [])))


def _finish(deps: Deps, slot: datetime, h: dict, journal: dict, trace: Trace, ctx: dict) -> None:
    rec = deps.records
    journal.update(status=h["status"], finished_at=h["finished_at"], error=h["error"], warnings=h["warnings"],
                   run_url=deps.run_url)
    if h["dry_run"]:
        h["warnings"].append("prova (--dry-run): nessun ordine inviato")
    h["commit_message"] = f"Record wake {h['slot_id']} {h['status']}"
    for step, fn in (("journal", lambda: rec.journal(journal)), ("handoff", lambda: rec.write_handoff(h))):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            _alert(h, f"scrittura {step} fallita: {type(e).__name__}")
            print(f"ERRORE scrittura {step}: {e}", file=sys.stderr)
    trace.outcome(h)
    trace.records(rec.root, rec.written)

    if notable(h) or h["dry_run"]:
        try:
            text = notify.wake_message(h)
        except Exception as e:  # noqa: BLE001 - fall back to the minimal message
            text = notify.failure_message(h["slot_id"], h["error"] or f"messaggio non costruito: {type(e).__name__}",
                                          deps.run_url)
        ok, detail = notify.safe_send(deps.send, text, rec.secrets)
        trace.telegram(ok, detail, h["dry_run"])
    else:
        ok, detail = True, "risveglio tranquillo: nessun messaggio"
        trace.step("telegram", detail)
    try:
        rec.update_handoff(notified=ok, notify_detail=detail)
    except Exception as e:  # noqa: BLE001
        print(f"ERRORE aggiornamento handoff: {e}", file=sys.stderr)


# ---- every 6 hours and once a day ------------------------------------------------------

def reflection_due(slot: datetime, done_bucket: str) -> bool:
    """Once per 6-hour bucket: the first wake of the bucket reflects, later ones do not."""
    return done_bucket < cadence_bucket(slot)


def _maybe_reflect(deps: Deps, slot: datetime, h: dict, trace: Trace, ctx: dict) -> None:
    rec = deps.records
    if deps.reflect is False:
        return
    if deps.reflect is None and not reflection_due(slot, rec.read_state("cadence").get("reflection", "")):
        return
    rec.update_state("cadence", reflection=cadence_bucket(slot))  # once per bucket, even if it fails
    alpaca, model, now = ctx["alpaca"], ctx["model"], deps.now()
    judge, why = None, ""
    try:
        judge = (deps.judge or _backtest_judge)(alpaca, ctx["symbols"], ctx["spreads"], deps, now)
    except Exception as e:  # noqa: BLE001 - without an evaluator no change is accepted
        why = f"backtest non disponibile: {type(e).__name__}: {e}"
    since = (now - timedelta(days=REFLECT_DAYS)).isoformat()
    try:
        orders = alpaca.orders("all", after=since)
        fills = ours(alpaca.fills(after=since), orders)
    except (BrokerUnavailable, BrokerHTTPError):
        fills = orders = None  # learn falls back to the evidence in the records
    out = learn.run_reflection(root=deps.root, records=rec, model=model, judge=judge, now=now,
                               slot=slot_id(slot), days=REFLECT_DAYS, fills=fills, orders=orders)
    if why:
        out["evaluator_error"] = why
    rec.journal(out)
    ctx["reflection"] = out
    trace.reflection(out)
    if out.get("commit_reason"):
        h["commit_message"] += f"; parametri: {out['commit_reason']}"
        rec.update_handoff(commit_message=h["commit_message"])
    ok, detail = notify.safe_send(deps.send, notify.reflection_message(out, h["dry_run"], deps.run_url), rec.secrets)
    trace.telegram(ok, detail, h["dry_run"], what="messaggio della riflessione")


def _backtest_judge(alpaca: Alpaca, symbols: list[str], spreads: dict, deps: Deps, now: datetime):
    """judge(current, candidate) -> (ok, detail): backtest.compare over the last REFLECT_DAYS,
    on fresh 15m bars, with the live caps and each coin's spread."""
    start = now - timedelta(days=REFLECT_DAYS + HISTORY_DAYS)
    raw = alpaca.bars_each(symbols, "15Min", start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return bt.judge(raw, days=REFLECT_DAYS, caps=caps_from(deps.limits), costs=bt.Costs(spreads_pct=spreads))


def summary_day(slot: datetime) -> str:
    """The UTC day whose summary is due: today at the last slot, otherwise yesterday (catch-up)."""
    day = slot.date() if is_last_slot_of_day(slot) else slot.date() - timedelta(days=1)
    return day.isoformat()


def _cadence(deps: Deps, slot: datetime, h: dict, trace: Trace, ctx: dict) -> None:
    """The 6-hour status and the daily summary. Never in a dry run; each is marked sent only
    when Telegram took it, so a failed send is retried by the next wake."""
    if h["dry_run"]:
        return
    rec = deps.records
    cad = rec.read_state("cadence")
    if "baseline_equity" not in cad and h.get("equity"):
        rec.update_state("cadence", baseline_equity=h["equity"], baseline_at=h["started_at"])
        cad = rec.read_state("cadence")
    try:
        bucket = cadence_bucket(slot)
        if cad.get("status", "") < bucket and h.get("equity") is not None:
            text = notify.status_message(h, cad.get("baseline_equity"), rec.journal_for_day(slot.date().isoformat()),
                                         deps.run_url)
            ok, detail = notify.safe_send(deps.send, text, rec.secrets)
            trace.telegram(ok, detail, False, what="stato delle 6 ore")
            if ok:
                rec.update_state("cadence", status=bucket)
    except Exception as e:  # noqa: BLE001
        print(f"ERRORE stato: {e}", file=sys.stderr)
    try:
        _daily_summary(deps, slot, h, trace, cad.get("baseline_equity"), ctx.get("alpaca"))
    except Exception as e:  # noqa: BLE001
        print(f"ERRORE riepilogo giornaliero: {e}", file=sys.stderr)


def _daily_summary(deps: Deps, slot: datetime, h: dict, trace: Trace, baseline, alpaca: Alpaca | None) -> None:
    rec = deps.records
    day = summary_day(slot)
    if rec.summary_sent_for() >= day:
        return
    wakes = [w for w in rec.journal_for_day(day) if w.get("kind", "wake") == "wake"]
    if not wakes:
        return
    now = deps.now()
    try:  # Alpaca first: this wake's own fills are not in the evidence yet
        since = (now - timedelta(days=9)).isoformat()
        fills, orders = alpaca.fills(after=since), alpaca.orders("all", after=since)
    except (AttributeError, BrokerUnavailable, BrokerHTTPError):
        ev = list(rec._evidence_rows(9, now))
        fills = [r["alpaca"] for r in ev if r.get("kind") == "fill" and isinstance(r.get("alpaca"), dict)]
        orders = [r["alpaca"] for r in ev if r.get("kind") == "order" and isinstance(r.get("alpaca"), dict)]
    journal = rec.journal_since(f"{now - timedelta(days=9):%Y-%m-%d}")
    closed = [t for t in learn.closed_trades(ours(fills, orders), orders, journal) if t.exit_time[:10] == day]
    start_equity = next((w["equity"] for w in wakes if w.get("equity")), None)
    end_equity = next((w["equity"] for w in reversed(wakes) if w.get("equity")), None)
    text = notify.daily_message(day, wakes, closed, start_equity, end_equity, baseline, h.get("positions", []),
                                deps.run_url)
    ok, detail = notify.safe_send(deps.send, text, rec.secrets)
    trace.telegram(ok, detail, False, what=f"riepilogo del {day}")
    if ok:
        rec.mark_summary_sent(day, deps.now())
