"""Load test: 1,000 concurrent WebSockets across both instances.

Run from a separate container (so the load generator does not share a
process with api-1):
    docker compose run --rm --no-deps -e BENCH_TARGET=http://nginx api-1 python -m benchmarks.load

What it does:
1. Creates 1,000 temporary users and 20 rooms of 50 members (prefix
   "load-"), directly in Postgres, and deletes them afterwards.
2. Opens all 1,000 sockets through the nginx LOAD BALANCER (/ws). The hello
   event says which instance each landed on, so we see the split.
3. 2 senders per room send 1 message per second for DURATION seconds.
   Each message body carries its send timestamp; every receiver records
   now - sent_at. Same machine, same clock, so the latency is end to end:
   client -> nginx -> instance -> Postgres -> Redis pub/sub -> other
   instance -> nginx -> client.
4. Checks every expected delivery arrived (messages x 50 members).
5. Reads /metrics/instance from each instance before and after to get
   memory per instance and per connection.
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import time
import urllib.request
import uuid
from collections import Counter

import asyncpg
import bcrypt
import websockets

from app.auth import create_access_token
from app.config import get_settings

TARGET = os.getenv("BENCH_TARGET", "http://nginx")
DSN = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
PREFIX = "load-"
RESULTS = os.path.join(os.path.dirname(__file__), "results")


async def setup(conn, users: int, rooms: int) -> tuple[list[int], list[int]]:
    await cleanup(conn)
    password_hash = bcrypt.hashpw(b"load-password", bcrypt.gensalt(4)).decode()
    user_ids = [r["id"] for r in await conn.fetch(
        "INSERT INTO users (username, password_hash) SELECT unnest($1::text[]), $2 RETURNING id",
        [f"{PREFIX}{i}" for i in range(users)], password_hash,
    )]
    room_ids = [r["id"] for r in await conn.fetch(
        "INSERT INTO rooms (name, created_by) SELECT unnest($1::text[]), $2 RETURNING id",
        [f"{PREFIX}room-{i}" for i in range(rooms)], user_ids[0],
    )]
    # User i joins room i % rooms: 50 members per room for 1,000 users / 20 rooms.
    await conn.executemany(
        "INSERT INTO room_members (room_id, user_id) VALUES ($1, $2)",
        [(room_ids[i % rooms], uid) for i, uid in enumerate(user_ids)],
    )
    return user_ids, room_ids


async def cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM messages WHERE room_id IN (SELECT id FROM rooms WHERE name LIKE $1)", f"{PREFIX}%")
    await conn.execute(
        "DELETE FROM room_members WHERE room_id IN (SELECT id FROM rooms WHERE name LIKE $1)", f"{PREFIX}%")
    await conn.execute("DELETE FROM rooms WHERE name LIKE $1", f"{PREFIX}%")
    await conn.execute("DELETE FROM users WHERE username LIKE $1", f"{PREFIX}%")


def instance_metrics() -> dict[str, dict]:
    out = {}
    for name in ("api-1", "api-2"):
        with urllib.request.urlopen(f"{TARGET}/direct/{name}/metrics/instance", timeout=10) as response:
            out[name] = json.load(response)
    return out


class Client:
    def __init__(self, user_id: int, room_id: int, token: str):
        self.user_id, self.room_id, self.token = user_id, room_id, token
        self.ws = None
        self.instance = None
        self.latencies_ms: list[float] = []
        self.received = 0

    async def connect(self) -> None:
        url = TARGET.replace("http", "ws") + f"/ws?token={self.token}"
        self.ws = await websockets.connect(url, max_queue=None, open_timeout=60, ping_interval=None)
        hello = json.loads(await self.ws.recv())
        self.instance = hello["instance"]

    async def read_loop(self) -> None:
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                if event["type"] == "message":
                    sent_at = json.loads(event["body"])["t"]
                    self.latencies_ms.append((time.time() - sent_at) * 1000)
                    self.received += 1
        except websockets.ConnectionClosed:
            pass

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.ws.send(json.dumps({"type": "ping"}))

    async def send_every_second(self, duration: float, counter: Counter, burst: bool) -> None:
        # Steady mode: random start offset, since real users do not all hit
        # send at the same instant. Burst mode: every sender fires together,
        # the worst case (e.g. a goal in a match-day chat).
        if not burst:
            await asyncio.sleep(random.random())
        end = time.monotonic() + duration
        while time.monotonic() < end:
            body = json.dumps({"t": time.time()})
            await self.ws.send(json.dumps(
                {"type": "send", "room_id": self.room_id, "client_msg_id": str(uuid.uuid4()), "body": body}))
            counter[self.room_id] += 1
            await asyncio.sleep(1)


def pct(values: list[float], p: int) -> float:
    return round(statistics.quantiles(values, n=100)[p - 1], 1)


async def run(users: int, rooms: int, senders_per_room: int, duration: float, burst: bool) -> dict:
    conn = await asyncpg.connect(DSN)
    user_ids, room_ids = await setup(conn, users, rooms)
    settings = get_settings()
    clients = [Client(uid, room_ids[i % rooms], create_access_token(settings, uid, f"{PREFIX}{i}"))
               for i, uid in enumerate(user_ids)]
    before = instance_metrics()
    try:
        started = time.perf_counter()
        # Connect in batches of 100 so we measure the servers, not a SYN flood.
        for i in range(0, len(clients), 100):
            await asyncio.gather(*(c.connect() for c in clients[i:i + 100]))
        connect_seconds = time.perf_counter() - started
        during = instance_metrics()
        split = Counter(c.instance for c in clients)

        readers = [asyncio.create_task(c.read_loop()) for c in clients]
        beats = [asyncio.create_task(c.heartbeat()) for c in clients]
        await asyncio.sleep(1)  # let presence announcements settle

        sent = Counter()
        senders = [c for room in room_ids for c in [x for x in clients if x.room_id == room][:senders_per_room]]
        await asyncio.gather(*(s.send_every_second(duration, sent, burst) for s in senders))
        await asyncio.sleep(3)  # let the last messages arrive

        for task in beats + readers:
            task.cancel()
        await asyncio.gather(*(c.ws.close() for c in clients), return_exceptions=True)
    finally:
        await cleanup(conn)
        await conn.close()

    members_per_room = users // rooms
    expected = sum(sent.values()) * members_per_room
    latencies = [lat for c in clients for lat in c.latencies_ms]
    received = sum(c.received for c in clients)
    return {
        "connections": users, "rooms": rooms, "members_per_room": members_per_room,
        "mode": "burst" if burst else "steady", "senders": len(senders), "duration_s": duration,
        "connect_seconds": round(connect_seconds, 2),
        "connections_per_instance": dict(split),
        "messages_sent": sum(sent.values()),
        "deliveries_expected": expected, "deliveries_received": received,
        "delivery_rate_per_s": round(received / duration, 1),
        "latency_ms": {"p50": pct(latencies, 50), "p95": pct(latencies, 95), "p99": pct(latencies, 99),
                       "max": round(max(latencies), 1)},
        "memory": {
            name: {"idle_rss_mb": before[name]["rss_mb"], "with_connections_rss_mb": during[name]["rss_mb"],
                   "connections": during[name]["connections"],
                   "kb_per_connection": round((during[name]["rss_mb"] - before[name]["rss_mb"]) * 1024
                                              / max(1, during[name]["connections"]), 1)}
            for name in before
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--rooms", type=int, default=20)
    parser.add_argument("--senders-per-room", type=int, default=2)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--burst", action="store_true", help="all senders fire at the same instant")
    args = parser.parse_args()
    result = asyncio.run(run(args.users, args.rooms, args.senders_per_room, args.duration, args.burst))
    print(json.dumps(result, indent=2))
    os.makedirs(RESULTS, exist_ok=True)
    name = f"load_{args.users}_{'burst' if args.burst else 'steady'}.json"
    with open(os.path.join(RESULTS, name), "w") as handle:
        json.dump(result, handle, indent=2)


if __name__ == "__main__":
    main()
