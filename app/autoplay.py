"""Autoplay — keeping the stream alive when nobody is requesting anything.

A room with an empty queue currently broadcasts silence. That is correct but
dull: the point of a radio is that it keeps playing. So when the queue runs
dry and somebody is still listening, the worker queues one track by itself.

Where that track comes from, in order:

* the host's fallback playlist, if they set one — a playlist URL or a list of
  links, treated as a pool and drawn from at random so it does not replay in
  the same order every time;
* otherwise YouTube's radio mix for whatever played last, which is what makes
  the picks *match* the room instead of being arbitrary.

Two rules keep this from running away:

* nothing is queued unless a listener is present, so an abandoned room drains
  and goes idle rather than streaming to nobody forever;
* only one autoplay track is ever pending, at a priority below every real
  request, so a listener adding a song always wins.
"""

from __future__ import annotations

import logging
import random
import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import Settings
from app.models import STATE_QUEUED, Room, Track
from app.service import queued_youtube_ids, radio_listener, recent_tracks
from app.youtube import (
    YouTubeError,
    fetch_metadata,
    parse_youtube_id,
    playlist_ids,
    radio_for,
)

logger = logging.getLogger(__name__)

# Autoplay sits below every real request. order_queue sorts on -priority, so a
# negative priority is always last however the fairness rules shake out.
AUTOPLAY_PRIORITY = -1

# How far back to look before repeating ourselves.
HISTORY_WINDOW = 40

# Candidates to consider before giving up. A mix can be mostly things the room
# just heard; there is no point hydrating all of it to find that out.
MAX_CANDIDATES = 12


def parse_playlist_field(value: str | None) -> tuple[list[str], list[str]]:
    """Split a host's fallback playlist into (playlist URLs, video ids).

    Accepts what a host would plausibly paste: one playlist link, a pile of
    video links, bare ids, separated by newlines, commas or spaces.
    """
    urls: list[str] = []
    ids: list[str] = []

    for chunk in (value or "").replace(",", " ").split():
        # A playlist link resolves to many ids, so it is kept whole; anything
        # else has to name a single video or it is not usable.
        if "list=" in chunk or "/playlist" in chunk:
            if chunk not in urls:
                urls.append(chunk)
            continue
        youtube_id = parse_youtube_id(chunk)
        if youtube_id and youtube_id not in ids:
            ids.append(youtube_id)

    return urls, ids


async def _fallback_pool(redis: Redis, room: Room) -> list[str]:
    """The host's playlist, shuffled — a pool, not a running order."""
    urls, ids = parse_playlist_field(room.fallback_playlist)

    pool = list(ids)
    for url in urls:
        try:
            pool.extend(await playlist_ids(redis, url))
        except YouTubeError as exc:
            logger.info("room %s: fallback playlist %s failed (%s)", room.token, url, exc)

    random.shuffle(pool)
    return pool


async def _radio_pool(db: AsyncSession, redis: Redis, room_id: uuid.UUID) -> list[str]:
    """YouTube's mix for the most recent track, i.e. more of the same."""
    history = await recent_tracks(db, room_id, limit=1)
    if not history:
        return []

    try:
        return await radio_for(redis, history[0].youtube_id)
    except YouTubeError as exc:
        logger.info("room %s: radio mix failed (%s)", room_id, exc)
        return []


async def _recent_ids(db: AsyncSession, room_id: uuid.UUID) -> set[str]:
    history = await recent_tracks(db, room_id, limit=HISTORY_WINDOW)
    return {track.youtube_id for track in history}


async def pick(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    room: Room,
) -> str | None:
    """Choose an id worth queueing, or None if there is nothing suitable."""
    pool = await _fallback_pool(redis, room)
    if not pool:
        pool = await _radio_pool(db, redis, room.id)
    if not pool:
        return None

    queued = await queued_youtube_ids(db, room.id)
    recent = await _recent_ids(db, room.id)

    # Queued or playing is a hard exclusion — the add endpoint rejects those as
    # duplicates too. Recently played is only a preference: a ten-track
    # playlist has to be allowed to come round again rather than fall silent.
    fresh = [i for i in pool if i not in queued and i not in recent]
    candidates = fresh or [i for i in pool if i not in queued]
    if not candidates:
        return None

    for youtube_id in candidates[:MAX_CANDIDATES]:
        try:
            metadata = await fetch_metadata(redis, youtube_id, settings.metadata_cache_ttl_s)
        except YouTubeError:
            continue  # dead or blocked; try the next one
        if metadata.duration_s and metadata.duration_s > settings.max_track_duration_s:
            continue
        return youtube_id

    return None


async def top_up(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    room_id: uuid.UUID,
) -> Track | None:
    """Queue one autoplay track if the room needs one. Commits on success."""
    room = (await db.execute(select(Room).where(Room.id == room_id))).scalar_one_or_none()
    if room is None:
        return None

    # Streaming to an empty room is the one thing this must not do.
    if await events.count_present(redis, room_id) <= 0:
        return None

    pending = await db.execute(
        select(Track.id).where(Track.room_id == room_id, Track.state == STATE_QUEUED).limit(1)
    )
    if pending.first() is not None:
        return None

    youtube_id = await pick(db, redis, settings, room)
    if youtube_id is None:
        return None

    try:
        metadata = await fetch_metadata(redis, youtube_id, settings.metadata_cache_ttl_s)
    except YouTubeError as exc:
        logger.info("room %s: autoplay pick %s failed (%s)", room.token, youtube_id, exc)
        return None

    listener = await radio_listener(db, room)
    track = Track(
        room_id=room_id,
        youtube_id=metadata.youtube_id,
        title=metadata.title,
        duration_s=metadata.duration_s,
        thumbnail_url=metadata.thumbnail_url,
        channel=metadata.channel,
        added_by_id=listener.id,
        state=STATE_QUEUED,
        priority=AUTOPLAY_PRIORITY,
    )
    db.add(track)
    await db.commit()

    logger.info("room %s: autoplay queued %s", room.token, metadata.title)
    await events.publish(redis, room_id, events.QUEUE_CHANGED, {})
    return track
