"""CandleAggregator: turns a stream of ticks into 1-minute OHLC candles."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Candle:
    ticker: str
    start: int          # bucket start, Unix seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def update(self, price: float, volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("open", "high", "low", "close"):
            d[k] = round(d[k], 4)
        d["volume"] = round(d["volume"], 4)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Candle":
        return cls(d["ticker"], int(d["start"]), float(d["open"]), float(d["high"]),
                   float(d["low"]), float(d["close"]), float(d.get("volume", 0)))


@dataclass
class AggregationResult:
    accepted: bool                    # False for a late tick that belongs to a closed minute
    current: Optional[Candle] = None  # the open (in-progress) candle after this tick
    closed: Optional[Candle] = None   # the candle that this tick closed, if any


class CandleAggregator:
    """Keeps one open candle per ticker.

    * Same minute as the open candle  -> update high / low / close / volume.
    * A later minute                  -> close the open candle, start a new one.
    * An earlier (already closed) minute -> ignore, so finished candles never change.
    """

    def __init__(self, interval: int = 60):
        self.interval = interval
        self._open: dict[str, Candle] = {}

    def bucket(self, ts: float) -> int:
        return int(ts // self.interval) * self.interval

    def current(self, ticker: str) -> Optional[Candle]:
        return self._open.get(ticker)

    def seed(self, candle: Candle) -> None:
        """Continue from the last historical candle if nothing is open yet."""
        if candle.ticker not in self._open:
            self._open[candle.ticker] = Candle(**vars(candle))

    def on_tick(self, ticker: str, price: float, volume: float, ts: float) -> AggregationResult:
        b = self.bucket(ts)
        cur = self._open.get(ticker)

        if cur is not None and b < cur.start:
            return AggregationResult(accepted=False, current=cur)

        closed = None
        if cur is None or b > cur.start:
            closed = cur
            cur = Candle(ticker, b, price, price, price, price, 0.0)
            self._open[ticker] = cur

        cur.update(price, volume)
        return AggregationResult(accepted=True, current=cur, closed=closed)
