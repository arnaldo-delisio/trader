"""The one call site for Jev: a market-regime classifier that scales position size.

Jev (TypeSafe's classifier) answers one `choice` question: risk_on, neutral or
risk_off. The answer only picks a size multiplier (MULTIPLIERS). It is never a
go/no-go and never touches the hard limits: the gate applies those after sizing.

Nothing here raises.
  - Jev unavailable (no key, network error, HTTP 429/529/5xx): try the Groq
    fallback, a plain chat call returning {"choice": ...}. The fallback has no
    probability distribution, so its answer is used as-is when the thresholds
    file says `trust_fallback`.
  - HTTP 401/422 from Jev is our bug (bad key, bad question shape), not an
    outage: no fallback, the result is neutral and `error` says "bug".
  - Anything else (fallback down, bad JSON, unknown option, confidence below
    the floor): neutral.
A neutral result carries MULTIPLIERS["neutral"], so a failure never sizes up.

Every call returns a Regime whose as_dict() belongs in the wake's journal.
Keys come from the environment only: JEV_API_KEY and GROQ_API_KEY.
Real response shape checked on 2026-09-26 against jev-1.13.0:
  {"model": "jev-1.13.0", "answers": {"market_regime": {"type": "choice",
   "choice": "risk_on", "confidence": 0.93, "probabilities": {...}}},
   "usage": {"input_tokens": 424, "output_tokens": 43}}
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Not llama-3.1-8b-instant: Groq answered 404 for it on 2026-09-26.
GROQ_MODEL = "openai/gpt-oss-20b"
TIMEOUT = 10  # seconds per request; a wake has minutes, not hours

QUESTION = "market_regime"
REGIMES = ("risk_on", "neutral", "risk_off")
# Size multipliers. None is above 1.0: the regime can only shrink a size the gate already allows.
MULTIPLIERS = {"risk_on": 1.0, "neutral": 0.7, "risk_off": 0.4}
NEUTRAL = "neutral"

# The thresholds file can raise the confidence floor, never lower it below this.
MIN_CONFIDENCE_FLOOR = 0.5
DEFAULT_THRESHOLDS = {"confidence_floor": 0.6, "trust_fallback": True}
THRESHOLDS_FILE = "config/jev-thresholds.json"

INSTRUCTIONS = ("Classify the current crypto market regime, to size long-only spot positions. "
                "Judge the whole market, not a single coin.")
CRITERIA = {
    "risk_on": "broad uptrend: most coins above their averages, positive momentum, calm or falling volatility",
    "neutral": "mixed, flat or unclear signals",
    "risk_off": "broad downtrend or stress: most coins below their averages, falling prices, rising volatility",
}


@dataclass(frozen=True)
class Regime:
    regime: str
    multiplier: float
    source: str            # "jev", "fallback" or "none"
    model: str | None
    choice: str | None     # the raw answer before banding, if any
    confidence: float | None
    latency_ms: int
    error: str             # what went wrong; empty only when Jev answered and the answer was used

    def as_dict(self) -> dict:
        return asdict(self)


def multiplier_for(regime: str) -> float:
    """Pure mapping; anything unknown is neutral."""
    return MULTIPLIERS.get(regime, MULTIPLIERS[NEUTRAL])


def load_thresholds(root: Path) -> tuple[dict, str]:
    """(thresholds, problem). A missing or bad file gives the defaults and says why."""
    p = Path(root) / THRESHOLDS_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8")).get(QUESTION)
    except (OSError, ValueError, AttributeError) as e:
        return dict(DEFAULT_THRESHOLDS), f"{THRESHOLDS_FILE} illeggibile: {type(e).__name__}"
    return clamp_thresholds(raw)


def clamp_thresholds(raw) -> tuple[dict, str]:
    if not isinstance(raw, dict) or raw.get("type") != "choice":
        return dict(DEFAULT_THRESHOLDS), f"{QUESTION}: serve una voce di tipo choice"
    floor = raw.get("confidence_floor")
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor) or floor > 1:
        return dict(DEFAULT_THRESHOLDS), f"{QUESTION}: confidence_floor non valido"
    trust = raw.get("trust_fallback", DEFAULT_THRESHOLDS["trust_fallback"])
    return {"confidence_floor": max(float(floor), MIN_CONFIDENCE_FLOOR), "trust_fallback": trust is True}, ""


class _Unavailable(Exception):
    """Jev itself is down or not configured: the boundary the fallback covers."""


class _Bug(Exception):
    """Our request is wrong. Falling back would hide it."""


def _post(opener, url: str, key: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "trader"})
    with opener(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _state_text(state) -> str:
    return state if isinstance(state, str) else json.dumps(state, sort_keys=True, default=str)


def _ask_jev(opener, key: str, state, timeout: float) -> tuple[str | None, float | None, str | None]:
    if not key:
        raise _Unavailable("JEV_API_KEY mancante")
    body = {"state": _state_text(state), "model": JEV_MODEL, "questions": {
        QUESTION: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}}}
    try:
        out = _post(opener, JEV_URL, key, body, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 422):
            raise _Bug(f"HTTP {e.code}") from None
        raise _Unavailable(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _Unavailable(type(e).__name__) from None
    ans = out["answers"][QUESTION]
    conf = ans.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf):
        conf = None
    return ans.get("choice"), conf, out.get("model")


def _ask_groq(opener, key: str, state, timeout: float) -> tuple[str | None, str | None]:
    if not key:
        raise _Unavailable("GROQ_API_KEY mancante")
    prompt = "\n".join([INSTRUCTIONS, "", "State:", _state_text(state), "", "Options:", json.dumps(CRITERIA), "",
                        'Respond with only this JSON shape, nothing else: {"choice": "<one of the option keys, verbatim>"}'])
    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0}
    out = _post(opener, GROQ_URL, key, body, timeout)
    return json.loads(out["choices"][0]["message"]["content"]).get("choice"), out.get("model", GROQ_MODEL)


def classify(state, *, env: dict | None = None, thresholds: dict | None = None, opener=None,
             timeout: float = TIMEOUT, clock=None) -> Regime:
    """Ask Jev for the market regime. Never raises; every failure is neutral with a reason."""
    env = os.environ if env is None else env
    opener = opener or urllib.request.urlopen  # resolved per call, so a test's fake is always the one used
    clock = clock or time.monotonic
    th = thresholds or dict(DEFAULT_THRESHOLDS)
    started = clock()
    source, model, choice, conf, error = "none", None, None, None, ""
    try:
        try:
            source = "jev"
            choice, conf, model = _ask_jev(opener, env.get("JEV_API_KEY", ""), state, timeout)
        except _Unavailable as e:
            error = f"Jev non disponibile: {e}"
            source = "fallback"
            choice, model = _ask_groq(opener, env.get("GROQ_API_KEY", ""), state, timeout)
    except _Bug as e:
        error = f"bug nella richiesta a Jev: {e}"
    except _Unavailable as e:
        error = f"{error}; fallback non disponibile: {e}"
    except urllib.error.HTTPError as e:
        error = f"{error}; fallback HTTP {e.code}".lstrip("; ")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError,
            AttributeError) as e:
        error = f"{error}; risposta inutilizzabile ({source}): {type(e).__name__}".lstrip("; ")

    regime = NEUTRAL
    if choice is not None and not error.startswith("bug"):
        if choice not in REGIMES:
            error = (error + "; " if error else "") + f"scelta sconosciuta {str(choice)[:40]!r}"
        elif source == "jev" and (conf is None or conf < th["confidence_floor"]):
            error = f"confidenza {conf} sotto la soglia {th['confidence_floor']}"
        elif source == "fallback" and not th.get("trust_fallback"):
            error = (error + "; " if error else "") + "risposta del fallback non considerata affidabile"
        else:
            regime = choice
    elif not error:
        error = "nessuna risposta"
    return Regime(regime=regime, multiplier=multiplier_for(regime), source=source, model=model,
                  choice=choice if isinstance(choice, str) else None, confidence=conf,
                  latency_ms=round((clock() - started) * 1000), error=error)
