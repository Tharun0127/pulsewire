"""Signup and login."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import create_access_token, hash_password, verify_password
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    user_id: int
    username: str


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: Credentials, request: Request):
    settings = request.app.state.settings
    user = User(username=body.username.lower(), password_hash=await hash_password(body.password, settings.bcrypt_rounds))
    async with request.app.state.session_factory() as session:
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            raise HTTPException(409, "username already taken")
    return TokenResponse(access_token=create_access_token(settings, user.id, user.username),
                         user_id=user.id, username=user.username)


@router.post("/login", response_model=TokenResponse)
async def login(body: Credentials, request: Request):
    settings = request.app.state.settings
    async with request.app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.username == body.username.lower()))
    if user is None or not await verify_password(body.password, user.password_hash):
        raise HTTPException(401, "wrong username or password")
    return TokenResponse(access_token=create_access_token(settings, user.id, user.username),
                         user_id=user.id, username=user.username)
