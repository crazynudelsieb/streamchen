"""URL parsing — the one place a listener's paste meets the system."""

from __future__ import annotations

import pytest

from app.youtube import parse_youtube_id

VIDEO = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        VIDEO,
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"http://youtube.com/watch?v={VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        f"https://music.youtube.com/watch?v={VIDEO}&list=RDAMVM",
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
    ],
)
def test_rejects_everything_else(value: str):
    assert parse_youtube_id(value) is None
