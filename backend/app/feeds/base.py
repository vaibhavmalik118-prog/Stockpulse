"""Common interface for live market data feeds."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

# on_tick(ticker, price, volume, ts_seconds)
TickHandler = Callable[[str, float, float, float], Awaitable[None]]


class MarketFeed(ABC):
    name = "base"
    live = False  # True when prices come from a real exchange feed

    @abstractmethod
    async def start(self, on_tick: TickHandler) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def subscribe(self, ticker: str) -> None: ...

    @abstractmethod
    async def unsubscribe(self, ticker: str) -> None: ...

    def set_reference_price(self, ticker: str, price: float) -> None:
        """Optional hint (used by the demo feed) of the latest known price."""
