"""YouTube resolution: URL -> id -> metadata -> stream URL.

Two caches with very different reasons to exist (concept §12):

* metadata (24 h) — titles and durations do not change, and a queue page
  should not cost one extraction per entry.
* stream URL (5 min) — extraction is slow and the URLs expire quickly. Cached
  because it is expensive, not because it is valuable.

Neither cache holds audio.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from redis.asyncio import Redis

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_EMBED_PATHS = ("/embed/", "/shorts/", "/v/", "/live/")


class YouTubeError(RuntimeError):
    """Raised when a URL cannot be resolved into something playable."""


@dataclass(frozen=True)
class TrackMetadata:
    youtube_id: str
    title: str
    duration_s: int
    thumbnail_url: str | None = None
    channel: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_youtube_id(value: str) -> str | None:
    """Accept anything a listener might paste; return the 11-character id.

    Handles watch URLs, youtu.be, /shorts/, /embed/, /live/, extra query
    parameters, and a bare id.
    """
    value = (value or "").strip()
    if not value:
        return None

    if _ID_RE.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")

    if host in ("youtu.be",):
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _ID_RE.match(candidate) else None

    if host not in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        return None

    if parsed.path in ("/watch", "/watch/"):
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        return candidate if _ID_RE.match(candidate) else None

    for prefix in _EMBED_PATHS:
        if parsed.path.startswith(prefix):
            candidate = parsed.path[len(prefix) :].split("/")[0]
            return candidate if _ID_RE.match(candidate) else None

    return None


def _metadata_key(youtube_id: str) -> str:
    return f"streamchen:meta:{youtube_id}"


def _stream_key(youtube_id: str) -> str:
    return f"streamchen:stream:{youtube_id}"


def _extract(youtube_id: str, *, for_playback: bool) -> dict[str, Any]:
    """Blocking yt-dlp call. Always run through ``asyncio.to_thread``."""
    from yt_dlp import YoutubeDL  # imported here: the API path rarely needs it

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": "bestaudio/best",
        "socket_timeout": 15,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={youtube_id}", download=False)

    if not info:
        raise YouTubeError("could not resolve video")
    if for_playback and not info.get("url"):
        # A geo-blocked or age-gated video still resolves, just without a
        # playable stream. Better to fail here than to hand ffmpeg nothing.
        raise YouTubeError("no playable audio stream")
    return info


def metadata_from_info(youtube_id: str, info: dict[str, Any]) -> TrackMetadata:
    thumbnails = info.get("thumbnails") or []
    thumbnail = info.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
    channel = info.get("uploader") or info.get("channel")
    return TrackMetadata(
        youtube_id=youtube_id,
        title=str(info.get("title") or "Unknown track")[:200],
        duration_s=int(info.get("duration") or 0),
        thumbnail_url=str(thumbnail)[:400] if thumbnail else None,
        channel=str(channel)[:120] if channel else None,
    )


async def fetch_metadata(
    redis: Redis,
    youtube_id: str,
    ttl_s: int = 24 * 3600,
) -> TrackMetadata:
    """Title / duration / thumbnail / channel, cached for a day."""
    cached = await redis.get(_metadata_key(youtube_id))
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return TrackMetadata(**json.loads(cached))
        except (json.JSONDecodeError, TypeError):
            pass  # poisoned entry; fall through and re-extract

    try:
        info = await asyncio.to_thread(_extract, youtube_id, for_playback=False)
    except YouTubeError:
        raise
    except Exception as exc:  # yt-dlp raises its own hierarchy
        raise YouTubeError(str(exc)[:200]) from exc

    metadata = metadata_from_info(youtube_id, info)
    await redis.set(_metadata_key(youtube_id), json.dumps(metadata.as_dict()), ex=ttl_s)
    return metadata


async def resolve_stream_url(redis: Redis, youtube_id: str, ttl_s: int = 300) -> str:
    """Direct audio URL for ffmpeg. Short TTL — these expire upstream."""
    cached = await redis.get(_stream_key(youtube_id))
    if cached:
        return cached.decode("utf-8") if isinstance(cached, bytes) else cached

    try:
        info = await asyncio.to_thread(_extract, youtube_id, for_playback=True)
    except YouTubeError:
        raise
    except Exception as exc:
        raise YouTubeError(str(exc)[:200]) from exc

    url = info["url"]
    await redis.set(_stream_key(youtube_id), url, ex=ttl_s)
    return url
