"""Where the stream currently is, according to the worker.

The worker is the only writer of playback state; the API reads it. Position is
derived from the start timestamp rather than pushed every second, so a client
that reconnects lands at the right place without waiting for the next tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.events import get_now_playing


async def playback_position(redis: Redis, room_id: uuid.UUID, track_id: uuid.UUID | None) -> float:
    """Seconds into the currently playing track, or 0 if we cannot tell."""
    if track_id is None:
        return 0.0

    state = await get_now_playing(redis, room_id)
    if not state or state.get("track_id") != str(track_id):
        return 0.0

    started_at = state.get("started_at")
    if not started_at:
        return 0.0

    try:
        started = float(started_at)
    except (TypeError, ValueError):
        return 0.0

    elapsed = datetime.now(UTC).timestamp() - started
    return max(0.0, round(elapsed, 2))
