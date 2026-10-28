"""The WebSocket endpoint: handshake, auth, read loop, cleanup.

Protocol (JSON text frames):
  client -> server
    {"type": "send", "room_id", "client_msg_id", "body"}
    {"type": "subscribe", "room_id"}          after joining a room via REST
    {"type": "ping"}                          every 15 s (presence heartbeat)
    {"type": "typing", "room_id"}             throttled by the client
    {"type": "delivered" | "read", "room_id", "seq"}
  server -> client
    {"type": "hello", "instance", "user_id", "username", "rooms"}
    {"type": "ack", "client_msg_id", "room_id", "seq", "id"}   = "sent" tick
    {"type": "message", ..., "served_by", "delivered_via"}
    {"type": "presence" | "typing" | "receipt" | "pong" | "error", ...}
"""
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import chat, receipts
from app.auth import decode_token
from app.realtime import presence, sweeper, typing
from app.realtime.connections import CLOSE_UNAUTHORIZED, Connection

logger = logging.getLogger("pulsewire.ws")
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    state = websocket.app.state
    # Browsers cannot set an Authorization header on a WebSocket, so the JWT
    # comes in the query string. We check it BEFORE doing any work; a bad
    # token gets a close code the client recognises as "do not retry".
    user = decode_token(state.settings, token)
    await websocket.accept()
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    user_id, username = user
    conn = Connection(websocket, user_id, username, state.settings.send_queue_size)
    room_ids = await chat.room_ids_for_user(state.session_factory, user_id)
    # Order matters: subscribe FIRST, then read each room's last seq. Any
    # message published after the subscription arrives live; anything before
    # it is <= room_seqs and the client fetches it. Nothing falls in between.
    await state.hub.register(conn, room_ids)
    hello = {"type": "hello", "instance": state.settings.instance_id, "user_id": user_id,
             "username": username, "rooms": room_ids,
             # Tells a reconnecting client at once whether it missed anything.
             "room_seqs": await chat.last_seqs(state.session_factory, room_ids)}
    await websocket.send_text(json.dumps(hello))  # always the first frame
    conn.start_writer()
    await on_connect(state, conn)
    try:
        while True:
            raw = await websocket.receive_text()
            conn.last_seen = time.monotonic()
            await handle(state, conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("socket %s crashed", conn.id)
    finally:
        # Runs on clean close, network drop, server-side close, or crash.
        await state.hub.unregister(conn)
        await conn.stop_writer()
        await on_disconnect(state, conn)


async def on_connect(state, conn: Connection) -> None:
    await presence.touch(state.redis, conn.user_id, conn.id, state.settings.presence_ttl_seconds)
    await announce_presence(state, conn.rooms)


async def on_disconnect(state, conn: Connection) -> None:
    # Clean close: remove this connection now instead of waiting for the TTL.
    # Other tabs of the same user keep them online.
    await presence.leave(state.redis, conn.user_id, conn.id)
    await announce_presence(state, conn.rooms)


async def announce_presence(state, room_ids) -> None:
    """Publish fresh presence for these rooms to every instance right away.
    (The sweeper covers changes nobody announces, like TTL expiry.)"""
    for room_id in list(room_ids):
        await state.hub.publish(room_id, await sweeper.presence_event(state, room_id))


async def handle(state, conn: Connection, raw: str) -> None:
    try:
        event = json.loads(raw)
        handler = HANDLERS[event["type"]]
    except (ValueError, KeyError, TypeError):
        conn.enqueue({"type": "error", "detail": "unknown or malformed event"})
        return
    await handler(state, conn, event)


async def handle_send(state, conn: Connection, event: dict) -> None:
    room_id = event.get("room_id")
    body = str(event.get("body", "")).strip()
    try:
        client_msg_id = uuid.UUID(str(event.get("client_msg_id")))
    except ValueError:
        conn.enqueue({"type": "error", "detail": "client_msg_id must be a UUID"})
        return
    if room_id not in conn.rooms:
        conn.enqueue({"type": "error", "detail": "not a member of this room", "client_msg_id": str(client_msg_id)})
        return
    if not body or len(body) > chat.MAX_BODY:
        conn.enqueue({"type": "error", "detail": "message must be 1-4000 characters",
                      "client_msg_id": str(client_msg_id)})
        return

    try:
        message, created = await chat.persist_message(
            state.session_factory, room_id, conn.user_id, body, client_msg_id, state.settings.instance_id
        )
    except chat.DuplicateSend:
        return  # a concurrent copy of the same send won; it acks and broadcasts
    # 1. Persisted -> ack the sender (the "sent" tick) with the server seq.
    conn.enqueue({"type": "ack", "client_msg_id": str(client_msg_id), "room_id": room_id,
                  "seq": message.seq, "id": message.id})
    # 2. Only now broadcast. A retry of an already-stored message is acked
    #    again but NOT re-broadcast, so nobody sees it twice.
    if created:
        await state.hub.publish(room_id, chat.message_event(message, conn.username))


async def handle_subscribe(state, conn: Connection, event: dict) -> None:
    room_id = event.get("room_id")
    async with state.session_factory() as session:
        allowed = isinstance(room_id, int) and await chat.is_member(session, room_id, conn.user_id)
    if not allowed:
        conn.enqueue({"type": "error", "detail": "join the room first"})
        return
    await state.hub.join(conn, room_id)
    conn.enqueue({"type": "subscribed", "room_id": room_id})
    await announce_presence(state, [room_id])


async def handle_ping(state, conn: Connection, event: dict) -> None:
    # The heartbeat: keeps this connection's presence entry alive.
    await presence.touch(state.redis, conn.user_id, conn.id, state.settings.presence_ttl_seconds)
    conn.enqueue({"type": "pong"})


async def handle_typing(state, conn: Connection, event: dict) -> None:
    room_id = event.get("room_id")
    if room_id not in conn.rooms:
        return
    if await typing.should_broadcast(state.redis, room_id, conn.user_id):
        await state.hub.publish(room_id, {
            "type": "typing", "room_id": room_id, "user_id": conn.user_id, "username": conn.username,
            "expires_in_ms": int(state.settings.typing_ttl_seconds * 1000),
        })


async def handle_receipt(state, conn: Connection, event: dict) -> None:
    room_id, seq = event.get("room_id"), event.get("seq")
    if room_id not in conn.rooms or not isinstance(seq, int) or seq < 1:
        return
    update = await receipts.advance(state.session_factory, room_id, conn.user_id, event["type"], seq)
    if update is not None:  # only broadcast real progress
        await state.hub.publish(room_id, update)


HANDLERS = {
    "send": handle_send,
    "subscribe": handle_subscribe,
    "ping": handle_ping,
    "typing": handle_typing,
    "delivered": handle_receipt,
    "read": handle_receipt,
}
