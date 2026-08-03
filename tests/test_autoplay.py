"""Autoplay — what the radio queues when nobody is asking for anything."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import autoplay, events
from app.config import Settings
from app.models import STATE_PLAYED, STATE_QUEUED, Listener, Room, Track, utcnow
from app.scheduling import Candidate, order_queue
from app.service import RADIO_SESSION_ID, count_listeners
from app.youtube import SearchCandidate
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
    """No network: mixes and playlists resolve to predictable candidates."""

    async def _metadata(_redis, youtube_id: str, _ttl: int = 0):
        from app.youtube import TrackMetadata

        return TrackMetadata(
            youtube_id=youtube_id,
            title=f"Track {youtube_id}",
            duration_s=180,
            channel="Test Channel",
        )

    async def _mix(_redis, youtube_id: str, limit: int = 25, ttl_s: int = 0):
        return [
            SearchCandidate(
                youtube_id=video_id(900 + index),
                title=f"Band {index} - Song {index}",
                duration_s=180,
                channel=f"Band {index}",
            )
            for index in range(5)
        ]

    async def _playlist(_redis, _url: str, limit: int = 100, ttl_s: int = 0):
        return [
            SearchCandidate(
                youtube_id=video_id(500 + index),
                title=f"Playlist Band {index} - Song {index}",
                duration_s=200,
                channel=f"Playlist Band {index}",
            )
            for index in range(3)
        ]

    monkeypatch.setattr("app.autoplay.fetch_metadata", _metadata)
    monkeypatch.setattr("app.autoplay.radio_candidates", _mix)
    monkeypatch.setattr("app.autoplay.playlist_candidates", _playlist)


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


def test_playlist_field_takes_a_mix_with_its_seed():
    """A mix is only playable from inside it, so the seed video has to survive."""
    urls, ids = autoplay.parse_playlist_field(
        f"https://music.youtube.com/watch?v={video_id(7)}&list=RDEM7AbogW0cCnElSU0WYm1GqA"
    )

    assert urls == [
        f"https://www.youtube.com/watch?v={video_id(7)}&list=RDEM7AbogW0cCnElSU0WYm1GqA"
    ]
    assert ids == []


def test_playlist_field_drops_a_mix_with_no_seed():
    """Nothing resolves it, and the host is told so where they pasted it."""
    field = "https://music.youtube.com/playlist?list=RDEM7AbogW0cCnElSU0WYm1GqA"

    assert autoplay.parse_playlist_field(field) == ([], [])


def test_playlist_field_drops_links_that_are_not_youtube():
    """The field feeds yt-dlp directly, so a stranger's URL must not reach it."""
    urls, ids = autoplay.parse_playlist_field("https://example.com/playlist?list=PLabc")

    assert (urls, ids) == ([], [])


def test_playlist_field_is_empty_when_unset():
    assert autoplay.parse_playlist_field(None) == ([], [])
    assert autoplay.parse_playlist_field("   ") == ([], [])


# --- The pool with no ceiling -----------------------------------------------
async def test_the_endless_radio_survives_a_dead_seed(monkeypatch):
    """One unplayable recent track must not be what stops a room playing.

    The host's playlist ends; this pool is the reason a room is still going at
    four in the morning, so it is tried against every recent seed rather than
    against one of them.
    """
    from app.youtube import YouTubeError

    async def _mix(_redis, youtube_id: str, limit: int = 25, ttl_s: int = 0):
        if youtube_id == video_id(1):
            raise YouTubeError("pulled")
        if youtube_id == video_id(2):
            return []  # resolves, has nothing to offer
        return [SearchCandidate(youtube_id=video_id(42), title="Band - Song", duration_s=180)]

    monkeypatch.setattr("app.autoplay.radio_candidates", _mix)

    history = [Track(youtube_id=video_id(i), title=f"Track {i}", duration_s=180) for i in (1, 2, 3)]
    pool = await autoplay._mix_pool(None, history)

    assert [c.youtube_id for c in pool] == [video_id(42)]


async def test_the_endless_radio_gives_up_only_when_every_seed_does(monkeypatch):
    from app.youtube import YouTubeError

    async def _mix(_redis, youtube_id: str, limit: int = 25, ttl_s: int = 0):
        raise YouTubeError("upstream said no")

    monkeypatch.setattr("app.autoplay.radio_candidates", _mix)

    history = [Track(youtube_id=video_id(i), title=f"Track {i}", duration_s=180) for i in (1, 2)]

    assert await autoplay._mix_pool(None, history) == []


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
async def _room_with_history(
    api,
    *,
    played: str | list[tuple[str, str, str]],
    fallback: str | None = None,
) -> Room:
    """A room that has heard something. ``played`` is one id, or (id, title, channel)
    entries most-recent first."""
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

        entries = [(played, "Played", None)] if isinstance(played, str) else played
        for index, (youtube_id, title, channel) in enumerate(entries):
            db.add(
                Track(
                    room_id=room.id,
                    youtube_id=youtube_id,
                    title=title,
                    channel=channel,
                    duration_s=180,
                    added_by_id=listener.id,
                    state=STATE_PLAYED,
                    ended_at=utcnow() - timedelta(seconds=index),
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


# --- Not playing the same band all evening ----------------------------------
def _ranked_ids(**kwargs) -> list[str]:
    """rank_pool's order, without its scores."""
    kwargs.setdefault("excluded_ids", set())
    kwargs.setdefault("recent_ids", set())
    kwargs.setdefault("blocked_artists", set())
    kwargs.setdefault("artist_counts", Counter())
    kwargs.setdefault("max_duration_s", 900)
    return [candidate.youtube_id for _score, candidate in autoplay.rank_pool(**kwargs)]


def test_the_playlist_leads_at_the_start_and_stays_the_majority():
    """What a host pasted is what the room is for, most of all before it has a
    history for anything else to be based on."""
    assert autoplay.playlist_share(0) == 1.0
    assert autoplay.playlist_share(autoplay.PLAYLIST_ONLY_UNTIL) == 1.0

    # Monotonic down from there, and never below the floor.
    shares = [autoplay.playlist_share(n) for n in range(0, 60)]
    assert shares == sorted(shares, reverse=True)
    assert min(shares) == autoplay.PLAYLIST_SHARE_FLOOR
    assert autoplay.playlist_share(1000) == autoplay.PLAYLIST_SHARE_FLOOR


def test_a_mix_full_of_the_seed_band_still_yields_a_different_band():
    """The reason any of this exists: a third of a mix is the seed's own
    catalogue, sitting at the front of it."""
    pool = [
        SearchCandidate(youtube_id=video_id(i), title=f"Guns N' Roses - Song {i}",
                        channel="Guns N' Roses", duration_s=300)
        for i in range(5)
    ] + [
        SearchCandidate(youtube_id=video_id(90), title="Scorpions - Wind Of Change",
                        channel="Scorpions", duration_s=300)
    ]

    ranked = _ranked_ids(pool=pool, blocked_artists={autoplay.artist_key("", "Guns N' Roses")})

    assert ranked[0] == video_id(90)


def test_a_band_heard_often_sinks_below_one_not_heard_at_all():
    """A soft preference, not a ban: the pool may be nothing else."""
    heard = SearchCandidate(youtube_id=video_id(1), title="Nickelback - How You Remind Me",
                            channel="Nickelback", duration_s=300)
    fresh = SearchCandidate(youtube_id=video_id(2), title="Nirvana - Lithium",
                            channel="Nirvana", duration_s=300)

    ranked = _ranked_ids(
        pool=[heard, fresh],
        artist_counts=Counter({autoplay.artist_key("", "Nickelback"): 4}),
    )

    assert ranked[0] == video_id(2)


def test_a_short_playlist_comes_round_again_rather_than_falling_silent():
    """Every penalty gets out of the way when the pool holds nothing else."""
    pool = [
        SearchCandidate(youtube_id=video_id(i), title=f"Band {i} - Song {i}",
                        channel=f"Band {i}", duration_s=300)
        for i in range(3)
    ]
    everything = {c.youtube_id for c in pool}

    ranked = _ranked_ids(
        pool=pool,
        recent_ids=everything,
        blocked_artists={autoplay.artist_key("", f"Band {i}") for i in range(3)},
    )

    assert set(ranked) == everything


def test_a_replay_sits_below_a_band_the_room_just_heard():
    """The tiers, in order: something new, then that band again, then that song
    again."""
    unheard = SearchCandidate(youtube_id=video_id(1), title="Nirvana - Lithium",
                              channel="Nirvana", duration_s=300)
    same_band = SearchCandidate(youtube_id=video_id(2), title="Queen - Radio Ga Ga",
                                channel="Queen", duration_s=300)
    replay = SearchCandidate(youtube_id=video_id(3), title="Nirvana - Lithium",
                             channel="Nirvana", duration_s=300)

    ranked = _ranked_ids(
        pool=[replay, same_band, unheard],
        recent_ids={video_id(3)},
        blocked_artists={autoplay.artist_key("", "Queen")},
    )

    assert ranked == [video_id(1), video_id(2), video_id(3)]


def _pool_of_equals(count: int) -> list[SearchCandidate]:
    return [
        SearchCandidate(youtube_id=video_id(i), title=f"Band {i} - Song",
                        channel=f"Band {i}", duration_s=300)
        for i in range(count)
    ]


def test_the_draw_is_not_the_same_song_every_time():
    """Ranking alone would put the room in the same groove every evening."""
    ranked = autoplay.rank_pool(
        _pool_of_equals(4), excluded_ids=set(), recent_ids=set(),
        blocked_artists=set(), artist_counts=Counter(), max_duration_s=900,
    )

    assert len({autoplay.draw(ranked)[0].youtube_id for _ in range(60)}) > 1


def test_the_draw_never_reaches_into_a_worse_tier():
    """A pool smaller than the draw window must not flatten the tiers back into
    a coin flip — which is exactly what shuffling a fixed top-N would do."""
    pool = _pool_of_equals(3)
    ranked = autoplay.rank_pool(
        pool,
        excluded_ids=set(),
        # Two of the three are replays, so only one is a legitimate pick.
        recent_ids={video_id(1), video_id(2)},
        blocked_artists=set(),
        artist_counts=Counter(),
        max_duration_s=900,
    )

    assert {autoplay.draw(ranked)[0].youtube_id for _ in range(60)} == {video_id(0)}


def test_a_queued_id_is_gone_for_good():
    """The one hard exclusion — the add endpoint rejects it as a duplicate too."""
    pool = [SearchCandidate(youtube_id=video_id(1), title="Nirvana - Lithium")]

    assert _ranked_ids(pool=pool, excluded_ids={video_id(1)}) == []


def test_a_game_soundtrack_loses_to_a_song():
    """What a rock room actually got: film and game scores off the back of a
    mix that ran out of rock."""
    song = SearchCandidate(youtube_id=video_id(1), title="Nirvana - Lithium",
                           channel="Nirvana", duration_s=300)
    for title, channel in [
        ("Alan Silvestri - The Avengers (From \"The Avengers\")", "Alan Silvestri"),
        ("Anime Kei - Vigorously (Naruto OST)", "Anime Kei"),
        ("Assassin's Creed Valhalla - Main Theme", "Ubisoft"),
        ("Dragon Ball Z - Gohan's Anger Theme | Epic Rock Cover", "Some Channel"),
        ("Skyrim Gameplay Walkthrough Part 1", "A Streamer"),
    ]:
        other = SearchCandidate(youtube_id=video_id(2), title=title,
                                channel=channel, duration_s=300)
        assert _ranked_ids(pool=[other, song])[0] == video_id(1), title


async def test_the_radio_does_not_replay_the_band_it_just_heard(api, settings):
    room = await _room_with_history(
        api,
        played=[(video_id(900 + index), f"Band 0 - Song {index}", "Band 0") for index in range(3)],
    )
    await events.mark_present(api.state.redis, room.id, "sess")

    async with api.state.sessionmaker() as db:
        track = await autoplay.top_up(db, api.state.redis, settings, room.id)

    assert track is not None
    # Band 0 is the whole of the room's history and the front of the mix.
    assert track.youtube_id not in {video_id(900), video_id(901), video_id(902)}


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
