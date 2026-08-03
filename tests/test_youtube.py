"""URL parsing — the one place a listener's paste meets the system."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import fakeredis.aioredis
import pytest

from app import youtube
from app.youtube import (
    SearchCandidate,
    YouTubeError,
    artist_key,
    is_dynamic_mix,
    is_seedless_mix,
    parse_playlist_url,
    parse_youtube_id,
    playlist_candidates,
    song_score,
)

VIDEO = "dQw4w9WgXcQ"
ALBUM = "OLAK5uy_lJv8Ky2iQZ0dJ3RiJcTUMDLcMYCrHxNQg"


@pytest.mark.parametrize(
    "value",
    [
        VIDEO,
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"http://youtube.com/watch?v={VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        f"https://music.youtube.com/watch?v={VIDEO}&list=RDAMVM",
        # A song opened from inside an album is still one song.
        f"https://music.youtube.com/watch?v={VIDEO}&list={ALBUM}",
        f"music.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}&t=42s",
        f"https://youtu.be/{VIDEO}",
        f"https://youtu.be/{VIDEO}?t=42",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        f"https://www.youtube.com/live/{VIDEO}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO}",
        f"  https://www.youtube.com/watch?v={VIDEO}  ",
        f"youtube.com/watch?v={VIDEO}",
    ],
)
def test_accepts_every_shape_people_paste(value: str):
    assert parse_youtube_id(value) == VIDEO


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not a url",
        "https://vimeo.com/12345",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/results?search_query=music",
        # A playlist names no single video, so the queue cannot take it.
        f"https://music.youtube.com/playlist?list={ALBUM}",
        "https://www.youtube.com/playlist?list=PLabc",
    ],
)
def test_rejects_everything_else(value: str):
    assert parse_youtube_id(value) is None


# --- Playlists ---------------------------------------------------------------
# Host-side only: a playlist is the pool the radio draws from, never something
# a listener drops into the queue.
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            f"https://music.youtube.com/playlist?list={ALBUM}",
            f"https://www.youtube.com/playlist?list={ALBUM}",
        ),
        (
            "https://www.youtube.com/playlist?list=PLabc",
            "https://www.youtube.com/playlist?list=PLabc",
        ),
        (
            "music.youtube.com/playlist?list=PLabc",
            "https://www.youtube.com/playlist?list=PLabc",
        ),
        # Tracking parameters from a share sheet must not fork the cache key.
        (
            "https://music.youtube.com/playlist?list=PLabc&si=xyz&feature=share",
            "https://www.youtube.com/playlist?list=PLabc",
        ),
        # A link naming both means the playlist when a host pastes it.
        (
            f"https://music.youtube.com/watch?v={VIDEO}&list=PLabc",
            "https://www.youtube.com/playlist?list=PLabc",
        ),
        # The album page as the YouTube Music address bar shows it.
        (
            "https://music.youtube.com/browse/MPREb_abc123",
            "https://music.youtube.com/browse/MPREb_abc123",
        ),
    ],
)
def test_playlist_links_are_canonicalised(value: str, expected: str):
    assert parse_playlist_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        VIDEO,
        f"https://www.youtube.com/watch?v={VIDEO}",
        "https://www.youtube.com/playlist",
        # Never hand a stranger's URL to yt-dlp's generic extractor.
        "https://example.com/playlist?list=PLabc",
        "https://youtube.com.evil.test/playlist?list=PLabc",
        # /browse/ is a music-catalogue path; it means nothing on the video site.
        "https://www.youtube.com/browse/MPREb_abc123",
    ],
)
def test_playlist_rejects_everything_else(value: str):
    assert parse_playlist_url(value) is None


# --- Mixes -------------------------------------------------------------------
# A mix is a station, not a stored list, and YouTube will only serve one from a
# watch URL: /playlist?list=RD… is answered with "This playlist type is
# unviewable". So the seed video is part of the address and canonicalising it
# away is what made these unplayable.
@pytest.mark.parametrize(
    "value",
    [
        "https://music.youtube.com/playlist?list=RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",
        "https://www.youtube.com/playlist?list=RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",
        f"https://www.youtube.com/watch?v={VIDEO}&list=RD{VIDEO}",
        "RDMM4xyzabc",
        "RDEMabcdef",
    ],
)
def test_dynamic_mixes_are_recognised(value: str):
    assert is_dynamic_mix(value) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The link a host copies out of YouTube Music while a mix is playing.
        (
            f"https://music.youtube.com/watch?v={VIDEO}&list=RDEM7AbogW0cCnElSU0WYm1GqA",
            f"https://www.youtube.com/watch?v={VIDEO}&list=RDEM7AbogW0cCnElSU0WYm1GqA",
        ),
        # A track's own radio, and the share sheet's leftovers.
        (
            f"https://www.youtube.com/watch?v={VIDEO}&list=RD{VIDEO}&si=xyz",
            f"https://www.youtube.com/watch?v={VIDEO}&list=RD{VIDEO}",
        ),
    ],
)
def test_mixes_keep_their_seed_video(value: str, expected: str):
    """The seed is half the address: a mix is unfetchable without one."""
    assert parse_playlist_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://music.youtube.com/playlist?list=RDEM7AbogW0cCnElSU0WYm1GqA",
        "https://www.youtube.com/playlist?list=RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",
        f"https://www.youtube.com/watch?list=RD{VIDEO}",
    ],
)
def test_seedless_mixes_are_rejected_rather_than_canonicalised(value: str):
    """Better no URL than one that is guaranteed to fail on every fetch."""
    assert parse_playlist_url(value) is None
    assert is_seedless_mix(value) is True


@pytest.mark.parametrize(
    "value",
    [
        f"https://music.youtube.com/watch?v={VIDEO}&list=RDEM7AbogW0cCnElSU0WYm1GqA",
        "https://www.youtube.com/playlist?list=PLabc",
        f"https://www.youtube.com/watch?v={VIDEO}",
        "",
    ],
)
def test_playable_links_are_not_seedless_mixes(value: str):
    assert is_seedless_mix(value) is False


# --- Walking a mix -----------------------------------------------------------
# One window is not the station. The pool is what successive refreshes have
# merged, which is how a mix reaches its ceiling of a few dozen tracks without
# any single autoplay pick paying for the five extractions that would take.
MIX = f"https://www.youtube.com/watch?v={VIDEO}&list=RDEM7AbogW0cCnElSU0WYm1GqA"


def _window(*ids: str) -> list[dict]:
    return [
        {"id": i, "title": f"Band {i} - Song", "duration": 180, "channel": f"Band {i}"}
        for i in ids
    ]


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _pool_ids(redis) -> list[str]:
    # Through playlist_candidates, because that is the door autoplay comes in by
    # and the routing is half of what makes a mix usable.
    return [c.youtube_id for c in await playlist_candidates(redis, MIX)]


async def test_a_mix_window_is_reused_until_it_is_due_a_refresh(redis, monkeypatch):
    calls = []

    def _entries(url: str, limit: int):
        calls.append(url)
        return _window("aaaaaaaaaaa", "bbbbbbbbbbb")

    monkeypatch.setattr(youtube, "_flat_entries", _entries)

    assert await _pool_ids(redis) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert await _pool_ids(redis) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert len(calls) == 1  # the second pick cost nothing upstream


async def test_each_refresh_walks_the_station_further(redis, monkeypatch):
    """A hop seeds from the frontier: the same vantage point twice learns nothing."""
    windows = {
        VIDEO: _window("aaaaaaaaaaa", "bbbbbbbbbbb"),
        "aaaaaaaaaaa": _window("aaaaaaaaaaa", "ccccccccccc"),
        "bbbbbbbbbbb": _window("ddddddddddd"),
    }

    def _entries(url: str, limit: int):
        return windows.get(parse_qs(urlparse(url).query)["v"][0], [])

    monkeypatch.setattr(youtube, "_flat_entries", _entries)

    assert await _pool_ids(redis) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]

    await redis.delete(youtube._mix_fresh_key(MIX))
    assert await _pool_ids(redis) == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]

    await redis.delete(youtube._mix_fresh_key(MIX))
    assert await _pool_ids(redis) == [
        "aaaaaaaaaaa",
        "bbbbbbbbbbb",
        "ccccccccccc",
        "ddddddddddd",
    ]


async def test_a_failed_hop_keeps_the_pool_it_already_has(redis, monkeypatch):
    monkeypatch.setattr(youtube, "_flat_entries", lambda url, limit: _window("aaaaaaaaaaa"))
    assert await _pool_ids(redis) == ["aaaaaaaaaaa"]

    def _boom(url: str, limit: int):
        raise RuntimeError("upstream said no")

    monkeypatch.setattr(youtube, "_flat_entries", _boom)
    await redis.delete(youtube._mix_fresh_key(MIX))

    # The room keeps playing what the walk already found. The hop is retried at
    # the next refresh rather than emptying the pool now.
    assert await _pool_ids(redis) == ["aaaaaaaaaaa"]


async def test_a_seedless_mix_is_an_error_rather_than_a_doomed_fetch(redis):
    with pytest.raises(YouTubeError):
        await playlist_candidates(redis, "https://www.youtube.com/playlist?list=RDEMabcdef")


@pytest.mark.parametrize(
    "value",
    [
        f"https://www.youtube.com/playlist?list={ALBUM}",
        "https://www.youtube.com/playlist?list=PLabc",
        # Editorial playlists are fixed for everybody, mix-shaped id or not.
        "https://www.youtube.com/playlist?list=RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs",
        ALBUM,
        "",
    ],
)
def test_fixed_playlists_are_not_mixes(value: str):
    assert is_dynamic_mix(value) is False


# --- Judging a track with no query -------------------------------------------
@pytest.mark.parametrize(
    ("title", "channel", "expected"),
    [
        ("Sweet Child O' Mine", "Guns N' Roses", "guns n roses"),
        ("Sweet Child O' Mine", "GunsNRosesVEVO", "gunsnroses"),
        ("Bohemian Rhapsody", "Queen Official", "queen"),
        ("Lithium", "Nirvana - Topic", "nirvana"),
        # No channel in a flat playlist listing — the title carries it instead.
        ("Scorpions - Wind Of Change", None, "scorpions"),
        ("Untitled", None, ""),
    ],
)
def test_artist_key_names_whoever_it_is_by(title, channel, expected):
    assert artist_key(title, channel) == expected


def test_a_record_outscores_what_is_merely_near_one():
    record = SearchCandidate(
        youtube_id=VIDEO, title="Nirvana - Lithium", channel="Nirvana", duration_s=257
    )
    for title in [
        'Alan Silvestri - The Avengers (From "The Avengers")',
        "Anime Kei - Vigorously (Naruto OST)",
        "Skyrim Gameplay Walkthrough Part 1",
        "Nirvana - Lithium (Karaoke Version)",
        "Nirvana Greatest Hits Full Album",
        "Nirvana - Lithium REACTION!!",
    ]:
        other = SearchCandidate(youtube_id=VIDEO, title=title, duration_s=257)
        assert song_score(record) > song_score(other), title


def test_the_catalogue_upload_wins_over_the_music_video():
    """The point of preferring it: no spoken intro, no long fade of credits."""
    video = SearchCandidate(
        youtube_id=VIDEO, title="Nirvana - Lithium (Official Music Video)",
        channel="Nirvana", duration_s=257,
    )
    topic = SearchCandidate(
        youtube_id=VIDEO, title="Lithium", channel="Nirvana - Topic",
        duration_s=257, catalog=True,
    )
    assert song_score(topic) > song_score(video)


def test_an_overlong_upload_is_pushed_down():
    fits = SearchCandidate(youtube_id=VIDEO, title="Nirvana - Lithium", duration_s=257)
    hour = SearchCandidate(youtube_id=VIDEO, title="Nirvana - Lithium", duration_s=3600)
    assert song_score(fits, max_duration_s=900) > song_score(hour, max_duration_s=900)
