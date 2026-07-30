"""Room service layer — the business logic the API exposes and the worker reuses.

Kept out of the routers so that "what the queue looks like" has exactly one
implementation, shared by the HTTP handlers, the WebSocket snapshot and the
playback worker.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import events
from app.avatars import avatar_seed, listener_seed, new_seed
from app.config import Settings
from app.models import (
    FINISHED_STATES,
    STATE_PLAYED,
    STATE_PLAYING,
    STATE_QUEUED,
    STATE_SKIPPED,
    Ban,
    Listener,
    Room,
    Track,
    Vote,
    utcnow,
)
from app.scheduling import Candidate, order_queue
from app.schemas import ListenerInfo, NowPlaying, RoomSettings, TrackOut
from app.security import hash_secret, new_host_secret, new_room_token

HISTORY_LIMIT = 20

# Autoplay's stand-in submitter. Real session ids are uuid4 strings, so this
# can never collide with one, and no browser can ever present it.
RADIO_SESSION_ID = "radio"
RADIO_DISPLAY_NAME = "radio"

# Guest names. Anonymous but not indistinguishable -- a queue full of
# "listener-3f2a" is unreadable, and asking for a name would be an account.
_ADJECTIVES = (
    "amber", "brisk", "calm", "dusky", "eager", "faint", "glossy", "hazy",
    "ivory", "jolly", "keen", "lucid", "mellow", "noble", "opal", "prism",
    "quiet", "rapid", "silver", "tidal", "umber", "vivid", "warm", "zesty",
)
_NOUNS = (
    "arc", "bass", "chord", "drift", "echo", "flux", "groove", "hum",
    "iris", "jam", "kick", "loop", "moon", "note", "orbit", "pulse",
    "reed", "swell", "tone", "verse", "wave", "yarn", "zenith", "riff",
)


def generate_display_name(seed: uuid.UUID | None = None) -> str:
    value = (seed or uuid.uuid4()).int
    return f"{_ADJECTIVES[value % len(_ADJECTIVES)]}-{_NOUNS[(value // 97) % len(_NOUNS)]}"


# Matches Listener.display_name.
DISPLAY_NAME_MAX = 40


def clean_display_name(value: str | None) -> str:
    """A name a listener actually chose, or "" if what they sent amounts to
    nothing.

    Odd characters are cleaned up rather than rejected: a name arriving with a
    stray line break is a paste, not an attack, and the queue has to render it
    on one line either way. Whitespace is folded *before* unprintables are
    dropped, so a line break separates two words instead of welding them
    together, and zero-width characters cannot smuggle in an invisible name.
    """
    words = ("".join(char for char in word if char.isprintable()) for word in (value or "").split())
    return " ".join(word for word in words if word)[:DISPLAY_NAME_MAX].strip()


# --- Lookups ----------------------------------------------------------------
async def get_room(db: AsyncSession, token: str) -> Room | None:
    result = await db.execute(select(Room).where(Room.token == token))
    return result.scalar_one_or_none()


async def create_room(db: AsyncSession, settings: Settings, name: str) -> tuple[Room, str]:
    """Returns the room and the plaintext host secret — the only time it exists."""
    host_secret = new_host_secret()
    room = Room(
        token=new_room_token(),
        name=(name or "").strip()[: settings.room_name_max_length] or "streamchen radio",
        host_secret_hash=hash_secret(host_secret),
        max_listeners=settings.default_max_listeners,
        max_pending_per_listener=settings.default_max_pending_per_listener,
    )
    db.add(room)
    await db.flush()
    return room, host_secret


async def is_banned(db: AsyncSession, room_id: uuid.UUID, session_id: str) -> bool:
    result = await db.execute(
        select(Ban.id).where(Ban.room_id == room_id, Ban.session_id == session_id)
    )
    return result.first() is not None


async def get_listener(db: AsyncSession, room_id: uuid.UUID, session_id: str) -> Listener | None:
    result = await db.execute(
        select(Listener).where(Listener.room_id == room_id, Listener.session_id == session_id)
    )
    return result.scalar_one_or_none()


async def count_listeners(db: AsyncSession, room_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count(Listener.id)).where(
            Listener.room_id == room_id, Listener.session_id != RADIO_SESSION_ID
        )
    )
    return int(result.scalar_one())


async def online_listeners(db: AsyncSession, redis: Redis, room_id: uuid.UUID) -> list[Listener]:
    """Who is in the room right now, in join order.

    A listener needs both halves: a row in Postgres and live presence in Redis.
    Either on its own lies. Rows alone count everybody who ever opened the link,
    and presence alone can outlive the row it belonged to -- a removed listener
    whose socket is still open keeps refreshing a key nobody owns any more. This
    is the one answer the header count and the listener list are both built from,
    so the two can never disagree.
    """
    present = await events.present_sessions(redis, room_id)
    present.discard(RADIO_SESSION_ID)
    if not present:
        return []

    result = await db.execute(
        select(Listener)
        .where(Listener.room_id == room_id, Listener.session_id.in_(present))
        .order_by(Listener.created_at)
    )
    return list(result.scalars().all())


async def radio_listener(db: AsyncSession, room: Room) -> Listener:
    """The room's stand-in submitter for autoplay picks.

    Autoplay needs an author because every track has one, but it is not a
    participant: it holds no session, is filtered out of the listener count and
    the host's listener list, and exists only so a radio pick can be shown and
    attributed like any other track. Created on first use.
    """
    listener = await get_listener(db, room.id, RADIO_SESSION_ID)
    if listener is not None:
        return listener

    listener = Listener(
        room_id=room.id,
        session_id=RADIO_SESSION_ID,
        display_name=RADIO_DISPLAY_NAME,
    )
    db.add(listener)
    await db.flush()
    return listener


async def join_room(
    db: AsyncSession,
    room: Room,
    session_id: str,
    *,
    as_host: bool = False,
) -> Listener:
    """Idempotent: the same session rejoining is the same listener."""
    listener = await get_listener(db, room.id, session_id)
    if listener is not None:
        listener.last_seen_at = utcnow()
        # Someone with the room open is reason enough to keep it alive: it is
        # what stops the worker letting go of a room whose listeners are just
        # listening, and what lets autoplay carry a quiet room.
        room.last_active_at = listener.last_seen_at
        if as_host:
            listener.is_host = True
        return listener

    listener = Listener(
        room_id=room.id,
        session_id=session_id,
        display_name=generate_display_name(),
        is_host=as_host,
    )
    db.add(listener)
    room.last_active_at = utcnow()
    await db.flush()
    return listener


async def rename_listener(db: AsyncSession, listener: Listener, name: str | None) -> Listener:
    """Let a listener pick their own name in this room.

    Blank means "give me a different one", which is what keeps the generated
    names reachable after a rename rather than making the first edit final. It
    is still not an account: the name lives on the listener row for this room
    only, and identity remains the session cookie (concept §14).
    """
    listener.display_name = clean_display_name(name) or generate_display_name()
    listener.last_seen_at = utcnow()
    await db.flush()
    return listener


async def reroll_avatar(db: AsyncSession, listener: Listener) -> Listener:
    """Give a listener a different cat.

    The one they start with is drawn from their id and cannot be changed by
    changing anything about themselves, so wanting another one has to be a thing
    they can ask for. Stored on the listener row, like the name: it belongs to
    this room and travels no further (concept §14).
    """
    listener.chosen_avatar = new_seed(unlike=listener_seed(listener))
    listener.last_seen_at = utcnow()
    await db.flush()
    return listener


# --- Queue ------------------------------------------------------------------
def _track_query(room_id: uuid.UUID):
    return (
        select(Track)
        .where(Track.room_id == room_id)
        .options(selectinload(Track.votes), selectinload(Track.added_by))
    )


async def queued_tracks(
    db: AsyncSession, room_id: uuid.UUID, *, include_shadow_for: uuid.UUID | None = None
) -> list[Track]:
    """Pending tracks in play order.

    ``include_shadow_for`` folds a shadow-banned listener's own submissions
    back into their view, so nothing tells them they have been muted.
    """
    result = await db.execute(
        _track_query(room_id).where(Track.state == STATE_QUEUED).order_by(Track.created_at)
    )
    tracks = list(result.scalars().all())

    visible = [
        track
        for track in tracks
        if not track.shadow or track.added_by_id == include_shadow_for
    ]

    by_id = {str(track.id): track for track in visible}
    ordered = order_queue(
        [
            Candidate(
                id=str(track.id),
                added_by=str(track.added_by_id),
                score=track.score,
                created_at=track.created_at,
                priority=track.priority,
            )
            for track in visible
        ]
    )
    return [by_id[candidate.id] for candidate in ordered]


async def playable_tracks(db: AsyncSession, room_id: uuid.UUID) -> list[Track]:
    """What the worker may play: never a shadowed submission."""
    return await queued_tracks(db, room_id, include_shadow_for=None)


async def current_track(db: AsyncSession, room_id: uuid.UUID) -> Track | None:
    result = await db.execute(
        _track_query(room_id).where(Track.state == STATE_PLAYING).order_by(Track.started_at.desc())
    )
    return result.scalars().first()


async def recent_tracks(db: AsyncSession, room_id: uuid.UUID, limit: int = HISTORY_LIMIT):
    result = await db.execute(
        _track_query(room_id)
        .where(Track.state.in_((STATE_PLAYED, STATE_SKIPPED)))
        .order_by(Track.ended_at.desc().nullslast(), Track.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def pending_count_for(db: AsyncSession, room_id: uuid.UUID, listener_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count(Track.id)).where(
            Track.room_id == room_id,
            Track.added_by_id == listener_id,
            Track.state == STATE_QUEUED,
        )
    )
    return int(result.scalar_one())


async def is_duplicate(db: AsyncSession, room_id: uuid.UUID, youtube_id: str) -> bool:
    """Reject an id that is already queued or currently playing (concept §8)."""
    result = await db.execute(
        select(Track.id).where(
            Track.room_id == room_id,
            Track.youtube_id == youtube_id,
            Track.state.in_((STATE_QUEUED, STATE_PLAYING)),
        )
    )
    return result.first() is not None


async def queued_youtube_ids(db: AsyncSession, room_id: uuid.UUID) -> set[str]:
    """Every id the duplicate check would reject right now."""
    result = await db.execute(
        select(Track.youtube_id).where(
            Track.room_id == room_id,
            Track.state.in_((STATE_QUEUED, STATE_PLAYING)),
        )
    )
    return set(result.scalars().all())


async def set_vote(
    db: AsyncSession, track: Track, listener: Listener, value: int
) -> int:
    """Apply a vote and return the track's new score. 0 withdraws it."""
    existing = next((v for v in track.votes if v.listener_id == listener.id), None)

    if value == 0:
        if existing is not None:
            track.votes.remove(existing)
            await db.delete(existing)
    elif existing is not None:
        existing.value = value
    else:
        vote = Vote(track_id=track.id, listener_id=listener.id, value=value)
        db.add(vote)
        track.votes.append(vote)

    await db.flush()
    return track.score


async def prune_idle_rooms(db: AsyncSession, settings: Settings) -> int:
    """Drop rooms nobody has touched in a while. Rooms are disposable by
    design and this is the only cleanup the system needs."""
    cutoff = utcnow() - timedelta(days=settings.room_idle_days)
    result = await db.execute(select(Room).where(Room.last_active_at < cutoff))
    rooms = list(result.scalars().all())
    for room in rooms:
        await db.delete(room)
    return len(rooms)


# --- Serialization ----------------------------------------------------------
def serialize_track(track: Track, viewer: Listener | None = None) -> TrackOut:
    my_vote = 0
    if viewer is not None:
        my_vote = next((v.value for v in track.votes if v.listener_id == viewer.id), 0)

    is_radio = track.added_by is not None and track.added_by.session_id == RADIO_SESSION_ID

    return TrackOut(
        id=track.id,
        youtube_id=track.youtube_id,
        title=track.title,
        duration_s=track.duration_s,
        thumbnail_url=track.thumbnail_url,
        channel=track.channel,
        state=track.state,
        score=track.score,
        upvotes=track.upvotes,
        downvotes=track.downvotes,
        added_by=track.added_by.display_name if track.added_by else "guest",
        added_by_id=track.added_by_id,
        added_by_avatar=(
            listener_seed(track.added_by) if track.added_by else avatar_seed(track.added_by_id)
        ),
        mine=viewer is not None and track.added_by_id == viewer.id,
        my_vote=my_vote,
        radio=is_radio,
        shadowed=track.shadow,
        created_at=track.created_at,
        started_at=track.started_at,
    )


def room_settings(room: Room) -> RoomSettings:
    return RoomSettings(
        voting_enabled=room.voting_enabled,
        queue_locked=room.queue_locked,
        stream_stopped=room.stream_stopped,
        chat_enabled=room.chat_enabled,
        max_pending_per_listener=room.max_pending_per_listener,
        max_listeners=room.max_listeners,
        fallback_playlist=room.fallback_playlist,
        news_enabled=room.news_enabled,
        news_interval_min=room.news_interval_min,
    )


def listener_info(listener: Listener) -> ListenerInfo:
    return ListenerInfo(
        id=listener.id,
        display_name=listener.display_name,
        avatar=listener_seed(listener),
        is_host=listener.is_host,
    )


def stream_url(settings: Settings, room: Room) -> str:
    return f"{settings.stream_base_url}{room.mount}"


def now_playing(track: Track | None, viewer: Listener | None, position_s: float) -> NowPlaying:
    if track is None:
        return NowPlaying(track=None, position_s=0.0, started_at=None)
    return NowPlaying(
        track=serialize_track(track, viewer),
        position_s=position_s,
        started_at=track.started_at,
    )


__all__ = [
    "DISPLAY_NAME_MAX",
    "FINISHED_STATES",
    "RADIO_DISPLAY_NAME",
    "RADIO_SESSION_ID",
    "clean_display_name",
    "count_listeners",
    "create_room",
    "current_track",
    "generate_display_name",
    "get_listener",
    "get_room",
    "is_banned",
    "is_duplicate",
    "join_room",
    "listener_info",
    "now_playing",
    "online_listeners",
    "pending_count_for",
    "playable_tracks",
    "prune_idle_rooms",
    "queued_tracks",
    "queued_youtube_ids",
    "radio_listener",
    "recent_tracks",
    "rename_listener",
    "reroll_avatar",
    "room_settings",
    "serialize_track",
    "set_vote",
    "stream_url",
]
