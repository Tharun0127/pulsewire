"""Typing indicators: throttled on the client AND the server, short-lived in Redis.

The client sends {"type": "typing"} at most once every 2 s while the user
types. The server additionally does
    SET typing:<room>:<user> 1 NX PX <throttle>
and only broadcasts when the key was NOT already there. So even a buggy or
malicious client cannot flood a room with typing events, and if users of the
same room sit on different instances the throttle still holds, because the
key lives in shared Redis.

Receivers show "alice is typing..." for `expires_in_ms` after the last
event; nobody needs to send "stopped typing".
"""
from redis.asyncio import Redis

SERVER_THROTTLE_MS = 2000


def typing_key(room_id: int, user_id: int) -> str:
    return f"typing:{room_id}:{user_id}"


async def should_broadcast(redis: Redis, room_id: int, user_id: int) -> bool:
    return bool(await redis.set(typing_key(room_id, user_id), "1", nx=True, px=SERVER_THROTTLE_MS))
