"""Passwords (bcrypt) and JWTs, used by both REST and the WebSocket handshake."""
import asyncio
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings

bearer_scheme = HTTPBearer(auto_error=False)


async def hash_password(password: str, rounds: int) -> str:
    # bcrypt is CPU-heavy on purpose; a thread keeps the event loop (and every
    # open WebSocket on this instance) responsive meanwhile.
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt(rounds))
    return hashed.decode()


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(bcrypt.checkpw, password.encode(), password_hash.encode())


def create_access_token(settings: Settings, user_id: int, username: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "name": username, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(settings: Settings, token: str) -> tuple[int, str] | None:
    """(user_id, username) for a valid token, None otherwise."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    return int(payload["sub"]), payload["name"]


def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)
) -> tuple[int, str]:
    """REST dependency: (user_id, username) or 401."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    user = decode_token(request.app.state.settings, credentials.credentials)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return user
