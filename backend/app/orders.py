"""Pending limit / stop orders: kept in memory per ticker, checked on every tick (like alerts).

    limit buy   fills when price <= limit   (buy the dip)
    limit sell  fills when price >= limit   (sell into strength)
    stop buy    fills when price >= stop    (buy the breakout)
    stop sell   fills when price <= stop    (sell the breakdown)
"""
from __future__ import annotations

from typing import Optional

from .trading import OrderRejected, validate_exits


def triggers(order: dict, price: float) -> bool:
    level, buy = order["price"], order["side"] == "buy"
    if order["type"] == "limit":
        return price <= level if buy else price >= level
    return price >= level if buy else price <= level


class OrderBook:
    def __init__(self, db):
        self.db = db
        self._open: dict[str, dict[int, dict]] = {}

    async def load(self) -> None:
        self._open.clear()
        for o in await self.db.list_orders(status="open", limit=10_000):
            self._open.setdefault(o["ticker"], {})[o["id"]] = o

    def tickers(self) -> set[str]:
        return {t for t, orders in self._open.items() if orders}

    def has_orders(self, ticker: str) -> bool:
        return bool(self._open.get(ticker))

    async def place(self, order: dict, current_price: Optional[float]) -> dict:
        if current_price is None:
            raise OrderRejected("no_price", f"No price available for {order['ticker']} yet. Open its chart first.",
                                status=400)
        if triggers(order, current_price):
            raise OrderRejected("invalid_price",
                                f"A {order['type']} {order['side']} at {order['price']:.2f} would fill immediately "
                                f"(price is {current_price:.2f}). Use a market order instead.", status=400)
        if order["side"] == "buy":
            validate_exits(order["price"], order.get("stop_loss"), order.get("take_profit"), at="order price")
        saved = await self.db.add_order(order)
        self._open.setdefault(saved["ticker"], {})[saved["id"]] = saved
        return saved

    async def close(self, order_id: int, status: str, note: Optional[str] = None,
                    fill_price: Optional[float] = None) -> Optional[dict]:
        for orders in self._open.values():
            orders.pop(order_id, None)
        return await self.db.close_order(order_id, status, note, fill_price)

    async def cancel_side(self, ticker: str, side: str, note: str) -> list[dict]:
        """Cancel every open order on one side of a ticker (e.g. sells after the position is exited)."""
        ids = [i for i, o in self._open.get(ticker, {}).items() if o["side"] == side]
        return [c for c in [await self.close(i, "cancelled", note) for i in ids] if c]

    def check(self, ticker: str, price: float) -> list[dict]:
        """Return (and remove) every open order on this ticker that the price has triggered."""
        orders = self._open.get(ticker)
        if not orders:
            return []
        hit = [o for o in orders.values() if triggers(o, price)]
        for o in hit:
            del orders[o["id"]]
        return hit

    def clear(self) -> None:
        self._open.clear()
