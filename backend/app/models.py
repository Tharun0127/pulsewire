"""ORM models: users, rooms, room members, messages."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # WHY: the per-room message counter. Incremented with
    # UPDATE ... RETURNING inside the insert transaction, so every message in
    # a room gets the next number with no gaps and no duplicates.
    last_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoomMember(Base):
    __tablename__ = "room_members"

    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    # Receipts are stored as "highest seq delivered/read" per member: one
    # row update instead of one row per message per member.
    last_delivered_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_read_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Ordering: seq is unique within a room.
        UniqueConstraint("room_id", "seq", name="uq_messages_room_seq"),
        # Idempotent send: a client that re-sends after a reconnect with the
        # same client_msg_id gets the original message back, not a duplicate.
        UniqueConstraint("room_id", "user_id", "client_msg_id", name="uq_messages_client_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    seq: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    client_msg_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    # The instance that accepted and persisted the message.
    served_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
