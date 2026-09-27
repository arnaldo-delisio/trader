"""Self-improvement: what the closed trades say, what the model learns, what code accepts.

Every 6 hours a reflection wake runs run_reflection():
  1. closed_trades() pairs buy and sell fills FIFO per symbol, net of fees, and
     finds the signals that were behind each entry in the journal.
  2. attribution() says, per signal, how trades went when it was strong or weak.
  3. The model reads that, the recent journal and the last lessons (prompts/reflect.md)
     and answers {"lessons": [...], "param_changes": [{name, new_value, reason}]}.
  4. decide_changes() accepts a change only if strategy.check_change allows it (LEARNABLE,
     PARAM_BOUNDS, step size) and the judge, backtest.compare on a walk-forward window
     net of fees, finds it no worse in net return and in drawdown. Hard limits are never
     learnable: they are not in PARAM_BOUNDS.
  5. Lessons go on top of lessons/lessons.md; accepted changes go to config/params.json.
The returned dict is the journal record, with every rejection and its reason.

Contract with the rest of the wake, read from the records:
  evidence rows  {"kind": "order"|"fill", "alpaca": {...}} as Alpaca returned them;
                 fills carry order_id, orders carry id and client_order_id.
  journal rows   {"slot": "...", "signals": {"BTC/USD": {"trend": 0.8, ...}}, ...}
                 signals are the per-signal contributions to the score at decision
                 time; exit reasons come from rows in "exits" or "verdicts" whose
                 client_order_id matches the sell ("reason" or "reason_model").
Fills can also come straight from Alpaca, so outcomes survive lost records;
only the signals and exit reasons need the journal.
"""

from __future__ import annotations

import copy
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import strategy as st
from .model import ModelError

# ---- what the reflection may change, and by how much ------------------------
# One table for the whole repo: strategy.PARAM_BOUNDS (name -> lowest, highest, largest
# step per reflection) and strategy.LEARNABLE (the names a reflection may touch).
# Dotted names reach into nested params ("weights.rsi").
MAX_CHANGES = 3          # per reflection; more than this and the extras are rejected
MAX_LESSONS = 5
MAX_TEXT = 400
MAX_LESSON_ENTRIES = 60  # sections kept in lessons/lessons.md

FEE_PCT = st.TAKER_FEE_PCT  # per side, taker: the one fee the backtest and the entry rule use too
LESSONS_FILE = "lessons/lessons.md"
PARAMS_FILE = "config/params.json"
PROMPT_FILE = "prompts/reflect.md"
ENTRY_MARK = "<!-- lesson -->"
LESSONS_HEAD = ("# Lezioni\n\nScritte dalla riflessione ogni 6 ore, la più recente in alto. "
                "Il modello le rilegge a ogni risveglio.\n\n")
EPS = 1e-9
DUST_FRACTION = 0.01  # risk.gate sells the whole holding when asked for 99% of it or more

# ---- the model's answer --------------------------------------------------------
CHANGE_KEYS = ("name", "new_value", "reason")
REFLECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lessons", "param_changes"],
    "properties": {
        "lessons": {"type": "array", "maxItems": MAX_LESSONS, "items": {"type": "string"}},
        "param_changes": {
            "type": "array",
            "maxItems": MAX_CHANGES + 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(CHANGE_KEYS),
                "properties": {"name": {"type": "string"}, "new_value": {"type": "number"},
                               "reason": {"type": "string"}},
            },
        },
    },
}
REFLECT_SYSTEM = ("You are the reflection step of a small paper-trading agent. You have no tools. "
                  "Answer with one JSON object matching the given schema and nothing else.")
FAKE_REPLY = json.dumps({"lessons": ["risposta finta: nessuna lezione reale"], "param_changes": []})


class InvalidReflection(ValueError):
    pass


def _text(v, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise InvalidReflection(f"{what} deve essere un testo non vuoto")
    return v.strip()[:MAX_TEXT]


def validate_reflection(obj) -> tuple[list[str], list[dict]]:
    """Strict: exact keys, non-empty texts, finite numbers, one change per name."""
    if not isinstance(obj, dict) or set(obj) != {"lessons", "param_changes"}:
        raise InvalidReflection("serve un oggetto con esattamente lessons e param_changes")
    lessons, changes = obj["lessons"], obj["param_changes"]
    if not isinstance(lessons, list) or len(lessons) > MAX_LESSONS:
        raise InvalidReflection(f"lessons deve essere una lista di al massimo {MAX_LESSONS} testi")
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES + 2:
        raise InvalidReflection(f"param_changes deve essere una lista di al massimo {MAX_CHANGES + 2} voci")
    out, seen = [], set()
    for i, c in enumerate(changes):
        if not isinstance(c, dict) or set(c) != set(CHANGE_KEYS):
            raise InvalidReflection(f"param_changes[{i}] deve avere esattamente {', '.join(CHANGE_KEYS)}")
        name = _text(c["name"], f"param_changes[{i}].name")
        if name in seen:
            raise InvalidReflection(f"{name} compare due volte")
        seen.add(name)
        v = c["new_value"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise InvalidReflection(f"param_changes[{i}].new_value deve essere un numero finito")
        out.append({"name": name, "new_value": v, "reason": _text(c["reason"], f"param_changes[{i}].reason")})
    return [_text(x, f"lessons[{i}]") for i, x in enumerate(lessons)], out


def parse_reflection(raw) -> tuple[list[str], list[dict]]:
    """Model output (text or already-parsed JSON) to (lessons, changes). Raises InvalidReflection."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            raw = json.loads(text)
        except ValueError:
            raise InvalidReflection("la risposta del modello non è JSON valido") from None
    return validate_reflection(raw)


# ---- closed trades --------------------------------------------------------------
@dataclass
class ClosedTrade:
    symbol: str
    entry_order_id: str
    entry_client_order_id: str
    exit_client_order_ids: list[str]
    entry_time: str
    exit_time: str
    qty: float
    entry_price: float
    exit_price: float        # quantity-weighted over the sells that closed it
    fees_usd: float
    pnl_usd: float           # net of fees on both sides
    pnl_pct: float           # net, on the entry notional
    hold_hours: float
    signals: dict = field(default_factory=dict)
    exit_reason: str = ""
    explore: bool = False    # an exploration buy (below the entry rule, small size)

    def as_dict(self) -> dict:
        return asdict(self)


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _hours(a: str, b: str) -> float:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except ValueError:
        return 0.0


CID_RE = re.compile(r"^trd-(?P<slot>[0-9]{8}T[0-9]{4}Z)-(?P<sym>[A-Z0-9]+)-(?P<side>buy|sell)$")


def _journal_index(journal: list[dict]) -> tuple[dict, dict, set]:
    """(slot -> {symbol: signals}, client_order_id -> exit reason, exploration buy ids)."""
    signals, reasons, explore = {}, {}, set()
    for row in journal:
        if not isinstance(row, dict):
            continue
        sig = row.get("signals")
        if isinstance(sig, dict):
            signals.setdefault(str(row.get("slot", "")), {}).update(
                {k.replace("/", ""): v for k, v in sig.items() if isinstance(v, dict)})
        for key in ("exits", "verdicts"):
            for item in row.get(key) or []:
                if isinstance(item, dict) and item.get("client_order_id") and item.get("explore") is True:
                    explore.add(item["client_order_id"])
                if isinstance(item, dict) and item.get("client_order_id"):
                    reason = item.get("reason") or item.get("reason_model") or ""
                    if reason and item["client_order_id"] not in reasons:
                        reasons[item["client_order_id"]] = str(reason)[:MAX_TEXT]
    return signals, reasons, explore


def closed_trades(fills: list[dict], orders: list[dict], journal: list[dict],
                  fee_pct: float = FEE_PCT) -> list[ClosedTrade]:
    """Pair fills FIFO per symbol. A trade closes when its entry order's quantity is sold,
    up to the buy fee Alpaca keeps in the coin.

    fills and orders are Alpaca objects (from evidence or straight from the API).
    A sell with nothing open before it (its buy is older than the window) is skipped.
    """
    cid_of = {o.get("id"): o.get("client_order_id", "") for o in orders if isinstance(o, dict)}
    signals, reasons, explore = _journal_index(journal)
    seen, rows = set(), []
    for f in fills:
        if not isinstance(f, dict) or f.get("id") in seen or f.get("side") not in ("buy", "sell"):
            continue
        seen.add(f.get("id"))
        rows.append(f)
    rows.sort(key=lambda f: str(f.get("transaction_time", "")))

    # symbol -> list of open lots, oldest first; one lot per entry order (partial fills merged)
    lots: dict[str, list[dict]] = {}
    out: list[ClosedTrade] = []
    rate = fee_pct / 100
    for f in rows:
        sym = str(f.get("symbol", "")).replace("/", "")
        qty, px, t = _num(f.get("qty")), _num(f.get("price")), str(f.get("transaction_time", ""))
        if qty <= 0 or px <= 0:
            continue
        book = lots.setdefault(sym, [])
        if f["side"] == "buy":
            oid = f.get("order_id", "")
            lot = next((x for x in book if x["order_id"] == oid and oid), None)
            if lot:
                lot["cost"] += qty * px
                lot["qty"] += qty
                lot["open"] += qty
            else:
                book.append({"order_id": oid, "qty": qty, "open": qty, "cost": qty * px, "time": t,
                             "proceeds": 0.0, "sold": 0.0, "exits": [], "exit_time": t})
            continue
        left = qty
        for lot in list(book):
            if left <= EPS:
                break
            take = min(lot["open"], left)
            lot["open"] -= take
            lot["sold"] += take
            lot["proceeds"] += take * px
            lot["exit_time"] = t
            cid = cid_of.get(f.get("order_id"), "")
            if cid and cid not in lot["exits"]:
                lot["exits"].append(cid)
            left -= take
            # Alpaca keeps the buy fee in the coin (checked on paper fills, 2026-09-26): selling
            # everything held leaves the fee's share of the bought quantity unsold. Dust under
            # DUST_FRACTION closes the lot, the same line under which the gate sells everything.
            if lot["open"] <= lot["qty"] * max(DUST_FRACTION, rate * 1.5) + EPS * max(1.0, lot["qty"]):
                book.remove(lot)
                out.append(_close(sym, lot, cid_of, signals, reasons, explore, rate))
    return out


def _close(sym: str, lot: dict, cid_of: dict, signals: dict, reasons: dict, explore: set,
           rate: float) -> ClosedTrade:
    entry_px = lot["cost"] / lot["qty"]
    exit_px = lot["proceeds"] / lot["sold"] if lot["sold"] else 0.0
    fees = rate * (lot["cost"] + lot["proceeds"])
    # The buy fee is either kept in the coin (the unsold dust: already missing from the
    # proceeds) or charged in cash; count it once.
    buy_fee_cash = max(0.0, rate * lot["cost"] - lot["open"] * entry_px)
    pnl = lot["proceeds"] - lot["cost"] - rate * lot["proceeds"] - buy_fee_cash
    cid = cid_of.get(lot["order_id"], "")
    m = CID_RE.match(cid)
    sig = signals.get(m["slot"], {}).get(sym, {}) if m else {}
    reason = next((reasons[c] for c in reversed(lot["exits"]) if c in reasons), "")
    return ClosedTrade(symbol=sym, entry_order_id=lot["order_id"], entry_client_order_id=cid,
                       exit_client_order_ids=list(lot["exits"]), entry_time=lot["time"], exit_time=lot["exit_time"],
                       qty=lot["qty"], entry_price=entry_px, exit_price=exit_px, fees_usd=round(fees, 6),
                       pnl_usd=round(pnl, 6), pnl_pct=round(100 * pnl / lot["cost"], 6),
                       hold_hours=round(_hours(lot["time"], lot["exit_time"]), 3),
                       signals={k: v for k, v in sig.items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
                       exit_reason=reason, explore=cid in explore)


def summarize(trades: list[ClosedTrade]) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl_usd > 0]
    by_reason: dict[str, dict] = {}
    for t in trades:
        r = by_reason.setdefault(t.exit_reason or "sconosciuto", {"trades": 0, "pnl_usd": 0.0})
        r["trades"] += 1
        r["pnl_usd"] = round(r["pnl_usd"] + t.pnl_usd, 2)
    return {"trades": len(trades), "win_rate_pct": round(100 * len(wins) / len(trades), 1),
            "pnl_usd": round(sum(t.pnl_usd for t in trades), 2), "fees_usd": round(sum(t.fees_usd for t in trades), 2),
            "avg_pnl_pct": round(statistics.fmean(t.pnl_pct for t in trades), 3),
            "avg_hold_hours": round(statistics.fmean(t.hold_hours for t in trades), 2),
            "best_pct": max(t.pnl_pct for t in trades), "worst_pct": min(t.pnl_pct for t in trades),
            "by_exit_reason": by_reason}


def summary_by_kind(trades: list[ClosedTrade]) -> dict:
    """The strategy's own entries and the exploration buys, judged apart: exploration
    trades below the entry threshold on purpose, so mixing them would blur both."""
    return {"regola": summarize([t for t in trades if not t.explore]),
            "esplorazione": summarize([t for t in trades if t.explore])}


def attribution(trades: list[ClosedTrade]) -> dict:
    """Per signal: how trades went when it pushed for the entry (> 0) and when it did not."""
    names = sorted({k for t in trades for k in t.signals})
    out = {}
    for name in names:
        pairs = [(t.signals[name], t.pnl_pct) for t in trades if name in t.signals]
        hi = [p for v, p in pairs if v > 0]
        lo = [p for v, p in pairs if v <= 0]
        try:
            corr = round(statistics.correlation([v for v, _ in pairs], [p for _, p in pairs]), 3)
        except (statistics.StatisticsError, ValueError):
            corr = None  # fewer than two trades, or a constant signal
        out[name] = {"n": len(pairs), "corr_with_pnl": corr,
                     "when_positive": _group(hi), "when_not_positive": _group(lo)}
    return out


def _group(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0}
    return {"n": len(pnls), "avg_pnl_pct": round(statistics.fmean(pnls), 3),
            "win_rate_pct": round(100 * sum(p > 0 for p in pnls) / len(pnls), 1)}


# ---- acceptance -------------------------------------------------------------------
def get_param(params: dict, name: str):
    cur = params
    for part in name.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def with_param(params: dict, name: str, value) -> dict:
    out = copy.deepcopy(params)
    cur = out
    parts = name.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    return out


def check_change(params: dict, change: dict) -> tuple[object, str]:
    """(value to apply, "") if the change may be tried, else (None, reason). What may change
    and by how much is strategy.check_change; this only skips a change that changes nothing."""
    name, v = change["name"], change["new_value"]
    reasons = st.check_change(params, {name: v})
    if reasons:
        return None, "; ".join(r.removeprefix(f"{name}: ") for r in reasons)
    if name in st.INTEGER_PARAMS:
        v = int(v)
    if abs(v - get_param(params, name)) <= EPS:
        return None, "nessun cambiamento"
    return v, ""


def decide_changes(params: dict, changes: list[dict], judge) -> tuple[dict, list[dict], list[dict]]:
    """(new params, accepted, rejected). Greedy: each change is tried on top of the ones
    accepted so far. judge(current, candidate) -> (ok, detail) is backtest.compare on recent
    bars (wake._backtest_judge): not worse in net return and not deeper in drawdown.
    None means no judge, so nothing is accepted; a judge that fails rejects the change."""
    accepted, rejected, candidates = [], [], []
    for i, c in enumerate(changes):
        v, why = check_change(params, c) if i < MAX_CHANGES else (None, f"oltre le {MAX_CHANGES} modifiche ammesse")
        if why:
            rejected.append({**c, "why": why})
        else:
            candidates.append({**c, "new_value": v})
    if not candidates:
        return params, accepted, rejected
    if judge is None:
        rejected += [{**c, "why": "nessun backtest disponibile per valutarla"} for c in candidates]
        return params, accepted, rejected
    current = params
    for c in candidates:
        trial = with_param(current, c["name"], c["new_value"])
        old = get_param(current, c["name"])
        verdict = _judge(judge, current, trial)
        if verdict is None:
            rejected.append({**c, "old_value": old, "why": "backtest fallito"})
        elif not verdict[0]:
            rejected.append({**c, "old_value": old, "why": f"backtest: {verdict[1]}"[:MAX_TEXT]})
        else:
            accepted.append({**c, "old_value": old, "backtest": verdict[1][:MAX_TEXT]})
            current = trial
    return current, accepted, rejected


def _judge(judge, current: dict, trial: dict) -> tuple[bool, str] | None:
    try:
        out = judge(current, trial)
    except Exception:  # noqa: BLE001 - a broken backtest rejects the change, it never accepts it
        return None
    if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], bool)):
        return None
    return out[0], str(out[1])


# ---- the model call ------------------------------------------------------------------
class ReflectError(RuntimeError):
    pass


def ask_model(model, prompt: str):
    """Send the reflection prompt through the adapter's ask() (model.py) with the reflection
    schema. Returns the raw answer (text or parsed JSON); raises ReflectError on any failure."""
    ask = getattr(model, "ask", None)
    if not callable(ask):
        raise ReflectError(f"adattatore {type(model).__name__} non supportato per la riflessione")
    try:
        return ask(prompt, schema=REFLECT_SCHEMA, system=REFLECT_SYSTEM)
    except ModelError as e:
        raise ReflectError(str(e)) from None


# ---- records -------------------------------------------------------------------------
def load_params(root: Path) -> dict:
    return json.loads((Path(root) / PARAMS_FILE).read_text(encoding="utf-8"))


def write_params(records, params: dict) -> None:
    records._write(PARAMS_FILE, json.dumps(params, indent=2, sort_keys=True) + "\n")


def write_lessons(records, lessons: list[str], slot: str, now: datetime) -> None:
    p = Path(records.root) / LESSONS_FILE
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    entries = old.split(ENTRY_MARK)[1:] if ENTRY_MARK in old else []
    entry = f"\n## {now.astimezone(UTC):%Y-%m-%d %H:%M} UTC · slot {slot}\n\n" + \
        "".join(f"- {x}\n" for x in lessons)
    entries = [entry] + entries[: MAX_LESSON_ENTRIES - 1]
    records._write(LESSONS_FILE, LESSONS_HEAD + "".join(ENTRY_MARK + e for e in entries))


def recent_lessons(root: Path, entries: int = 3) -> str:
    """The newest lesson sections, for the decision prompt. Empty if there are none."""
    p = Path(root) / LESSONS_FILE
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return "".join(text.split(ENTRY_MARK)[1:entries + 1]).strip()


def _journal_rows(records, now: datetime, days: int) -> list[dict]:
    return records.journal_since(f"{now - timedelta(days=days):%Y-%m-%d}")


def _journal_digest(rows: list[dict], limit: int = 40) -> list[dict]:
    """The last wakes, reduced to what a reflection can use."""
    out = []
    for r in rows[-limit:]:
        if r.get("kind") == "reflection":
            continue
        prop = r.get("proposal") or {}
        out.append({"slot": r.get("slot"), "status": r.get("status"), "market_view": prop.get("market_view"),
                    "error": prop.get("error") or r.get("error"),
                    "verdicts": [{k: v.get(k) for k in ("symbol", "action", "approved", "reason", "reason_model", "outcome")
                                  if k in v} for v in r.get("verdicts") or [] if isinstance(v, dict)],
                    "exits": r.get("exits"), "jev": (r.get("jev") or {}).get("regime")})
    return out


def build_prompt(template: str, *, trades: list[ClosedTrade], summary: dict, attrib: dict, params: dict,
                 journal: list[dict], lessons: str, now: datetime, fee_pct: float) -> str:
    bounds = {k: {"min": lo, "max": hi, "max_step": step, "current": get_param(params, k)}
              for k, (lo, hi, step) in st.PARAM_BOUNDS.items() if k in st.LEARNABLE}
    values = {
        "NOW": f"{now.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
        "FEE_PCT": f"{fee_pct}",
        "SUMMARY": json.dumps(summary, ensure_ascii=False, indent=1),
        "BY_KIND": json.dumps(summary_by_kind(trades), ensure_ascii=False, indent=1),
        "TRADES": json.dumps([t.as_dict() for t in trades[-40:]], ensure_ascii=False, default=str),
        "ATTRIBUTION": json.dumps(attrib, ensure_ascii=False, indent=1),
        "PARAMS": json.dumps(params, indent=1, sort_keys=True),
        "BOUNDS": json.dumps(bounds, indent=1),
        "MAX_CHANGES": str(MAX_CHANGES),
        "JOURNAL": json.dumps(_journal_digest(journal), ensure_ascii=False, default=str)[:20000],
        "LESSONS": lessons or "(nessuna lezione ancora)",
    }
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def run_reflection(*, root: Path, records, model, judge, now: datetime, slot: str,
                   fee_pct: float = FEE_PCT, days: int = 14, params: dict | None = None,
                   fills: list[dict] | None = None, orders: list[dict] | None = None) -> dict:
    """One reflection. Writes lessons and params, returns the journal record. Never raises.

    fills/orders default to the evidence of the last `days`; pass Alpaca's own lists to
    rebuild outcomes when the evidence is missing.
    """
    rec = {"kind": "reflection", "slot": slot, "at": now.isoformat(), "lessons": [], "accepted": [],
           "rejected": [], "params_written": False, "error": ""}
    try:
        params = load_params(root) if params is None else params
        journal = _journal_rows(records, now, days)
        if fills is None or orders is None:
            ev = list(records._evidence_rows(days, now))
            fills = [r["alpaca"] for r in ev if r.get("kind") == "fill" and isinstance(r.get("alpaca"), dict)]
            orders = [r["alpaca"] for r in ev if r.get("kind") == "order" and isinstance(r.get("alpaca"), dict)]
        trades = closed_trades(fills, orders, journal, fee_pct)
        rec["summary"], rec["attribution"] = summarize(trades), attribution(trades)
        rec["summary_by_kind"] = summary_by_kind(trades)
        prompt = build_prompt((Path(root) / PROMPT_FILE).read_text(encoding="utf-8"), trades=trades,
                              summary=rec["summary"], attrib=rec["attribution"], params=params, journal=journal,
                              lessons=recent_lessons(root), now=now, fee_pct=fee_pct)
    except Exception as e:  # noqa: BLE001 - a reflection must never stop the wake
        rec["error"] = f"preparazione della riflessione fallita: {type(e).__name__}: {e}"[:MAX_TEXT]
        return rec
    try:
        lessons, changes = parse_reflection(ask_model(model, prompt))
    except (ReflectError, InvalidReflection) as e:
        rec["error"] = f"risposta del modello scartata: {e}"
        return rec
    new_params, rec["accepted"], rec["rejected"] = decide_changes(params, changes, judge)
    rec["lessons"] = lessons
    if lessons:
        write_lessons(records, lessons, slot, now)
    if rec["accepted"]:
        write_params(records, new_params)
        rec["params_written"] = True
        rec["commit_reason"] = "; ".join(f"{a['name']} {a['old_value']} -> {a['new_value']}: {a['reason']}"
                                         for a in rec["accepted"])[:MAX_TEXT]
    return rec
