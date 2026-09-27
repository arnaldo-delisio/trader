from datetime import UTC, datetime

import pytest

from trader.__main__ import main
from trader.broker import Alpaca
from trader.config import ConfigError, Switches, assert_paper, require_env, switches
from trader.slot import (
    cadence_bucket,
    is_last_slot_of_day,
    missed_count,
    parse_slot,
    slot_for,
    slot_id,
)


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets",
    "https://paper-api.alpaca.markets.evil.example",
    "http://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets/v2",
])
def test_paper_guard_refuses_anything_but_paper(url):
    with pytest.raises(ConfigError):
        assert_paper(url)
    with pytest.raises(ConfigError):
        Alpaca("k", "s", base_url=url)


def test_paper_guard_accepts_paper():
    assert_paper("https://paper-api.alpaca.markets/")


def test_live_url_in_env_stops_the_wake(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("MODEL", "fake")
    monkeypatch.setenv("BROKER", "fake")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    code = main(["wake", "--dry-run", "--records", str(tmp_path), "--slot", "2026-09-26T08:00Z"])
    assert code == 2
    assert "paper" in capsys.readouterr().err
    assert (tmp_path / "state/last_handoff.json").exists()  # even a refusal leaves a handoff


def test_missing_secrets_fail_loudly(monkeypatch, tmp_path, capsys):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CLAUDE_CODE_OAUTH_TOKEN", "BROKER", "MODEL", "ALPACA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    code = main(["wake", "--records", str(tmp_path), "--slot", "2026-09-26T08:00Z"])
    err = capsys.readouterr().err
    assert code == 2 and "TELEGRAM_BOT_TOKEN" in err


def test_require_env_names_every_missing_variable():
    with pytest.raises(ConfigError) as e:
        require_env(["A", "B"], {"A": "x"})
    assert "B" in str(e.value) and "A," not in str(e.value)


def test_kill_file_halts_everything_and_the_variable_only_stops_buys(tmp_path):
    assert switches(tmp_path, {}) == Switches()
    off = switches(tmp_path, {"TRADING_ENABLED": "false"})
    assert (off.halted, off.buys, off.liquidate) == (False, False, False)
    liq = switches(tmp_path, {"TRADING_ENABLED": "false", "LIQUIDATE": "true"})
    assert (liq.halted, liq.buys, liq.liquidate) == (False, False, True)
    (tmp_path / "KILL").touch()
    for env in ({"TRADING_ENABLED": "true"}, {"LIQUIDATE": "true"}):
        k = switches(tmp_path, env)
        assert (k.halted, k.buys, k.liquidate) == (True, False, False) and "KILL" in k.why


@pytest.mark.parametrize("value", ["", "false", "0", "maybe", "tru"])
def test_liquidate_needs_an_explicit_true(tmp_path, value):
    assert not switches(tmp_path, {"LIQUIDATE": value}).liquidate


def test_slot_is_the_15_minute_bucket():
    assert slot_for(datetime(2026, 9, 26, 8, 17, tzinfo=UTC)) == datetime(2026, 9, 26, 8, 15, tzinfo=UTC)
    assert slot_for(datetime(2026, 9, 26, 8, 14, 59, tzinfo=UTC)) == datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    assert slot_for(datetime(2026, 9, 26, 23, 59, tzinfo=UTC)) == datetime(2026, 9, 26, 23, 45, tzinfo=UTC)
    assert slot_id(datetime(2026, 9, 26, 8, 45, tzinfo=UTC)) == "20260926T0845Z"


def test_slot_override_must_be_on_the_schedule():
    assert parse_slot("2026-09-26T08:15Z") == parse_slot("20260926T0815Z")
    for bad in ("2026-09-26T08:10Z", "2026-09-26T08:17Z", "2026-09-26T08:15:30Z", "yesterday"):
        with pytest.raises(ValueError):
            parse_slot(bad)


def test_missed_slots_between():
    a = datetime(2026, 9, 26, 7, 30, tzinfo=UTC)
    assert missed_count(a, datetime(2026, 9, 26, 8, 15, tzinfo=UTC)) == 2
    assert missed_count(a, datetime(2026, 9, 26, 7, 45, tzinfo=UTC)) == 0


def test_cadence_bucket_is_six_hours_and_the_day_ends_at_23_45():
    assert cadence_bucket(datetime(2026, 9, 26, 11, 45, tzinfo=UTC)) == "20260926T0600Z"
    assert cadence_bucket(datetime(2026, 9, 26, 12, 0, tzinfo=UTC)) == "20260926T1200Z"
    assert is_last_slot_of_day(datetime(2026, 9, 26, 23, 45, tzinfo=UTC))
    assert not is_last_slot_of_day(datetime(2026, 9, 26, 23, 30, tzinfo=UTC))


@pytest.mark.parametrize("value", ["false", "FALSE", " False ", "0", "no", "off"])
def test_kill_variable_accepts_every_off_spelling(tmp_path, value):
    assert not switches(tmp_path, {"TRADING_ENABLED": value}).buys


def test_account_with_crypto_disabled_counts_as_blocked():
    from trader.context import snapshot
    snap = snapshot({"equity": "1", "last_equity": "1", "cash": "1", "trading_blocked": False,
                     "crypto_status": "INACTIVE"}, [], [], {})
    assert snap.trading_blocked


def test_cli_wiring_redacts_every_secret_env_value(monkeypatch, tmp_path, capsys):
    # The production path builds its secret list from the environment; check that wiring, not a fixture list.
    secrets = {"ALPACA_API_KEY": "PKWIRINGKEY00000", "ALPACA_SECRET_KEY": "wiring-secret-alpaca-xxxx",
               "TELEGRAM_BOT_TOKEN": "777:wiring-telegram-token", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-wiring-yyyy",
               "ANTHROPIC_API_KEY": "sk-ant-api-wiring-zzzz", "JEV_API_KEY": "jev-wiring-key-wwww",
               "GROQ_API_KEY": "gsk-wiring-groq-vvvv"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)  # no chat id: a dry run prints instead of sending
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    monkeypatch.setenv("BROKER", "fake")
    monkeypatch.setenv("MODEL", "fake")
    monkeypatch.setenv("RUN_URL", "https://example.invalid/" + "/".join(secrets.values()))
    from trader import jev  # the fake market has exploration candidates: Jev would be called over HTTP
    monkeypatch.setattr(jev, "classify", lambda state, **kw: jev.Regime(
        "neutral", jev.multiplier_for("neutral"), "none", None, None, None, 0, "finto"))
    assert main(["wake", "--dry-run", "--records", str(tmp_path), "--slot", "2026-09-26T08:00Z"]) == 0
    written = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    out = capsys.readouterr().out
    assert "[redatto]" in written
    for v in secrets.values():
        assert v not in written and v not in out, v


def test_dry_run_with_telegram_configured_really_sends():
    from trader import notify
    from trader.__main__ import sender_from_env
    env = {"TELEGRAM_BOT_TOKEN": "1:fake", "TELEGRAM_CHAT_ID": "-1"}
    assert sender_from_env(env, dry_run=True) is not notify.print_sender
    assert sender_from_env({}, dry_run=True) is notify.print_sender


def test_every_secret_the_workflow_passes_is_redacted():
    # A key the wake receives but SECRET_ENV does not list would reach the records unredacted.
    import re
    from pathlib import Path

    from trader.config import SECRET_ENV
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/wake.yml").read_text()
    passed = set(re.findall(r"^\s+([A-Z_]+):\s*\$\{\{\s*secrets\.", wf, re.MULTILINE))
    assert passed and passed <= set(SECRET_ENV), passed - set(SECRET_ENV)


def test_the_workflow_does_not_trade_unless_the_variable_says_true():
    # The code's own default is "enabled" (local runs); the scheduled wake must default to off.
    import re
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/wake.yml").read_text()
    line = re.search(r"^\s+TRADING_ENABLED:(.*)$", wf, re.MULTILINE).group(1)
    assert "vars.TRADING_ENABLED" in line and "'false'" in line
    assert switches(Path("/nonexistent"), {"TRADING_ENABLED": "false"}).buys is False


def test_the_workflow_passes_liquidate_defaulting_to_false():
    import re
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/wake.yml").read_text()
    line = re.search(r"^\s+LIQUIDATE:(.*)$", wf, re.MULTILINE).group(1)
    assert "vars.LIQUIDATE" in line and "'false'" in line
