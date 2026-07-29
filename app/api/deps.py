"""FastAPI dependencies: settings, database session, Redis, room, listener, host."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Listener, Room
from app.security import is_valid_room_token, verify_secret
from app.service import count_listeners, get_listener, get_room, is_banned, join_room

HOST_SECRET_HEADER = "X-Host-Secret"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit on success, roll back on anything."""
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_session_id(request: Request) -> str:
    session_id = getattr(request.state, "session_id", None)
    if not session_id:  # pragma: no cover - the middleware always sets it
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no session")
    return session_id


async def require_room(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Room:
    if not is_valid_room_token(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    room = await get_room(db, token)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    return room


async def current_listener(
    request: Request,
    room: Room = Depends(require_room),
    db: AsyncSession = Depends(get_db),
    session_id: str = Depends(get_session_id),
) -> Listener:
    """Join-on-first-touch. There is no separate signup step (concept §14)."""
    if await is_banned(db, room.id, session_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You have been removed from this room")

    host_secret = request.headers.get(HOST_SECRET_HEADER, "")
    as_host = bool(host_secret) and verify_secret(room.host_secret_hash, host_secret)

    # Capacity applies to newcomers only -- someone already in the room does
    # not get bounced because the host later lowered the limit.
    if not as_host and await get_listener(db, room.id, session_id) is None:
        if await count_listeners(db, room.id) >= room.max_listeners:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This room is full")

    return await join_room(db, room, session_id, as_host=as_host)


def has_host_secret(request: Request, room: Room) -> bool:
    secret = request.headers.get(HOST_SECRET_HEADER, "")
    return bool(secret) and verify_secret(room.host_secret_hash, secret)


async def require_host(
    request: Request,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
) -> Room:
    """Host rights are proven once and then carried by the session.

    Presenting the secret marks the listener row as host; from then on the
    session cookie is the credential. That is what lets the server render the
    host controls without the browser having to hold the secret, and it is the
    same trust level as the session itself.
    """
    if listener.is_host or has_host_secret(request, room):
        return room
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Host privileges required")
