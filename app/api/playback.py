"""Where the stream currently is, according to the worker.

The worker is the only writer of playback state; the API reads it. Position is
derived from the start timestamp rather than pushed every second, so a client
that reconnects lands at the right place without waiting for the next tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import news
from app.events import get_now_playing
from app.models import Listener
from app.schemas import BulletinOut, NowPlaying
from app.service import current_track, now_playing

# How long past its own length a bulletin may still claim to be on air. The
# worker overwrites the key on the next transition, but a worker that was
# stopped mid-bulletin leaves it behind, and a stale key must not leave a room
# looking like it is listening to yesterday's news.
BULLETIN_SLACK_S = 30


def _elapsed(state: dict) -> float | None:
    started_at = state.get("started_at")
    if not started_at:
        return None
    try:
        started = float(started_at)
    except (TypeError, ValueError):
        return None
    return max(0.0, round(datetime.now(UTC).timestamp() - started, 2))


async def playback_position(redis: Redis, room_id: uuid.UUID, track_id: uuid.UUID | None) -> float:
    """Seconds into the currently playing track, or 0 if we cannot tell."""
    if track_id is None:
        return 0.0

    state = await get_now_playing(redis, room_id)
    if not state or state.get("track_id") != str(track_id):
        return 0.0

    return _elapsed(state) or 0.0


async def bulletin_on_air(redis: Redis, room_id: uuid.UUID) -> tuple[BulletinOut, float] | None:
    """The news bulletin the worker says is playing, and how far into it.

    Only ever asked when the database has no playing track: a bulletin goes out
    between two tracks, so the two can never both be true.
    """
    state = await get_now_playing(redis, room_id)
    if not state or state.get("kind") != news.KIND:
        return None

    elapsed = _elapsed(state)
    if elapsed is None:
        return None

    duration = int(state.get("duration_s") or 0)
    if duration and elapsed > duration + BULLETIN_SLACK_S:
        return None  # left behind by a worker that stopped mid-bulletin

    return (
        BulletinOut(
            title=str(state.get("title") or "News"),
            source=str(state.get("source") or ""),
            duration_s=duration,
        ),
        elapsed,
    )


async def now_playing_state(
    db: AsyncSession,
    redis: Redis,
    room_id: uuid.UUID,
    viewer: Listener | None,
) -> NowPlaying:
    """What the room is hearing: a track, a news bulletin, or nothing.

    One implementation, because the room snapshot and the rendered page must
    never disagree about what is on air.
    """
    track = await current_track(db, room_id)
    if track is not None:
        position = await playback_position(redis, room_id, track.id)
        return now_playing(track, viewer, position)

    on_air = await bulletin_on_air(redis, room_id)
    if on_air is None:
        return now_playing(None, viewer, 0.0)

    bulletin, position = on_air
    return NowPlaying(track=None, bulletin=bulletin, position_s=position)
