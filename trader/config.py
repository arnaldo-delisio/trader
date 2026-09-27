"""Constants, limits and environment. Every value the rest of the code trusts starts here."""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets/v1beta3/crypto/us"

# A slot is a 15-minute bucket (UTC). The cron fires every 15 minutes and GitHub
# starts it 5 to 15 minutes late: the slot is the bucket the run starts in.
SLOT_MINUTES = 15
# Reflection and the status message happen once per bucket of this many hours (00, 06, 12, 18 UTC).
CADENCE_HOURS = 6

ORDER_PREFIX = "trd-"

# The universe: every tradable crypto/USD pair on Alpaca except stablecoins and PAXG, as
# listed on 2026-09-26. Each wake intersects it with the live /v2/assets list. PEPE/USD is
# tradable too but is left out: the backtest had no history for it.
HARD_SYMBOLS = tuple(f"{b}/USD" for b in (
    "AAVE", "ADA", "ARB", "AVAX", "BAT", "BCH", "BONK", "BTC", "CRV", "DOGE", "DOT", "ETH", "FIL",
    "GRT", "HYPE", "LDO", "LINK", "LTC", "ONDO", "POL", "RENDER", "SHIB", "SKY", "SOL", "SUSHI",
    "TRUMP", "UNI", "WIF", "XRP", "XTZ", "YFI"))
EXCLUDED_BASES = frozenset({"USDC", "USDT", "USDG", "PAXG"})
# Memecoins get a lower cap per position (max_memecoin_pct).
MEMECOIN_BASES = frozenset({"DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP", "FLOKI"})

# Ceilings the config file cannot raise, and floors it cannot lower.
HARD_CEILINGS = {
    "max_invested_pct": 60.0,      # crypto held + new buys, in % of equity
    "max_position_pct": 8.0,       # one coin, in % of equity
    "max_memecoin_pct": 3.0,       # one memecoin, in % of equity
    # 25 since 2026-09-27: at 10, eight positions plus two unfilled buys blocked every buy
    # overnight. The money caps (60% invested, 8% and 3% per coin) bind long before 25.
    "max_open_positions": 25,
    "max_orders_per_wake": 5,
    "daily_loss_limit_pct": 5.0,   # beyond this loss since the previous close: no buys
    # Exploration: small buys below the entry threshold, so the system trades and the
    # reflection has outcomes to read (strategy.explore_ok). 0 in the config turns it off.
    "explore_position_pct": 1.5,   # one exploration buy, in % of equity
    "explore_total_pct": 15.0,     # every exploration position together, in % of equity
}
HARD_FLOORS = {"min_order_usd": 10.0}
# A sell of the whole holding (an exit) may go below min_order_usd, down to Alpaca's own
# minimum: min_order_size of each /USD asset was worth 0.96 to 1.07 $ on 2026-09-26. The gate
# uses the asset's min_order_size when it knows it, and this floor in any case. Without this,
# a position that shrank under min_order_usd could never be sold by its stop.
EXIT_MIN_ORDER_USD = 1.0
# A holding worth less than this is fee dust Alpaca leaves behind after a sale (the buy fee
# is kept in the coin): it is not a position for the caps, the position count or the exits.
DUST_USD = 1.0
INTEGER_LIMITS = frozenset({"max_open_positions", "max_orders_per_wake"})

SECRET_ENV = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "JEV_API_KEY",
    "GROQ_API_KEY",
)


class ConfigError(Exception):
    """The program must not start. The message says why, in plain words."""


def is_position(value_usd: float) -> bool:
    """A holding that counts as a position: worth at least DUST_USD."""
    return value_usd >= DUST_USD


def is_memecoin(symbol: str) -> bool:
    return symbol.replace("/", "").upper().removesuffix("USD") in MEMECOIN_BASES


@dataclass(frozen=True)
class Limits:
    symbols: tuple[str, ...]
    max_invested_pct: float
    max_position_pct: float
    max_memecoin_pct: float
    max_open_positions: int
    max_orders_per_wake: int
    daily_loss_limit_pct: float
    min_order_usd: float
    explore_position_pct: float
    explore_total_pct: float

    def position_cap_pct(self, symbol: str) -> float:
        return self.max_memecoin_pct if is_memecoin(symbol) else self.max_position_pct


def clamp_limits(raw: dict) -> Limits:
    """Apply the hard ceilings and floors to whatever the config asked for."""
    known = {f.name for f in fields(Limits)}
    missing = known - raw.keys()
    if missing:
        raise ConfigError(f"limits: missing keys {sorted(missing)}")
    values = {}
    for name in known - {"symbols"}:
        try:
            v = float(raw[name])
        except (TypeError, ValueError):
            raise ConfigError(f"limits: {name} must be a number") from None
        if math.isnan(v) or v < 0:
            raise ConfigError(f"limits: {name} must be a non-negative number")
        if name in HARD_CEILINGS:
            v = min(v, HARD_CEILINGS[name])
        if name in HARD_FLOORS:
            v = max(v, HARD_FLOORS[name])
        values[name] = int(v) if name in INTEGER_LIMITS else v
    symbols = tuple(s for s in raw["symbols"] if s in HARD_SYMBOLS)
    return Limits(symbols=symbols, **values)


def load_limits(path: Path) -> Limits:
    with open(path, "rb") as f:
        return clamp_limits(tomllib.load(f))


def assert_paper(*urls: str) -> None:
    """Refuse to run against anything but the paper trading host."""
    for url in urls:
        if url.rstrip("/") != PAPER_URL:
            raise ConfigError(f"refusing to start: {url!r} is not the Alpaca paper URL {PAPER_URL}")


def require_env(names: list[str], env: dict | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise ConfigError("missing required environment variables: " + ", ".join(missing))
    return {n: env[n] for n in names}


def secret_values(env: dict | None = None) -> list[str]:
    """Values that must never reach a record or a message."""
    env = os.environ if env is None else env
    return [env[n] for n in SECRET_ENV if env.get(n) and len(env[n]) >= 6]


OFF_WORDS = ("false", "0", "no", "off")
ON_WORDS = ("true", "1", "yes", "on")


@dataclass(frozen=True)
class Switches:
    """What the owner allows this wake to do.

    halted     a KILL file in the repo root: no order at all, exits included
    buys       TRADING_ENABLED is not off: new entries allowed. Off means exits only: stops,
               take-profits and the other code exits still sell
    liquidate  LIQUIDATE is on: sell every position, cancel the open orders, report the P&L
    """
    halted: bool = False
    buys: bool = True
    liquidate: bool = False
    why: str = ""


def switches(root: Path, env: dict | None = None) -> Switches:
    env = os.environ if env is None else env
    if (root / "KILL").exists():
        return Switches(halted=True, buys=False, why="file KILL presente nella root del repo")
    liquidate = env.get("LIQUIDATE", "").strip().lower() in ON_WORDS
    if liquidate:
        return Switches(buys=False, liquidate=True, why="variabile LIQUIDATE=true: si vende tutto")
    if env.get("TRADING_ENABLED", "true").strip().lower() in OFF_WORDS:
        return Switches(buys=False, why="variabile TRADING_ENABLED=false: niente acquisti, le uscite restano")
    return Switches()
