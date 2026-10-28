"""Periodic housekeeping, one task per instance.

Every PRESENCE_SWEEP_SECONDS:
1. Close sockets that have been silent longer than IDLE_TIMEOUT_SECONDS.
   A half-open TCP connection (the other side vanished without a FIN) can
   look "open" to the server for a long time; no pings means it is dead.
2. Recompute who is online in each room that has local sockets, and push a
   presence update to those sockets if it changed. This is what notices
   presence TTLs running out: nobody sends an event when a laptop dies,
   so someone has to look.

Each instance only sweeps rooms it serves and only delivers locally, so the
work scales with the instance's own connections.
"""
import asyncio
import logging
import time

from app import chat
from app.realtime import presence
from app.realtime.connections import CLOSE_IDLE

logger = logging.getLogger("pulsewire.sweeper")


async def presence_event(state, room_id: int) -> dict:
    member_ids = await chat.room_member_ids(state.session_factory, room_id)
    online = sorted(await presence.online_user_ids(state.redis, member_ids))
    return {"type": "presence", "room_id": room_id, "online_user_ids": online, "online_count": len(online)}


async def sweep_once(state) -> None:
    hub, settings = state.hub, state.settings
    now = time.monotonic()
    for conn in list(hub.registry.all):
        if now - conn.last_seen > settings.idle_timeout_seconds:
            logger.info("closing idle socket %s (user %s)", conn.id, conn.user_id)
            await conn.close(CLOSE_IDLE, "no heartbeat")

    for room_id in list(hub.registry.by_room):
        event = await presence_event(state, room_id)
        if hub.last_presence.get(room_id) != event["online_user_ids"]:
            hub.deliver(room_id, event)


async def run(state) -> None:
    while True:
        await asyncio.sleep(state.settings.presence_sweep_seconds)
        try:
            await sweep_once(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep failed; will retry")
