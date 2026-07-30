"""Room chat.

Deliberately the cheapest thing that is still a chat. Messages live in a capped
Redis list per room and nowhere else: they are not in Postgres, they are not in
the room snapshot, and they do not survive the room. That is both the privacy
answer (nothing to retain, in a room that keeps no media either — concept §12)
and the performance answer: posting is four Redis commands and a publish, and
receiving is the WebSocket that is already open.

This is the one place where the socket carries *data* rather than a
notification (concept §13). The rule earns its exception here: refetching the
rendered room because somebody typed "lol" would cost a database round trip and
three DOM swaps per message, and unlike queue state a chat line has no
authoritative version to disagree with — a client that misses one is missing a
line, not showing a lie. Reconnecting listeners refill from ``recent`` and are
whole again.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.avatars import avatar_seed
from app.models import Listener

# What a room remembers. Enough that somebody arriving mid-conversation can see
# what is being talked about, short enough to stay a few kilobytes.
HISTORY_LIMIT = 60

# Chat is a room's liveliest signal, so it keeps its own history alive for a
# while after the last word — but never past the room.
HISTORY_TTL_S = 12 * 3600

MESSAGE_MAX_LENGTH = 300


def key(room_id: uuid.UUID | str) -> str:
    return f"streamchen:chat:{room_id}"


def clean_message(value: str | None) -> str:
    """The text as it will be shown, or "" if it amounts to nothing.

    Folded onto one line and stripped of unprintables for the same reason
    display names are (see ``service.clean_display_name``): a message is
    rendered in a fixed row, and a wall of newlines or zero-width padding is a
    way to take that row over.
    """
    words = ("".join(char for char in word if char.isprintable()) for word in (value or "").split())
    return " ".join(word for word in words if word)[:MESSAGE_MAX_LENGTH]


def message_for(listener: Listener, text: str) -> dict:
    """One chat line, in the shape both the socket and the history use."""
    return {
        "id": uuid.uuid4().hex,
        "name": listener.display_name,
        "avatar": avatar_seed(listener.id),
        "text": text,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def store(redis: Redis, room_id: uuid.UUID | str, message: dict) -> None:
    """Keep the message, and only the last ``HISTORY_LIMIT`` of them."""
    room_key = key(room_id)
    pipe = redis.pipeline()
    pipe.lpush(room_key, json.dumps(message))
    pipe.ltrim(room_key, 0, HISTORY_LIMIT - 1)
    pipe.expire(room_key, HISTORY_TTL_S)
    await pipe.execute()


async def recent(redis: Redis, room_id: uuid.UUID | str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """What has been said lately, oldest first."""
    raw = await redis.lrange(key(room_id), 0, max(0, limit - 1))

    messages: list[dict] = []
    for entry in reversed(raw):
        if isinstance(entry, bytes):
            entry = entry.decode("utf-8")
        try:
            messages.append(json.loads(entry))
        except json.JSONDecodeError:
            continue  # not ours, or not any more
    return messages


async def clear(redis: Redis, room_id: uuid.UUID | str) -> None:
    await redis.delete(key(room_id))


__all__ = [
    "HISTORY_LIMIT",
    "HISTORY_TTL_S",
    "MESSAGE_MAX_LENGTH",
    "clean_message",
    "clear",
    "key",
    "message_for",
    "recent",
    "store",
]
