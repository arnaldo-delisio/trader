"""scripts/reset_records.py on a throwaway copy of the repo's layout."""

import importlib.util
from pathlib import Path

from trader.learn import LESSONS_HEAD

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/reset_records.py"
spec = importlib.util.spec_from_file_location("reset_records", SCRIPT)
reset_records = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_records)


def layout(root: Path) -> None:
    for rel, text in {"state/cadence.json": '{"baseline_equity": 99999.87}', "state/progress.md": "x",
                      "journal/decisions.jsonl": "{}\n", "evidence/2026-09-26.jsonl": "{}\n",
                      "lessons/lessons.md": LESSONS_HEAD + "<!-- lesson -->\n- una lezione\n",
                      "config/params.json": "{}", "config/limits.toml": "x", "trader/wake.py": "code",
                      "prompts/decide.md": "p"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_without_yes_it_only_lists_and_touches_nothing(tmp_path):
    layout(tmp_path)
    before = {p: p.read_text() for p in tmp_path.rglob("*") if p.is_file()}
    lines = []
    assert reset_records.reset(tmp_path, yes=False, log=lines.append) == 1
    assert {p: p.read_text() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert any("state/cadence.json" in x for x in lines)


def test_with_yes_it_empties_the_records_and_nothing_else(tmp_path):
    layout(tmp_path)
    assert reset_records.reset(tmp_path, yes=True, log=lambda s: None) == 0
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == ["config/limits.toml", "config/params.json", "lessons/lessons.md", "prompts/decide.md",
                    "trader/wake.py"]
    assert (tmp_path / "lessons/lessons.md").read_text() == LESSONS_HEAD
    assert reset_records.reset(tmp_path, yes=True, log=lambda s: None) == 0  # a second run: nothing to do
