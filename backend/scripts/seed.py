"""Seed demo data: two users and two rooms (idempotent).

alice / password123 and bob / password123, both members of #general and
#random, so cross-server chat can be demoed straight away.
"""
import asyncio
import os

import asyncpg
import bcrypt

USERS = ["alice", "bob"]
PASSWORD = "password123"
ROOMS = ["general", "random"]


async def seed(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(12)).decode()
        for username in USERS:
            await conn.execute(
                "INSERT INTO users (username, password_hash) VALUES ($1, $2) ON CONFLICT (username) DO NOTHING",
                username, password_hash,
            )
        owner = await conn.fetchval("SELECT id FROM users WHERE username = 'alice'")
        for name in ROOMS:
            await conn.execute(
                "INSERT INTO rooms (name, created_by) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING", name, owner
            )
        await conn.execute(
            "INSERT INTO room_members (room_id, user_id)"
            " SELECT r.id, u.id FROM rooms r, users u WHERE r.name = ANY($1) AND u.username = ANY($2)"
            " ON CONFLICT DO NOTHING",
            ROOMS, USERS,
        )
        print(f"[seed] users {USERS} (password {PASSWORD}), rooms {ROOMS}")
    finally:
        await conn.close()


def main() -> None:
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://pulse:pulse@localhost:5433/pulsewire")
    asyncio.run(seed(url.replace("postgresql+asyncpg://", "postgresql://")))


if __name__ == "__main__":
    main()
