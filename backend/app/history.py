"""History seeding with a multi-tier fallback.

When a chart opens it should never be empty. Sources are tried in order and the
first one that returns usable data wins:

  1. Candles already stored in SQLite (recent enough)
  2. Finnhub REST candles      (only if an API key is configured)
  3. yfinance                  (last trading session; works when the market is closed)
  4. Simulated history         (demo mode only)

Every tier returns the same candle dicts, so the frontend never needs to know
where the data came from. If every tier fails, an empty list and a message are
returned instead of an error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .feeds.demo import DEFAULT_PRICES

log = logging.getLogger("stockpulse.history")

MIN_STORED = 30          # tier 1 needs at least this many recent candles
CACHE_SECONDS = 60


@dataclass
class HistoryResult:
    ticker: str
    candles: list[dict] = field(default_factory=list)
    source: str = "none"
    message: Optional[str] = None

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "candles": self.candles, "source": self.source, "message": self.message}


class HistoryService:
    def __init__(self, db, finnhub_key: str = "", demo: bool = False, minutes: int = 240):
        self.db = db
        self.finnhub_key = finnhub_key
        self.demo = demo
        self.minutes = minutes
        self._cache: dict[str, tuple[float, HistoryResult]] = {}

    async def load(self, ticker: str) -> HistoryResult:
        cached = self._cache.get(ticker)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]

        tiers = [("stored", self._from_db)]
        if self.finnhub_key:
            tiers.append(("finnhub", self._from_finnhub))
        tiers.append(("yfinance", self._from_yfinance))
        if self.demo:
            tiers.append(("demo", self._simulated))

        for name, fn in tiers:
            try:
                candles = await fn(ticker)
            except Exception as exc:
                log.info("history tier %s failed for %s: %s", name, ticker, exc)
                continue
            if candles:
                result = HistoryResult(ticker, candles, name)
                if name in ("finnhub", "yfinance"):
                    asyncio.create_task(self._store(candles))
                self._cache[ticker] = (time.time(), result)
                return result

        return HistoryResult(ticker, [], "none",
                             f"No price history available for {ticker} right now. "
                             "The chart will fill in as live prices arrive.")

    async def _store(self, candles: list[dict]) -> None:
        try:
            await self.db.save_candles(candles)
        except Exception as exc:
            log.warning("could not store history: %s", exc)

    # ---- tier 1 -----------------------------------------------------------
    async def _from_db(self, ticker: str) -> list[dict]:
        since = int(time.time()) - self.minutes * 60
        rows = await self.db.recent_candles(ticker, since, self.minutes)
        return rows if len(rows) >= MIN_STORED else []

    # ---- tier 2 -----------------------------------------------------------
    async def _from_finnhub(self, ticker: str) -> list[dict]:
        now = int(time.time())
        q = urllib.parse.urlencode({"symbol": ticker, "resolution": "1", "from": now - 3 * 86400,
                                    "to": now, "token": self.finnhub_key})
        url = f"https://finnhub.io/api/v1/stock/candle?{q}"

        def fetch():
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read().decode())

        data = await asyncio.to_thread(fetch)
        if data.get("s") != "ok":
            return []
        rows = list(zip(data["t"], data["o"], data["h"], data["l"], data["c"], data.get("v", [0] * len(data["t"]))))
        rows = rows[-self.minutes:]
        return [{"ticker": ticker, "start": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}
                for t, o, h, l, c, v in rows]

    # ---- tier 3 -----------------------------------------------------------
    async def _from_yfinance(self, ticker: str) -> list[dict]:
        def fetch():
            import yfinance as yf  # imported lazily; optional dependency
            df = yf.Ticker(ticker).history(period="5d", interval="1m", auto_adjust=False)
            if df is None or df.empty:
                return []
            df = df.tail(self.minutes)
            out = []
            for idx, row in df.iterrows():
                out.append({"ticker": ticker, "start": int(idx.timestamp()) // 60 * 60,
                            "open": round(float(row["Open"]), 4), "high": round(float(row["High"]), 4),
                            "low": round(float(row["Low"]), 4), "close": round(float(row["Close"]), 4),
                            "volume": float(row.get("Volume", 0) or 0)})
            return out

        return await asyncio.to_thread(fetch)

    # ---- tier 4 (demo only) ----------------------------------------------
    async def _simulated(self, ticker: str) -> list[dict]:
        rng = random.Random(ticker)
        price = DEFAULT_PRICES.get(ticker, rng.uniform(50, 300))
        end = int(time.time()) // 60 * 60
        candles = []
        for i in range(self.minutes, 0, -1):
            o = price
            ticks = [o * (1 + rng.gauss(0, 0.0007)) for _ in range(6)]
            c = ticks[-1]
            candles.append({"ticker": ticker, "start": end - i * 60, "open": round(o, 2),
                            "high": round(max([o] + ticks), 2), "low": round(min([o] + ticks), 2),
                            "close": round(c, 2), "volume": float(rng.randint(500, 5000))})
            price = c
        return candles
