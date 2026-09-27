import json
import urllib.error

from tests.conftest import FAKE_ENV
from trader import notify

TOKEN = FAKE_ENV["TELEGRAM_BOT_TOKEN"]


def test_sender_reports_failure_instead_of_raising_and_hides_the_token():
    def opener(req, timeout):
        raise urllib.error.URLError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    send = notify.telegram_sender(TOKEN, "-1", [TOKEN], opener=opener, sleep=lambda s: None)
    ok, detail = send("ciao")
    assert ok is False and TOKEN not in detail


def test_sender_posts_html_to_the_chat():
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return R()
    ok, _ = notify.telegram_sender(TOKEN, "-1", [], opener=opener)("<b>x</b>")
    assert ok and seen["body"]["parse_mode"] == "HTML" and seen["body"]["chat_id"] == "-1"
    assert seen["url"].endswith("/sendMessage")


def test_safe_send_swallows_a_crashing_sender():
    def boom(text):
        raise RuntimeError("x")
    assert notify.safe_send(boom, "t", [])[0] is False


def test_model_text_is_escaped_in_html():
    msg = notify.wake_message({"slot": "2026-09-26T08:00:00+00:00", "status": "ok", "decisions": [],
                               "warnings": [], "market_view": "<script>&"})
    assert "<script>" not in msg and "&lt;script&gt;&amp;" in msg


def test_amounts_are_in_italian_format():
    assert notify.usd(1234.5) == "1.234,50 $"


def test_secret_is_redacted_in_its_json_escaped_form():
    from trader.records import redact
    secret = 'abc"def\\ghi'
    line = json.dumps({"x": "leak " + secret})
    assert secret not in json.loads(redact(line, [secret])).get("x", "")
    assert "[redatto]" in redact(line, [secret])


def test_status_message_says_when_the_kill_switch_is_on():
    h = {"slot": "2026-09-26T12:00:00+00:00", "status": "killed", "equity": 100.0, "positions": []}
    assert "Kill switch" in notify.status_message(h, 100.0, [])
    assert "Kill switch" not in notify.status_message({**h, "status": "no_trade"}, 100.0, [])


def test_trade_message_shows_the_exit_reason_and_the_new_stop():
    h = {"slot": "2026-09-26T12:00:00+00:00", "status": "ok", "warnings": [], "alerts": [], "decisions": [
        {"symbol": "BTC/USD", "action": "sell", "requested_usd": 50.0, "approved": True, "reason": "ammesso",
         "auto": True, "exit": "stop", "reason_model": "uscita automatica (stop): prezzo 90, stop 95",
         "outcome": "placed"},
        {"symbol": "SOL/USD", "action": "buy", "requested_usd": 40.0, "approved": True, "reason": "ammesso",
         "outcome": "placed", "stop": 140.5, "take_profit": 170.25, "bull_case": "b", "bear_case": "c"}]}
    text = notify.wake_message(h)
    assert "🛑" in text and "uscita automatica (stop)" in text
    assert "stop 140,5 · take-profit 170,25" in text
