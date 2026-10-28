"""RedisBus: room events shared between instances with Redis pub/sub.

    api-1                      Redis                       api-2
    alice sends "hi" ──► PUBLISH room:7 {...} ──► both instances subscribed
                                                    to room:7 receive it
    deliver to local sockets in room 7             deliver to bob

- One channel per room (`room:<id>`). An instance subscribes to a room's
  channel only while at least one of ITS sockets is in that room, so an
  instance never receives (or pays for) traffic for rooms it does not serve.
- One pub/sub connection per instance, with a single listener task. Each
  received event is handed to the local Hub.

Pub/sub is fire-and-forget: if an instance is down or disconnected from
Redis at the moment of PUBLISH, it never gets that event. That is
acceptable HERE because every message is persisted before it is published
and clients catch up by seq after a reconnect. See docs/DECISIONS.md for
when Redis Streams or Kafka would be the better choice.
"""
import asyncio
import json
import logging

from redis.asyncio import Redis

logger = logging.getLogger("pulsewire.bus")


def channel(room_id: int) -> str:
    return f"room:{room_id}"


class RedisBus:
    def __init__(self, redis: Redis, hub):
        self.redis = redis
        self.hub = hub
        self.pubsub = redis.pubsub(ignore_subscribe_messages=True)
        self.listener: asyncio.Task | None = None

    async def start(self) -> None:
        self.listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self.listener:
            self.listener.cancel()
            try:
                await self.listener
            except asyncio.CancelledError:
                pass
        await self.pubsub.aclose()

    async def subscribe(self, room_id: int) -> None:
        await self.pubsub.subscribe(channel(room_id))

    async def unsubscribe(self, room_id: int) -> None:
        await self.pubsub.unsubscribe(channel(room_id))

    async def publish(self, room_id: int, event: dict) -> None:
        await self.redis.publish(channel(room_id), json.dumps(event))

    async def _listen(self) -> None:
        while True:
            try:
                if not self.pubsub.subscribed:
                    # Nothing to read yet (no local sockets in any room).
                    await asyncio.sleep(0.05)
                    continue
                message = await self.pubsub.get_message(timeout=1.0)
                if message is None:
                    continue
                room_id = int(message["channel"].split(":", 1)[1])
                self.hub.deliver(room_id, json.loads(message["data"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A Redis hiccup must not kill the listener; redis-py
                # reconnects and re-subscribes on the next call.
                logger.exception("pub/sub listener error; retrying")
                await asyncio.sleep(0.5)
