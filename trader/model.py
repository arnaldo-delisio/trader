"""The model proposes; this module makes sure a proposal is exactly the agreed shape.

Anything else (bad JSON, extra keys, a refusal, a timeout, a crash) becomes
Proposal.hold(reason): every symbol holds and the reason is reported.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import ConfigError

ACTIONS = ("buy", "sell", "hold")
MAX_REASON = 500
MAX_DECISIONS = 20  # the shortlist plus every position, with room to spare
# bull_case and bear_case: the model argues both sides before choosing. The gate never reads them.
DECISION_KEYS = ("symbol", "action", "notional_usd", "bull_case", "bear_case", "reason")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions", "market_view", "next_job"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(DECISION_KEYS),
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "notional_usd": {"type": "number", "minimum": 0},
                    "bull_case": {"type": "string"},
                    "bear_case": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "market_view": {"type": "string"},
        "next_job": {"type": "string"},
    },
}

SYSTEM = ("You are the decision step of a small paper-trading agent. You have no tools. "
          "Answer with one JSON object matching the given schema and nothing else.")


@dataclass
class Decision:
    symbol: str
    action: str
    notional_usd: float
    reason: str
    bull_case: str = ""
    bear_case: str = ""


@dataclass
class Proposal:
    decisions: list[Decision] = field(default_factory=list)
    market_view: str = ""
    next_job: str = ""
    error: str = ""  # non-empty means the model output was not usable: hold everything

    @classmethod
    def hold(cls, error: str) -> Proposal:
        return cls(error=error)

    @property
    def valid(self) -> bool:
        return not self.error


class InvalidProposal(ValueError):
    pass


def _check_str(v, name: str) -> str:
    if not isinstance(v, str):
        raise InvalidProposal(f"{name} must be a string")
    return v[:MAX_REASON]


def validate(obj) -> Proposal:
    """Strict: exact keys, known actions, finite non-negative amounts, one decision per symbol."""
    if not isinstance(obj, dict):
        raise InvalidProposal("proposal is not a JSON object")
    if set(obj) != {"decisions", "market_view", "next_job"}:
        raise InvalidProposal(f"proposal keys {sorted(obj)} != decisions, market_view, next_job")
    if not isinstance(obj["decisions"], list):
        raise InvalidProposal("decisions must be a list")
    if len(obj["decisions"]) > MAX_DECISIONS:
        raise InvalidProposal("too many decisions")
    out, seen = [], set()
    for i, d in enumerate(obj["decisions"]):
        if not isinstance(d, dict) or set(d) != set(DECISION_KEYS):
            raise InvalidProposal(f"decision {i} must have exactly {', '.join(DECISION_KEYS)}")
        symbol = _check_str(d["symbol"], "symbol").strip()
        if symbol in seen:
            raise InvalidProposal(f"symbol {symbol} appears twice")
        seen.add(symbol)
        if d["action"] not in ACTIONS:
            raise InvalidProposal(f"decision {i}: action {d['action']!r} not in {ACTIONS}")
        n = d["notional_usd"]
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0:
            raise InvalidProposal(f"decision {i}: notional_usd must be a finite number >= 0")
        out.append(Decision(symbol, d["action"], float(n), _check_str(d["reason"], "reason"),
                            _check_str(d["bull_case"], "bull_case"), _check_str(d["bear_case"], "bear_case")))
    return Proposal(out, _check_str(obj["market_view"], "market_view"), _check_str(obj["next_job"], "next_job"))


def parse(text: str) -> Proposal:
    """Raw model text to Proposal. Never raises."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return Proposal.hold("la risposta del modello non è JSON valido")
    try:
        return validate(obj)
    except InvalidProposal as e:
        return Proposal.hold(f"proposta non valida: {e}")


# ---- adapters ----------------------------------------------------------------
# Each adapter has one call, ask(prompt, schema, system) -> the raw answer (a dict when the
# model returned structured output, else text), raising ModelError on any failure. propose()
# is the decision on top of it and never raises. The reflection (learn.py) uses ask() with
# its own schema, so there is one way to call each model.

class ModelError(RuntimeError):
    """The model gave no usable answer. The message says why, in Italian."""


def _proposal(raw) -> Proposal:
    if isinstance(raw, dict):
        try:
            return validate(raw)
        except InvalidProposal as e:
            return Proposal.hold(f"proposta non valida: {e}")
    text = str(raw).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return parse(text)


class _Adapter:
    name = "adapter"

    def ask(self, prompt: str, schema: dict, system: str):
        raise NotImplementedError

    def propose(self, prompt: str) -> Proposal:
        try:
            raw = self.ask(prompt, schema=SCHEMA, system=SYSTEM)
        except ModelError as e:
            return Proposal.hold(str(e))
        return _proposal(raw)


class FakeModel(_Adapter):
    """Returns canned replies in order (the last one repeats). Used by tests, the simulator
    and local dry runs. An Exception in the list is raised as a model failure."""
    name = "fake"

    def __init__(self, replies: list | None = None):
        self.replies = list(replies) if replies else [json.dumps({
            "decisions": [],
            "market_view": "prova locale con modello finto: nessuna proposta",
            "next_job": "niente di particolare"})]
        self.prompts: list[str] = []

    def ask(self, prompt: str, schema: dict, system: str):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise ModelError(f"errore del modello: {reply}")
        return reply


class ClaudeCLI(_Adapter):
    """Claude Code headless: one prompt on stdin, no tools, structured JSON out."""
    name = "claude-cli"

    def __init__(self, model: str = "sonnet", timeout: float = 240, runner=subprocess.run):
        self.model = model
        self.timeout = timeout
        self.runner = runner

    def command(self, schema: dict = SCHEMA, system: str = SYSTEM) -> list[str]:
        return ["claude", "-p", "--output-format", "json", "--tools", "",
                "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--disable-slash-commands", "--max-budget-usd", "1",
                "--model", self.model, "--system-prompt", system,
                "--json-schema", json.dumps(schema)]

    def ask(self, prompt: str, schema: dict, system: str):
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ModelError("CLAUDE_CODE_OAUTH_TOKEN mancante")
        try:
            with tempfile.TemporaryDirectory() as cwd:  # empty dir: no CLAUDE.md is picked up
                r = self.runner(self.command(schema, system), input=prompt, capture_output=True, text=True,
                                timeout=self.timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise ModelError(f"il modello non ha risposto entro {self.timeout:.0f}s") from None
        except OSError as e:
            raise ModelError(f"impossibile avviare claude: {type(e).__name__}") from None
        try:
            out = json.loads(r.stdout)
        except (TypeError, ValueError):
            raise ModelError(f"claude exit {r.returncode}, output non JSON") from None
        if not isinstance(out, dict):
            raise ModelError("claude: output JSON inatteso")
        if out.get("is_error") or out.get("subtype") != "success":
            raise ModelError(f"claude ha riportato un errore: {out.get('subtype')}")
        structured = out.get("structured_output")
        return structured if isinstance(structured, dict) else out.get("result", "")


class AnthropicAPI(_Adapter):
    """Messages API over urllib."""
    name = "anthropic-api"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, key: str, model: str = "claude-sonnet-5", timeout: float = 120, opener=urllib.request.urlopen):
        self.key, self.model, self.timeout, self.opener = key, model, timeout, opener

    def ask(self, prompt: str, schema: dict, system: str):
        body = {"model": self.model, "max_tokens": 3000, "system": system + " Schema: " + json.dumps(schema),
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(self.URL, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ModelError(f"API Anthropic: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ModelError(f"API Anthropic non raggiungibile: {type(e).__name__}") from None
        if out.get("stop_reason") == "refusal":
            raise ModelError("il modello ha rifiutato di rispondere")
        return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()


def from_env(env: dict | None = None):
    env = os.environ if env is None else env
    kind = env.get("MODEL", "claude-cli")
    if kind == "claude-cli":
        return ClaudeCLI(model=env.get("CLAUDE_MODEL", "sonnet"))
    if kind == "anthropic-api":
        if not env.get("ANTHROPIC_API_KEY"):
            raise ConfigError("MODEL=anthropic-api needs ANTHROPIC_API_KEY")
        return AnthropicAPI(env["ANTHROPIC_API_KEY"], model=env.get("ANTHROPIC_MODEL", "claude-sonnet-5"))
    if kind == "fake":
        return FakeModel()
    raise ConfigError(f"MODEL={kind!r}: use claude-cli, anthropic-api or fake")
