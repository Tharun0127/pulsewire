"""The sockets open on THIS instance.

Each Connection has its own outgoing queue and writer task. Broadcasting to
a room then means "put the event on N queues", which never blocks: one slow
phone on a bad network cannot delay delivery to everyone else in the room.
"""
import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger("pulsewire.ws")

# Close codes. 4000-4999 are free for applications.
CLOSE_UNAUTHORIZED = 4401
CLOSE_SLOW_CONSUMER = 4408
CLOSE_IDLE = 4410


class Connection:
    def __init__(self, websocket: WebSocket, user_id: int, username: str, queue_size: int):
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self.user_id = user_id
        self.username = username
        self.rooms: set[int] = set()
        self.last_seen = time.monotonic()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_size)
        self.writer: asyncio.Task | None = None

    def start_writer(self) -> None:
        """Start draining the queue. Called after `hello` has been sent, so
        hello is always the first frame even if room events arrived while
        the connection was being registered (they wait in the queue)."""
        self.writer = asyncio.create_task(self._write_loop())

    def enqueue(self, event: dict) -> bool:
        """Queue an event for this client. False if the client is too slow."""
        return self.enqueue_payload(json.dumps(event))

    def enqueue_payload(self, payload: str) -> bool:
        """Queue an already-serialised event (used for room fan-out)."""
        try:
            self.queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            return False

    async def _write_loop(self) -> None:
        try:
            while (payload := await self.queue.get()) is not None:
                await self.websocket.send_text(payload)
        except Exception:  # socket already gone; the reader side cleans up
            pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

    async def stop_writer(self) -> None:
        if self.writer is None:
            return
        self.writer.cancel()
        try:
            await self.writer
        except (asyncio.CancelledError, Exception):
            pass


class ConnectionRegistry:
    """room_id -> connections, user_id -> connections, for this instance only."""

    def __init__(self):
        self.by_room: dict[int, set[Connection]] = defaultdict(set)
        self.by_user: dict[int, set[Connection]] = defaultdict(set)
        self.all: set[Connection] = set()

    def add(self, conn: Connection) -> None:
        self.all.add(conn)
        self.by_user[conn.user_id].add(conn)

    def join_room(self, conn: Connection, room_id: int) -> bool:
        """Returns True if this is the first local socket in the room."""
        first = not self.by_room.get(room_id)
        self.by_room[room_id].add(conn)
        conn.rooms.add(room_id)
        return first

    def remove(self, conn: Connection) -> list[int]:
        """Forget a connection. Returns rooms that now have no local sockets."""
        self.all.discard(conn)
        self.by_user[conn.user_id].discard(conn)
        if not self.by_user[conn.user_id]:
            del self.by_user[conn.user_id]
        emptied = []
        for room_id in conn.rooms:
            members = self.by_room.get(room_id, set())
            members.discard(conn)
            if not members:
                self.by_room.pop(room_id, None)
                emptied.append(room_id)
        return emptied

    def in_room(self, room_id: int) -> list[Connection]:
        return list(self.by_room.get(room_id, ()))

    def has_user(self, user_id: int) -> bool:
        return user_id in self.by_user
