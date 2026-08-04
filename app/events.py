"""Realtime events (concept §13) and presence.

Redis is the bus, not the truth: every event is a *hint* that something in
Postgres changed. Clients that miss one (or connect late) refetch the room
snapshot and are correct again, which is what lets Redis be cleared at any
time.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from redis.asyncio import Redis

# --- Event names ------------------------------------------------------------
QUEUE_CHANGED = "QUEUE_CHANGED"
SONG_ADDED = "SONG_ADDED"
SONG_REMOVED = "SONG_REMOVED"
SONG_STARTED = "SONG_STARTED"
SONG_SKIPPED = "SONG_SKIPPED"
# A news bulletin went on air between two songs (app/news.py). Like the others,
# only a hint: clients refetch the room and find out what is playing.
NEWS_STARTED = "NEWS_STARTED"
VOTE_CHANGED = "VOTE_CHANGED"
LISTENER_JOINED = "LISTENER_JOINED"
LISTENER_LEFT = "LISTENER_LEFT"
ROOM_UPDATED = "ROOM_UPDATED"
PLAYBACK_POSITION = "PLAYBACK_POSITION"
# The one event that carries its payload rather than a hint to refetch; see
# app/chat.py for why chat is allowed to be the exception.
CHAT_MESSAGE = "CHAT_MESSAGE"

PRESENCE_TTL_S = 60

# How long a room keeps its source after the last listener has gone. Presence
# is exact (see ``presence_key``), so "nobody is here" is known the instant the
# last socket closes rather than a TTL later — and taking the encoder down that
# instant would mean a listener who reloads the page comes back to a mount that
# was torn down and rebuilt underneath them. Long enough to ride out a reload,
# short enough that nothing is encoded for an empty room for long.
LAST_LISTENER_GRACE_S = 15

# Not a room event: every worker listens here, and the message only means "look
# for rooms to play now" (see ``request_worker``).
WAKE_CHANNEL = "streamchen:worker-wake"


def channel(room_id: uuid.UUID | str) -> str:
    return f"streamchen:room:{room_id}:events"


def presence_key(room_id: uuid.UUID | str) -> str:
    """One sorted set per room: session id -> when its presence expires.

    A key per listener would be the obvious shape and is the one thing Redis
    cannot answer cheaply: "who is in this room" then means SCAN, which walks
    the whole keyspace however few listeners the room has. Every rendered
    fragment asks that question, and every event makes every client in the room
    ask it at once, so the cost is paid per listener per event — which is
    exactly when the server has least to spare.

    A sorted set answers it in one round trip, and scores the members by their
    own expiry so an abandoned session still ages out of a room nobody is
    refreshing.
    """
    return f"streamchen:presence:v2:{room_id}"


# Rooms with somebody in them, scored by when that stops being true. The
# worker's sweep reads this and nothing else, so "which rooms need a source"
# is one range query rather than a scan per sweep.
LIVE_ROOMS_KEY = "streamchen:live-rooms"

# The live-rooms set is trimmed by every reader, so this is only here to keep an
# instance that stops running workers from leaving the key behind forever.
LIVE_ROOMS_TTL_S = 3600


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


def _text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def mark_present(redis: Redis, room_id: uuid.UUID | str, session_id: str) -> bool:
    """Refresh a listener's presence. Returns True if this was a new arrival."""
    now = time.time()
    key = presence_key(room_id)

    pipe = redis.pipeline(transaction=False)
    # Read before the write, so the answer is about the room as it was when
    # this listener arrived rather than after they had.
    pipe.zscore(key, session_id)
    pipe.zadd(key, {session_id: now + PRESENCE_TTL_S})
    # Whoever is here keeps the set alive; the key can only expire once nobody
    # has refreshed anything in it for a whole TTL.
    pipe.expire(key, PRESENCE_TTL_S)
    # Anybody being here is what makes the room live, and the grace is added on
    # the way *out* rather than here: a room with a listener in it needs a
    # source for as long as they stay, which is what refreshing this says.
    pipe.zadd(LIVE_ROOMS_KEY, {str(room_id): now + PRESENCE_TTL_S})
    pipe.expire(LIVE_ROOMS_KEY, LIVE_ROOMS_TTL_S)
    # Sessions that stopped refreshing. Trimmed on the way past rather than by
    # a sweep of its own: presence is written far more often than it is read.
    pipe.zremrangebyscore(key, "-inf", now)
    previous = (await pipe.execute())[0]

    return previous is None or float(previous) <= now


async def mark_absent(redis: Redis, room_id: uuid.UUID | str, session_id: str) -> None:
    """Drop a listener's presence, and the room's source with the last of them.

    The set says who else is here, so the last one out is known rather than
    waited out. That is the difference between a room that stops encoding a few
    seconds after everyone has gone and one that spends another minute on it.
    """
    now = time.time()
    key = presence_key(room_id)

    pipe = redis.pipeline(transaction=False)
    pipe.zrem(key, session_id)
    pipe.zremrangebyscore(key, "-inf", now)
    pipe.zcard(key)
    remaining = (await pipe.execute())[-1]

    if remaining:
        return

    # Nobody left. Not dropped outright: a listener who reloads the page is
    # briefly the last one out, and taking the encoder down for that would cost
    # them the mount they are coming back to.
    await redis.zadd(LIVE_ROOMS_KEY, {str(room_id): now + LAST_LISTENER_GRACE_S})

    # Somebody may have arrived between the count above and the write, and a
    # room with a listener in it must not be running on the grace meant for an
    # empty one -- their next heartbeat could be a good deal further off than
    # the grace is long. GT, so this can only ever give the room longer.
    if await redis.zcard(key):
        await redis.zadd(
            LIVE_ROOMS_KEY, {str(room_id): time.time() + PRESENCE_TTL_S}, gt=True
        )


async def _trim_presence(redis: Redis, room_id: uuid.UUID | str) -> str:
    """Drop expired sessions and hand back the key worth reading."""
    key = presence_key(room_id)
    await redis.zremrangebyscore(key, "-inf", time.time())
    return key


async def anyone_present(redis: Redis, room_id: uuid.UUID | str) -> bool:
    return await count_present(redis, room_id) > 0


async def is_present(redis: Redis, room_id: uuid.UUID | str, session_id: str) -> bool:
    """Whether one session is here. For a whole roster ask ``present_sessions``
    once instead: this is a round trip per listener."""
    score = await redis.zscore(presence_key(room_id), session_id)
    return score is not None and float(score) > time.time()


async def live_room_ids(redis: Redis) -> set[str]:
    """Every room that should have a source right now.

    Somebody is in it, or somebody was until a moment ago — see
    ``LAST_LISTENER_GRACE_S`` for why the second half is worth having.
    """
    await redis.zremrangebyscore(LIVE_ROOMS_KEY, "-inf", time.time())
    return {_text(member) for member in await redis.zrange(LIVE_ROOMS_KEY, 0, -1)}


async def present_sessions(redis: Redis, room_id: uuid.UUID | str) -> set[str]:
    """The sessions with live presence in this room.

    A session, not a listener: presence is written by whoever holds the socket
    and can outlive the row it belonged to, so anything shown to people pairs
    this with the database rather than trusting it alone (``online_listeners``).
    """
    key = await _trim_presence(redis, room_id)
    return {_text(member) for member in await redis.zrange(key, 0, -1)}


async def count_present(redis: Redis, room_id: uuid.UUID | str) -> int:
    """How many sessions are here. The worker's "is this room empty" question,
    answered without touching the database."""
    key = await _trim_presence(redis, room_id)
    return int(await redis.zcard(key))


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
