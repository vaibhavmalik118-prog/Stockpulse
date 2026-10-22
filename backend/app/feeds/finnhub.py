"""Live trades from Finnhub's WebSocket API (needs a free API key from finnhub.io).

One upstream connection is shared by every user; each ticker is subscribed upstream
once, no matter how many browsers are watching it. The connection is re-opened with
exponential backoff if it drops.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .base import MarketFeed, TickHandler

log = logging.getLogger("stockpulse.feed.finnhub")


class FinnhubFeed(MarketFeed):
    name = "finnhub"
    live = True
    URL = "wss://ws.finnhub.io?token={key}"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._tickers: set[str] = set()
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._on_tick: Optional[TickHandler] = None

    async def start(self, on_tick: TickHandler) -> None:
        self._on_tick = on_tick
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def subscribe(self, ticker: str) -> None:
        self._tickers.add(ticker)
        await self._send({"type": "subscribe", "symbol": ticker})

    async def unsubscribe(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        await self._send({"type": "unsubscribe", "symbol": ticker})

    async def _send(self, msg: dict) -> None:
        if self._ws is None:
            return  # will be (re)sent when the connection opens
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            log.warning("finnhub send failed: %s", exc)

    async def _run(self) -> None:
        import websockets  # imported lazily so demo mode doesn't need it

        backoff = 1.0
        url = self.URL.format(key=self.api_key)
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("connected to Finnhub")
                    for t in list(self._tickers):
                        await ws.send(json.dumps({"type": "subscribe", "symbol": t}))
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Finnhub connection lost (%s); retrying in %.0fs", exc, backoff)
            finally:
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if data.get("type") != "trade" or not self._on_tick:
            return
        for t in data.get("data", []):
            try:
                await self._on_tick(t["s"], float(t["p"]), float(t.get("v", 0)), float(t["t"]) / 1000.0)
            except (KeyError, TypeError, ValueError):
                continue  # malformed entry: skip it
