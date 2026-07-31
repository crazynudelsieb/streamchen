"""Autoplay — what the radio queues when nobody is asking for anything."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import autoplay, events
from app.config import Settings
from app.models import STATE_PLAYED, STATE_QUEUED, Listener, Room, Track, utcnow
from app.scheduling import Candidate, order_queue
from app.service import RADIO_SESSION_ID, count_listeners
from tests.conftest import video_id


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        add_rate_limit=100,
        vote_rate_limit=100,
        max_track_duration_s=900,
    )


@pytest.fixture(autouse=True)
def fake_lookups(monkeypatch):
    """No network: searches and mixes resolve to predictable ids."""

    async def _metadata(_redis, youtube_id: str, _ttl: int = 0):
        from app.youtube import TrackMetadata

        return TrackMetadata(
            youtube_id=youtube_id,
            title=f"Track {youtube_id}",
            duration_s=180,
            channel="Test Channel",
        )

    async def _radio(_redis, youtube_id: str, limit: int = 25, ttl_s: int = 0):
        return [video_id(900 + index) for index in range(5)]

    async def _playlist(_redis, _url: str, limit: int = 100, ttl_s: int = 0):
        return [video_id(500 + index) for index in range(3)]

    monkeypatch.setattr("app.autoplay.fetch_metadata", _metadata)
    monkeypatch.setattr("app.autoplay.radio_for", _radio)
    monkeypatch.setattr("app.autoplay.playlist_ids", _playlist)


# --- Parsing ----------------------------------------------------------------
def test_playlist_field_accepts_what_a_host_would_paste():
    urls, ids = autoplay.parse_playlist_field(
        "https://www.youtube.com/playlist?list=PLabc\n"
        "https://youtu.be/aaaaaaaaaaa, bbbbbbbbbbb\n"
        "not-a-link"
    )

    assert urls == ["https://www.youtube.com/playlist?list=PLabc"]
    assert ids == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_playlist_field_takes_youtube_music():
    """A host whose room has a sound pastes from the music catalogue."""
    urls, ids = autoplay.parse_playlist_field(
        "https://music.youtube.com/playlist?list=OLAK5uy_abc\n"
        "https://music.youtube.com/browse/MPREb_abc123\n"
        f"https://music.youtube.com/watch?v={video_id(7)}"
    )

    assert urls == [
        "https://www.youtube.com/playlist?list=OLAK5uy_abc",
        "https://music.youtube.com/browse/MPREb_abc123",
    ]
    assert ids == [video_id(7)]


def test_playlist_field_drops_links_that_are_not_youtube():
    """The field feeds yt-dlp directly, so a stranger's URL must not reach it."""
    urls, ids = autoplay.parse_playlist_field("https://example.com/playlist?list=PLabc")

    assert (urls, ids) == ([], [])


def test_playlist_field_is_empty_when_unset():
    assert autoplay.parse_playlist_field(None) == ([], [])
    assert autoplay.parse_playlist_field("   ") == ([], [])


# --- Ordering ---------------------------------------------------------------
def test_a_real_request_outranks_a_radio_pick():
    """The point of the negative priority: asking for a song always wins."""
    now = utcnow()
    radio = Candidate(
        id="radio", added_by="radio", score=50, created_at=now,
        priority=autoplay.AUTOPLAY_PRIORITY,
    )
    # Later, unvoted, and still first.
    request = Candidate(id="request", added_by="alice", score=0, created_at=now)

    assert [c.id for c in order_queue([radio, request])] == ["request", "radio"]


# --- Queueing ---------------------------------------------------------------
async def _room_with_history(api, *, played: str, fallback: str | None = None) -> Room:
    async with api.state.sessionmaker() as db:
        from app.security import hash_secret

        room = Room(
            token="autoplay-room-token-aaaaaaaaaa",
            name="Radio",
            host_secret_hash=hash_secret("secret"),
            fallback_playlist=fallback,
        )
        db.add(room)
        await db.flush()

        listener = Listener(room_id=room.id, session_id="sess", display_name="alice")
        db.add(listener)
        await db.flush()

        db.add(
            Track(
                room_id=room.id,
                youtube_id=played,
                title="Played",
                duration_s=180,
                added_by_id=listener.id,
                state=STATE_PLAYED,
                ended_at=utcnow(),
            )
        )
        await db.commit()
        return room


async def _queued(api, room_id) -> list[Track]:
    async with api.state.sessionmaker() as db:
        result = await db.execute(
            select(Track).where(Track.room_id == room_id, Track.state == STATE_QUEUED)
        )
        return list(result.scalars().all())


async def test_radio_keeps_a_listening_room_playing(api, settings):
    room = await _room_with_history(api, played=video_id(1))
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        track = await autoplay.top_up(db, api.state.redis, settings, room.id)

    assert track is not None
    # Seeded from what just played, and marked as the radio's doing.
    assert track.youtube_id.startswith("vid")
    assert track.priority == autoplay.AUTOPLAY_PRIORITY

    queued = await _queued(api, room.id)
    assert len(queued) == 1


async def test_radio_does_not_stream_to_an_empty_room(api, settings):
    """No presence, no autoplay — otherwise an abandoned room plays forever."""
    room = await _room_with_history(api, played=video_id(1))

    async with api.state.sessionmaker() as db:
        track = await autoplay.top_up(db, api.state.redis, settings, room.id)

    assert track is None
    assert await _queued(api, room.id) == []


async def test_a_host_playlist_wins_over_the_mix(api, settings):
    room = await _room_with_history(
        api, played=video_id(1), fallback="https://www.youtube.com/playlist?list=PLabc"
    )
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        track = await autoplay.top_up(db, api.state.redis, settings, room.id)

    assert track is not None
    assert track.youtube_id in {video_id(500 + index) for index in range(3)}


async def test_radio_stays_out_of_the_way_while_the_queue_has_songs(api, settings):
    room = await _room_with_history(api, played=video_id(1))
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        listener = (
            await db.execute(select(Listener).where(Listener.room_id == room.id))
        ).scalar_one()
        db.add(
            Track(
                room_id=room.id,
                youtube_id=video_id(7),
                title="Requested",
                duration_s=180,
                added_by_id=listener.id,
                state=STATE_QUEUED,
            )
        )
        await db.commit()

    async with api.state.sessionmaker() as db:
        assert await autoplay.top_up(db, api.state.redis, settings, room.id) is None


async def test_radio_does_not_repeat_what_the_room_just_heard(api, settings):
    """The mix always opens with things close to the seed, including replays."""
    room = await _room_with_history(api, played=video_id(900))
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        track = await autoplay.top_up(db, api.state.redis, settings, room.id)

    assert track is not None
    assert track.youtube_id != video_id(900)


async def test_the_radio_is_not_counted_as_a_listener(api, settings):
    room = await _room_with_history(api, played=video_id(1))
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        before = await count_listeners(db, room.id)
        await autoplay.top_up(db, api.state.redis, settings, room.id)

    async with api.state.sessionmaker() as db:
        assert await count_listeners(db, room.id) == before
        # It does exist, it is just not a participant.
        stand_in = (
            await db.execute(
                select(Listener).where(
                    Listener.room_id == room.id, Listener.session_id == RADIO_SESSION_ID
                )
            )
        ).scalar_one()
        assert stand_in.is_host is False
