"""Commit this wake's records and push them to the branch, even when another wake pushed first.

Two wakes that start from the same commit both append to journal/decisions.jsonl and
evidence/, and both rewrite state/progress.md and state/last_handoff.json. A plain
`git pull --rebase` then conflicts on those files every time (reproduced with two clones
on 2026-09-27), and the retries cannot help. The records are almost all appends, so on a
rejected push this module does not rebase: it resets to the remote branch and writes this
wake's records on top of it.

  append files   journal/*.jsonl, evidence/*.jsonl: the lines this wake added go at the
                 end of the remote file, the ones already there are not repeated
  entry files    state/progress.md, lessons/lessons.md: the entries this wake added join
                 the remote ones, newest on top
  counters       state/cadence.json, state/daily_summary.json: key by key, the side that
                 changed it wins; when both did, the later value (they are dates and buckets)
  everything else, handoff, exit levels, params: the side that changed it wins; when both
                 did, the wake with the later slot, since it read Alpaca last

Only RECORD_PATHS are ever added: a change to the code in the working tree is never
committed by a wake.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .learn import ENTRY_MARK as LESSON_MARK
from .learn import LESSONS_FILE, LESSONS_HEAD, MAX_LESSON_ENTRIES
from .records import ENTRY_MARK as PROGRESS_MARK
from .records import MAX_PROGRESS_ENTRIES, PROGRESS_HEAD

RECORD_PATHS = ("state", "journal", "evidence", "lessons", "config/params.json")
HANDOFF = "state/last_handoff.json"
ENTRY_FILES = {"state/progress.md": (PROGRESS_MARK, PROGRESS_HEAD, MAX_PROGRESS_ENTRIES),
               LESSONS_FILE: (LESSON_MARK, LESSONS_HEAD, MAX_LESSON_ENTRIES)}
KEYWISE_FILES = ("state/cadence.json", "state/daily_summary.json")


class PublishError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise PublishError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return r


def _show(repo: Path, rev: str, path: str) -> str | None:
    r = _git(repo, "show", f"{rev}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def commit_message(repo: Path) -> str:
    try:
        h = json.loads((repo / HANDOFF).read_text(encoding="utf-8"))
        return h.get("commit_message") or f"Record wake {h['slot_id']} {h['status']}"
    except (OSError, ValueError, KeyError):
        return "Record wake"


# ---- merging one file ----------------------------------------------------------------

def _lines(text: str | None) -> list[str]:
    return (text or "").splitlines()


def merge_appends(base: str | None, ours: str | None, theirs: str | None) -> str:
    """The remote file plus the lines this wake added, in order, without repeats."""
    base_lines = _lines(base)
    ours_lines = _lines(ours)
    added = ours_lines[len(base_lines):] if ours_lines[:len(base_lines)] == base_lines else [
        x for x in ours_lines if x not in set(base_lines)]
    out = _lines(theirs)
    have = set(out)
    out += [x for x in added if x not in have]
    return "\n".join(out) + "\n" if out else ""


def _entries(text: str | None, mark: str) -> list[str]:
    return (text or "").split(mark)[1:] if text and mark in text else []


def merge_entries(base: str | None, ours: str | None, theirs: str | None, mark: str, head: str, keep: int) -> str:
    """Entry files (newest on top): the remote entries plus the ones this wake added, sorted
    by their heading (a date or slot, so newest first), at most `keep`."""
    old = set(_entries(base, mark))
    added = [e for e in _entries(ours, mark) if e not in old]
    theirs_entries = _entries(theirs, mark)
    merged = theirs_entries + [e for e in added if e not in set(theirs_entries)]
    merged.sort(key=lambda e: e.strip().splitlines()[0] if e.strip() else "", reverse=True)
    return head + "".join(mark + e for e in merged[:keep])


def merge_keywise(base: str | None, ours: str | None, theirs: str | None) -> str:
    """Small JSON dicts of counters: per key, the side that changed it; when both did, the
    larger value (a later date or bucket)."""
    def load(t):
        try:
            v = json.loads(t) if t else {}
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}
    b, o, t = load(base), load(ours), load(theirs)
    out = dict(t)
    for k in o.keys() | t.keys():
        if k not in t or (o.get(k) != b.get(k) and t.get(k) == b.get(k)):
            out[k] = o[k] if k in o else t[k]
        elif k in o and o[k] != t[k] and o.get(k) != b.get(k):
            out[k] = max(o[k], t[k], key=str) if isinstance(o[k], str) and isinstance(t[k], str) else t[k]
    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def merge_file(path: str, base: str | None, ours: str | None, theirs: str | None, ours_newer: bool) -> str | None:
    """This wake's version of `path` rewritten on top of the remote one. None: delete it."""
    if path.endswith(".jsonl"):
        return merge_appends(base, ours, theirs)
    if path in ENTRY_FILES:
        return merge_entries(base, ours, theirs, *ENTRY_FILES[path])
    if path in KEYWISE_FILES:
        return merge_keywise(base, ours, theirs)
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    return ours if ours_newer else theirs


def _handoff_key(text: str | None) -> tuple[str, str]:
    try:
        h = json.loads(text or "")
        return str(h.get("slot", "")), str(h.get("started_at", ""))
    except (ValueError, AttributeError):
        return "", ""


# ---- commit and push ------------------------------------------------------------------

def save(repo: Path, message: str | None = None, remote: str = "origin", branch: str = "work",
         attempts: int = 5, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> bool:
    """Commit the records under RECORD_PATHS and push them. Returns False when nothing was
    pushed after `attempts` tries; raises PublishError on a git failure other than a
    rejected push."""
    repo = Path(repo)
    present = [p for p in RECORD_PATHS if (repo / p).exists() or _show(repo, "HEAD", p) is not None]
    if not present:
        log("nessun record da salvare")
        return True
    _git(repo, "add", "-A", "--", *present)
    changed = [x for x in _git(repo, "diff", "--cached", "--name-only").stdout.splitlines() if x]
    if not changed:
        log("nessun record da salvare")
        return True
    message = message or commit_message(repo)
    base_rev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ours = {p: ((repo / p).read_text(encoding="utf-8") if (repo / p).exists() else None) for p in changed}
    base = {p: _show(repo, base_rev, p) for p in changed}
    _git(repo, "commit", "-q", "-m", message)

    for attempt in range(attempts):
        if attempt:
            sleep(min(5 * attempt, 20))
        push = _git(repo, "push", "-q", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            log(f"record salvati su {branch}" + (f" dopo {attempt} riscritture" if attempt else ""))
            return True
        log(f"push respinto ({push.stderr.strip()[:160]}): riscrivo i record sopra {remote}/{branch}")
        _git(repo, "fetch", "-q", remote, branch)
        _git(repo, "reset", "-q", "--hard", "FETCH_HEAD")
        theirs = {p: ((repo / p).read_text(encoding="utf-8") if (repo / p).exists() else None) for p in changed}
        ours_newer = _handoff_key(ours.get(HANDOFF, base.get(HANDOFF))) >= _handoff_key(
            (repo / HANDOFF).read_text(encoding="utf-8") if (repo / HANDOFF).exists() else None)
        for p in changed:
            merged = merge_file(p, base[p], ours[p], theirs[p], ours_newer)
            target = repo / p
            if merged is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(merged, encoding="utf-8")
        _git(repo, "add", "-A", "--", *[p for p in RECORD_PATHS if (repo / p).exists()
                                          or _show(repo, "HEAD", p) is not None])
        if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
            log("i record di questo risveglio erano già sul branch")
            return True
        _git(repo, "commit", "-q", "-m", message)
    log(f"push dei record fallito dopo {attempts} tentativi")
    return False
