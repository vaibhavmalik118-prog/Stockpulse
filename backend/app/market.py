"""US market-hours helpers. Regular session: Mon-Fri, 9:30-16:00 New York time
(exchange holidays are not handled)."""
from __future__ import annotations

from datetime import datetime, time as dtime, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # tzdata missing: fall back to UTC so the app still runs
    NY = timezone.utc

OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)


def ny_now(ts: float | None = None) -> datetime:
    if ts is None:
        return datetime.now(NY)
    return datetime.fromtimestamp(ts, NY)


def is_market_open(ts: float | None = None) -> bool:
    now = ny_now(ts)
    if now.weekday() >= 5:
        return False
    return OPEN <= now.time() < CLOSE


def trading_day(ts: float) -> str:
    """The calendar day (New York time) used for daily trade limits and halts."""
    return ny_now(ts).strftime("%Y-%m-%d")
