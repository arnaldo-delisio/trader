"""Build the dashboard from the repo records.

    python -m ui.build [--root REPO] [--out DIR]     (from the repo root)

Writes DIR/index.html (self-contained) and DIR/data.json (what the page shows).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ui import data, render

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _write(path: Path, text: str) -> None:
    """Write then rename, so the server never hands out a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build(root: Path, out: Path, now: datetime | None = None) -> dict:
    model = data.load(root, now or datetime.now(UTC))
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "data.json", json.dumps(model, ensure_ascii=False, indent=1, default=str) + "\n")
    _write(out / "index.html", render.page(model))
    return model


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Genera la dashboard dai record del repo.")
    ap.add_argument("--root", type=Path, default=REPO, help="radice del repo con i record (default: questo repo)")
    ap.add_argument("--out", type=Path, default=HERE / "site", help="cartella di uscita (default: ui/site)")
    a = ap.parse_args(argv)
    m = build(a.root, a.out)
    print(f"dashboard: {a.out / 'index.html'} ({len(m['wakes'])} risvegli, {len(m['trades'])} operazioni"
          + (f", {len(m['problems'])} problemi nei record" if m["problems"] else "") + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
