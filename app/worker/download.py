"""Fetching audio into the temporary cache.

A local file rather than streaming straight from the resolved URL, for one
reason: the next track can be prepared while the current one plays, which is
what makes transitions gapless (concept §11). The file is deleted the moment
playback ends.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.youtube import YouTubeError

logger = logging.getLogger(__name__)


def download_audio(youtube_id: str, directory: Path) -> Path:
    """Blocking. Call through ``asyncio.to_thread``.

    Downloads bestaudio in whatever container YouTube offers — ffmpeg reads
    all of them, so there is no reason to spend CPU transcoding twice.
    """
    from yt_dlp import YoutubeDL

    directory.mkdir(parents=True, exist_ok=True)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "socket_timeout": 20,
        "retries": 2,
        "overwrites": True,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={youtube_id}", download=True
            )
    except Exception as exc:  # yt-dlp has its own exception hierarchy
        raise YouTubeError(str(exc)[:200]) from exc

    if not info:
        raise YouTubeError("download produced no result")

    path = Path(ydl.prepare_filename(info))
    if path.exists():
        return path

    # yt-dlp may have remuxed into a different extension than the template
    # predicted; the id is still the stem.
    for candidate in sorted(directory.glob(f"{youtube_id}.*")):
        if candidate.is_file():
            return candidate

    raise YouTubeError("downloaded file not found")
