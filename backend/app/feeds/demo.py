"""Demo feed: simulated ticks (a random walk) for when no Finnhub key is configured.

Each subscribed ticker starts from its latest real price (taken from history,
e.g. yfinance) when available, otherwise from a built-in reference price.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from .base import MarketFeed, TickHandler

log = logging.getLogger("stockpulse.feed.demo")

DEFAULT_PRICES = {"AAPL": 230.0, "TSLA": 250.0, "MSFT": 420.0, "NVDA": 135.0, "AMZN": 190.0, "GOOGL": 165.0}


class DemoFeed(MarketFeed):
    name = "demo"
    live = False

    def __init__(self, min_interval: float = 0.25, max_interval: float = 1.2, seed: Optional[int] = None):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._on_tick: Optional[TickHandler] = None

    def set_reference_price(self, ticker: str, price: float) -> None:
        if ticker not in self._tasks or ticker not in self._prices:
            self._prices[ticker] = price

    async def start(self, on_tick: TickHandler) -> None:
        self._on_tick = on_tick

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()

    async def subscribe(self, ticker: str) -> None:
        if ticker not in self._tasks:
            self._tasks[ticker] = asyncio.create_task(self._run(ticker))

    async def unsubscribe(self, ticker: str) -> None:
        task = self._tasks.pop(ticker, None)
        if task:
            task.cancel()

    def next_price(self, ticker: str) -> float:
        price = self._prices.get(ticker) or DEFAULT_PRICES.get(ticker) or self._rng.uniform(20, 400)
        # small random move (about 0.06% per tick)
        change = self._rng.gauss(0, 0.0006)
        price = max(0.01, price * (1 + change))
        self._prices[ticker] = price
        return round(price, 2)

    async def _run(self, ticker: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._rng.uniform(self.min_interval, self.max_interval))
                if self._on_tick:
                    await self._on_tick(ticker, self.next_price(ticker), float(self._rng.randint(1, 400)), time.time())
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("demo feed for %s stopped", ticker)
