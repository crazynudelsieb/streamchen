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
import logging
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_EMBED_PATHS = ("/embed/", "/shorts/", "/v/", "/live/")

# Search draws on two places at once. A video search is the one that actually
# knows what a query means — it answers "Antilopen Gang - Pizza" with the
# record, where a music-catalogue search answers it with whichever cut of it
# the catalogue happens to rank first. A flat video search also carries title,
# duration, channel and thumbnail in the listing itself, so a whole page of
# results costs one request instead of one extraction per hit.
#
# The music catalogue is still asked, concurrently, but only for its ids: being
# in it is the evidence that a result is a song rather than a lecture, a podcast
# or an hour-long compilation, which is what the music restriction was for.
_VIDEO_SEARCH_URL = "ytsearch{count}:{query}"
_MUSIC_SEARCH_URL = "https://music.youtube.com/search?q={query}#songs"
_RADIO_URL = "https://www.youtube.com/watch?v={video}&list=RD{video}"


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


# --- Discovery ---------------------------------------------------------------
# Bumped when the cached shape changes: entries written by an older release are
# then simply missed rather than misread.
def _search_key(query: str) -> str:
    return f"streamchen:search:v2:{query}"


def _radio_key(youtube_id: str) -> str:
    return f"streamchen:radio:{youtube_id}"


def normalize_query(value: str) -> str:
    """Collapse whitespace and case so equivalent searches share a cache entry."""
    return " ".join((value or "").split()).lower()


def _flat_entries(url: str, limit: int) -> list[dict[str, Any]]:
    """Blocking. The raw listing behind a playlist-ish URL, in order.

    ``extract_flat`` keeps this to one request: full metadata per entry would
    be one extraction each. ``playlistend`` matters more than it looks — a
    search result set is hundreds of entries long and yt-dlp will page through
    all of them given the chance.
    """
    from yt_dlp import YoutubeDL

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 15,
        "playlistend": limit,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []
    return [entry for entry in (info.get("entries") or []) if entry]


def _flat_ids(url: str, limit: int) -> list[str]:
    """Blocking. The video ids of a playlist-ish URL, in order."""
    ids: list[str] = []
    for entry in _flat_entries(url, limit):
        candidate = entry.get("id") or ""
        # Music results mix in channel, album and playlist ids, which are
        # longer. Only an 11-character id is a video.
        if _ID_RE.match(candidate) and candidate not in ids:
            ids.append(candidate)
    return ids[:limit]


async def _cached_ids(redis: Redis, key: str, url: str, limit: int, ttl_s: int) -> list[str]:
    cached = await redis.get(key)
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass  # poisoned entry; fall through and re-extract

    try:
        ids = await asyncio.to_thread(_flat_ids, url, limit)
    except Exception as exc:  # yt-dlp raises its own hierarchy
        raise YouTubeError(str(exc)[:200]) from exc

    await redis.set(key, json.dumps(ids), ex=ttl_s)
    return ids


# --- Ranking -----------------------------------------------------------------
# How many candidates to ask for before ranking. A page of eight results picked
# from eight candidates is not a search, it is whatever YouTube said first —
# and what YouTube says first for "<artist> <song>" is regularly a live cut
# followed by the rest of the artist's catalogue.
SEARCH_POOL = 25

# Words that describe a *different recording* of the song being asked for.
# Penalised unless the query asks for them, which is what keeps the studio
# version above the live one without ever hiding the live one.
_VARIANTS = {
    "live": 26.0,
    "cover": 22.0,
    "karaoke": 34.0,
    "instrumental": 22.0,
    "remix": 16.0,
    "mashup": 16.0,
    "nightcore": 26.0,
    "slowed": 24.0,
    "sped": 24.0,
    "reverb": 12.0,
    "8d": 24.0,
    "acoustic": 12.0,
    "unplugged": 14.0,
    "demo": 10.0,
    "snippet": 20.0,
    "teaser": 20.0,
    "trailer": 22.0,
    "reaction": 34.0,
    "review": 22.0,
    "interview": 26.0,
    "tutorial": 26.0,
    "lesson": 22.0,
    "megamix": 26.0,
    "compilation": 26.0,
    "mixtape": 18.0,
    "playlist": 20.0,
    "lyrics": 5.0,
    "letra": 5.0,
}

# A full-album upload answers a song query with an hour of audio.
_BULK_PHRASES = ("full album", "greatest hits", "best of", "all songs", "non stop")

# Phrases that mean the same as the single words above but only make sense
# together, so a token test would miss them.
_VARIANT_PHRASES = (("lip sync", 24.0), ("live at", 26.0), ("live in", 22.0))

# A date in a title is a bootleg of a gig almost every time — the live
# recordings that never say "live", only the venue and when.
_DATE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")

# Words that describe the *upload* rather than the recording. They are not
# noise — an "(Official Video)" is evidence of the real thing — so they neither
# count against a title's tidiness nor dilute how much of it the query covers.
_RELEASE_MARKERS = frozenset(
    {
        "official", "video", "audio", "music", "visualizer", "visualiser",
        "hd", "hq", "4k", "remaster", "remastered", "mv", "clip", "lyric",
        "lyrics", "letra", "version", "original", "explicit", "prod",
    }
)

_TOPIC_SUFFIX = " - topic"

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def _fold(value: str | None) -> str:
    """Casefolded, de-accented, punctuation-free text.

    Accents are folded so that a query typed without them still matches, which
    is most of them: nobody reaches for ä on the way to a song.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


def _tokens(value: str | None) -> list[str]:
    return _fold(value).split()


@dataclass(frozen=True)
class SearchCandidate:
    """One hit as the flat listing describes it, before ranking."""

    youtube_id: str
    title: str
    duration_s: int = 0
    thumbnail_url: str | None = None
    channel: str | None = None
    view_count: int = 0
    verified: bool = False
    # Present in the YouTube Music catalogue, i.e. this id is a song.
    catalog: bool = False

    def as_metadata(self) -> TrackMetadata:
        return TrackMetadata(
            youtube_id=self.youtube_id,
            title=self.title[:200],
            duration_s=self.duration_s,
            thumbnail_url=self.thumbnail_url[:400] if self.thumbnail_url else None,
            channel=self.channel[:120] if self.channel else None,
        )


def score_candidate(query: str, candidate: SearchCandidate, max_duration_s: int = 0) -> float:
    """How well one hit answers ``query``. Higher is better.

    The shape of it: relevance is what a result *is*, everything else is a
    tiebreak. A hit missing half the words of the query cannot climb back with
    view counts, and a hit that is a different recording of the right song
    sits below the right recording but stays on the page.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0

    title_tokens = set(_tokens(candidate.title))
    channel_folded = _fold(candidate.channel)
    channel_tokens = set(channel_folded.split())
    haystack = title_tokens | channel_tokens

    # Relevance. Words found in the title count for more than words found only
    # in the channel name, but both count: for "antilopen gang pizza" the
    # artist is often only the channel.
    matched = sum(1 for token in query_tokens if token in haystack)
    in_title = sum(1 for token in query_tokens if token in title_tokens)
    coverage = matched / len(query_tokens)
    score = coverage * 100.0 + (in_title / len(query_tokens)) * 20.0

    # Every missing word is a wrong answer, not a slightly worse one.
    score -= (len(query_tokens) - matched) * 30.0

    folded_query = _fold(query)
    folded_title = _fold(candidate.title)
    if folded_query and folded_query in f"{folded_title} {channel_folded}":
        score += 12.0

    # An upload whose title is *only* the query is the record itself; one that
    # buries it in twenty other words is a compilation or a video essay.
    extra = title_tokens - set(query_tokens) - _RELEASE_MARKERS
    score -= min(12.0, len(extra) * 1.5)

    # "(Official Video)", "(Official Audio)": the artist's own upload of it.
    if "official" in title_tokens:
        score += 8.0

    # Different-recording words, unless they were asked for.
    for word, penalty in _VARIANTS.items():
        if word in title_tokens and word not in query_tokens:
            score -= penalty
    for phrase, penalty in _VARIANT_PHRASES:
        if phrase in folded_title and phrase not in folded_query:
            score -= penalty
    for phrase in _BULK_PHRASES:
        if phrase in folded_title and phrase not in folded_query:
            score -= 30.0
    if _DATE_RE.search(candidate.title) and not _DATE_RE.search(query):
        score -= 14.0

    # Provenance. An artist's own channel and the catalogue's "- Topic" upload
    # are the two things that reliably mean "the actual release".
    if candidate.catalog:
        score += 14.0
    if channel_folded and channel_folded in folded_query:
        score += 16.0
    if candidate.channel and candidate.channel.casefold().endswith(_TOPIC_SUFFIX):
        score += 12.0
    if candidate.verified:
        score += 6.0

    # Length. A room cannot play what it will refuse to accept, and a 40-second
    # upload of a 4-minute song is a clip of it.
    if max_duration_s and candidate.duration_s > max_duration_s:
        score -= 45.0
    if 0 < candidate.duration_s < 45:
        score -= 25.0

    # Popularity decides between hits that are otherwise the same answer — the
    # record against somebody's upload of the record. Logarithmic and capped so
    # it stays a tiebreak, and measured from a floor rather than from zero so
    # that the interesting range (a million plays against a billion) is not
    # squashed into the top of the scale.
    if candidate.view_count > 0:
        score += min(12.0, max(0.0, math.log10(candidate.view_count + 1) - 4.0) * 1.5)

    return score


def rank_candidates(
    query: str,
    candidates: list[SearchCandidate],
    limit: int,
    max_duration_s: int = 0,
) -> list[SearchCandidate]:
    """Best answers to ``query`` first, at most ``limit`` of them."""
    scored = sorted(
        ((score_candidate(query, c, max_duration_s), index, c) for index, c in enumerate(candidates)),
        # Index breaks ties by the order upstream returned them, so an equal
        # score never shuffles between two identical searches.
        key=lambda row: (-row[0], row[1]),
    )
    return [candidate for _score, _index, candidate in scored[:limit]]


def _candidate_from_entry(entry: dict[str, Any]) -> SearchCandidate | None:
    """One flat search entry, or None if it is not a playable video."""
    youtube_id = entry.get("id") or ""
    if not _ID_RE.match(youtube_id):
        return None
    # A livestream has no end and cannot be queued; an upcoming one has no
    # audio at all.
    if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return None

    thumbnails = entry.get("thumbnails") or []
    thumbnail = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
    channel = entry.get("channel") or entry.get("uploader")

    return SearchCandidate(
        youtube_id=youtube_id,
        title=str(entry.get("title") or "Unknown track"),
        duration_s=int(entry.get("duration") or 0),
        thumbnail_url=str(thumbnail) if thumbnail else None,
        channel=str(channel) if channel else None,
        view_count=int(entry.get("view_count") or 0),
        verified=bool(entry.get("channel_is_verified")),
    )


async def _video_candidates(query: str, count: int) -> list[SearchCandidate]:
    entries = await asyncio.to_thread(
        _flat_entries, _VIDEO_SEARCH_URL.format(count=count, query=query), count
    )
    candidates: list[SearchCandidate] = []
    seen: set[str] = set()
    for entry in entries:
        candidate = _candidate_from_entry(entry)
        if candidate is not None and candidate.youtube_id not in seen:
            seen.add(candidate.youtube_id)
            candidates.append(candidate)
    return candidates


async def _catalog_ids(query: str, count: int) -> set[str]:
    url = _MUSIC_SEARCH_URL.format(query=quote(query))
    return set(await asyncio.to_thread(_flat_ids, url, count))


async def _gather_candidates(query: str, count: int) -> list[SearchCandidate]:
    """Both sources at once, merged into one ranked-ready pool.

    Concurrent because they are independent and the slower one decides how long
    a search takes. Either may fail on its own: a search with no catalogue is
    still a search, and a catalogue with no videos is better than an error.
    """
    videos, catalog = await asyncio.gather(
        _video_candidates(query, count),
        _catalog_ids(query, count),
        return_exceptions=True,
    )

    if isinstance(catalog, BaseException):
        logger.info("catalogue lookup failed for %r: %s", query, catalog)
        catalog = set()
    if isinstance(videos, BaseException):
        logger.info("video search failed for %r: %s", query, videos)
        raise YouTubeError(str(videos)[:200])

    return [
        replace(candidate, catalog=candidate.youtube_id in catalog) for candidate in videos
    ]


async def search_music(
    redis: Redis,
    query: str,
    limit: int = 8,
    ttl_s: int = 3600,
    metadata_ttl_s: int = 24 * 3600,
    max_duration_s: int = 0,
) -> list[TrackMetadata]:
    """Songs matching ``query``, best answer first.

    The whole result is cached, not just the ids: a flat listing already
    carries everything a result row shows, so a search costs two upstream
    requests and no extractions at all.
    """
    query = normalize_query(query)
    if not query:
        return []

    key = _search_key(query)
    cached = await redis.get(key)
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return [TrackMetadata(**row) for row in json.loads(cached)][:limit]
        except (json.JSONDecodeError, TypeError):
            pass  # poisoned entry; fall through and search again

    candidates = await _gather_candidates(query, SEARCH_POOL)
    results = [
        candidate.as_metadata()
        for candidate in rank_candidates(query, candidates, limit, max_duration_s)
    ]

    await redis.set(key, json.dumps([r.as_dict() for r in results]), ex=ttl_s)
    # Seed the per-id metadata cache too, so queueing one of these results
    # costs nothing upstream either. Only where the listing was complete: a
    # missing duration would otherwise be cached for a day as "0 seconds".
    await asyncio.gather(
        *(
            redis.set(_metadata_key(r.youtube_id), json.dumps(r.as_dict()), ex=metadata_ttl_s)
            for r in results
            if r.duration_s > 0
        )
    )
    return results


async def radio_for(
    redis: Redis,
    youtube_id: str,
    limit: int = 25,
    ttl_s: int = 6 * 3600,
) -> list[str]:
    """Ids of YouTube's radio mix for a track — songs that go with this one.

    Returned as bare ids: the caller filters against what the room has already
    played before it is worth paying for metadata.
    """
    if not _ID_RE.match(youtube_id):
        return []
    url = _RADIO_URL.format(video=youtube_id)
    ids = await _cached_ids(redis, _radio_key(youtube_id), url, limit, ttl_s)
    # The mix always opens with the seed itself.
    return [candidate for candidate in ids if candidate != youtube_id]


async def playlist_ids(redis: Redis, url: str, limit: int = 100, ttl_s: int = 3600) -> list[str]:
    """Ids behind a playlist URL, for a host-supplied fallback playlist."""
    return await _cached_ids(redis, f"streamchen:playlist:{url}", url, limit, ttl_s)


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
