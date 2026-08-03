"""Autoplay — keeping the stream alive when nobody is requesting anything.

A room with an empty queue currently broadcasts silence. That is correct but
dull: the point of a radio is that it keeps playing. So when the queue runs
dry and somebody is still listening, the worker queues one track by itself.

Where that track comes from:

* the host's radio playlist, if they set one. A host who pasted a playlist
  said what the room is *for*, so early on — while there is no history for
  anything else to be based on — it is the whole answer, and it stays the
  majority of the answer afterwards. A YouTube mix counts as one of these: it
  is a coherent pool of a few dozen tracks, which is a playlist in every way
  that matters here, and like a playlist it has an end;
* YouTube's mix for something the room played recently, which is what widens a
  room beyond the list it started from — and, because re-seeding it from what
  the room just heard never runs out, what keeps the room playing for as long
  as somebody is listening. A pool with an end is the baseline; this is the one
  that has to still be there at four in the morning.

Two rules keep this from running away:

* nothing is queued unless a listener is present, so an abandoned room drains
  and goes idle rather than streaming to nobody forever;
* only one autoplay track is ever pending, at a priority below every real
  request, so a listener adding a song always wins.

And three rules keep it from being boring, all of which exist because a YouTube
mix is far more repetitive than it looks. A third of the mix for a Guns N'
Roses song is more Guns N' Roses, most of it in the first handful of entries —
so taking the first unheard entry off the front of the mix, then seeding the
next mix from what that gave you, walks one band's catalogue and calls it a
radio. Hence: a band the room just heard is pushed to the back, the pick is
drawn from whatever ties for best rather than from whatever sorted first, and
the next mix is seeded from any of the last few tracks rather than always the
latest one.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections import Counter

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import Settings
from app.models import STATE_QUEUED, Room, Track
from app.service import queued_youtube_ids, radio_listener, recent_tracks
from app.youtube import (
    SearchCandidate,
    YouTubeError,
    artist_key,
    fetch_metadata,
    parse_playlist_url,
    parse_youtube_id,
    playlist_candidates,
    radio_candidates,
    song_score,
)

logger = logging.getLogger(__name__)

# Autoplay sits below every real request. order_queue sorts on -priority, so a
# negative priority is always last however the fairness rules shake out.
AUTOPLAY_PRIORITY = -1

# How far back to look before repeating ourselves.
HISTORY_WINDOW = 40

# How many of the most recent tracks put their artist on cooldown.
ARTIST_COOLDOWN = 8

# The three penalties that order a pool, deliberately far enough apart to be
# read as tiers rather than as weights: anything not heard yet, then a band the
# room has just had, then an outright replay. A song-likeness score spans a few
# hundred at its worst, so it decides *within* a tier and never across one.
ARTIST_REPEAT_PENALTY = 25.0
ARTIST_REPEAT_CAP = 250.0
COOLDOWN_PENALTY = 1_000.0
REPLAY_PENALTY = 5_000.0

# Candidates to try before giving up. A mix can be mostly things the room just
# heard; there is no point hydrating all of it to find that out.
MAX_CANDIDATES = 12

# How many of the best candidates the pick is drawn from, and how far behind
# the leader still counts as tied with it.
#
# The cap is there for a mix, which is somebody else's list and can be a long
# tail of things nobody would have chosen. A host's playlist is the opposite —
# every entry in it was picked for this room — so drawing from five of it would
# open the room on the same handful of songs every time, and that is not what a
# playlist was pasted in for. See ``pick``.
PICK_FROM_TOP = 5
DRAW_BAND = 40.0

# The host's playlist as a share of the picks. A room with nothing played has
# nothing for a mix to be *about* — a mix seeded from one arbitrary song is how
# a room acquires a genre nobody chose — so the opening songs are the playlist
# and nothing else, and it stays the majority afterwards. The mix is there to
# widen a room, not to take it over.
PLAYLIST_ONLY_UNTIL = 5
PLAYLIST_SETTLES_AFTER = 20
PLAYLIST_SHARE_FLOOR = 0.7

# Seeding every mix from the single last track compounds whatever drift the
# last one introduced. Drawing from the last few keeps it about the room.
RADIO_SEED_DEPTH = 3


def parse_playlist_field(value: str | None) -> tuple[list[str], list[str]]:
    """Split a host's radio playlist into (playlist URLs, video ids).

    Accepts what a host would plausibly paste: one playlist link, a pile of
    video links, bare ids, separated by newlines, commas or spaces. YouTube
    Music albums and playlists count — for a room whose baseline is *records*
    rather than *videos*, that is the catalogue worth pasting from.
    """
    urls: list[str] = []
    ids: list[str] = []

    for chunk in (value or "").replace(",", " ").split():
        # A playlist link resolves to many ids, so it is kept whole; anything
        # else has to name a single video or it is not usable. Playlist first:
        # a link carrying both means the playlist here, where the whole field
        # is a pool to draw from.
        playlist = parse_playlist_url(chunk)
        if playlist:
            if playlist not in urls:
                urls.append(playlist)
            continue
        youtube_id = parse_youtube_id(chunk)
        if youtube_id and youtube_id not in ids:
            ids.append(youtube_id)

    return urls, ids


async def _playlist_pool(redis: Redis, room: Room) -> list[SearchCandidate]:
    """Everything behind the host's radio playlist field."""
    urls, ids = parse_playlist_field(room.fallback_playlist)

    pool = [SearchCandidate(youtube_id=i, title="") for i in ids]
    for url in urls:
        try:
            pool.extend(await playlist_candidates(redis, url))
        except YouTubeError as exc:
            logger.info("room %s: radio playlist %s failed (%s)", room.token, url, exc)

    return pool


async def _mix_pool(redis: Redis, history: list[Track]) -> list[SearchCandidate]:
    """YouTube's mix for something the room played recently.

    Every recent seed is tried, not one of them, because this is the pool with
    no ceiling: a host's playlist runs out of songs eventually and a mix seeded
    from what the room just played does not. One unresolvable track — pulled,
    geo-blocked, gone — is not allowed to be the reason a room falls silent.
    """
    seeds = list(dict.fromkeys(track.youtube_id for track in history[:RADIO_SEED_DEPTH]))
    random.shuffle(seeds)

    for seed in seeds:
        try:
            candidates = await radio_candidates(redis, seed)
        except YouTubeError as exc:
            logger.info("mix for %s failed (%s)", seed, exc)
            continue
        if candidates:
            return candidates

    return []


def playlist_share(played: int) -> float:
    """How often the host's playlist should answer, after ``played`` tracks.

    All of them at the start, easing to :data:`PLAYLIST_SHARE_FLOOR`. The room
    has to sound like the playlist before it is allowed to sound like anything
    else.
    """
    if played <= PLAYLIST_ONLY_UNTIL:
        return 1.0
    if played >= PLAYLIST_SETTLES_AFTER:
        return PLAYLIST_SHARE_FLOOR
    reach = (played - PLAYLIST_ONLY_UNTIL) / (PLAYLIST_SETTLES_AFTER - PLAYLIST_ONLY_UNTIL)
    return 1.0 - (1.0 - PLAYLIST_SHARE_FLOOR) * reach


def rank_pool(
    pool: list[SearchCandidate],
    *,
    excluded_ids: set[str],
    recent_ids: set[str],
    blocked_artists: set[str],
    artist_counts: Counter[str],
    max_duration_s: int,
) -> list[tuple[float, SearchCandidate]]:
    """The pool scored, least-recently-heard and most song-like first.

    Only one thing here is a hard exclusion: an id already queued or playing,
    which the add endpoint would reject as a duplicate anyway. Everything else
    is a penalty steep enough to act like a rule while the pool has anything
    else in it, and to get out of the way when it does not — a ten-song
    playlist has to be allowed to come round again rather than fall silent.
    """
    candidates = [c for c in pool if c.youtube_id not in excluded_ids]
    if not candidates:
        return []

    def rank(candidate: SearchCandidate) -> float:
        score = song_score(candidate, max_duration_s)
        key = artist_key(candidate.title, candidate.channel)
        if key:
            # Every previous outing in the window costs it, so a mix that is a
            # third one band puts that band behind the two thirds that are not.
            score -= min(ARTIST_REPEAT_CAP, artist_counts.get(key, 0) * ARTIST_REPEAT_PENALTY)
            if key in blocked_artists:
                score -= COOLDOWN_PENALTY
        if candidate.youtube_id in recent_ids:
            score -= REPLAY_PENALTY
        return score

    scored = sorted(
        ((rank(c), index, c) for index, c in enumerate(candidates)),
        # Index breaks ties by the order upstream returned them, so an equal
        # score never shuffles between two identical pools.
        key=lambda row: (-row[0], row[1]),
    )
    return [(score, candidate) for score, _index, candidate in scored]


def draw(
    ranked: list[tuple[float, SearchCandidate]], *, top: int = PICK_FROM_TOP
) -> list[SearchCandidate]:
    """The ranking, with the candidates that tie for best shuffled among
    themselves.

    Ranking alone is deterministic, and a mix is stable enough that
    deterministic means the room hears the same song in the same slot every
    time the list comes round. What counts as a tie is a narrow band — narrower
    than the gap between the tiers in :func:`rank_pool` — so this is only ever
    unpredictable between picks that were equally good, and never promotes a
    replay over something the room has not heard.

    ``top`` is how far down the ranking is still eligible to be drawn from. The
    band is what keeps that honest either way: however wide the draw, a replay
    or a band the room just heard is a tier below and cannot be reached.
    """
    if not ranked:
        return []

    best = ranked[0][0]
    tied = [c for score, c in ranked[: max(1, top)] if best - score <= DRAW_BAND]
    random.shuffle(tied)
    return tied + [c for _score, c in ranked[len(tied) :]]


async def pick(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    room: Room,
) -> str | None:
    """Choose an id worth queueing, or None if there is nothing suitable."""
    history = await recent_tracks(db, room.id, limit=HISTORY_WINDOW)

    playlist = await _playlist_pool(redis, room)
    # Asked for even when the playlist wins, because the playlist may be
    # exhausted by the time it is ranked and there is no second chance then.
    mix = await _mix_pool(redis, history)

    if playlist and random.random() < playlist_share(len(history)):
        pool, source = playlist, "playlist"
    elif mix:
        pool, source = mix, "mix"
    elif playlist:
        pool, source = playlist, "playlist"
    else:
        return None

    blocked = {
        artist_key(track.title, track.channel) for track in history[:ARTIST_COOLDOWN]
    } - {""}
    counts = Counter(
        key for key in (artist_key(track.title, track.channel) for track in history) if key
    )

    ranked = draw(
        rank_pool(
            pool,
            excluded_ids=await queued_youtube_ids(db, room.id),
            recent_ids={track.youtube_id for track in history},
            blocked_artists=blocked,
            artist_counts=counts,
            max_duration_s=settings.max_track_duration_s,
        ),
        # The host's playlist is drawn from whole. A room that starts on it
        # should start somewhere different every time — "put this list on" is
        # what a host means by pasting one, not "play me the five entries of it
        # that most look like records". The mix keeps the cap: it is YouTube's
        # list rather than anybody's choice, and its tail is where a room
        # wanders off.
        top=len(pool) if source == "playlist" else PICK_FROM_TOP,
    )

    for candidate in ranked[:MAX_CANDIDATES]:
        try:
            metadata = await fetch_metadata(
                redis, candidate.youtube_id, settings.metadata_cache_ttl_s
            )
        except YouTubeError:
            continue  # dead or blocked; try the next one
        if metadata.duration_s and metadata.duration_s > settings.max_track_duration_s:
            continue
        logger.info("room %s: autoplay picked %s from the %s", room.token, metadata.title, source)
        return candidate.youtube_id

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
