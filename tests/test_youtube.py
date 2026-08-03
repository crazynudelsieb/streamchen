"""URL parsing — the one place a listener's paste meets the system."""

from __future__ import annotations

import pytest

from app.youtube import (
    SearchCandidate,
    artist_key,
    is_dynamic_mix,
    parse_playlist_url,
    parse_youtube_id,
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
# A host pastes one of these because their browser showed them a coherent
# playlist. The server has no account, so it is shown something else — and
# something else again on the next fetch.
@pytest.mark.parametrize(
    "value",
    [
        # The rock mix that played film scores: "My Mix 1" to a signed-out client.
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
