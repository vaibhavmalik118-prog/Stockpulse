"""StockPulse API: REST endpoints + one WebSocket endpoint (/ws)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .alerts import AlertError
from .config import load_settings
from .market import is_market_open
from .models import TICKER_PATTERN, AlertCreate, ClientMessage, ExitsUpdate, OrderCreate
from .pubsub import Client
from .service import MarketHub
from .trading import OrderRejected

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = MarketHub(settings)
    await hub.start()
    app.state.hub = hub
    yield
    await hub.stop()


app = FastAPI(title="StockPulse API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


def hub_of(request: Request) -> MarketHub:
    return request.app.state.hub


@app.exception_handler(OrderRejected)
async def order_rejected(_: Request, exc: OrderRejected):
    return JSONResponse(status_code=exc.status, content={"detail": exc.to_dict()})


# ---- REST ----------------------------------------------------------------
@app.get("/api/health")
async def health(request: Request):
    hub = hub_of(request)
    return {"status": "ok", "mode": settings.data_mode, "market_open": is_market_open(),
            "rooms": {t: len(c) for t, c in hub.pubsub.rooms.items()},
            "clients": len(hub.pubsub.clients), "dropped_ticks": hub.dropped_ticks}


@app.get("/api/config")
async def config():
    return {"tickers": settings.tickers, "mode": settings.data_mode, "starting_cash": settings.starting_cash,
            "cooldown_seconds": settings.cooldown_seconds, "daily_trade_limit": settings.daily_trade_limit,
            "max_consecutive_losses": settings.max_consecutive_losses}


@app.get("/api/history/{ticker}")
async def history(ticker: str, request: Request):
    ticker = ticker.upper()
    if not re.match(TICKER_PATTERN, ticker):
        raise HTTPException(422, "Invalid ticker symbol")
    return await hub_of(request).load_history(ticker)


@app.get("/api/alerts")
async def list_alerts(request: Request):
    return await hub_of(request).db.list_alerts()


@app.post("/api/alerts", status_code=201)
async def create_alert(body: AlertCreate, request: Request):
    hub = hub_of(request)
    try:
        return await hub.alerts.create(body.ticker, body.target, body.direction, hub.price_of(body.ticker))
    except AlertError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: int, request: Request):
    if not await hub_of(request).alerts.delete(alert_id):
        raise HTTPException(404, "Alert not found")
    return {"deleted": alert_id}


@app.get("/api/portfolio")
async def portfolio(request: Request):
    return await hub_of(request).trading.portfolio()


@app.post("/api/orders")
async def place_order(body: OrderCreate, request: Request):
    """Market orders fill now ({"trade": ...}); limit / stop orders wait on the server ({"order": ...})."""
    return await hub_of(request).place_order(body.ticker, body.side, body.quantity, body.stop_loss,
                                             body.take_profit, body.type, body.price, body.trail_pct)


@app.get("/api/orders")
async def list_orders(request: Request, status: Optional[str] = None, limit: int = 50):
    return await hub_of(request).db.list_orders(status, max(1, min(limit, 500)))


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: int, request: Request):
    order = await hub_of(request).cancel_order(order_id)
    if order is None:
        raise HTTPException(404, "No open order with that id")
    return order


def _ticker(ticker: str) -> str:
    ticker = ticker.upper()
    if not re.match(TICKER_PATTERN, ticker):
        raise HTTPException(422, "Invalid ticker symbol")
    return ticker


@app.put("/api/positions/{ticker}/exits")
async def set_exits(ticker: str, body: ExitsUpdate, request: Request):
    """Set or clear the stop-loss / target-profit / trailing stop of an open position."""
    return await hub_of(request).set_exits(_ticker(ticker), body.stop_loss, body.take_profit, body.trail_pct)


@app.post("/api/positions/{ticker}/exit")
async def exit_position(ticker: str, request: Request):
    """Sell the whole position now. Never blocked by the risk engine."""
    return await hub_of(request).exit_position(_ticker(ticker))


@app.get("/api/trades")
async def trades(request: Request, limit: int = 50):
    return await hub_of(request).db.list_trades(max(1, min(limit, 500)))


@app.post("/api/account/reset")
async def reset_account(request: Request):
    await hub_of(request).reset_account()
    return {"reset": True}


# ---- WebSocket -------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Browser protocol:
        -> {"type": "subscribe", "ticker": "AAPL"}   <- history, then tick / candle messages
        -> {"type": "unsubscribe", "ticker": "AAPL"}
        -> {"type": "ping"}                          <- {"type": "pong"}
       The server may also push {"type": "alert"}, {"type": "exit"} (stop-loss / target hit),
       {"type": "order"} (pending order filled / rejected), {"type": "trail"} (trailing stop moved)
       and {"type": "status"} at any time.
    """
    hub: MarketHub = ws.app.state.hub
    await ws.accept()
    client = Client(ws)
    hub.pubsub.register(client)
    writer = asyncio.create_task(client.writer())
    client.send(hub.status())
    try:
        while not client.closed:
            raw = await ws.receive_text()
            try:
                msg = ClientMessage.model_validate(json.loads(raw))
            except (ValueError, ValidationError):
                client.send({"type": "error", "message": "Invalid message"})
                continue
            if msg.type == "ping":
                client.send({"type": "pong"})
            elif msg.ticker is None:
                client.send({"type": "error", "message": "ticker is required"})
            elif msg.type == "subscribe":
                await hub.subscribe(client, msg.ticker)
            else:
                await hub.unsubscribe(client, msg.ticker)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(client)
        writer.cancel()
