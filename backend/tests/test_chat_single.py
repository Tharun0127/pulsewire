"""WebSocket chat on ONE instance: lifecycle, persistence, ordering, history."""
import asyncio
import uuid

import pytest
import websockets

from tests.conftest import connect, receive_until, rest, send


def is_type(kind):
    return lambda e: e["type"] == kind


async def test_bad_token_is_closed_with_4401(start_servers):
    (api1,) = await start_servers(1)
    ws = await websockets.connect(f"{api1.ws}?token=not-a-jwt")
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), 5)
    assert closed.value.rcvd.code == 4401


async def test_hello_lists_my_rooms_and_instance(start_servers, make_user, make_room):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    room = await make_room("general", [alice_id])
    async with await connect(api1, alice) as ws:
        hello = await receive_until(ws, is_type("hello"))
    assert hello["rooms"] == [room] and hello["instance"] == "test-1"


async def test_message_is_persisted_acked_then_broadcast(start_servers, make_user, make_room, db):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])
    async with await connect(api1, alice) as a, await connect(api1, bob) as b:
        await receive_until(a, is_type("hello"))
        await receive_until(b, is_type("hello"))
        client_id = str(uuid.uuid4())
        await send(a, type="send", room_id=room, client_msg_id=client_id, body="hi bob")

        ack = await receive_until(a, is_type("ack"))
        got = await receive_until(b, is_type("message"))

    assert ack["client_msg_id"] == client_id and ack["seq"] == 1
    assert got["body"] == "hi bob" and got["seq"] == 1 and got["username"] == "alice"
    assert got["served_by"] == "test-1"
    assert await db.fetchval("SELECT body FROM messages WHERE seq = 1") == "hi bob"


async def test_resending_the_same_client_msg_id_does_not_duplicate(start_servers, make_user, make_room, db):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    room = await make_room("general", [alice_id])
    client_id = str(uuid.uuid4())
    async with await connect(api1, alice) as ws:
        await receive_until(ws, is_type("hello"))
        for _ in range(3):
            await send(ws, type="send", room_id=room, client_msg_id=client_id, body="once")
            ack = await receive_until(ws, is_type("ack"))
            assert ack["seq"] == 1
    assert await db.fetchval("SELECT count(*) FROM messages") == 1


async def test_concurrent_senders_get_gapless_unique_seqs(start_servers, make_user, make_room, db):
    (api1,) = await start_servers(1)
    users = [await make_user(f"user{i}") for i in range(10)]
    room = await make_room("busy", [uid for uid, _ in users])
    sockets = [await connect(api1, token) for _, token in users]
    try:
        for ws in sockets:
            await receive_until(ws, is_type("hello"))
        await asyncio.gather(*(
            send(ws, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body=f"m{i}-{j}")
            for i, ws in enumerate(sockets) for j in range(5)
        ))
        for ws in sockets:
            for _ in range(5):
                await receive_until(ws, is_type("ack"))
    finally:
        for ws in sockets:
            await ws.close()
    seqs = [r["seq"] for r in await db.fetch("SELECT seq FROM messages ORDER BY seq")]
    assert seqs == list(range(1, 51))


async def test_cannot_send_to_a_room_you_are_not_in(start_servers, make_user, make_room):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    bob_id, _ = await make_user("bob")
    room = await make_room("private", [bob_id])
    async with await connect(api1, alice) as ws:
        await receive_until(ws, is_type("hello"))
        await send(ws, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body="let me in")
        error = await receive_until(ws, is_type("error"))
    assert "not a member" in error["detail"]


async def test_subscribe_after_joining_via_rest(start_servers, make_user, make_room):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id])
    async with await connect(api1, bob) as b, rest(api1, bob) as client:
        await receive_until(b, is_type("hello"))
        await client.post(f"/rooms/{room}/join")
        await send(b, type="subscribe", room_id=room)
        await receive_until(b, is_type("subscribed"))
        async with await connect(api1, alice) as a:
            await receive_until(a, is_type("hello"))
            await send(a, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body="welcome")
            assert (await receive_until(b, is_type("message")))["body"] == "welcome"


async def test_history_pages_upwards_with_a_cursor(start_servers, make_user, make_room):
    (api1,) = await start_servers(1)
    alice_id, alice = await make_user("alice")
    room = await make_room("general", [alice_id])
    async with await connect(api1, alice) as ws:
        await receive_until(ws, is_type("hello"))
        for i in range(1, 121):
            await send(ws, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body=f"#{i}")
        for _ in range(120):
            await receive_until(ws, is_type("ack"))

    async with rest(api1, alice) as client:
        latest = (await client.get(f"/rooms/{room}/messages", params={"limit": 50})).json()
        older = (await client.get(f"/rooms/{room}/messages",
                                  params={"limit": 50, "before_seq": latest["next_before_seq"]})).json()
        oldest = (await client.get(f"/rooms/{room}/messages",
                                   params={"limit": 50, "before_seq": older["next_before_seq"]})).json()
        catch_up = (await client.get(f"/rooms/{room}/messages", params={"after_seq": 115})).json()

    assert [m["seq"] for m in latest["messages"]] == list(range(71, 121))
    assert [m["seq"] for m in older["messages"]] == list(range(21, 71))
    assert [m["seq"] for m in oldest["messages"]] == list(range(1, 21))
    assert oldest["next_before_seq"] is None
    assert [m["seq"] for m in catch_up["messages"]] == [116, 117, 118, 119, 120]
