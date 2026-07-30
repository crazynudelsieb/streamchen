"""Room chat.

Two endpoints and no state of its own: what is said goes into Redis and out
over the WebSocket the room already holds open. See ``app/chat.py`` for why it
is the one thing the socket carries whole.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import chat, events
from app.api.deps import (
    current_listener,
    get_db,
    get_redis,
    get_settings_dep,
    require_room,
)
from app.api.guards import spam_guard
from app.config import Settings
from app.models import Listener, Room
from app.ratelimit import Limit
from app.schemas import ChatMessage, ChatPost

router = APIRouter(tags=["chat"])


@router.get("/rooms/{token}/chat", response_model=list[ChatMessage])
async def history(
    room: Room = Depends(require_room),
    _listener: Listener = Depends(current_listener),
    redis: Redis = Depends(get_redis),
) -> list[ChatMessage]:
    """What has been said lately.

    Fetched once on arrival and again after a dropped socket, which is what
    makes a missed live message cost nothing.
    """
    if not room.chat_enabled:
        return []
    return [ChatMessage(**message) for message in await chat.recent(redis, room.id)]


@router.post("/rooms/{token}/chat", response_model=ChatMessage, status_code=status.HTTP_201_CREATED)
async def post(
    payload: ChatPost,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> ChatMessage:
    if not room.chat_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Chat is off in this room")

    text = chat.clean_message(payload.text)
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Say something")

    await spam_guard(
        db,
        redis,
        settings,
        listener,
        "chat",
        Limit(settings.chat_rate_limit, settings.chat_rate_window_s),
    )

    message = chat.message_for(listener, text)

    # A shadow-banned listener sees their own messages land and nobody else
    # ever does — the same silence their queued songs get (concept §10).
    if listener.is_shadow_banned():
        return ChatMessage(**message)

    await chat.store(redis, room.id, message)
    await events.publish(redis, room.id, events.CHAT_MESSAGE, message)
    return ChatMessage(**message)
