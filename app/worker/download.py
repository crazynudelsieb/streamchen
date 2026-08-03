"""Fetching audio into the temporary cache.

A local file rather than streaming straight from the resolved URL, for one
reason: the next track can be prepared while the current one plays, which is
what makes transitions gapless (concept §11). The file is deleted the moment
playback ends.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.news import USER_AGENT, NewsError
from app.worker.cache import PARTIAL_SUFFIX
from app.youtube import YouTubeError

logger = logging.getLogger(__name__)

CLIP_TIMEOUT_S = 30
CLIP_CHUNK_BYTES = 64 * 1024

# Extensions ffmpeg is being handed. It sniffs the container either way, so this
# is only about not writing a file called ``.php`` into the cache.
CLIP_SUFFIXES = (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav")


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
    # predicted; the id is still the stem. Its own partial files share that
    # stem too, and one of those is not a download that finished.
    for candidate in sorted(directory.glob(f"{youtube_id}.*")):
        if candidate.is_file() and candidate.suffix != PARTIAL_SUFFIX:
            return candidate

    raise YouTubeError("downloaded file not found")


def _clip_suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for suffix in CLIP_SUFFIXES:
        if path.endswith(suffix):
            return suffix
    return ".mp3"


def download_clip(url: str, key: str, directory: Path, max_bytes: int) -> Path:
    """Blocking. Call through ``asyncio.to_thread``.

    A news bulletin's enclosure, straight into the cache. yt-dlp is not in the
    way here — the URL *is* the audio file — but the cache's rules still are:
    one stem per item, so ``release`` and the eviction sweeps can find it.

    Written to ``<key>.part`` first. That shares the stem, so the sweeps spare
    the partial file the same way they spare the finished one, and a download
    that dies half way through leaves nothing anyone can mistake for playable
    audio — ``AudioCache.find`` will not hand a ``.part`` back to anybody.
    """
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f"{key}{PARTIAL_SUFFIX}"
    path = directory / f"{key}{_clip_suffix(url)}"

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=CLIP_TIMEOUT_S) as response:
            written = 0
            with partial.open("wb") as sink:
                while chunk := response.read(CLIP_CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_bytes:
                        raise NewsError("news clip is larger than the cap")
                    sink.write(chunk)
        if written == 0:
            raise NewsError("news clip is empty")
        partial.replace(path)
    except NewsError:
        partial.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        raise NewsError(f"could not fetch the news clip: {exc}"[:200]) from exc

    return path
