"""URL parsing — the one place a listener's paste meets the system."""

from __future__ import annotations

import pytest

from app.youtube import parse_playlist_url, parse_youtube_id

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
