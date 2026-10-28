"""Messages in the database: persist with a per-room sequence, read history.

Nothing here knows about WebSockets. The socket handler persists a message
FIRST and only then broadcasts it, so anything a client has seen is
guaranteed to be in Postgres (and can be re-fetched after a reconnect).
"""
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Message, Room, RoomMember, User

MAX_BODY = 4000


def message_event(message: Message, username: str) -> dict:
    """The JSON shape every client receives for a chat message."""
    return {
        "type": "message", "room_id": message.room_id, "id": message.id, "seq": message.seq,
        "user_id": message.user_id, "username": username, "body": message.body,
        "client_msg_id": str(message.client_msg_id), "created_at": message.created_at.isoformat(),
        "served_by": message.served_by,
    }


async def is_member(session: AsyncSession, room_id: int, user_id: int) -> bool:
    return await session.scalar(
        select(RoomMember.user_id).where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)
    ) is not None


async def room_member_ids(session_factory: async_sessionmaker, room_id: int) -> list[int]:
    async with session_factory() as session:
        return list(await session.scalars(select(RoomMember.user_id).where(RoomMember.room_id == room_id)))


async def room_ids_for_user(session_factory: async_sessionmaker, user_id: int) -> list[int]:
    async with session_factory() as session:
        return list(await session.scalars(select(RoomMember.room_id).where(RoomMember.user_id == user_id)))


async def persist_message(
    session_factory: async_sessionmaker, room_id: int, user_id: int, body: str,
    client_msg_id: uuid.UUID, instance_id: str,
) -> tuple[Message, bool]:
    """Store a message and give it the next seq in its room.

    Returns (message, created). `created` is False when the same
    client_msg_id was already stored: a retry after a reconnect. The caller
    then acks with the ORIGINAL message and does not broadcast it again.
    """
    async with session_factory() as session, session.begin():
        existing = await find_by_client_id(session, room_id, user_id, client_msg_id)
        if existing is not None:
            return existing, False

        # Row-locks the room until COMMIT. Two concurrent senders in the same
        # room therefore get seq N and N+1, never the same number and never a
        # gap. Different rooms do not block each other.
        seq = await session.scalar(
            update(Room).where(Room.id == room_id).values(last_seq=Room.last_seq + 1).returning(Room.last_seq)
        )
        message = await session.scalar(
            insert(Message)
            .values(room_id=room_id, seq=seq, user_id=user_id, body=body,
                    client_msg_id=client_msg_id, served_by=instance_id)
            .on_conflict_do_nothing(constraint="uq_messages_client_id")
            .returning(Message)
        )
        if message is None:
            # Lost a race with a concurrent duplicate send of the same
            # client_msg_id. Roll back our seq bump so the room has no gap.
            raise DuplicateSend()
        return message, True


class DuplicateSend(Exception):
    pass


async def find_by_client_id(session: AsyncSession, room_id: int, user_id: int, client_msg_id: uuid.UUID):
    return await session.scalar(select(Message).where(
        Message.room_id == room_id, Message.user_id == user_id, Message.client_msg_id == client_msg_id
    ))


async def history(
    session_factory: async_sessionmaker, room_id: int, *, before_seq: int | None = None,
    after_seq: int | None = None, limit: int = 50,
) -> list[dict]:
    """Cursor-based pagination on seq. Always returned oldest first.

    - before_seq: scrolling UP. "The 50 messages just older than seq X."
    - after_seq:  catching UP after a reconnect. "Everything after seq X."

    Why a cursor and not OFFSET: new messages arrive while you scroll, so
    "page 2" would shift and you would see duplicates or skip messages.
    `seq < X` always means the same set of rows, and with the
    (room_id, seq) index it is an index range scan however deep you go.
    """
    query = select(Message, User.username).join(User, User.id == Message.user_id).where(Message.room_id == room_id)
    if after_seq is not None:
        query = query.where(Message.seq > after_seq).order_by(Message.seq.asc()).limit(limit)
    else:
        if before_seq is not None:
            query = query.where(Message.seq < before_seq)
        query = query.order_by(Message.seq.desc()).limit(limit)
    async with session_factory() as session:
        rows = (await session.execute(query)).all()
    events = [message_event(m, username) for m, username in rows]
    return sorted(events, key=lambda e: e["seq"])
