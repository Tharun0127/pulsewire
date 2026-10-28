"""Reconnection with missed-message recovery, idempotent resend, receipts."""
import asyncio
import json
import uuid

from tests.conftest import connect, receive_until, rest, send


def is_type(kind):
    return lambda e: e["type"] == kind


class Timeline:
    """The client-side algorithm (the React app does the same in TypeScript).

    Messages are keyed by seq, so a message that arrives both live and from
    the history fetch is stored once (no duplicates), and `gaps()` shows any
    seq we know exists but have not got (no silent holes).
    """

    def __init__(self):
        self.by_seq: dict[int, dict] = {}

    def add(self, message: dict) -> None:
        self.by_seq[message["seq"]] = message

    @property
    def last_seq(self) -> int:
        return max(self.by_seq, default=0)

    def gaps(self) -> list[int]:
        return [s for s in range(1, self.last_seq + 1) if s not in self.by_seq]


async def reconnect_and_catch_up(server, token: str, room_id: int, timeline: Timeline):
    """1. Connect first, so live messages start flowing (and are kept).
    2. Read hello: room_seqs says how far the room has got.
    3. Fetch everything after our last seq from history.
    4. Merge history and live messages by seq."""
    ws = await connect(server, token)
    hello = await receive_until(ws, is_type("hello"))
    async with rest(server, token) as client:
        missed = (await client.get(f"/rooms/{room_id}/messages",
                                   params={"after_seq": timeline.last_seq, "limit": 200})).json()["messages"]
    for message in missed:
        timeline.add(message)
    return ws, hello


async def drain_messages(ws, timeline: Timeline, seconds: float = 0.5) -> None:
    try:
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), seconds))
            if event["type"] == "message":
                timeline.add(event)
    except asyncio.TimeoutError:
        pass


async def test_reconnect_recovers_missed_messages_without_gaps_or_duplicates(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])
    bob_timeline = Timeline()

    async with await connect(api1, alice) as a:
        await receive_until(a, is_type("hello"))

        async def alice_says(text):
            await send(a, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body=text)
            await receive_until(a, is_type("ack"))

        # Bob is on api-2 and sees messages 1-2.
        b = await connect(api2, bob)
        await receive_until(b, is_type("hello"))
        await alice_says("one")
        await alice_says("two")
        for _ in range(2):
            bob_timeline.add(await receive_until(b, is_type("message")))
        await b.close()  # Bob's train enters a tunnel

        await alice_says("three")
        await alice_says("four")
        await alice_says("five")

        # Bob reconnects, this time landing on api-1. Alice keeps talking
        # while he is catching up, which is the tricky race.
        b, hello = await reconnect_and_catch_up(api1, bob, room, bob_timeline)
        await alice_says("six")
        await drain_messages(b, bob_timeline)
        await b.close()

    assert hello["room_seqs"][str(room)] >= 5
    assert sorted(bob_timeline.by_seq) == [1, 2, 3, 4, 5, 6]
    assert bob_timeline.gaps() == []
    assert [bob_timeline.by_seq[s]["body"] for s in range(1, 7)] == ["one", "two", "three", "four", "five", "six"]


async def test_message_sent_just_before_connection_died_is_not_duplicated(start_servers, make_user, make_room, db):
    """Alice hits send, the connection dies before the ack arrives. She does
    not know if the server got it, so her client resends with the SAME
    client_msg_id after reconnecting. Exactly one message must exist."""
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])
    client_id = str(uuid.uuid4())

    async with await connect(api2, bob) as b:
        await receive_until(b, is_type("hello"))
        a = await connect(api1, alice)
        await receive_until(a, is_type("hello"))
        await send(a, type="send", room_id=room, client_msg_id=client_id, body="did this send?")
        await asyncio.sleep(0.3)  # the server stores it...
        a.transport.abort()       # ...but the ack never reaches Alice

        a = await connect(api2, alice)
        await receive_until(a, is_type("hello"))
        await send(a, type="send", room_id=room, client_msg_id=client_id, body="did this send?")
        ack = await receive_until(a, is_type("ack"))
        await a.close()

        bob_got = [await receive_until(b, is_type("message"))]
        try:
            bob_got.append(await receive_until(b, is_type("message"), timeout=1))
        except asyncio.TimeoutError:
            pass

    assert ack["seq"] == 1
    assert len(bob_got) == 1  # broadcast once, not on the resend
    assert await db.fetchval("SELECT count(*) FROM messages") == 1


async def test_delivered_and_read_receipts_travel_across_instances(start_servers, make_user, make_room):
    api1, api2 = await start_servers(2)
    alice_id, alice = await make_user("alice")
    bob_id, bob = await make_user("bob")
    room = await make_room("general", [alice_id, bob_id])

    async with await connect(api1, alice) as a, await connect(api2, bob) as b:
        await receive_until(a, is_type("hello"))
        await receive_until(b, is_type("hello"))
        for text in ("first", "second"):
            await send(a, type="send", room_id=room, client_msg_id=str(uuid.uuid4()), body=text)
        await receive_until(b, lambda e: e["type"] == "message" and e["seq"] == 2)

        await send(b, type="delivered", room_id=room, seq=2)
        delivered = await receive_until(a, is_type("receipt"))
        await send(b, type="read", room_id=room, seq=1)
        read = await receive_until(a, is_type("receipt"))

        # Stale receipt (a late duplicate): no change, so no broadcast.
        await send(b, type="delivered", room_id=room, seq=1)
        try:
            await receive_until(a, is_type("receipt"), timeout=0.8)
            stale_broadcast = True
        except asyncio.TimeoutError:
            stale_broadcast = False

        # A client claiming to have read the future is clamped to the last seq.
        await send(b, type="read", room_id=room, seq=999)
        clamped = await receive_until(a, is_type("receipt"))

    assert (delivered["user_id"], delivered["delivered_seq"], delivered["read_seq"]) == (bob_id, 2, 0)
    assert (read["delivered_seq"], read["read_seq"]) == (2, 1)
    assert stale_broadcast is False
    assert clamped["read_seq"] == 2
    async with rest(api1, alice) as client:
        members = {m["username"]: m for m in (await client.get(f"/rooms/{room}/members")).json()}
    assert members["bob"]["delivered_seq"] == 2 and members["bob"]["read_seq"] == 2
