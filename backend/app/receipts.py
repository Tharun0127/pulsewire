"""Delivered and read receipts.

Ticks, like WhatsApp:
  ✓    sent       the server persisted the message (the "ack" event)
  ✓✓   delivered  every other member's client has received it
  ✓✓ (blue) read  every other member has seen it on screen

Stored per member as two numbers: the highest seq delivered and the highest
seq read. Messages in a room are totally ordered by seq, so "delivered up to
57" covers messages 1..57 at once. One small UPDATE per receipt instead of
a row per message per member.

Receipts only move FORWARD (GREATEST), and are clamped to the room's last
seq, so a late or duplicated receipt from a reconnecting client cannot undo
progress, and a buggy client cannot claim to have read the future.
"""
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Room, RoomMember

DELIVERED = "delivered"
READ = "read"


async def advance(session_factory: async_sessionmaker, room_id: int, user_id: int, kind: str,
                  seq: int) -> dict | None:
    """Move a member's receipt forward. Returns the new state, or None if
    nothing changed (so nothing needs broadcasting)."""
    room_last_seq = select(Room.last_seq).where(Room.id == room_id).scalar_subquery()
    target = func.least(seq, room_last_seq)
    values = {"last_delivered_seq": func.greatest(RoomMember.last_delivered_seq, target)}
    progress = RoomMember.last_delivered_seq < target
    if kind == READ:
        # Reading something implies it was delivered.
        values["last_read_seq"] = func.greatest(RoomMember.last_read_seq, target)
        progress = progress | (RoomMember.last_read_seq < target)

    async with session_factory() as session, session.begin():
        row = (await session.execute(
            update(RoomMember)
            .where(RoomMember.room_id == room_id, RoomMember.user_id == user_id, progress)
            .values(**values)
            .returning(RoomMember.last_delivered_seq, RoomMember.last_read_seq)
        )).first()
    if row is None:
        return None
    return {"type": "receipt", "room_id": room_id, "user_id": user_id,
            "delivered_seq": row.last_delivered_seq, "read_seq": row.last_read_seq}
