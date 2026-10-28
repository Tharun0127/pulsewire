"""The core claim: users on DIFFERENT instances chat with each other."""
import asyncio
import uuid

from tests.conftest import connect, receive_until, send


def is_type(kind):
    return lambda e: e["type"] == kind


async def test_alice_on_api1_and_bob_on_api2_exchange_messages(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])

    async with await connect(api1, alice) as a, await connect(api2, bob) as b:
        assert (await receive_until(a, is_type("hello")))["instance"] == "test-1"
        assert (await receive_until(b, is_type("hello")))["instance"] == "test-2"

        await send(a, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body="hi from api-1")
        to_bob = await receive_until(b, is_type("message"))
        await send(b, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body="hello from api-2")
        to_alice = await receive_until(a, lambda e: e["type"] == "message" and e["user_id"] == bob_id)

    # The message was accepted and stored by api-1 and pushed to Bob by api-2.
    assert to_bob["body"] == "hi from api-1"
    assert to_bob["served_by"] == "test-1" and to_bob["delivered_via"] == "test-2"
    assert to_alice["body"] == "hello from api-2"
    assert to_alice["served_by"] == "test-2" and to_alice["delivered_via"] == "test-1"
    assert (to_bob["seq"], to_alice["seq"]) == (1, 2)


async def test_every_member_on_every_instance_gets_every_message_in_order(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    users = [await make_user(f"user{i}") for i in range(6)]
    room = await make_room("general", [uid for uid, _ in users])
    # Alternate users between the two instances.
    sockets = [await connect(api1 if i % 2 == 0 else api2, token) for i, (_, token) in enumerate(users)]
    try:
        for ws in sockets:
            await receive_until(ws, is_type("hello"))
        for i, ws in enumerate(sockets):
            await send(ws, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body=f"from {i}")

        for ws in sockets:
            received = [await receive_until(ws, is_type("message")) for _ in range(6)]
            assert [m["seq"] for m in received] == [1, 2, 3, 4, 5, 6]
    finally:
        for ws in sockets:
            await ws.close()


async def test_instance_only_subscribes_to_rooms_it_serves(start_servers, make_user, make_room, redis_client):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    room = await make_room("general", [alice_id])

    async with await connect(api1, alice) as a:
        await receive_until(a, is_type("hello"))
        # Only api-1 has a socket in the room, so only one subscriber.
        assert (await redis_client.pubsub_numsub(f"room:{room}"))[0][1] == 1

    # Last local socket gone -> api-1 unsubscribes.
    for _ in range(50):
        if (await redis_client.pubsub_numsub(f"room:{room}"))[0][1] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await redis_client.pubsub_numsub(f"room:{room}"))[0][1] == 0


async def test_without_redis_bus_cross_server_chat_breaks(start_servers, make_user, make_room):
    """Control experiment: in-process fan-out only reaches the same instance."""
    api1, api2 = await start_servers(2, use_redis_bus=False)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])
    async with await connect(api1, alice) as a, await connect(api2, bob) as b:
        await receive_until(a, is_type("hello"))
        await receive_until(b, is_type("hello"))
        await send(a, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body="anyone there?")
        await receive_until(a, is_type("message"))  # Alice sees her own message (same instance)
        try:
            await receive_until(b, is_type("message"), timeout=1.0)
            delivered = True
        except asyncio.TimeoutError:
            delivered = False
    assert delivered is False
