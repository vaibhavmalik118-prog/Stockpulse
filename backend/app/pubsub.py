"""WebSocket publish-subscribe manager: one subscriber room per ticker."""
from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("stockpulse.pubsub")
_ids = itertools.count(1)


class Client:
    """Wraps one browser connection.

    Messages are queued and sent by a dedicated writer task, so a slow or broken
    browser can never block publishing to everyone else.
    """

    def __init__(self, ws, max_queue: int = 1000):
        self.ws = ws
        self.id = next(_ids)
        self.queue: asyncio.Queue = asyncio.Queue(max_queue)
        self.closed = False

    def send(self, message: dict) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            log.warning("client %s too slow, dropping connection", self.id)
            self.close()
            return False

    async def writer(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                if msg is None:
                    break
                await self.ws.send_json(msg)
        except Exception as exc:  # connection dropped without a clean disconnect
            log.info("client %s send failed: %s", self.id, exc)
        finally:
            self.closed = True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


RoomHook = Optional[Callable[[str], Awaitable[None]]]


class PubSubManager:
    def __init__(self, on_first_subscriber: RoomHook = None, on_last_unsubscribe: RoomHook = None):
        self.rooms: dict[str, set[Client]] = {}
        self.memberships: dict[Client, set[str]] = {}
        self._on_first = on_first_subscriber
        self._on_last = on_last_unsubscribe

    @property
    def clients(self) -> list[Client]:
        return list(self.memberships)

    def register(self, client: Client) -> None:
        self.memberships.setdefault(client, set())

    async def subscribe(self, client: Client, ticker: str) -> None:
        self.register(client)
        room = self.rooms.get(ticker)
        created = room is None
        if created:
            room = self.rooms[ticker] = set()
        room.add(client)
        self.memberships[client].add(ticker)
        if created and self._on_first:
            await self._on_first(ticker)

    async def unsubscribe(self, client: Client, ticker: str) -> None:
        room = self.rooms.get(ticker)
        if room is None or client not in room:
            return
        room.discard(client)
        self.memberships.get(client, set()).discard(ticker)
        if not room:
            del self.rooms[ticker]
            if self._on_last:
                await self._on_last(ticker)

    async def disconnect(self, client: Client) -> None:
        for ticker in list(self.memberships.get(client, ())):
            await self.unsubscribe(client, ticker)
        self.memberships.pop(client, None)
        client.close()

    def publish(self, ticker: str, message: dict) -> int:
        """Send to everyone in the ticker's room. Returns the number of clients reached."""
        delivered, dead = 0, []
        for client in list(self.rooms.get(ticker, ())):
            if client.send(message):
                delivered += 1
            else:
                dead.append(client)
        for client in dead:
            self._drop(client)
        return delivered

    def broadcast(self, message: dict) -> int:
        delivered, dead = 0, []
        for client in list(self.memberships):
            if client.send(message):
                delivered += 1
            else:
                dead.append(client)
        for client in dead:
            self._drop(client)
        return delivered

    def _drop(self, client: Client) -> None:
        """Remove a failed client without awaiting (used from the publish path)."""
        for ticker in list(self.memberships.get(client, ())):
            room = self.rooms.get(ticker)
            if room is not None:
                room.discard(client)
                if not room:
                    del self.rooms[ticker]
                    if self._on_last:
                        asyncio.ensure_future(self._on_last(ticker))
        self.memberships.pop(client, None)
        client.close()

    def room_size(self, ticker: str) -> int:
        return len(self.rooms.get(ticker, ()))
