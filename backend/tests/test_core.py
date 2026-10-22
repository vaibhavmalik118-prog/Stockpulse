"""Unit tests for StockPulse's core logic.

Run from the backend folder with either:
    python -m pytest            (if pytest is installed)
    python -m unittest -v
"""
import asyncio
import os
import tempfile
import time
import unittest

from app.alerts import AlertEngine, AlertError
from app.candles import CandleAggregator
from app.config import Settings
from app.db import Database
from app.feeds.base import MarketFeed
from app.history import HistoryResult
from app.pubsub import Client, PubSubManager
from app.service import MarketHub
from app.trading import OrderRejected, PaperTradingEngine, RiskConfig, RiskEngine


# ---------------------------------------------------------------- helpers
class FakeWS:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_json(self, msg):
        if self.fail:
            raise ConnectionError("browser tab closed")
        self.sent.append(msg)


class FakeFeed(MarketFeed):
    name = "fake"

    def __init__(self):
        self.subscribed, self.on_tick = set(), None

    async def start(self, on_tick): self.on_tick = on_tick
    async def stop(self): pass
    async def subscribe(self, t): self.subscribed.add(t)
    async def unsubscribe(self, t): self.subscribed.discard(t)


class FakeHistory:
    async def load(self, ticker):
        return HistoryResult(ticker, [{"ticker": ticker, "start": 60, "open": 100.0, "high": 101.0,
                                       "low": 99.0, "close": 100.5, "volume": 10}], "stored")


def day_fn(ts):  # one "day" = 1000 seconds, to make day roll-over easy to test
    return str(int(ts // 1000))


async def drain(client):
    await asyncio.sleep(0)
    msgs = []
    while not client.queue.empty():
        m = client.queue.get_nowait()
        if m is not None:
            msgs.append(m)
    return msgs


class TempDB:
    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path, starting_cash=10_000)

    async def __aenter__(self):
        await self.db.init()
        return self.db

    async def __aexit__(self, *a):
        await self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- candles
class CandleAggregatorTests(unittest.TestCase):
    def test_report_example(self):
        """Annexure A.2: five ticks in one minute -> O 182.10 H 182.35 L 181.95 C 182.15."""
        agg = CandleAggregator()
        base = 1_700_000_100  # start of a minute
        base -= base % 60
        for sec, price in [(2, 182.10), (14, 182.35), (31, 181.95), (48, 182.20), (59, 182.15)]:
            r = agg.on_tick("AAPL", price, 10, base + sec)
            self.assertTrue(r.accepted)
            self.assertIsNone(r.closed)
        c = agg.current("AAPL")
        self.assertEqual((c.open, c.high, c.low, c.close), (182.10, 182.35, 181.95, 182.15))
        self.assertEqual(c.start, base)
        self.assertEqual(c.volume, 50)

        # next minute closes the candle and starts a new one
        r = agg.on_tick("AAPL", 182.30, 5, base + 61)
        self.assertEqual(r.closed.close, 182.15)
        self.assertEqual(r.current.start, base + 60)
        self.assertEqual(r.current.open, 182.30)

        # a late tick for the closed minute is ignored
        r = agg.on_tick("AAPL", 999.0, 1, base + 58)
        self.assertFalse(r.accepted)
        self.assertEqual(agg.current("AAPL").high, 182.30)

    def test_tickers_are_independent(self):
        agg = CandleAggregator()
        agg.on_tick("AAPL", 100, 1, 600)
        agg.on_tick("TSLA", 200, 1, 600)
        self.assertEqual(agg.current("AAPL").close, 100)
        self.assertEqual(agg.current("TSLA").close, 200)


# ---------------------------------------------------------------- risk engine
class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskEngine(RiskConfig(cooldown_seconds=300, daily_trade_limit=3, max_consecutive_losses=2),
                               day_fn=day_fn)
        self.acct = {"cash": 1000, "trades_today": 0, "trade_day": None, "loss_streak": 0,
                     "last_trade_at": None, "halted_day": None}

    def test_cooldown(self):
        self.risk.record_trade(self.acct, 100, None)
        d = self.risk.check(self.acct, 220)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "cooldown")
        self.assertAlmostEqual(d.retry_after, 180)
        self.assertTrue(self.risk.check(self.acct, 400).allowed)

    def test_daily_limit_and_reset(self):
        for t in (0, 301, 602):
            self.assertTrue(self.risk.check(self.acct, t).allowed)
            self.risk.record_trade(self.acct, t, None)
        d = self.risk.check(self.acct, 950)
        self.assertEqual(d.code, "daily_limit")
        self.assertTrue(self.risk.check(self.acct, 1500).allowed)  # next "day"

    def test_auto_halt_after_consecutive_losses(self):
        self.risk = RiskEngine(RiskConfig(cooldown_seconds=300, daily_trade_limit=10, max_consecutive_losses=2),
                               day_fn=day_fn)
        self.risk.record_trade(self.acct, 0, -10)
        self.risk.record_trade(self.acct, 301, +5)      # a win resets the streak
        self.assertEqual(self.acct["loss_streak"], 0)
        self.risk.record_trade(self.acct, 602, -1)
        self.assertTrue(self.risk.check(self.acct, 903).allowed)
        self.risk.record_trade(self.acct, 903, -2)
        d = self.risk.check(self.acct, 999)
        self.assertEqual(d.code, "halted")
        self.assertTrue(self.risk.check(self.acct, 2000).allowed)  # halt lifts next day
        self.assertEqual(self.acct["loss_streak"], 0)


# ---------------------------------------------------------------- trading
class TradingTests(unittest.IsolatedAsyncioTestCase):
    async def test_buy_sell_and_rules(self):
        async with TempDB() as db:
            now = [10_000.0]
            prices = {"AAPL": 100.0}
            risk = RiskEngine(RiskConfig(300, 10, 3), day_fn=day_fn)
            eng = PaperTradingEngine(db, risk, prices.get, clock=lambda: now[0])

            r = await eng.execute("AAPL", "buy", 10)
            self.assertEqual(r["trade"]["price"], 100.0)
            pf = await eng.portfolio()
            self.assertEqual(pf["cash"], 9_000)
            self.assertEqual(pf["positions"][0]["quantity"], 10)

            with self.assertRaises(OrderRejected) as cm:      # cooldown
                await eng.execute("AAPL", "sell", 5)
            self.assertEqual(cm.exception.code, "cooldown")

            now[0] += 301
            prices["AAPL"] = 90.0
            r = await eng.execute("AAPL", "sell", 4)
            self.assertEqual(r["trade"]["realized_pnl"], -40.0)
            self.assertEqual(r["risk"]["loss_streak"], 1)

            now[0] += 301
            with self.assertRaises(OrderRejected) as cm:      # can't sell more than held
                await eng.execute("AAPL", "sell", 100)
            self.assertEqual(cm.exception.code, "insufficient_shares")
            with self.assertRaises(OrderRejected) as cm:      # can't spend more than cash
                await eng.execute("AAPL", "buy", 1000)
            self.assertEqual(cm.exception.code, "insufficient_cash")
            with self.assertRaises(OrderRejected) as cm:      # no price yet
                await eng.execute("ZZZ", "buy", 1)
            self.assertEqual(cm.exception.code, "no_price")

            pf = await eng.portfolio()
            self.assertEqual(pf["positions"][0]["quantity"], 6)
            self.assertEqual(pf["realized_pnl"], -40.0)
            self.assertEqual(pf["cash"], 9_000 + 360)


# ---------------------------------------------------------------- pub-sub
class PubSubTests(unittest.IsolatedAsyncioTestCase):
    async def test_rooms_isolation_and_fault_tolerance(self):
        first, last = [], []

        async def on_first(t): first.append(t)
        async def on_last(t): last.append(t)

        ps = PubSubManager(on_first, on_last)
        a, b, c = Client(FakeWS()), Client(FakeWS()), Client(FakeWS())
        await ps.subscribe(a, "AAPL")
        await ps.subscribe(b, "AAPL")
        await ps.subscribe(b, "TSLA")
        await ps.subscribe(c, "TSLA")
        self.assertEqual(first, ["AAPL", "TSLA"])     # upstream subscribed once per ticker

        self.assertEqual(ps.publish("AAPL", {"p": 1}), 2)
        self.assertEqual([m["p"] for m in await drain(a)], [1])
        self.assertEqual(await drain(c), [])          # TSLA watcher never sees AAPL

        # a client whose queue overflows (stuck browser) is dropped; others keep receiving
        c.queue = asyncio.Queue(1)
        c.queue.put_nowait({"stuck": True})
        self.assertEqual(ps.publish("TSLA", {"p": 2}), 1)
        self.assertNotIn(c, ps.memberships)
        self.assertEqual(ps.room_size("TSLA"), 1)

        await ps.disconnect(a)
        await ps.disconnect(b)
        await asyncio.sleep(0)
        self.assertEqual(sorted(last), ["AAPL", "TSLA"])
        self.assertEqual(ps.rooms, {})

    async def test_writer_marks_broken_client_closed(self):
        client = Client(FakeWS(fail=True))
        client.send({"x": 1})
        await client.writer()
        self.assertTrue(client.closed)
        self.assertFalse(client.send({"x": 2}))


# ---------------------------------------------------------------- alerts
class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_alert_fires_once(self):
        async with TempDB() as db:
            eng = AlertEngine(db)
            a = await eng.create("TSLA", 250, "above", current_price=240)
            self.assertEqual(eng.check("TSLA", 249.9, 1), [])
            self.assertEqual(eng.check("AAPL", 999, 1), [])
            fired = eng.check("TSLA", 250.5, 2)
            self.assertEqual([f["id"] for f in fired], [a["id"]])
            self.assertEqual(eng.check("TSLA", 251, 3), [])        # only once
            with self.assertRaises(AlertError):
                await eng.create("TSLA", 200, "above", current_price=240)


# ---------------------------------------------------------------- whole data path
class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_receive_and_alert(self):
        tmp = TempDB()
        feed = FakeFeed()
        hub = MarketHub(Settings(db_path=tmp.path), db=tmp.db, feed=feed, history=FakeHistory())
        await hub.start()
        try:
            client = Client(FakeWS())
            hub.pubsub.register(client)
            await hub.subscribe(client, "AAPL")
            self.assertIn("AAPL", feed.subscribed)
            msgs = await drain(client)
            self.assertEqual(msgs[0]["type"], "history")
            self.assertEqual(hub.price_of("AAPL"), 100.5)       # seeded from history

            await hub.alerts.create("AAPL", 101, "above", hub.price_of("AAPL"))
            await feed.on_tick("AAPL", 101.2, 5, 1_700_000_000)
            await feed.on_tick("AAPL", -5, 5, 1_700_000_001)     # invalid -> dropped
            await feed.on_tick("AAPL", 101.4, 5, 1_700_000_070)  # new minute -> closes candle
            msgs = await drain(client)
            types = [m["type"] for m in msgs]
            self.assertEqual(types.count("tick"), 2)
            self.assertIn("alert", types)
            self.assertIn("candle", types)
            self.assertEqual(hub.dropped_ticks, 1)
            self.assertEqual(hub.price_of("AAPL"), 101.4)

            await hub.disconnect(client)
            self.assertNotIn("AAPL", feed.subscribed)
        finally:
            await hub.stop()
            await tmp.__aexit__()

    async def test_stop_loss_sells_even_when_nobody_watches(self):
        tmp = TempDB()
        feed = FakeFeed()
        hub = MarketHub(Settings(db_path=tmp.path), db=tmp.db, feed=feed, history=FakeHistory())
        await hub.start()
        try:
            client = Client(FakeWS())
            hub.pubsub.register(client)
            await hub.subscribe(client, "AAPL")
            await hub.place_order("AAPL", "buy", 10, stop_loss=99.0)
            await hub.unsubscribe(client, "AAPL")
            self.assertIn("AAPL", feed.subscribed)                 # still guarded by the stop-loss
            await drain(client)

            await feed.on_tick("AAPL", 98.5, 5, 1_700_000_000)
            await asyncio.gather(*hub._bg)
            exits = [m for m in await drain(client) if m["type"] == "exit"]
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["reason"], "stop_loss")
            self.assertEqual(exits[0]["trade"]["realized_pnl"], -20.0)
            self.assertNotIn("AAPL", feed.subscribed)              # nothing left to guard
            self.assertEqual((await hub.trading.portfolio())["positions"], [])
        finally:
            await hub.stop()
            await tmp.__aexit__()


class OrderTypeTests(unittest.IsolatedAsyncioTestCase):
    async def _hub(self):
        self.tmp = TempDB()
        self.feed = FakeFeed()
        hub = MarketHub(Settings(db_path=self.tmp.path, cooldown_seconds=300), db=self.tmp.db, feed=self.feed,
                        history=FakeHistory())
        await hub.start()
        self.client = Client(FakeWS())
        hub.pubsub.register(self.client)
        await hub.subscribe(self.client, "AAPL")          # price 100.5 from history
        return hub

    async def _tick(self, hub, price, ts=1_700_000_000):
        await self.feed.on_tick("AAPL", price, 1, ts)
        await asyncio.gather(*hub._bg)
        return [m for m in await drain(self.client) if m["type"] in ("order", "exit", "trail")]

    async def test_limit_and_stop_orders(self):
        hub = await self._hub()
        try:
            with self.assertRaises(OrderRejected) as cm:      # would fill immediately
                await hub.place_order("AAPL", "buy", 5, order_type="limit", price=101)
            self.assertEqual(cm.exception.code, "invalid_price")

            r = await hub.place_order("AAPL", "buy", 5, order_type="limit", price=99, take_profit=104)
            self.assertEqual(r["order"]["status"], "open")
            await hub.unsubscribe(self.client, "AAPL")
            self.assertIn("AAPL", self.feed.subscribed)        # pending order keeps the feed alive

            self.assertEqual(await self._tick(hub, 99.5), [])
            msgs = await self._tick(hub, 98.9)
            self.assertEqual(msgs[0]["status"], "filled")
            self.assertEqual(msgs[0]["trade"]["reason"], "limit")
            self.assertEqual(hub.trading.exits["AAPL"]["take_profit"], 104)

            # a stop sell below the market, then cancelled
            later = time.time() + 1000
            hub.trading.clock = lambda: later            # move past the cooldown
            stop = (await hub.place_order("AAPL", "sell", 5, order_type="stop", price=95))["order"]
            self.assertEqual((await hub.cancel_order(stop["id"]))["status"], "cancelled")
            self.assertIsNone(await hub.cancel_order(stop["id"]))
            self.assertEqual(await self._tick(hub, 94), [])    # cancelled -> nothing happens
        finally:
            await hub.stop()
            await self.tmp.__aexit__()

    async def test_trailing_stop_and_exit_button(self):
        hub = await self._hub()
        try:
            await hub.place_order("AAPL", "buy", 10, trail_pct=2)
            self.assertEqual(hub.trading.exits["AAPL"]["stop_loss"], 98.49)    # 100.5 * 0.98
            msgs = await self._tick(hub, 110)
            self.assertEqual(msgs, [{"type": "trail", "ticker": "AAPL", "stop_loss": 107.8}])
            self.assertEqual(await self._tick(hub, 108), [])                   # stop never moves down
            pf = await hub.trading.portfolio()
            self.assertEqual(pf["positions"][0]["stop_loss"], 107.8)

            # Exit is allowed during the cooldown and cancels pending sells
            await hub.orders.place({"ticker": "AAPL", "side": "sell", "quantity": 10, "type": "limit",
                                    "price": 120}, 108)
            r = await hub.exit_position("AAPL")
            self.assertEqual(r["trade"]["reason"], "exit")
            self.assertEqual(r["trade"]["quantity"], 10)
            self.assertEqual(len(r["cancelled"]), 1)
            self.assertNotIn("AAPL", hub.trading.exits)
            with self.assertRaises(OrderRejected):
                await hub.exit_position("AAPL")
        finally:
            await hub.stop()
            await self.tmp.__aexit__()


class ExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_levels_validate_fire_once_and_bypass_cooldown(self):
        async with TempDB() as db:
            now = [10_000.0]
            prices = {"AAPL": 100.0}
            risk = RiskEngine(RiskConfig(300, 10, 3), day_fn=day_fn)
            eng = PaperTradingEngine(db, risk, prices.get, clock=lambda: now[0])

            with self.assertRaises(OrderRejected) as cm:           # stop must be below price
                await eng.execute("AAPL", "buy", 1, stop_loss=101)
            self.assertEqual(cm.exception.code, "invalid_exit")
            with self.assertRaises(OrderRejected) as cm:           # target must be above price
                await eng.execute("AAPL", "buy", 1, take_profit=99)
            self.assertEqual(cm.exception.code, "invalid_exit")

            await eng.execute("AAPL", "buy", 10, stop_loss=95, take_profit=110)
            self.assertEqual(eng.exits["AAPL"], {"stop_loss": 95, "take_profit": 110, "trail_pct": None})
            self.assertIsNone(eng.check_exits("AAPL", 100))
            self.assertEqual(eng.check_exits("AAPL", 110.5), "take_profit")
            self.assertIsNone(eng.check_exits("AAPL", 111))       # fires once

            prices["AAPL"] = 110.5                                  # still inside the cooldown
            r = await eng.close_position("AAPL", "take_profit")
            self.assertEqual(r["trade"]["reason"], "take_profit")
            self.assertEqual(r["trade"]["realized_pnl"], 105.0)
            self.assertNotIn("AAPL", eng.exits)

            now[0] += 301
            await eng.execute("AAPL", "buy", 5)
            await eng.set_exits("AAPL", 100, None)                  # levels persist ...
            await eng.load_exits()
            self.assertEqual(eng.exits["AAPL"], {"stop_loss": 100, "take_profit": None, "trail_pct": None})
            await eng.set_exits("AAPL", None, None)                 # ... and can be cleared
            self.assertNotIn("AAPL", eng.exits)
            with self.assertRaises(OrderRejected) as cm:
                await eng.set_exits("TSLA", 1, None)
            self.assertEqual(cm.exception.code, "no_position")


if __name__ == "__main__":
    unittest.main()
