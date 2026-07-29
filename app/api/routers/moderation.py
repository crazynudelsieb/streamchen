"""Host controls: skip, reorder, ban, lock (concept §7)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.api.deps import get_db, get_redis, require_host
from app.models import STATE_QUEUED, STATE_SKIPPED, Ban, Listener, Room, Track, utcnow
from app.schemas import BanIn
from app.service import current_track

router = APIRouter(tags=["moderation"])


@router.post("/rooms/{token}/skip", status_code=status.HTTP_202_ACCEPTED)
async def skip(
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Ask the worker to move on.

    The API marks the track skipped; the worker notices its track is no longer
    ``playing`` and tears the pipe down. Playback state stays owned by exactly
    one component (concept §3).
    """
    track = await current_track(db, room.id)
    if track is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing is playing")

    track.state = STATE_SKIPPED
    track.ended_at = utcnow()
    await db.flush()

    await events.publish(redis, room.id, events.SONG_SKIPPED, {"track_id": str(track.id)})
    await events.publish(redis, room.id, events.QUEUE_CHANGED, {})
    return {"skipped": str(track.id)}


@router.post("/rooms/{token}/tracks/{track_id}/promote", status_code=status.HTTP_202_ACCEPTED)
async def promote(
    track_id: uuid.UUID,
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Move a track to the front, over the fairness rules.

    Ordering is derived rather than stored, so the reorder is expressed as a
    priority above every other pending track instead of as an index that would
    have to be rewritten on every insert.
    """
    result = await db.execute(
        select(Track).where(
            Track.id == track_id, Track.room_id == room.id, Track.state == STATE_QUEUED
        )
    )
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found in the queue")

    highest = await db.execute(
        select(func.max(Track.priority)).where(
            Track.room_id == room.id, Track.state == STATE_QUEUED
        )
    )
    track.priority = int(highest.scalar_one() or 0) + 1
    await db.flush()

    await events.publish(redis, room.id, events.QUEUE_CHANGED, {"promoted": str(track_id)})
    return {"promoted": str(track_id)}


@router.post("/rooms/{token}/bans", status_code=status.HTTP_201_CREATED)
async def ban(
    payload: BanIn,
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Remove a listener and everything they have waiting."""
    result = await db.execute(
        select(Listener).where(Listener.id == payload.listener_id, Listener.room_id == room.id)
    )
    listener = result.scalar_one_or_none()
    if listener is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Listener not found")
    if listener.is_host:
        raise HTTPException(status.HTTP_409_CONFLICT, "The host cannot be banned")

    existing = await db.execute(
        select(Ban).where(Ban.room_id == room.id, Ban.session_id == listener.session_id)
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            Ban(room_id=room.id, session_id=listener.session_id, reason=payload.reason)
        )

    pending = await db.execute(
        select(Track).where(
            Track.room_id == room.id,
            Track.added_by_id == listener.id,
            Track.state == STATE_QUEUED,
        )
    )
    for track in pending.scalars().all():
        await db.delete(track)

    await events.mark_absent(redis, room.id, listener.session_id)
    await db.delete(listener)
    await db.flush()

    await events.publish(redis, room.id, events.LISTENER_LEFT, {"banned": True})
    await events.publish(redis, room.id, events.QUEUE_CHANGED, {})
    return {"banned": str(payload.listener_id)}


@router.delete("/rooms/{token}/bans/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unban(
    session_id: str,
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Ban).where(Ban.room_id == room.id, Ban.session_id == session_id)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
