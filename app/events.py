"""Realtime events (concept §13) and presence.

Redis is the bus, not the truth: every event is a *hint* that something in
Postgres changed. Clients that miss one (or connect late) refetch the room
snapshot and are correct again, which is what lets Redis be cleared at any
time.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from redis.asyncio import Redis

# --- Event names ------------------------------------------------------------
QUEUE_CHANGED = "QUEUE_CHANGED"
SONG_ADDED = "SONG_ADDED"
SONG_REMOVED = "SONG_REMOVED"
SONG_STARTED = "SONG_STARTED"
SONG_SKIPPED = "SONG_SKIPPED"
VOTE_CHANGED = "VOTE_CHANGED"
LISTENER_JOINED = "LISTENER_JOINED"
LISTENER_LEFT = "LISTENER_LEFT"
ROOM_UPDATED = "ROOM_UPDATED"
PLAYBACK_POSITION = "PLAYBACK_POSITION"

PRESENCE_TTL_S = 60

# Not a room event: every worker listens here, and the message only means "look
# for rooms to play now" (see ``request_worker``).
WAKE_CHANNEL = "streamchen:worker-wake"


def channel(room_id: uuid.UUID | str) -> str:
    return f"streamchen:room:{room_id}:events"


def presence_key(room_id: uuid.UUID | str, session_id: str) -> str:
    return f"streamchen:presence:{room_id}:{session_id}"


def presence_pattern(room_id: uuid.UUID | str) -> str:
    return f"streamchen:presence:{room_id}:*"


def nowplaying_key(room_id: uuid.UUID | str) -> str:
    return f"streamchen:nowplaying:{room_id}"


def worker_lock_key(room_id: uuid.UUID | str) -> str:
    return f"streamchen:worker-lock:{room_id}"


async def publish(redis: Redis, room_id: uuid.UUID | str, event: str, payload: Any = None) -> None:
    """Fire and forget. A room with no subscribers is the normal case."""
    message = json.dumps({"type": event, "data": payload or {}}, default=str)
    await redis.publish(channel(room_id), message)


async def request_worker(redis: Redis, room_id: uuid.UUID | str) -> None:
    """Ask a worker to take this room up now rather than on its next sweep.

    Only a nudge: the sweep finds the room anyway, and a worker that misses the
    message is late by one interval and no more. What it buys is that the first
    person to open a new room finds something already connected to the mount,
    instead of pressing play into a 404.
    """
    await redis.publish(WAKE_CHANNEL, str(room_id))


async def mark_present(redis: Redis, room_id: uuid.UUID | str, session_id: str) -> bool:
    """Refresh a listener's presence. Returns True if this was a new arrival."""
    key = presence_key(room_id, session_id)
    was_absent = not await redis.exists(key)
    await redis.set(key, "1", ex=PRESENCE_TTL_S)
    return was_absent


async def mark_absent(redis: Redis, room_id: uuid.UUID | str, session_id: str) -> None:
    await redis.delete(presence_key(room_id, session_id))


async def count_present(redis: Redis, room_id: uuid.UUID | str) -> int:
    total = 0
    async for _ in redis.scan_iter(match=presence_pattern(room_id), count=200):
        total += 1
    return total


async def set_now_playing(redis: Redis, room_id: uuid.UUID | str, payload: dict | None) -> None:
    key = nowplaying_key(room_id)
    if payload is None:
        await redis.delete(key)
        return
    # Outlives any single track so a late joiner still learns what is playing;
    # the worker overwrites it on every transition.
    await redis.set(key, json.dumps(payload, default=str), ex=3600)


async def get_now_playing(redis: Redis, room_id: uuid.UUID | str) -> dict | None:
    raw = await redis.get(nowplaying_key(room_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
