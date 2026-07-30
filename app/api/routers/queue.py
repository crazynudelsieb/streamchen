"""Submitting, removing and voting on tracks."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import events
from app.api.deps import (
    current_listener,
    get_db,
    get_redis,
    get_settings_dep,
    has_host_secret,
    require_room,
)
from app.api.guards import spam_guard
from app.config import Settings
from app.models import STATE_QUEUED, Listener, Room, Track, utcnow
from app.ratelimit import Limit, check, clear_strikes
from app.schemas import SearchResult, TrackAdd, TrackOut, VoteIn
from app.service import (
    is_duplicate,
    pending_count_for,
    queued_youtube_ids,
    serialize_track,
    set_vote,
)
from app.youtube import (
    YouTubeError,
    fetch_metadata,
    normalize_query,
    parse_youtube_id,
    search_music,
)

router = APIRouter(tags=["queue"])

# Starlette renamed its 422 constant mid-1.x; the number never moved.
HTTP_422_UNPROCESSABLE = 422


@router.get("/rooms/{token}/search", response_model=list[SearchResult])
async def search(
    q: str,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> list[SearchResult]:
    """Find songs by name, so adding one does not require a link.

    Results are annotated with the two reasons adding them would fail, because
    a listener should not have to submit a track to discover it is nine
    minutes long or already in the queue.
    """
    query = normalize_query(q)
    if len(query) < 2:
        return []

    verdict = await check(
        redis,
        "search",
        str(listener.id),
        Limit(settings.search_rate_limit, settings.search_rate_window_s),
    )
    if not verdict.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Slow down a moment.",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )

    try:
        matches = await search_music(
            redis,
            query,
            limit=settings.search_results,
            metadata_ttl_s=settings.metadata_cache_ttl_s,
            # Not a filter: a hit the room would refuse is still shown, just
            # never above one it would accept.
            max_duration_s=settings.max_track_duration_s,
        )
    except YouTubeError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc

    taken = await queued_youtube_ids(db, room.id)
    return [
        SearchResult(
            youtube_id=match.youtube_id,
            title=match.title,
            duration_s=match.duration_s,
            thumbnail_url=match.thumbnail_url,
            channel=match.channel,
            too_long=bool(match.duration_s)
            and match.duration_s > settings.max_track_duration_s,
            queued=match.youtube_id in taken,
        )
        for match in matches
    ]


@router.post("/rooms/{token}/tracks", response_model=TrackOut, status_code=status.HTTP_201_CREATED)
async def add_track(
    request: Request,
    payload: TrackAdd,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TrackOut:
    is_host = listener.is_host or has_host_secret(request, room)

    if room.queue_locked and not is_host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The host locked the queue")

    if not is_host:
        await spam_guard(
            db,
            redis,
            settings,
            listener,
            "add",
            Limit(settings.add_rate_limit, settings.add_rate_window_s),
        )

    youtube_id = parse_youtube_id(payload.url)
    if youtube_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a YouTube link")

    pending = await pending_count_for(db, room.id, listener.id)
    if not is_host and pending >= room.max_pending_per_listener:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You already have {pending} songs waiting. Wait for one to play.",
        )

    if await is_duplicate(db, room.id, youtube_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "That song is already in the queue")

    try:
        metadata = await fetch_metadata(redis, youtube_id, settings.metadata_cache_ttl_s)
    except YouTubeError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc

    if metadata.duration_s and metadata.duration_s > settings.max_track_duration_s:
        limit_min = settings.max_track_duration_s // 60
        raise HTTPException(
            HTTP_422_UNPROCESSABLE,
            f"Tracks over {limit_min} minutes are not accepted",
        )

    # A shadow-banned listener's track is stored and shown back to them, and
    # is invisible to everyone else including the worker (concept §10).
    shadowed = listener.is_shadow_banned()

    track = Track(
        room_id=room.id,
        youtube_id=metadata.youtube_id,
        title=metadata.title,
        duration_s=metadata.duration_s,
        thumbnail_url=metadata.thumbnail_url,
        channel=metadata.channel,
        added_by_id=listener.id,
        state=STATE_QUEUED,
        shadow=shadowed,
    )
    db.add(track)
    room.last_active_at = utcnow()
    await db.flush()
    await db.refresh(track, attribute_names=["votes", "added_by"])

    await clear_strikes(redis, f"add:{listener.id}")

    if not shadowed:
        await events.publish(
            redis,
            room.id,
            events.SONG_ADDED,
            {"track_id": str(track.id), "title": track.title, "by": listener.display_name},
        )
        await events.publish(redis, room.id, events.QUEUE_CHANGED, {})
        # The first song in a room that had gone quiet: no point making it wait
        # for a sweep before anything is playing it.
        await events.request_worker(redis, room.id)

    return serialize_track(track, listener)


async def _load_track(db: AsyncSession, room: Room, track_id: uuid.UUID) -> Track:
    result = await db.execute(
        select(Track)
        .where(Track.id == track_id, Track.room_id == room.id)
        .options(selectinload(Track.votes), selectinload(Track.added_by))
    )
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found")
    return track


@router.delete("/rooms/{token}/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_track(
    request: Request,
    track_id: uuid.UUID,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """Hosts remove anything; listeners may withdraw their own pending track."""
    track = await _load_track(db, room, track_id)
    is_host = listener.is_host or has_host_secret(request, room)

    if not is_host and (track.added_by_id != listener.id or track.state != STATE_QUEUED):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not yours to remove")

    was_shadowed = track.shadow
    await db.delete(track)
    await db.flush()

    if not was_shadowed:
        await events.publish(redis, room.id, events.SONG_REMOVED, {"track_id": str(track_id)})
        await events.publish(redis, room.id, events.QUEUE_CHANGED, {})


@router.post("/rooms/{token}/tracks/{track_id}/vote", response_model=TrackOut)
async def vote(
    payload: VoteIn,
    track_id: uuid.UUID,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TrackOut:
    """One vote per listener; sending a new value overwrites the old one."""
    if not room.voting_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Voting is disabled in this room")

    await spam_guard(
        db,
        redis,
        settings,
        listener,
        "vote",
        Limit(settings.vote_rate_limit, settings.vote_rate_window_s),
    )

    track = await _load_track(db, room, track_id)
    score = await set_vote(db, track, listener, payload.value)

    if not track.shadow:
        await events.publish(
            redis,
            room.id,
            events.VOTE_CHANGED,
            {"track_id": str(track.id), "score": score},
        )
        await events.publish(redis, room.id, events.QUEUE_CHANGED, {})

    return serialize_track(track, listener)
