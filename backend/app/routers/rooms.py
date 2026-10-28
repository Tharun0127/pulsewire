"""Rooms: list, create, join."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app import chat
from app.auth import current_user
from app.models import Room, RoomMember

router = APIRouter(prefix="/rooms", tags=["rooms"])


class RoomCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")


class RoomOut(BaseModel):
    id: int
    name: str
    member_count: int
    is_member: bool
    last_seq: int
    online_count: int = 0


async def add_member(session, room_id: int, user_id: int) -> None:
    # ON CONFLICT DO NOTHING: joining twice (two tabs, a retry) is harmless.
    await session.execute(insert(RoomMember).values(room_id=room_id, user_id=user_id).on_conflict_do_nothing())


@router.get("", response_model=list[RoomOut])
async def list_rooms(request: Request, user=Depends(current_user)):
    user_id, _ = user
    members = func.count(RoomMember.user_id)
    is_member = func.bool_or(RoomMember.user_id == user_id)
    query = (
        select(Room, members, is_member)
        .outerjoin(RoomMember, RoomMember.room_id == Room.id)
        .group_by(Room.id)
        .order_by(Room.name)
    )
    async with request.app.state.session_factory() as session:
        rows = (await session.execute(query)).all()
    return [RoomOut(id=r.id, name=r.name, member_count=n, is_member=bool(m), last_seq=r.last_seq)
            for r, n, m in rows]


@router.post("", response_model=RoomOut, status_code=201)
async def create_room(body: RoomCreate, request: Request, user=Depends(current_user)):
    user_id, _ = user
    async with request.app.state.session_factory() as session:
        room = Room(name=body.name, created_by=user_id)
        session.add(room)
        try:
            await session.flush()
        except IntegrityError:
            raise HTTPException(409, "room name already taken")
        await add_member(session, room.id, user_id)
        await session.commit()
    return RoomOut(id=room.id, name=room.name, member_count=1, is_member=True, last_seq=0)


@router.get("/{room_id}/messages")
async def room_messages(
    room_id: int, request: Request, user=Depends(current_user),
    before_seq: int | None = Query(default=None, ge=1),
    after_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    """History with a seq cursor: ?before_seq=X scrolls up, ?after_seq=X catches up."""
    user_id, _ = user
    if before_seq is not None and after_seq is not None:
        raise HTTPException(422, "use before_seq or after_seq, not both")
    factory = request.app.state.session_factory
    async with factory() as session:
        if not await chat.is_member(session, room_id, user_id):
            raise HTTPException(403, "join the room first")
    messages = await chat.history(factory, room_id, before_seq=before_seq, after_seq=after_seq, limit=limit)
    return {"room_id": room_id, "messages": messages,
            # The next cursor for scrolling up; None when the start is reached.
            "next_before_seq": messages[0]["seq"] if len(messages) == limit and after_seq is None else None}


@router.post("/{room_id}/join", status_code=204)
async def join_room(room_id: int, request: Request, user=Depends(current_user)):
    user_id, _ = user
    async with request.app.state.session_factory() as session:
        if await session.get(Room, room_id) is None:
            raise HTTPException(404, "room not found")
        await add_member(session, room_id, user_id)
        await session.commit()
