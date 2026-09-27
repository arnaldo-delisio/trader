"""The repo's records. Every write goes through Records.write, which strips secrets.

  state/progress.md        handoff for a human, newest entry on top
  state/last_handoff.json  the same handoff, for the next wake-up
  state/daily_summary.json the last UTC day whose summary reached Telegram
  state/positions.json     exit levels (stop, take-profit) of every open position
  state/cadence.json       the 6-hour buckets already reflected and reported, the starting equity
  journal/decisions.jsonl  one line per wake: proposal, gate verdicts, final actions
  evidence/YYYY-MM-DD.jsonl orders and fills exactly as Alpaca returned them
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_PROGRESS_ENTRIES = 120
ENTRY_MARK = "<!-- entry -->"
PROGRESS_HEAD = "# Progress\n\nPassaggio di consegne tra un risveglio e il successivo. Il più recente in alto.\n\n"


def prepend_entry(old: str, mark: str, head: str, entry: str, keep: int) -> str:
    """A file of entries separated by `mark`, newest first: `entry` on top, at most `keep`."""
    entries = old.split(mark)[1:] if mark in old else []
    return head + "".join(mark + e for e in ([entry] + entries)[:keep])


def alpaca_objects(rows, kind: str) -> list[dict]:
    """The Alpaca objects of one kind ("order" or "fill") out of evidence rows."""
    return [r["alpaca"] for r in rows if isinstance(r, dict) and r.get("kind") == kind
            and isinstance(r.get("alpaca"), dict)]


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            # also the JSON-escaped spelling, since most records are JSON lines
            for form in {s, json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1]}:
                text = text.replace(form, "[redatto]")
    return text


class Records:
    def __init__(self, root: Path, secrets: list[str]):
        self.root = Path(root)
        self.secrets = secrets
        self.written: list[str] = []  # relative paths this run wrote, in order, for the trace

    def write(self, rel: str, text: str, append: bool = False) -> None:
        """Write (or append to) a record file under the root, secrets removed."""
        self._write(rel, text, append)

    def _write(self, rel: str, text: str, append: bool = False) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f:
            f.write(redact(text, self.secrets))
        if rel not in self.written:
            self.written.append(rel)

    def _line(self, obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str) + "\n"

    # ---- evidence -------------------------------------------------------
    def evidence(self, kind: str, raw: dict, fetched_at: datetime, note: str = "") -> None:
        rel = f"evidence/{fetched_at.astimezone(UTC):%Y-%m-%d}.jsonl"
        self._write(rel, self._line({"kind": kind, "fetched_at": fetched_at.isoformat(),
                                     "note": note, "alpaca": raw}), append=True)

    def evidence_rows(self, days: int, now: datetime):
        for i in range(days + 1):
            p = self.root / f"evidence/{(now - timedelta(days=i)):%Y-%m-%d}.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

    def alpaca(self, kind: str, days: int, now: datetime) -> list[dict]:
        """Orders or fills of the last `days` exactly as Alpaca returned them."""
        return alpaca_objects(self.evidence_rows(days, now), kind)

    def known_client_ids(self, now: datetime, days: int = 8) -> set[str]:
        return {r["alpaca"].get("client_order_id") for r in self.evidence_rows(days, now)
                if r.get("kind") == "order" and isinstance(r.get("alpaca"), dict)}

    def known_fill_ids(self, now: datetime, days: int = 8) -> set[str]:
        return {r["alpaca"].get("id") for r in self.evidence_rows(days, now)
                if r.get("kind") == "fill" and isinstance(r.get("alpaca"), dict)}

    # ---- journal --------------------------------------------------------
    def journal(self, entry: dict) -> None:
        self._write("journal/decisions.jsonl", self._line(entry), append=True)

    def journal_for_day(self, day: str) -> list[dict]:
        return self.journal_since(day, day)

    def journal_since(self, first_day: str, last_day: str = "9999-12-31") -> list[dict]:
        """Journal rows whose slot falls between two UTC days (YYYY-MM-DD), oldest first, in
        one read of the file: it grows by a row every 15 minutes."""
        p = self.root / "journal/decisions.jsonl"
        if not p.exists():
            return []
        lo, hi = first_day.replace("-", ""), last_day.replace("-", "")
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and lo <= str(r.get("slot", ""))[:8] <= hi:
                rows.append(r)
        return rows

    # ---- handoff --------------------------------------------------------
    def last_handoff(self) -> dict | None:
        p = self.root / "state/last_handoff.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_handoff(self, h: dict) -> None:
        self._write("state/last_handoff.json", json.dumps(h, ensure_ascii=False, indent=2, default=str) + "\n")
        self._prepend_progress(render_progress(h))

    def update_handoff(self, **fields) -> None:
        h = self.last_handoff() or {}
        h.update(fields)
        self._write("state/last_handoff.json", json.dumps(h, ensure_ascii=False, indent=2, default=str) + "\n")

    def _prepend_progress(self, entry: str) -> None:
        p = self.root / "state/progress.md"
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        self._write("state/progress.md", prepend_entry(old, ENTRY_MARK, PROGRESS_HEAD, entry, MAX_PROGRESS_ENTRIES))

    # ---- small state files ---------------------------------------------
    def read_state(self, name: str) -> dict:
        """state/<name>.json as a dict; empty when missing or unreadable (the caller rebuilds)."""
        try:
            out = json.loads((self.root / f"state/{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return out if isinstance(out, dict) else {}

    def write_state(self, name: str, data: dict) -> None:
        self._write(f"state/{name}.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True,
                                                     default=str) + "\n")

    def update_state(self, name: str, **fields) -> None:
        self.write_state(name, {**self.read_state(name), **fields})

    # ---- daily summary --------------------------------------------------
    def summary_sent_for(self) -> str:
        p = self.root / "state/daily_summary.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("last_sent_date", "")
        except (OSError, ValueError):
            return ""

    def mark_summary_sent(self, day: str, at: datetime) -> None:
        self._write("state/daily_summary.json",
                    json.dumps({"last_sent_date": day, "sent_at": at.isoformat()}, indent=2) + "\n")


def render_progress(h: dict) -> str:
    lines = [f"\n## {h['slot']} · {h['status']}\n",
             f"- Avvio: {h.get('started_at', '')}  Fine: {h.get('finished_at', '')}",
             f"- Run: {h.get('run_url') or 'locale'}"]
    if h.get("dry_run"):
        lines.append("- Modalità prova: nessun ordine inviato")
    lines.append(f"- Cosa è cambiato: {h.get('changed') or 'niente'}")
    lines.append(f"- Esito: {h.get('outcome', '')}")
    lines.append(f"- Rischio residuo: {h.get('remaining_risk', '')}")
    lines.append(f"- Prossimo compito: {h.get('next_job', '')}")
    for w in h.get("warnings", []):
        lines.append(f"- Attenzione: {w}")
    return "\n".join(lines) + "\n"
