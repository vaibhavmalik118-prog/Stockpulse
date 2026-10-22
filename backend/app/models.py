"""Pydantic schemas. Everything entering the system is validated against these."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

TICKER_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,9}$"


class _TickerModel(BaseModel):
    ticker: str = Field(pattern=TICKER_PATTERN)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalise(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class Tick(_TickerModel):
    """One price update from a market data feed."""
    price: float = Field(gt=0)
    volume: float = Field(ge=0, default=0)
    ts: float = Field(gt=0, description="Unix time in seconds")


class AlertCreate(_TickerModel):
    target: float = Field(gt=0)
    direction: Literal["above", "below"]


TrailPct = Optional[float]


class OrderCreate(_TickerModel):
    """market = fill now at the live price; limit / stop = wait on the server until `price` is reached."""
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, le=100_000)
    type: Literal["market", "limit", "stop"] = "market"
    price: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    trail_pct: TrailPct = Field(default=None, ge=0.1, le=50)

    @model_validator(mode="after")
    def _check(self):
        if self.side == "sell" and (self.stop_loss is not None or self.take_profit is not None or self.trail_pct):
            raise ValueError("stop_loss / take_profit / trail_pct can only be set on a buy order")
        if self.type != "market" and self.price is None:
            raise ValueError(f"a {self.type} order needs a price")
        return self


class ExitsUpdate(BaseModel):
    """Stop-loss / target-profit / trailing-stop % for an open position. null clears a level."""
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    trail_pct: TrailPct = Field(default=None, ge=0.1, le=50)


class ClientMessage(BaseModel):
    """Messages a browser may send over the WebSocket."""
    type: Literal["subscribe", "unsubscribe", "ping"]
    ticker: Optional[str] = Field(default=None, pattern=TICKER_PATTERN)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalise(cls, v):
        return v.strip().upper() if isinstance(v, str) else v
