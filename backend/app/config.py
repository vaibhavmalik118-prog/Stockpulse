"""Application settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    finnhub_api_key: str = ""
    data_mode: str = "demo"                 # "finnhub" (live) or "demo" (simulated ticks)
    db_path: str = "stockpulse.db"
    starting_cash: float = 100_000.0
    cooldown_seconds: int = 300             # 5-minute cooldown between trades
    daily_trade_limit: int = 10
    max_consecutive_losses: int = 3         # auto-halt after this many losing trades in a row
    history_minutes: int = 240              # how many 1-minute candles to seed the chart with
    tickers: list[str] = field(default_factory=lambda: ["AAPL", "TSLA", "MSFT", "NVDA", "AMZN", "GOOGL"])
    cors_origins: list[str] = field(default_factory=lambda: ["*"])


def load_dotenv() -> None:
    """Read KEY=VALUE lines from a .env file (backend/ or project root) without overriding real env vars."""
    here = Path(__file__).resolve().parent.parent          # backend/
    for path in (Path.cwd() / ".env", here / ".env", here.parent / ".env"):
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break


def load_settings() -> Settings:
    load_dotenv()
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    mode = os.getenv("DATA_MODE", "auto").strip().lower()
    if mode not in ("auto", "demo", "finnhub"):
        mode = "auto"
    if mode == "auto":
        mode = "finnhub" if key else "demo"
    if mode == "finnhub" and not key:
        mode = "demo"  # cannot stream live data without a key
    return Settings(
        finnhub_api_key=key,
        data_mode=mode,
        db_path=os.getenv("DB_PATH", "stockpulse.db"),
        starting_cash=_float("STARTING_CASH", 100_000.0),
        cooldown_seconds=_int("COOLDOWN_SECONDS", 300),
        daily_trade_limit=_int("DAILY_TRADE_LIMIT", 10),
        max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 3),
        history_minutes=_int("HISTORY_MINUTES", 240),
        tickers=_list("TICKERS", ["AAPL", "TSLA", "MSFT", "NVDA", "AMZN", "GOOGL"]),
        cors_origins=_list("CORS_ORIGINS", ["*"]) if os.getenv("CORS_ORIGINS") else ["*"],
    )
