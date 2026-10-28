"""The Hub: delivers room events to every socket in the room, on every instance.

    socket handler --publish(room, event)--> Bus --> every instance's Hub.deliver
                                                         --> local sockets in that room

All events (messages, typing, presence, receipts) take the same path, even
for a receiver connected to the same instance as the sender. One path means
one set of bugs, and the single-instance case is not special.

The Bus is pluggable:
- LocalBus: in-process only (phase 2, single instance).
- RedisBus: Redis pub/sub, one channel per room (app/realtime/redis_bus.py).
"""
import asyncio
import json
import logging

from app.realtime.connections import CLOSE_SLOW_CONSUMER, Connection, ConnectionRegistry

logger = logging.getLogger("pulsewire.hub")


class Hub:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self.registry = ConnectionRegistry()
        self.bus = None  # set by the app at startup
        # Last presence list pushed per room, so the sweeper only sends changes.
        self.last_presence: dict[int, list[int]] = {}

    async def register(self, conn: Connection, room_ids: list[int]) -> None:
        self.registry.add(conn)
        for room_id in room_ids:
            await self.join(conn, room_id)

    async def join(self, conn: Connection, room_id: int) -> None:
        if self.registry.join_room(conn, room_id):
            # First local socket in this room: start receiving its events.
            await self.bus.subscribe(room_id)

    async def unregister(self, conn: Connection) -> None:
        for room_id in self.registry.remove(conn):
            # Last local socket left: stop receiving events nobody here needs.
            self.last_presence.pop(room_id, None)
            await self.bus.unsubscribe(room_id)

    async def publish(self, room_id: int, event: dict) -> None:
        await self.bus.publish(room_id, event)

    def deliver(self, room_id: int, event: dict) -> None:
        """Called by the bus for every event on a room this instance follows."""
        if event.get("type") == "presence":
            self.last_presence[room_id] = event["online_user_ids"]
        # Serialise ONCE per event, not once per socket: with 25 local members
        # in a room that is 1 json.dumps instead of 25. Measured to matter
        # under bursts (see docs/BENCHMARKS.md).
        payload = json.dumps({**event, "delivered_via": self.instance_id})
        for conn in self.registry.in_room(room_id):
            if not conn.enqueue_payload(payload):
                logger.warning("closing slow consumer %s (user %s)", conn.id, conn.user_id)
                # Fire and forget: the reader loop notices and cleans up.
                asyncio.create_task(conn.close(CLOSE_SLOW_CONSUMER, "too slow"))


class LocalBus:
    """Single-instance bus: publish delivers straight to the local hub."""

    def __init__(self, hub: Hub):
        self.hub = hub

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def subscribe(self, room_id: int) -> None:
        pass

    async def unsubscribe(self, room_id: int) -> None:
        pass

    async def publish(self, room_id: int, event: dict) -> None:
        self.hub.deliver(room_id, event)
