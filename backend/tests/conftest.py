"""Test fixtures: TWO real API servers per test, like api-1 and api-2.

Each test starts two uvicorn servers inside the test process, on their own
ports, with different INSTANCE_IDs. They share one test Postgres database
and one Redis database (15), exactly like the real deployment shares db and
redis. Clients talk to them over real TCP sockets with the `websockets`
library, so the cross-server tests exercise the real network path.
"""
import asyncio
import json
import os
import socket
from contextlib import AsyncExitStack, asynccontextmanager

import asyncpg
import bcrypt
import httpx
import pytest
import uvicorn
import websockets
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis

from app.auth import create_access_token
from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://pulse:pulse@localhost:5433/pulsewire_test")
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6373/15")
PLAIN_DSN = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
PASSWORD = "password123"
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()


def make_settings(instance_id: str, **overrides) -> Settings:
    values = {"instance_id": instance_id, "database_url": TEST_DATABASE_URL, "redis_url": TEST_REDIS_URL,
              "bcrypt_rounds": 4}
    values.update(overrides)
    return Settings(**values)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _create_test_database() -> None:
    admin_dsn = PLAIN_DSN.rsplit("/", 1)[0] + "/postgres"
    db_name = PLAIN_DSN.rsplit("/", 1)[1]
    conn = await asyncpg.connect(admin_dsn)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name):
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    asyncio.run(_create_test_database())
    config = Config("alembic.ini")
    config.attributes["url"] = TEST_DATABASE_URL
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db(migrated_db):
    conn = await asyncpg.connect(PLAIN_DSN)
    await conn.execute("TRUNCATE messages, room_members, rooms, users RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


@pytest.fixture
async def redis_client():
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


class Server:
    def __init__(self, instance_id: str, port: int):
        self.instance_id = instance_id
        self.http = f"http://127.0.0.1:{port}"
        self.ws = f"ws://127.0.0.1:{port}/ws"


@asynccontextmanager
async def run_server(instance_id: str, **overrides):
    port = free_port()
    app = create_app(make_settings(instance_id, **overrides))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield Server(instance_id, port)
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def start_servers(db, redis_client):
    """Call it to start servers: `api1, api2 = await start_servers(2)`."""
    async with AsyncExitStack() as stack:
        async def start(count: int = 2, **overrides) -> list[Server]:
            return [await stack.enter_async_context(run_server(f"test-{i + 1}", **overrides))
                    for i in range(count)]

        yield start


@pytest.fixture
async def make_user(db):
    """Create a user directly in the DB; returns (user_id, token)."""
    settings = make_settings("test")

    async def make(username: str) -> tuple[int, str]:
        user_id = await db.fetchval(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2) RETURNING id", username, PASSWORD_HASH
        )
        return user_id, create_access_token(settings, user_id, username)

    return make


@pytest.fixture
async def make_room(db):
    async def make(name: str, member_ids: list[int]) -> int:
        room_id = await db.fetchval("INSERT INTO rooms (name, created_by) VALUES ($1, $2) RETURNING id",
                                    name, member_ids[0])
        await db.executemany("INSERT INTO room_members (room_id, user_id) VALUES ($1, $2)",
                             [(room_id, uid) for uid in member_ids])
        return room_id

    return make


# --- small WebSocket client helpers used by the realtime tests ---------------
async def connect(server: Server, token: str):
    # max_queue=None: tests often skip over events they do not care about.
    # With the default bounded queue (16 frames) an un-drained client stops
    # reading TCP, the server's close frame gets stuck behind unread data,
    # and every close() waits for its timeout (we measured 3 s per socket).
    return await websockets.connect(f"{server.ws}?token={token}", max_queue=None, close_timeout=2)


async def receive_until(ws, predicate, timeout: float = 5.0) -> dict:
    """Read events until one matches `predicate` (others are skipped)."""
    async def loop():
        while True:
            event = json.loads(await ws.recv())
            if predicate(event):
                return event

    return await asyncio.wait_for(loop(), timeout)


async def send(ws, **event) -> None:
    await ws.send(json.dumps(event))


def rest(server: Server, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(base_url=server.http, headers=headers, timeout=10)
