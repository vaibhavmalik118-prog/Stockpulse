"""Paper trading engine and the behavioural risk engine."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .market import trading_day


@dataclass(frozen=True)
class RiskConfig:
    cooldown_seconds: int = 300
    daily_trade_limit: int = 10
    max_consecutive_losses: int = 3


@dataclass
class RiskDecision:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    retry_after: Optional[float] = None


class RiskEngine:
    """Checks the user's recent behaviour before every order.

    Rules, checked in this order:
      1. Auto-halt   - after N consecutive losing trades, no more trading that day.
      2. Daily limit - at most N trades per day.
      3. Cooldown    - wait N seconds after each trade.
    """

    def __init__(self, cfg: RiskConfig, day_fn: Callable[[float], str] = trading_day):
        self.cfg = cfg
        self.day_fn = day_fn

    def roll_day(self, acct: dict, now: float) -> None:
        """Reset daily counters (and lift a halt) when a new trading day starts."""
        today = self.day_fn(now)
        if acct.get("trade_day") != today:
            acct["trade_day"] = today
            acct["trades_today"] = 0
            if acct.get("halted_day") and acct["halted_day"] != today:
                acct["halted_day"] = None
                acct["loss_streak"] = 0

    def check(self, acct: dict, now: float, cooldown: bool = True) -> RiskDecision:
        self.roll_day(acct, now)
        if acct.get("halted_day"):
            return RiskDecision(False, "halted",
                                f"Trading halted for today after {self.cfg.max_consecutive_losses} "
                                "consecutive losing trades. Take a break and come back tomorrow.")
        if acct["trades_today"] >= self.cfg.daily_trade_limit:
            return RiskDecision(False, "daily_limit",
                                f"Daily limit of {self.cfg.daily_trade_limit} trades reached. Try again tomorrow.")
        last = acct.get("last_trade_at")
        if cooldown and last is not None:
            remaining = self.cfg.cooldown_seconds - (now - last)
            if remaining > 0:
                m, s = divmod(int(remaining + 0.999), 60)
                return RiskDecision(False, "cooldown",
                                    f"Cooldown active. You can trade again in {m}m {s:02d}s.",
                                    retry_after=round(remaining, 1))
        return RiskDecision(True)

    def record_trade(self, acct: dict, now: float, realized_pnl: Optional[float]) -> None:
        self.roll_day(acct, now)
        acct["trades_today"] += 1
        acct["last_trade_at"] = now
        if realized_pnl is not None:
            if realized_pnl < 0:
                acct["loss_streak"] += 1
            elif realized_pnl > 0:
                acct["loss_streak"] = 0
        if acct["loss_streak"] >= self.cfg.max_consecutive_losses:
            acct["halted_day"] = acct["trade_day"]

    def status(self, acct: dict, now: float) -> dict:
        acct = dict(acct)
        self.roll_day(acct, now)
        last = acct.get("last_trade_at")
        cooldown = max(0.0, self.cfg.cooldown_seconds - (now - last)) if last is not None else 0.0
        return {
            "cooldown_remaining": round(cooldown, 1),
            "cooldown_seconds": self.cfg.cooldown_seconds,
            "trades_today": acct["trades_today"],
            "daily_trade_limit": self.cfg.daily_trade_limit,
            "loss_streak": acct["loss_streak"],
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "halted": bool(acct.get("halted_day")),
        }


class OrderRejected(Exception):
    def __init__(self, code: str, reason: str, retry_after: Optional[float] = None, status: int = 409):
        super().__init__(reason)
        self.code, self.reason, self.retry_after, self.status = code, reason, retry_after, status

    def to_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason, "retry_after": self.retry_after}


# Which behavioural rules each kind of trade must pass (the `reason` of a trade):
#   manual                         -> every rule
#   limit / stop (a pending fill)  -> halt + daily limit; the cooldown applied when the order was placed
#   exit / stop_loss / take_profit -> none: reducing risk is never blocked
AUTO_EXITS = {"exit", "stop_loss", "take_profit"}


def validate_exits(price: float, stop_loss: Optional[float], take_profit: Optional[float],
                   at: str = "current price") -> None:
    """For a long position the stop-loss must sit below the price and the target above it."""
    if stop_loss is not None and stop_loss >= price:
        raise OrderRejected("invalid_exit", f"Stop-loss ({stop_loss:.2f}) must be below the {at} "
                                            f"({price:.2f}).", status=400)
    if take_profit is not None and take_profit <= price:
        raise OrderRejected("invalid_exit", f"Target profit ({take_profit:.2f}) must be above the {at} "
                                            f"({price:.2f}).", status=400)


def trailed_stop(price: float, trail_pct: float) -> float:
    return round(price * (1 - trail_pct / 100), 2)


class PaperTradingEngine:
    """Executes simulated market orders at the latest live price.

    A position may carry a stop-loss, a target-profit level and a trailing-stop %. They are
    kept in memory per ticker (like alerts) so every tick is checked cheaply; when a level is
    crossed the whole position is sold automatically.
    """

    def __init__(self, db, risk: RiskEngine, price_of: Callable[[str], Optional[float]],
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.risk = risk
        self.price_of = price_of
        self.clock = clock
        self.exits: dict[str, dict] = {}   # ticker -> {"stop_loss": .., "take_profit": .., "trail_pct": ..}
        self._lock = asyncio.Lock()

    async def load_exits(self) -> None:
        self.exits.clear()
        for t, p in (await self.db.get_positions()).items():
            self._cache_exits(t, p)

    def _cache_exits(self, ticker: str, pos: Optional[dict]) -> None:
        if pos and pos["quantity"] > 0 and (pos.get("stop_loss") is not None or pos.get("take_profit") is not None):
            self.exits[ticker] = {"stop_loss": pos.get("stop_loss"), "take_profit": pos.get("take_profit"),
                                  "trail_pct": pos.get("trail_pct")}
        else:
            self.exits.pop(ticker, None)

    def trail(self, ticker: str, price: float) -> Optional[float]:
        """Ratchet a trailing stop up behind a rising price. Returns the new stop if it moved."""
        levels = self.exits.get(ticker)
        if not levels or not levels.get("trail_pct"):
            return None
        new_stop = trailed_stop(price, levels["trail_pct"])
        if levels["stop_loss"] is None or new_stop > levels["stop_loss"]:
            levels["stop_loss"] = new_stop
            return new_stop
        return None

    def check_exits(self, ticker: str, price: float) -> Optional[str]:
        """Return "stop_loss" / "take_profit" if this price crosses a level (and disarm it), else None."""
        levels = self.exits.get(ticker)
        if not levels:
            return None
        if levels["stop_loss"] is not None and price <= levels["stop_loss"]:
            reason = "stop_loss"
        elif levels["take_profit"] is not None and price >= levels["take_profit"]:
            reason = "take_profit"
        else:
            return None
        del self.exits[ticker]   # fire once; the sell that follows closes the position
        return reason

    async def close_position(self, ticker: str, reason: str) -> Optional[dict]:
        """Sell the whole position: the user pressed Exit, or a stop-loss / target was hit."""
        pos = (await self.db.get_positions()).get(ticker)
        if not pos or pos["quantity"] <= 0:
            return None
        return await self.execute(ticker, "sell", pos["quantity"], reason=reason)

    async def set_exits(self, ticker: str, stop_loss: Optional[float], take_profit: Optional[float],
                        trail_pct: Optional[float] = None) -> dict:
        async with self._lock:
            pos = (await self.db.get_positions()).get(ticker)
            if not pos or pos["quantity"] <= 0:
                raise OrderRejected("no_position", f"You have no open {ticker} position.", status=404)
            price = self.price_of(ticker)
            if price is not None:
                validate_exits(price, stop_loss, take_profit)
                if trail_pct:
                    stop_loss = max(stop_loss or 0, trailed_stop(price, trail_pct))
            await self.db.set_exits(ticker, stop_loss, take_profit, trail_pct)
            self._cache_exits(ticker, {**pos, "stop_loss": stop_loss, "take_profit": take_profit,
                                       "trail_pct": trail_pct})
            return {"ticker": ticker, "stop_loss": stop_loss, "take_profit": take_profit, "trail_pct": trail_pct}

    async def execute(self, ticker: str, side: str, quantity: int, stop_loss: Optional[float] = None,
                      take_profit: Optional[float] = None, reason: str = "manual",
                      trail_pct: Optional[float] = None) -> dict:
        async with self._lock:
            now = self.clock()
            acct = await self.db.get_account()
            if reason not in AUTO_EXITS:
                decision = self.risk.check(acct, now, cooldown=reason == "manual")
                if not decision.allowed:
                    await self.db.save_account(acct)   # persist any daily roll-over
                    raise OrderRejected(decision.code, decision.reason, decision.retry_after)

            price = self.price_of(ticker)
            if price is None:
                raise OrderRejected("no_price", f"No price available for {ticker} yet. Open its chart first.",
                                    status=400)

            positions = await self.db.get_positions()
            pos = positions.get(ticker)
            held = pos["quantity"] if pos else 0
            avg = pos["avg_cost"] if pos else 0.0
            sl = pos.get("stop_loss") if pos else None
            tp = pos.get("take_profit") if pos else None
            trail = pos.get("trail_pct") if pos else None
            if ticker in self.exits:                 # a trailing stop may have moved since it was saved
                sl = self.exits[ticker]["stop_loss"]
            realized = None

            if side == "buy":
                validate_exits(price, stop_loss, take_profit)
                cost = price * quantity
                if cost > acct["cash"] + 1e-9:
                    raise OrderRejected("insufficient_cash",
                                        f"Not enough virtual cash: need ${cost:,.2f}, have ${acct['cash']:,.2f}.",
                                        status=400)
                new_qty = held + quantity
                avg = (held * avg + quantity * price) / new_qty
                acct["cash"] -= cost
                # levels given with this order replace the old ones; levels left empty are kept
                new_pos = {"quantity": new_qty, "avg_cost": avg,
                           "stop_loss": stop_loss if stop_loss is not None else sl,
                           "take_profit": take_profit if take_profit is not None else tp,
                           "trail_pct": trail_pct if trail_pct is not None else trail}
                if new_pos["trail_pct"]:
                    new_pos["stop_loss"] = max(new_pos["stop_loss"] or 0, trailed_stop(price, new_pos["trail_pct"]))
            else:
                if quantity > held:
                    raise OrderRejected("insufficient_shares",
                                        f"You hold {held} {ticker} share(s); cannot sell {quantity}.", status=400)
                realized = round((price - avg) * quantity, 2)
                acct["cash"] += price * quantity
                new_pos = {"quantity": held - quantity, "avg_cost": avg, "stop_loss": sl, "take_profit": tp,
                           "trail_pct": trail}

            acct["cash"] = round(acct["cash"], 2)
            self.risk.record_trade(acct, now, realized)
            trade = {"ticker": ticker, "side": side, "quantity": quantity, "price": round(price, 4),
                     "realized_pnl": realized, "ts": now, "reason": reason}
            saved = await self.db.commit_trade(acct, new_pos, ticker, trade)
            self._cache_exits(ticker, new_pos)
            return {"trade": saved, "risk": self.risk.status(acct, now)}

    async def portfolio(self) -> dict:
        now = self.clock()
        acct = await self.db.get_account()
        positions = await self.db.get_positions()
        rows, market_value, unrealized = [], 0.0, 0.0
        for t, p in sorted(positions.items()):
            last = self.price_of(t)
            mv = (last or p["avg_cost"]) * p["quantity"]
            upnl = ((last - p["avg_cost"]) * p["quantity"]) if last is not None else 0.0
            market_value += mv
            unrealized += upnl
            live = self.exits.get(t, p)   # the in-memory stop is newer when a trailing stop is moving
            rows.append({"ticker": t, "quantity": p["quantity"], "avg_cost": round(p["avg_cost"], 4),
                         "last_price": last, "market_value": round(mv, 2), "unrealized_pnl": round(upnl, 2),
                         "stop_loss": live.get("stop_loss"), "take_profit": p.get("take_profit"),
                         "trail_pct": p.get("trail_pct")})
        return {
            "cash": round(acct["cash"], 2),
            "market_value": round(market_value, 2),
            "equity": round(acct["cash"] + market_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(await self.db.realized_pnl_total(), 2),
            "positions": rows,
            "risk": self.risk.status(acct, now),
        }
