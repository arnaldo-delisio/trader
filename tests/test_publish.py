"""Saving the records when another wake pushed first: two clones of one bare remote."""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from trader import publish
from trader.records import Records


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def remote(tmp_path):
    """A bare remote on branch work with one wake's records, and a function to clone it."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "work", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    setup(seed)
    (seed / "trader.py").write_text("code\n")
    write_wake(seed, "2026-09-26T08:00:00+00:00", "20260926T0800Z")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "HEAD:work")

    def clone(name):
        d = tmp_path / name
        git(tmp_path, "clone", "-q", "-b", "work", str(bare), str(d))
        setup(d)
        return d
    return bare, clone


def setup(d):
    git(d, "config", "user.name", "test")
    git(d, "config", "user.email", "test@example.invalid")


def write_wake(d, slot, slot_id, reflected=None):
    """What a wake writes: a journal row, an evidence row, a progress entry, the handoff."""
    rec = Records(d, [])
    rec.journal({"kind": "wake", "slot": slot_id})
    rec.evidence("fill", {"id": f"fill-{slot_id}"}, datetime(2026, 9, 26, 9, tzinfo=UTC))
    rec.write_handoff({"slot": slot, "slot_id": slot_id, "status": "no_trade", "started_at": slot,
                       "commit_message": f"Record wake {slot_id} no_trade"})
    if reflected:
        rec.update_state("cadence", reflection=reflected)


def remote_file(bare, path):
    return subprocess.run(["git", "show", f"work:{path}"], cwd=bare, capture_output=True, text=True, check=False).stdout


def journal_slots(text):
    return [json.loads(x)["slot"] for x in text.splitlines()]


def test_a_plain_rebase_conflicts_on_the_records(remote):
    # The problem this module exists for: two wakes from the same commit.
    _, clone = remote
    a, b = clone("a"), clone("b")
    write_wake(a, "2026-09-26T08:15:00+00:00", "20260926T0815Z")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "a")
    git(a, "push", "-q", "origin", "HEAD:work")
    write_wake(b, "2026-09-26T08:30:00+00:00", "20260926T0830Z")
    git(b, "add", "-A")
    git(b, "commit", "-q", "-m", "b")
    r = subprocess.run(["git", "pull", "--rebase", "-q", "origin", "work"], cwd=b, capture_output=True, text=True,
                       check=False)
    assert r.returncode != 0 and "conflict" in (r.stdout + r.stderr).lower()


def test_two_wakes_from_the_same_commit_both_land(remote):
    bare, clone = remote
    a, b = clone("a"), clone("b")
    write_wake(a, "2026-09-26T08:15:00+00:00", "20260926T0815Z", reflected="20260926T0600Z")
    write_wake(b, "2026-09-26T08:30:00+00:00", "20260926T0830Z")
    logs = []
    assert publish.save(a, sleep=lambda s: None, log=logs.append)
    assert publish.save(b, sleep=lambda s: None, log=logs.append)
    assert any("riscrivo" in x for x in logs)
    assert journal_slots(remote_file(bare, "journal/decisions.jsonl")) == [
        "20260926T0800Z", "20260926T0815Z", "20260926T0830Z"]
    assert remote_file(bare, "evidence/2026-09-26.jsonl").count("fill-") == 3
    progress = remote_file(bare, "state/progress.md")
    assert progress.index("08:30:00") < progress.index("08:15:00") < progress.index("08:00:00")
    assert json.loads(remote_file(bare, "state/last_handoff.json"))["slot_id"] == "20260926T0830Z"
    # a counter only one side changed survives the other side's rewrite
    assert json.loads(remote_file(bare, "state/cadence.json"))["reflection"] == "20260926T0600Z"
    assert git(bare, "log", "-1", "--format=%s", "work").strip() == "Record wake 20260926T0830Z no_trade"


def test_an_older_wake_saved_late_does_not_overwrite_the_newer_handoff(remote):
    bare, clone = remote
    a, b = clone("a"), clone("b")
    write_wake(a, "2026-09-26T08:30:00+00:00", "20260926T0830Z")
    write_wake(b, "2026-09-26T08:15:00+00:00", "20260926T0815Z")
    assert publish.save(a, sleep=lambda s: None, log=lambda s: None)
    assert publish.save(b, sleep=lambda s: None, log=lambda s: None)
    assert json.loads(remote_file(bare, "state/last_handoff.json"))["slot_id"] == "20260926T0830Z"
    assert "20260926T0815Z" in journal_slots(remote_file(bare, "journal/decisions.jsonl"))


def test_code_in_the_working_tree_is_never_committed(remote):
    bare, clone = remote
    a = clone("a")
    (a / "trader.py").write_text("changed by accident\n")
    (a / "notes.txt").write_text("not a record\n")
    write_wake(a, "2026-09-26T08:15:00+00:00", "20260926T0815Z")
    assert publish.save(a, sleep=lambda s: None, log=lambda s: None)
    assert remote_file(bare, "trader.py") == "code\n" and remote_file(bare, "notes.txt") == ""


def test_nothing_to_save_pushes_nothing(remote):
    bare, clone = remote
    a = clone("a")
    before = git(bare, "rev-parse", "work")
    assert publish.save(a, sleep=lambda s: None, log=lambda s: None)
    assert git(bare, "rev-parse", "work") == before


def test_it_gives_up_and_says_so_when_the_push_keeps_failing(remote, tmp_path):
    bare, clone = remote
    a = clone("a")
    write_wake(a, "2026-09-26T08:15:00+00:00", "20260926T0815Z")
    hook = bare / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    logs = []
    assert publish.save(a, attempts=2, sleep=lambda s: None, log=logs.append) is False
    assert "fallito dopo 2 tentativi" in logs[-1]


def test_merge_appends_keeps_order_and_skips_repeats():
    base = "a\nb\n"
    assert publish.merge_appends(base, "a\nb\nc\nd\n", "a\nb\nx\n") == "a\nb\nx\nc\nd\n"
    assert publish.merge_appends(base, "a\nb\nc\n", "a\nb\nc\n") == "a\nb\nc\n"
    assert publish.merge_appends(None, "c\n", None) == "c\n"


def test_merge_keywise_takes_the_side_that_changed_and_the_later_bucket_when_both_did():
    def m(b, o, t):
        return json.loads(publish.merge_keywise(json.dumps(b), json.dumps(o), json.dumps(t)))
    assert m({"x": 1.0}, {"x": 2.0}, {"x": 1.0}) == {"x": 2.0}  # only ours changed
    assert m({"x": 1.0}, {"x": 1.0}, {"x": 3.0}) == {"x": 3.0}  # only theirs changed
    assert m({"s": "0000Z"}, {"s": "0600Z"}, {"s": "1200Z"}) == {"s": "1200Z"}
    assert m({"s": "0000Z"}, {"s": "1200Z"}, {"s": "0600Z"}) == {"s": "1200Z"}
    assert m({}, {"new": "a"}, {"other": "b"}) == {"new": "a", "other": "b"}
