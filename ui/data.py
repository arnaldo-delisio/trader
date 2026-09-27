"""Read the repo records into one plain dict for the dashboard. Never raises on bad records.

Every source is optional. A missing file gives an empty section; a malformed line
is skipped and counted in `problems`, which the page shows. The rules that decide
things (fees, trade pairing, lessons format, limits) are imported from trader/,
so the page shows what the agent computes, not a second copy of it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

from trader import config, learn
from trader.records import ENTRY_MARK as PROGRESS_MARK

# trader.config owns the memecoin list; the fallback only covers a checkout that predates it.
_MEME_FALLBACK = frozenset({"DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP", "FLOKI"})
TRADE_OUTCOMES = ("placed", "adopted", "dry_run", "filled")
# Where the exit levels of open positions may live. The first one that exists wins.
LEVEL_FILES = ("state/positions.json", "state/exits.json", "state/levels.json")
HISTORY_FILES = ("state/portfolio_history.json", "state/equity.json")
RUN_URL_PREFIX = "https://github.com/"
MAX_WAKES = 96          # one day at a 15-minute cadence
MAX_TRADES = 12
MAX_CHANGES = 30
MAX_LESSON_SECTIONS = 6


# ---- small tolerant helpers ----------------------------------------------------------

def num(x) -> float | None:
    """A finite float, or None. Booleans are not numbers here."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def symbol(s) -> str:
    """BTCUSD, BTC/USD, btc/usd -> BTC/USD."""
    s = str(s or "").upper().strip()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    return s


def is_meme(sym) -> bool:
    s = symbol(sym)
    if hasattr(config, "is_memecoin"):
        return bool(config.is_memecoin(s))
    return s.split("/")[0] in _MEME_FALLBACK


def safe_url(u) -> str:
    """Only links to GitHub (the Actions run) are kept; anything else becomes empty."""
    u = str(u or "")
    return u if u.startswith(RUN_URL_PREFIX) and not any(c in u for c in "\"'<> \n") else ""


def parse_time(x) -> datetime | None:
    """ISO strings, slot ids (20260926T0815Z) and unix seconds, all to aware UTC."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        try:
            return datetime.fromtimestamp(float(x), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(x).strip()
    for fmt in ("%Y%m%dT%H%MZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def iso(d: datetime | None) -> str:
    return d.astimezone(UTC).isoformat() if d else ""


class Reader:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.problems: list[str] = []

    def text(self, rel: str) -> str:
        p = self.root / rel
        try:
            return p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError) as e:
            self.problems.append(f"{rel} illeggibile: {type(e).__name__}")
            return ""

    def json(self, rel: str):
        t = self.text(rel)
        if not t.strip():
            return None
        try:
            return json.loads(t)
        except ValueError:
            self.problems.append(f"{rel} non è JSON valido")
            return None

    def jsonl(self, rel: str) -> list[dict]:
        rows, bad = [], 0
        for line in self.text(rel).splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(r, dict):
                rows.append(r)
            else:
                bad += 1
        if bad:
            self.problems.append(f"{rel}: {bad} righe illeggibili saltate")
        return rows


# ---- sections -------------------------------------------------------------------------

def _is_reflection(row: dict) -> bool:
    return row.get("kind") == "reflection"


def equity_series(r: Reader, journal: list[dict], evidence: list[dict], handoff: dict) -> tuple[list[dict], str]:
    """[{t, equity}] oldest first, and where it came from."""
    for rel in HISTORY_FILES:
        h = r.json(rel)
        if isinstance(h, dict) and isinstance(h.get("timestamp"), list) and isinstance(h.get("equity"), list):
            pts = [{"t": iso(parse_time(t)), "equity": num(e)} for t, e in zip(h["timestamp"], h["equity"])]
            pts = [p for p in pts if p["t"] and p["equity"] is not None and p["equity"] > 0]
            if pts:
                return pts, "Alpaca, storico del portafoglio"
        if isinstance(h, list):
            pts = [{"t": iso(parse_time(x.get("t") or x.get("at") or x.get("timestamp"))), "equity": num(x.get("equity"))}
                   for x in h if isinstance(x, dict)]
            pts = [p for p in pts if p["t"] and p["equity"] is not None and p["equity"] > 0]
            if pts:
                return sorted(pts, key=lambda p: p["t"]), "Alpaca, storico del portafoglio"
    pts = []
    for e in evidence:
        a = e.get("alpaca") if isinstance(e.get("alpaca"), dict) else {}
        if e.get("kind") == "account" and num(a.get("equity")):
            pts.append({"t": iso(parse_time(e.get("fetched_at"))), "equity": num(a["equity"])})
    if not pts:
        for row in journal:
            if _is_reflection(row):
                continue
            eq = num(row.get("equity"))
            t = parse_time(row.get("finished_at")) or parse_time(row.get("slot"))
            if eq and eq > 0 and t:
                pts.append({"t": iso(t), "equity": eq})
    if not pts and num(handoff.get("equity")):
        t = parse_time(handoff.get("finished_at")) or parse_time(handoff.get("slot"))
        if t:
            pts.append({"t": iso(t), "equity": num(handoff["equity"])})
    pts = [p for p in pts if p["t"]]
    return sorted(pts, key=lambda p: p["t"]), ("record dei risvegli" if pts else "")


def _levels(r: Reader, handoff: dict) -> dict[str, dict]:
    raw = None
    for rel in LEVEL_FILES:
        raw = r.json(rel)
        if raw is not None:
            break
    if raw is None:
        raw = handoff.get("exits") or handoff.get("levels")
    if isinstance(raw, dict) and isinstance(raw.get("positions"), (dict, list)):
        raw = raw["positions"]
    out = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(x.get("symbol"), x) for x in raw if isinstance(x, dict)]
    else:
        items = []
    for k, v in items:
        if k and isinstance(v, dict):
            out[symbol(k)] = v
    return out


def positions(r: Reader, handoff: dict) -> list[dict]:
    levels = _levels(r, handoff)
    raw = handoff.get("positions")
    rows = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    if isinstance(raw, dict):
        rows = [{"symbol": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    by_sym = {symbol(x.get("symbol")): x for x in rows if x.get("symbol")}
    for s in levels:  # levels without a position row still show, with what we know
        by_sym.setdefault(s, {"symbol": s})
    out = []
    for s, p in by_sym.items():
        lv = levels.get(s, {})
        entry = num(p.get("avg_entry_price")) or num(p.get("entry_price")) or num(lv.get("entry_price"))
        price = num(p.get("current_price")) or num(p.get("price"))
        qty = num(p.get("qty"))
        mv = num(p.get("market_value")) or (qty * price if qty is not None and price else None)
        pnl_pct = num(p.get("unrealized_plpc"))
        pnl_pct = pnl_pct * 100 if pnl_pct is not None else (
            (price / entry - 1) * 100 if price and entry else None)
        opened = parse_time(lv.get("opened_at") or p.get("opened_at")) or parse_time(num(lv.get("entry_t")))
        out.append({"symbol": s, "meme": is_meme(s), "qty": qty, "entry_price": entry, "price": price,
                    "market_value": mv, "pnl_pct": pnl_pct, "pnl_usd": num(p.get("unrealized_pl")),
                    "stop": num(lv.get("stop")) or num(p.get("stop")),
                    "take_profit": num(lv.get("take_profit")) or num(p.get("take_profit")),
                    "opened_at": iso(opened), "explore": bool(lv.get("explore") or p.get("explore"))})
    return sorted(out, key=lambda x: -(x["market_value"] or 0))


def _order_index(evidence: list[dict]) -> tuple[dict, dict]:
    """client_order_id -> latest order as Alpaca returned it; order id -> [fills]."""
    orders, fills = {}, {}
    for e in evidence:
        a = e.get("alpaca")
        if not isinstance(a, dict):
            continue
        if e.get("kind") == "order" and a.get("client_order_id"):
            orders[a["client_order_id"]] = a
        elif e.get("kind") == "fill" and a.get("order_id"):
            fills.setdefault(a["order_id"], []).append(a)
    return orders, fills


def _fill_price(order: dict | None, fills: dict) -> float | None:
    if not order:
        return None
    fs = fills.get(order.get("id"), [])
    q = sum(num(f.get("qty")) or 0 for f in fs)
    if q > 0:
        return sum((num(f.get("qty")) or 0) * (num(f.get("price")) or 0) for f in fs) / q
    return num(order.get("filled_avg_price"))


def trades(journal: list[dict], evidence: list[dict]) -> list[dict]:
    """Orders the agent actually sent (or would have, in a dry run), newest first."""
    orders, fills = _order_index(evidence)
    out = []
    for row in journal:
        if _is_reflection(row):
            continue
        slot = row.get("slot", "")
        when = parse_time(row.get("finished_at")) or parse_time(slot)
        items = [(v, "model") for v in row.get("verdicts") or [] if isinstance(v, dict)]
        items += [(v, "exit") for v in row.get("exits") or [] if isinstance(v, dict)]
        for v, origin in items:
            if v.get("outcome") not in TRADE_OUTCOMES:
                continue
            cid = str(v.get("client_order_id") or "")
            o = orders.get(cid)
            side = v.get("action") or v.get("side") or ("sell" if origin == "exit" else "")
            order = v.get("order") if isinstance(v.get("order"), dict) else {}
            notional = num(order.get("notional")) or num(v.get("requested_usd")) or num(v.get("notional_usd"))
            out.append({
                "at": iso(when), "slot": slot, "symbol": symbol(v.get("symbol")), "meme": is_meme(v.get("symbol")),
                "side": side if side in ("buy", "sell") else "", "origin": origin,
                "notional": notional, "qty": num(order.get("qty")) or num(v.get("qty")),
                "price": _fill_price(o, fills) or num(v.get("price")),
                "outcome": v.get("outcome"), "dry_run": bool(row.get("dry_run")) or v.get("outcome") == "dry_run",
                "reason": str(v.get("reason_model") or (v.get("reason") if origin == "exit" else "") or ""),
                "exit_reason": str(v.get("exit_reason") or (v.get("reason") if origin == "exit" else "") or ""),
                "gate": str(v.get("reason") or "") if origin == "model" else "",
                "bull": str(v.get("bull_case") or ""), "bear": str(v.get("bear_case") or ""),
                "stop": num(v.get("stop")), "take_profit": num(v.get("take_profit")),
                "client_order_id": cid, "run_url": safe_url(row.get("run_url")),
                "explore": v.get("explore") is True,
            })
    out.sort(key=lambda t: t["at"], reverse=True)
    return out


def closed_stats(r: Reader, journal: list[dict], evidence: list[dict]) -> dict:
    fills = [e["alpaca"] for e in evidence if e.get("kind") == "fill" and isinstance(e.get("alpaca"), dict)]
    orders = [e["alpaca"] for e in evidence if e.get("kind") == "order" and isinstance(e.get("alpaca"), dict)]
    try:
        closed = learn.closed_trades(fills, orders, journal, learn.FEE_PCT)
        summary = learn.summarize(closed)
    except Exception as e:  # noqa: BLE001 - shown on the page, never hidden
        r.problems.append(f"operazioni chiuse non calcolate: {type(e).__name__}: {e}")
        return {"trades": 0, "list": []}
    best = max(closed, key=lambda t: t.pnl_pct, default=None)
    worst = min(closed, key=lambda t: t.pnl_pct, default=None)
    by_kind = learn.summary_by_kind(closed)
    return {**summary, "explore": by_kind["esplorazione"], "rule": by_kind["regola"],
            "best_symbol": symbol(best.symbol) if best else "", "worst_symbol": symbol(worst.symbol) if worst else "",
            "list": [{"symbol": symbol(t.symbol), "entry_time": t.entry_time, "exit_time": t.exit_time,
                      "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl_usd": t.pnl_usd,
                      "pnl_pct": t.pnl_pct, "hold_hours": t.hold_hours, "exit_reason": t.exit_reason,
                      "explore": t.explore}
                     for t in sorted(closed, key=lambda t: t.exit_time, reverse=True)[:MAX_TRADES]]}


def wakes(journal: list[dict]) -> list[dict]:
    out = []
    for row in journal:
        refl = _is_reflection(row)
        t = parse_time(row.get("slot")) or parse_time(row.get("at")) or parse_time(row.get("finished_at"))
        if not t:
            continue
        orders = sum(1 for key in ("verdicts", "exits") for v in row.get(key) or []
                     if isinstance(v, dict) and v.get("outcome") in TRADE_OUTCOMES)
        status = "reflection" if refl else str(row.get("status") or "unknown")
        if refl and row.get("error"):
            status = "failed"
        jev = row.get("jev") if isinstance(row.get("jev"), dict) else {}
        prop = row.get("proposal") if isinstance(row.get("proposal"), dict) else {}
        out.append({"slot": str(row.get("slot") or ""), "t": iso(t), "status": status, "reflection": refl,
                    "orders": orders, "dry_run": bool(row.get("dry_run")), "run_url": safe_url(row.get("run_url")),
                    "error": str(row.get("error") or "")[:300], "regime": str(jev.get("regime") or ""),
                    "note": str(row.get("skipped") or prop.get("market_view") or "")[:300]})
    out.sort(key=lambda w: w["t"])
    return out[-MAX_WAKES:]


def regime(journal: list[dict]) -> dict:
    for row in reversed(journal):
        j = row.get("jev")
        if isinstance(j, dict) and j.get("regime"):
            return {"regime": str(j["regime"]), "multiplier": num(j.get("multiplier")),
                    "confidence": num(j.get("confidence")), "source": str(j.get("source") or ""),
                    "error": str(j.get("error") or "")[:200], "slot": str(row.get("slot") or "")}
    return {}


def lessons(r: Reader) -> list[dict]:
    text = r.text(learn.LESSONS_FILE)
    parts = text.split(learn.ENTRY_MARK)[1:] if learn.ENTRY_MARK in text else \
        ["## " + p for p in text.split("\n## ")[1:]]
    out = []
    for part in parts[:MAX_LESSON_SECTIONS]:
        title, items = "", []
        for line in part.strip().splitlines():
            s = line.strip()
            if s.startswith("## ") and not title:
                title = s[3:].strip()
            elif s.startswith(("- ", "* ")):
                items.append(s[2:].strip())
            elif s and items:
                items[-1] += " " + s
        if title or items:
            out.append({"title": title, "items": items})
    return out


def param_changes(journal: list[dict]) -> list[dict]:
    out = []
    for row in journal:
        if not _is_reflection(row):
            continue
        at = iso(parse_time(row.get("at")) or parse_time(row.get("slot")))
        for key, accepted in (("accepted", True), ("rejected", False)):
            for c in row.get(key) or []:
                if not isinstance(c, dict):
                    continue
                out.append({"at": at, "slot": str(row.get("slot") or ""), "name": str(c.get("name") or "?"),
                            "old": c.get("old_value"), "new": c.get("new_value"), "accepted": accepted,
                            "reason": str(c.get("reason") or "")[:400], "why": str(c.get("why") or "")[:300],
                            "score_before": num(c.get("score_before")), "score_after": num(c.get("score_after"))})
    out.sort(key=lambda c: c["at"], reverse=True)
    return out[:MAX_CHANGES]


def limits(r: Reader) -> dict:
    """The limits in force: what the config asks, after the code's ceilings, and the ceilings."""
    effective = {}
    try:
        lim = config.load_limits(r.root / "config/limits.toml")
        effective = asdict(lim) if is_dataclass(lim) else dict(vars(lim))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 - shown on the page
        r.problems.append(f"config/limits.toml non caricato: {type(e).__name__}: {e}")
    ceilings = dict(getattr(config, "HARD_CEILINGS", {}))
    floors = dict(getattr(config, "HARD_FLOORS", {}))
    rows = []
    for k in list(dict.fromkeys([*effective, *ceilings, *floors])):
        v = effective.get(k)
        if isinstance(v, (tuple, list)):
            continue
        rows.append({"name": k, "value": num(v), "ceiling": num(ceilings.get(k)), "floor": num(floors.get(k))})
    syms = effective.get("symbols")
    enabled, why = config.trading_enabled(r.root, {})
    return {"rows": rows, "symbols": [symbol(s) for s in syms] if isinstance(syms, (list, tuple)) else [],
            "trading_enabled": enabled, "kill_reason": why}


def latest_progress(r: Reader) -> str:
    """The newest entry of state/progress.md, as plain text."""
    text = r.text("state/progress.md")
    parts = text.split(PROGRESS_MARK)
    return parts[1].strip() if len(parts) > 1 else ""


def load(root: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    r = Reader(root)
    handoff = r.json("state/last_handoff.json")
    handoff = handoff if isinstance(handoff, dict) else {}
    journal = r.jsonl("journal/decisions.jsonl")
    evidence = []
    ev_dir = Path(root) / "evidence"
    if ev_dir.is_dir():
        for p in sorted(ev_dir.glob("*.jsonl")):
            evidence += r.jsonl(f"evidence/{p.name}")
    series, series_source = equity_series(r, journal, evidence, handoff)
    pos = positions(r, handoff)
    params = r.json(learn.PARAMS_FILE)

    equity = num(handoff.get("equity")) or (series[-1]["equity"] if series else None)
    last_equity = num(handoff.get("last_equity"))
    day_pct = num(handoff.get("day_change_pct"))
    if day_pct is None and equity and last_equity:
        day_pct = (equity / last_equity - 1) * 100
    day_usd = equity - equity / (1 + day_pct / 100) if equity and day_pct is not None else None
    base = series[0]["equity"] if series else None
    invested = sum(p["market_value"] or 0 for p in pos)
    account = {"equity": equity, "day_pnl_pct": day_pct, "day_pnl_usd": day_usd,
               "total_pnl_usd": equity - base if equity and base else None,
               "total_pnl_pct": (equity / base - 1) * 100 if equity and base else None,
               "invested_usd": invested, "invested_pct": invested / equity * 100 if equity else None}

    model = {
        "account": account,
        "equity_series": series, "equity_source": series_source,
        "positions": pos,
        "trades": trades(journal, evidence)[:MAX_TRADES],
        "closed": closed_stats(r, journal, evidence),
        "wakes": wakes(journal),
        "regime": regime(journal),
        "handoff": {k: handoff.get(k) for k in ("slot", "slot_id", "status", "outcome", "changed", "remaining_risk",
                                                "next_job", "market_view", "finished_at", "dry_run", "error")
                    if handoff.get(k) not in (None, "")},
        "handoff_warnings": [str(w) for w in handoff.get("warnings") or [] if w][:8],
        "handoff_run_url": safe_url(handoff.get("run_url")),
        "progress": latest_progress(r),
        "lessons": lessons(r),
        "param_changes": param_changes(journal),
        "params": params if isinstance(params, dict) else {},
        "limits": limits(r),
        "problems": r.problems,
    }
    # The version changes only when the records do, so the page reloads only then.
    model["version"] = hashlib.sha256(json.dumps(model, sort_keys=True, default=str).encode()).hexdigest()[:16]
    model["built_at"] = now.isoformat()
    return model
