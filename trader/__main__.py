"""python -m trader wake [--slot ISO] [--dry-run] [--records DIR] [--reflect] [--always-ask]
python -m trader save-records [--branch work]     commit and push the records (trader/publish.py)
python -m trader alert MESSAGE                    a failure notice on Telegram

Environment:
  ALPACA_API_KEY, ALPACA_SECRET_KEY          paper keys (not needed with BROKER=fake)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID       required unless --dry-run (then messages are printed)
  MODEL=claude-cli|anthropic-api|fake        default claude-cli (needs CLAUDE_CODE_OAUTH_TOKEN)
  BROKER=alpaca|fake                         default alpaca; fake is an in-memory paper account
  TRADING_ENABLED=false                      no new buys; stops and take-profits still sell
  LIQUIDATE=true                             sell every position, cancel open orders, report the P&L
  (a KILL file in the repo root stops every order, exits included)
  JEV_API_KEY, GROQ_API_KEY                  market regime (Jev, Groq fallback); without them: neutral
  RUN_URL                                    link to the Actions run, shown in messages
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import model as models
from . import notify, publish
from .broker import Alpaca
from .config import ConfigError, assert_paper, load_limits, require_env, secret_values
from .fake_alpaca import FakeAlpaca
from .records import Records
from .slot import parse_slot, slot_for
from .wake import Deps, wake

ROOT = Path(__file__).resolve().parent.parent
RECORD_DIRS = ("state", "journal", "evidence", "lessons", "config")


def build_from_env(env: dict):
    def build():
        for name in ("ALPACA_BASE_URL", "APCA_API_BASE_URL", "ALPACA_TRADING_URL"):
            if env.get(name):
                assert_paper(env[name])
        if env.get("BROKER", "alpaca") == "fake":
            alpaca = Alpaca("fake-key", "fake-secret", transport=FakeAlpaca(), sleep=lambda s: None)
        else:
            keys = require_env(["ALPACA_API_KEY", "ALPACA_SECRET_KEY"], env)
            alpaca = Alpaca(keys["ALPACA_API_KEY"], keys["ALPACA_SECRET_KEY"])
        mdl = models.from_env(env)
        if getattr(mdl, "name", "") == "claude-cli":
            require_env(["CLAUDE_CODE_OAUTH_TOKEN"], env)
        return alpaca, mdl
    return build


def sender_from_env(env: dict, dry_run: bool):
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        return notify.telegram_sender(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], secret_values(env))
    if dry_run:
        return notify.print_sender
    return None


def cmd_wake(args, env: dict) -> int:
    slot = parse_slot(args.slot) if args.slot else slot_for(datetime.now(UTC))
    records_dir = Path(args.records) if args.records else ROOT
    if args.dry_run and not args.records:
        # A dry run reads the real records but writes to a throwaway copy.
        records_dir = Path(tempfile.mkdtemp(prefix="trader-dry-"))
        for d in RECORD_DIRS:
            if (ROOT / d).exists():
                shutil.copytree(ROOT / d, records_dir / d)
    send = sender_from_env(env, args.dry_run)
    missing_telegram = send is None
    if missing_telegram:
        send = notify.print_sender
    deps = Deps(root=ROOT, records=Records(records_dir, secret_values(env)),
                limits=load_limits(ROOT / "config/limits.toml"), send=send,
                build=build_from_env(env), env=env, run_url=env.get("RUN_URL", ""),
                reflect=True if args.reflect else None, always_ask=args.always_ask)
    if missing_telegram:
        # Still run: the handoff gets written, and the failure is loud.
        inner = deps.build

        def build():
            require_env(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"], env)
            return inner()
        deps.build = build
    return wake(deps, slot, dry_run=args.dry_run)


def cmd_alert(args, env: dict) -> int:
    """Last-resort notice from the workflow when the wake could not even start."""
    send = sender_from_env(env, dry_run=True)
    ok, detail = notify.safe_send(send, notify.failure_message(args.slot or "sconosciuto", args.message,
                                                               env.get("RUN_URL", "")), secret_values(env))
    print(f"notifica: {detail}")
    return 0 if ok else 1


def cmd_save_records(args, env: dict) -> int:
    try:
        return 0 if publish.save(ROOT, remote=args.remote, branch=args.branch) else 1
    except publish.PublishError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wake", help="one wake-up: reconcile, decide, gate, trade, record, notify")
    w.add_argument("--slot", help="scheduled slot to run, e.g. 2026-09-26T08:00Z (default: current slot)")
    w.add_argument("--dry-run", action="store_true", help="no orders; records go to a temp copy")
    w.add_argument("--records", help="directory for records (default: the repo)")
    w.add_argument("--reflect", action="store_true", help="run the reflection now, whatever the 6-hour rule says")
    w.add_argument("--always-ask", action="store_true", help="ask the model even without buy candidates")
    a = sub.add_parser("alert", help="send a failure notice")
    a.add_argument("message")
    a.add_argument("--slot")
    s = sub.add_parser("save-records", help="commit the records and push them, rewriting them on top if needed")
    s.add_argument("--remote", default="origin")
    s.add_argument("--branch", default="work")
    args = p.parse_args(argv)
    env = dict(os.environ)
    try:
        return {"wake": cmd_wake, "alert": cmd_alert, "save-records": cmd_save_records}[args.cmd](args, env)
    except (ConfigError, ValueError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
