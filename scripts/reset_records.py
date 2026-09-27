"""Svuota i record, per partire da zero con un fork.

    uv run python scripts/reset_records.py          # dice cosa toglierebbe, non tocca niente
    uv run python scripts/reset_records.py --yes    # lo fa

Un fork riceve i record del conto di chi l'ha pubblicato: handoff, journal, evidence,
lezioni e il patrimonio di partenza in state/cadence.json. Il primo risveglio li leggerebbe
come propri (guadagno "dall'inizio" sbagliato, lezioni altrui nel prompt, slot saltati).
Questo script toglie state/, journal/, evidence/ e riporta lessons/lessons.md alla sola
intestazione. Non tocca il codice, i prompt e la configurazione: config/params.json resta
com'è (sono i parametri in uso; quelli di partenza sono nella storia git del file).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.learn import LESSONS_FILE, LESSONS_HEAD

ROOT = Path(__file__).resolve().parent.parent
RECORD_DIRS = ("state", "journal", "evidence")


def plan(root: Path) -> list[Path]:
    """The record files to remove, under RECORD_DIRS only."""
    out = []
    for d in RECORD_DIRS:
        if (root / d).is_dir():
            out += sorted(p for p in (root / d).rglob("*") if p.is_file())
    return out


def reset(root: Path, yes: bool, log=print) -> int:
    files = plan(root)
    lessons = root / LESSONS_FILE
    lessons_dirty = lessons.exists() and lessons.read_text(encoding="utf-8") != LESSONS_HEAD
    if not files and not lessons_dirty:
        log("niente da togliere: i record sono già vuoti")
        return 0
    for p in files:
        log(("tolgo " if yes else "toglierei ") + str(p.relative_to(root)))
    if lessons_dirty:
        log(("svuoto " if yes else "svuoterei ") + LESSONS_FILE)
    if not yes:
        log("nessun file toccato: rilancia con --yes per farlo davvero")
        return 1
    for p in files:
        p.unlink()
    if lessons_dirty:
        lessons.write_text(LESSONS_HEAD, encoding="utf-8")
    log("fatto: fai commit e push, poi il primo risveglio parte da zero")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="togli davvero i file")
    p.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    return reset(Path(args.root), args.yes)


if __name__ == "__main__":
    sys.exit(main())
