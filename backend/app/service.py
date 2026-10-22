"""MarketHub wires everything together.

    feed -> validation -> CandleAggregator -> alerts / latest price -> pub-sub rooms
                                         \\-> SQLite (in the background)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from pydantic import ValidationError

from .alerts import AlertEngine
from .candles import Candle, CandleAggregator
from .config import Settings
from .db import Database
from .feeds import DemoFeed, FinnhubFeed, MarketFeed
from .history import HistoryService
from .market import is_market_open
from .models import Tick
from .orders import OrderBook
from .pubsub import Client, PubSubManager
from .trading import OrderRejected, PaperTradingEngine, RiskConfig, RiskEngine

log = logging.getLogger("stockpulse.hub")


class MarketHub:
    def __init__(self, settings: Settings, db: Optional[Database] = None, feed: Optional[MarketFeed] = None,
                 history: Optional[HistoryService] = None):
        self.settings = settings
        self.db = db or Database(settings.db_path, settings.starting_cash)
        if feed is None:
            feed = FinnhubFeed(settings.finnhub_api_key) if settings.data_mode == "finnhub" else DemoFeed()
        self.feed = feed
        self.history = history or HistoryService(self.db, settings.finnhub_api_key,
                                                 demo=settings.data_mode == "demo",
                                                 minutes=settings.history_minutes)
        self.aggregator = CandleAggregator(60)
        self.alerts = AlertEngine(self.db)
        self.pubsub = PubSubManager(on_first_subscriber=self._first_subscriber,
                                    on_last_unsubscribe=self._last_unsubscribe)
        self.risk = RiskEngine(RiskConfig(settings.cooldown_seconds, settings.daily_trade_limit,
                                          settings.max_consecutive_losses))
        self.trading = PaperTradingEngine(self.db, self.risk, self.price_of)
        self.orders = OrderBook(self.db)
        self.latest: dict[str, dict] = {}
        self.dropped_ticks = 0
        self._bg: set[asyncio.Task] = set()

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        await self.db.init()
        await self.alerts.load()
        await self.trading.load_exits()
        await self.orders.load()
        await self.feed.start(self.on_tick)
        # positions with a stop-loss / target and pending orders are watched even when no browser has them open
        for ticker in sorted(set(self.trading.exits) | self.orders.tickers()):
            await self.load_history(ticker)
            await self.feed.subscribe(ticker)
        log.info("StockPulse started in %s mode", self.settings.data_mode)

    async def stop(self) -> None:
        await self.feed.stop()
        for client in self.pubsub.clients:
            client.close()
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        await self.db.close()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ---- status ---------------------------------------------------------
    def status(self) -> dict:
        market_open = is_market_open()
        if self.settings.data_mode == "demo":
            msg = "Demo mode: live prices are simulated. Add a Finnhub API key for real-time data."
        elif market_open:
            msg = "Live: streaming real trades from Finnhub."
        else:
            msg = "US market is closed. Showing the last trading session; live candles resume at the open."
        return {"type": "status", "mode": self.settings.data_mode, "market_open": market_open, "message": msg}

    def price_of(self, ticker: str) -> Optional[float]:
        p = self.latest.get(ticker)
        return p["price"] if p else None

    # ---- data path ------------------------------------------------------
    async def on_tick(self, ticker: str, price: float, volume: float, ts: float) -> None:
        try:
            tick = Tick(ticker=ticker, price=price, volume=volume, ts=ts)
        except ValidationError:
            self.dropped_ticks += 1
            return

        result = self.aggregator.on_tick(tick.ticker, tick.price, tick.volume, tick.ts)
        if not result.accepted:
            return

        self.latest[tick.ticker] = {"price": tick.price, "ts": tick.ts}

        if result.closed is not None:
            closed = result.closed.to_dict()
            self.pubsub.publish(tick.ticker, {"type": "candle", "ticker": tick.ticker, "candle": closed, "final": True})
            self._spawn(self.db.save_candle(closed))   # publish first, persist second

        self.pubsub.publish(tick.ticker, {"type": "tick", "ticker": tick.ticker, "price": tick.price,
                                          "ts": tick.ts, "candle": result.current.to_dict()})

        for alert in self.alerts.check(tick.ticker, tick.price, tick.ts):
            self._spawn(self.db.mark_alert_triggered(alert["id"], tick.price, tick.ts))
            self.pubsub.broadcast({"type": "alert", "alert": alert})

        new_stop = self.trading.trail(tick.ticker, tick.price)
        if new_stop is not None:
            self._spawn(self.db.set_stop_loss(tick.ticker, new_stop))
            self.pubsub.publish(tick.ticker, {"type": "trail", "ticker": tick.ticker, "stop_loss": new_stop})

        reason = self.trading.check_exits(tick.ticker, tick.price)
        if reason:
            self._spawn(self._auto_exit(tick.ticker, reason))

        for order in self.orders.check(tick.ticker, tick.price):
            self._spawn(self._fill_order(order))

    async def _auto_exit(self, ticker: str, reason: str) -> None:
        try:
            result = await self.trading.close_position(ticker, reason)
        except Exception:
            log.exception("automatic %s exit for %s failed", reason, ticker)
            return
        if result is not None:
            self.pubsub.broadcast({"type": "exit", "reason": reason, **result})
            await self.orders.cancel_side(ticker, "sell", "Position closed")
        await self._sync_feed(ticker)

    async def _fill_order(self, order: dict) -> None:
        """A pending limit / stop order was triggered: execute it at the live price."""
        try:
            result = await self.trading.execute(order["ticker"], order["side"], order["quantity"],
                                                order.get("stop_loss"), order.get("take_profit"),
                                                reason=order["type"], trail_pct=order.get("trail_pct"))
        except OrderRejected as exc:
            closed = await self.orders.close(order["id"], "rejected", exc.reason)
            self.pubsub.broadcast({"type": "order", "status": "rejected", "order": closed or order})
        except Exception:
            log.exception("filling order %s failed", order["id"])
            await self.orders.close(order["id"], "rejected", "Internal error")
        else:
            closed = await self.orders.close(order["id"], "filled", fill_price=result["trade"]["price"])
            self.pubsub.broadcast({"type": "order", "status": "filled", "order": closed or order, **result})
        await self._sync_feed(order["ticker"])

    # ---- trading --------------------------------------------------------
    async def place_order(self, ticker: str, side: str, quantity: int, stop_loss: Optional[float] = None,
                          take_profit: Optional[float] = None, order_type: str = "market",
                          price: Optional[float] = None, trail_pct: Optional[float] = None) -> dict:
        if order_type == "market":
            result = await self.trading.execute(ticker, side, quantity, stop_loss, take_profit, trail_pct=trail_pct)
        else:
            # placing a pending order is a trading decision, so the behavioural rules apply now
            acct = await self.db.get_account()
            decision = self.risk.check(acct, self.trading.clock())
            if not decision.allowed:
                raise OrderRejected(decision.code, decision.reason, decision.retry_after)
            order = await self.orders.place(
                {"ticker": ticker, "side": side, "quantity": quantity, "type": order_type, "price": price,
                 "stop_loss": stop_loss, "take_profit": take_profit, "trail_pct": trail_pct},
                self.price_of(ticker))
            result = {"order": order}
        await self._sync_feed(ticker)
        return result

    async def cancel_order(self, order_id: int) -> Optional[dict]:
        order = await self.orders.close(order_id, "cancelled", "Cancelled by you")
        if order:
            await self._sync_feed(order["ticker"])
        return order

    async def exit_position(self, ticker: str) -> dict:
        """The Exit button: sell the whole position now and cancel its pending sells."""
        result = await self.trading.close_position(ticker, "exit")
        if result is None:
            raise OrderRejected("no_position", f"You have no open {ticker} position.", status=404)
        result["cancelled"] = await self.orders.cancel_side(ticker, "sell", "Position closed")
        await self._sync_feed(ticker)
        return result

    async def set_exits(self, ticker: str, stop_loss: Optional[float], take_profit: Optional[float],
                        trail_pct: Optional[float] = None) -> dict:
        result = await self.trading.set_exits(ticker, stop_loss, take_profit, trail_pct)
        await self._sync_feed(ticker)
        return result

    async def reset_account(self) -> None:
        await self.db.reset_account()
        tickers = set(self.trading.exits) | self.orders.tickers()
        self.trading.exits.clear()
        self.orders.clear()
        for t in tickers:
            await self._sync_feed(t)

    # ---- subscriptions --------------------------------------------------
    def _guarded(self, ticker: str) -> bool:
        """A stop-loss / target or a pending order needs this ticker's prices."""
        return ticker in self.trading.exits or self.orders.has_orders(ticker)

    async def _sync_feed(self, ticker: str) -> None:
        """Keep the upstream feed subscribed while anyone watches the ticker or something guards it."""
        if self.pubsub.room_size(ticker) > 0 or self._guarded(ticker):
            await self.feed.subscribe(ticker)
        else:
            await self.feed.unsubscribe(ticker)

    async def _first_subscriber(self, ticker: str) -> None:
        await self.feed.subscribe(ticker)

    async def _last_unsubscribe(self, ticker: str) -> None:
        if not self._guarded(ticker):
            await self.feed.unsubscribe(ticker)

    async def load_history(self, ticker: str) -> dict:
        result = await self.history.load(ticker)
        if result.candles:
            last = result.candles[-1]
            if ticker not in self.latest:
                self.latest[ticker] = {"price": float(last["close"]), "ts": float(last["start"])}
            self.feed.set_reference_price(ticker, float(self.latest[ticker]["price"]))
            # if the newest stored candle is the current minute, keep building on it
            if int(last["start"]) == self.aggregator.bucket(time.time()):
                self.aggregator.seed(Candle.from_dict(last))
        data = result.to_dict()
        current = self.aggregator.current(ticker)
        if current is not None and data["candles"] and current.start >= int(data["candles"][-1]["start"]):
            data["candles"] = [c for c in data["candles"] if int(c["start"]) < current.start] + [current.to_dict()]
        return data

    async def subscribe(self, client: Client, ticker: str) -> None:
        history = await self.load_history(ticker)
        client.send({"type": "history", **history, "market_open": is_market_open()})
        await self.pubsub.subscribe(client, ticker)

    async def unsubscribe(self, client: Client, ticker: str) -> None:
        await self.pubsub.unsubscribe(client, ticker)

    async def disconnect(self, client: Client) -> None:
        await self.pubsub.disconnect(client)
