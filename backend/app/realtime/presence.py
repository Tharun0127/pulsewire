"""Online presence with heartbeats and TTLs in Redis.

Storage: one sorted set per user,  presence:user:<id>
    member = connection id, score = time (unix s) when that connection expires

- Connect / every ping (client pings every 15 s): ZADD conn (now + TTL) and
  EXPIRE the key. A user is online if ANY of their connections has an
  expiry in the future. That is correct with several tabs and with tabs
  connected to different instances.
- Clean disconnect: ZREM that connection. Other tabs keep the user online.
- Sudden disconnect (laptop lid closed, Wi-Fi gone): no close frame ever
  arrives and the pings stop. The connection's score simply falls into the
  past, so the user reads as offline within TTL (30 s) of the last ping.
  The key's own EXPIRE removes abandoned keys entirely.

Why not a plain key per user with a TTL? Closing one tab would have to DEL
it (wrongly marking the user offline while another tab is open) or leave it
(staying "online" for 30 s after the last tab closed).
"""
import time

from redis.asyncio import Redis


def presence_key(user_id: int) -> str:
    return f"presence:user:{user_id}"


async def touch(redis: Redis, user_id: int, conn_id: str, ttl_seconds: float) -> None:
    key = presence_key(user_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(key, {conn_id: time.time() + ttl_seconds})
        # Drop this user's long-dead connections while we are here.
        pipe.zremrangebyscore(key, "-inf", time.time())
        pipe.expire(key, int(ttl_seconds) + 1)
        await pipe.execute()


async def leave(redis: Redis, user_id: int, conn_id: str) -> None:
    await redis.zrem(presence_key(user_id), conn_id)


async def online_user_ids(redis: Redis, user_ids: list[int]) -> set[int]:
    """Which of these users have at least one live connection right now."""
    if not user_ids:
        return set()
    now = time.time()
    async with redis.pipeline(transaction=False) as pipe:
        for user_id in user_ids:
            pipe.zcount(presence_key(user_id), now, "+inf")
        counts = await pipe.execute()
    return {uid for uid, count in zip(user_ids, counts) if count > 0}
