"""Users, rooms, members, messages.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("last_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "room_members",
        sa.Column("room_id", sa.Integer, sa.ForeignKey("rooms.id"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("last_delivered_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_read_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_room_members_user_id", "room_members", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("room_id", sa.Integer, sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("client_msg_id", UUID(as_uuid=True), nullable=False),
        sa.Column("served_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # The (room_id, seq) unique index also serves history queries:
        # WHERE room_id = ? AND seq < ? ORDER BY seq DESC LIMIT 50.
        sa.UniqueConstraint("room_id", "seq", name="uq_messages_room_seq"),
        sa.UniqueConstraint("room_id", "user_id", "client_msg_id", name="uq_messages_client_id"),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("room_members")
    op.drop_table("rooms")
    op.drop_table("users")
