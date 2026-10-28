"""Presence (heartbeats + TTL) and typing indicators, across two instances."""
import asyncio
import time

from tests.conftest import connect, receive_until, rest, send

FAST = {"presence_ttl_seconds": 1.5, "presence_sweep_seconds": 0.3, "idle_timeout_seconds": 3}


def presence_with(online: set[int]):
    return lambda e: e["type"] == "presence" and set(e["online_user_ids"]) == online


def is_type(kind):
    return lambda e: e["type"] == kind


async def test_presence_is_shared_across_instances(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])

    async with await connect(api1, alice) as a:
        await receive_until(a, presence_with({alice_id}))
        async with await connect(api2, bob) as b:
            # Alice (api-1) learns Bob (api-2) came online.
            await receive_until(a, presence_with({alice_id, bob_id}))
            async with rest(api1, alice) as client:
                rooms = (await client.get("/rooms")).json()
                members = (await client.get(f"/rooms/{room}/members")).json()
            assert rooms[0]["online_count"] == 2
            assert all(m["online"] for m in members)
        # Bob closes his socket cleanly: offline immediately, no TTL wait.
        await receive_until(a, presence_with({alice_id}), timeout=2)


async def test_user_goes_offline_after_ungraceful_disconnect(start_servers, make_user, make_room):
    """Bob's laptop lid closes: no close frame, no more pings. The socket
    even stays technically open. He must still show offline within ~TTL."""
    api1, api2 = await start_servers(2, **FAST)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    await make_room("general", [alice_id, bob_id])

    async with await connect(api1, alice) as a:
        b = await connect(api2, bob)
        await receive_until(a, presence_with({alice_id, bob_id}))
        went_silent = time.monotonic()

        # Alice keeps pinging (she is alive); Bob sends nothing at all.
        async def alice_heartbeat():
            while True:
                await send(a, type="ping")
                await asyncio.sleep(0.5)

        heartbeat = asyncio.create_task(alice_heartbeat())
        try:
            await receive_until(a, presence_with({alice_id}), timeout=5)
        finally:
            heartbeat.cancel()
        offline_after = time.monotonic() - went_silent
        await b.close()

    # TTL 1.5 s + sweep 0.3 s, so ~1.5-2 s (in production: 30 s + 5 s).
    # Below the 3 s idle-close: proves the TTL, not the socket close, did it.
    assert 1.0 < offline_after < 2.6


async def test_silent_socket_is_closed_by_the_server(start_servers, make_user, make_room):
    (api1,) = await start_servers(1, **FAST)
    alice_id, alice = await make_user("alice")
    await make_room("general", [alice_id])
    ws = await connect(api1, alice)
    started = time.monotonic()
    await asyncio.wait_for(ws.wait_closed(), timeout=6)
    assert ws.close_code == 4410
    assert 2.5 < time.monotonic() - started < 5


async def test_abrupt_tcp_drop_shows_offline(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2, **FAST)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    await make_room("general", [alice_id, bob_id])
    async with await connect(api1, alice) as a:
        b = await connect(api2, bob)
        await receive_until(a, presence_with({alice_id, bob_id}))
        b.transport.abort()  # kill the TCP connection, no WebSocket close frame
        await receive_until(a, presence_with({alice_id}), timeout=5)


async def test_second_tab_keeps_user_online(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    await make_room("general", [alice_id, bob_id])
    async with await connect(api1, alice) as a:
        tab1 = await connect(api1, bob)
        tab2 = await connect(api2, bob)
        await receive_until(a, presence_with({alice_id, bob_id}))
        await tab1.close()
        await asyncio.sleep(0.5)
        async with rest(api1, alice) as client:
            assert (await client.get("/rooms")).json()[0]["online_count"] == 2
        await tab2.close()
        await receive_until(a, presence_with({alice_id}), timeout=3)


async def test_typing_reaches_other_instance_and_is_throttled(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])
    async with await connect(api1, alice) as a, await connect(api2, bob) as b:
        await receive_until(b, is_type("hello"))
        for _ in range(10):  # a client spamming typing events
            await send(a, type="typing", room_id=room)
        first = await receive_until(b, is_type("typing"))
        assert first["username"] == "alice" and first["expires_in_ms"] == 5000
        try:
            await receive_until(b, is_type("typing"), timeout=1.0)
            second = True
        except asyncio.TimeoutError:
            second = False
    assert second is False  # server-side throttle: one broadcast per 2 s window
