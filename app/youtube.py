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
from urllib.parse import ParseResult, parse_qs, quote, urlparse

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_EMBED_PATHS = ("/embed/", "/shorts/", "/v/", "/live/")

# Playlist ids have none of the tidiness of video ids: two characters (WL) up
# to album ids like OLAK5uy_…. The alphabet is all they reliably share.
_LIST_RE = re.compile(r"^[A-Za-z0-9_-]{2,100}$")
_ALBUM_PATH = "/browse/"
_YOUTUBE_HOSTS = ("youtube.com", "music.youtube.com", "youtube-nocookie.com")

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

# Mixes — YouTube's ``RD…`` lists — are stations rather than stored playlists,
# and YouTube serves them only from a watch URL: ``/playlist?list=RD…`` is
# answered with "This playlist type is unviewable". The seed video is therefore
# part of a mix's address rather than a detail of how a host copied the link,
# which is the whole reason a mix needs its own template here.
#
# ``RDCLAK`` ids are the exception: those are editorial playlists, fixed for
# everyone, and behave like any other list.
_MIX_URL = "https://www.youtube.com/watch?v={video}&list={listing}"
_DYNAMIC_MIX_RE = re.compile(r"^RD(?!CLAK)", re.IGNORECASE)


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


def _youtube_url(value: str) -> tuple[str, ParseResult] | None:
    """(bare host, parsed URL) if ``value`` points at a YouTube domain.

    The host is stripped of ``www.`` and ``m.`` so the callers below can match
    it exactly rather than by suffix — ``youtube.com.evil.test`` is not YouTube.
    """
    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host != "youtu.be" and host not in _YOUTUBE_HOSTS:
        return None
    return host, parsed


def parse_youtube_id(value: str) -> str | None:
    """Accept anything a listener might paste; return the 11-character id.

    Handles watch URLs, youtu.be, /shorts/, /embed/, /live/, extra query
    parameters, and a bare id. YouTube Music watch links come out the same way:
    the catalogue is a different front end onto the same videos.

    A link that names both a video and a playlist resolves to the video, which
    is what somebody adding one song to a queue meant by pasting it.
    """
    value = (value or "").strip()
    if not value:
        return None

    if _ID_RE.match(value):
        return value

    found = _youtube_url(value)
    if found is None:
        return None
    host, parsed = found

    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _ID_RE.match(candidate) else None

    if parsed.path in ("/watch", "/watch/"):
        candidate = parse_qs(parsed.query).get("v", [""])[0]
        return candidate if _ID_RE.match(candidate) else None

    for prefix in _EMBED_PATHS:
        if parsed.path.startswith(prefix):
            candidate = parsed.path[len(prefix) :].split("/")[0]
            return candidate if _ID_RE.match(candidate) else None

    return None


def parse_playlist_url(value: str) -> str | None:
    """A playlist link in canonical form, or None if it names no playlist.

    Canonical rather than verbatim because the URL is the cache key: the same
    album pasted with and without whatever the share sheet appended should be
    one entry, not two. Being a parser rather than a substring test also keeps
    ``https://example.com/playlist`` from reaching yt-dlp's generic extractor.

    YouTube Music is where a host copies a link when they mean *this record* —
    both the shared ``?list=OLAK5uy_…`` form and the ``/browse/MPREb_…`` one
    the address bar shows on an album page.

    A mix keeps its seed video, because without one there is no address that
    resolves: see :data:`_MIX_URL`. A mix link copied from somewhere that shows
    no video — ``/playlist?list=RD…`` — therefore names nothing fetchable and
    is rejected here rather than turned into a URL that always fails.
    """
    value = (value or "").strip()
    if not value:
        return None

    found = _youtube_url(value)
    if found is None:
        return None
    host, parsed = found

    # Album pages exist only on the music front end, and only under /browse/.
    if host == "music.youtube.com" and parsed.path.startswith(_ALBUM_PATH):
        album = parsed.path[len(_ALBUM_PATH) :].split("/")[0]
        if not _LIST_RE.match(album):
            return None
        return f"https://music.youtube.com/browse/{album}"

    query = parse_qs(parsed.query)
    playlist = query.get("list", [""])[0]
    if not _LIST_RE.match(playlist):
        return None

    if _DYNAMIC_MIX_RE.match(playlist):
        seed = query.get("v", [""])[0]
        if not _ID_RE.match(seed):
            return None
        return _MIX_URL.format(video=seed, listing=playlist)

    return f"https://www.youtube.com/playlist?list={playlist}"


def _mix_listing(value: str) -> str:
    """The ``RD…`` id ``value`` names, or "" if it names no mix at all."""
    value = (value or "").strip()
    if not value:
        return ""

    found = _youtube_url(value)
    listing = parse_qs(found[1].query).get("list", [""])[0] if found else value
    return listing if _LIST_RE.match(listing) and _DYNAMIC_MIX_RE.match(listing) else ""


def is_dynamic_mix(value: str) -> bool:
    """True if ``value`` names a station rather than a fixed playlist."""
    return bool(_mix_listing(value))


def is_seedless_mix(value: str) -> bool:
    """True for a mix link with no video in it, which nothing can resolve.

    The one case a host has to fix themselves: copying a mix from a page that
    shows no video loses the seed, and the seed is half the address.
    """
    return bool(_mix_listing(value)) and parse_playlist_url(value) is None


def _metadata_key(youtube_id: str) -> str:
    return f"streamchen:meta:{youtube_id}"


def _extract(youtube_id: str) -> dict[str, Any]:
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
        info = await asyncio.to_thread(_extract, youtube_id)
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
    return f"streamchen:radio:v2:{youtube_id}"


def _playlist_key(url: str) -> str:
    return f"streamchen:playlist:v2:{url}"


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


# --- Judging a track with no query ------------------------------------------
# Search ranks against what somebody typed. A radio pick has nothing to rank
# against, so these decide the only two questions left: is this a record at
# all, and is it by somebody the room has just heard.

# Not music. Blunter than the search penalties on purpose — nobody asked for
# this track, so passing over a good one costs nothing, where playing a film
# trailer to a room costs the room.
_NOT_MUSIC = {
    "trailer": 40.0, "reaction": 40.0, "review": 30.0, "interview": 30.0,
    "tutorial": 30.0, "lesson": 24.0, "podcast": 34.0, "documentary": 30.0,
    "gameplay": 44.0, "walkthrough": 44.0, "speedrun": 44.0, "cutscene": 40.0,
    "montage": 24.0, "asmr": 30.0, "meme": 26.0, "amv": 30.0,
}

# Music, but written for something else. Not wrong — a host may well want it —
# only the thing a mix drifts into when it runs out of the genre it started in,
# which is how a rock room ends up on orchestral cues.
_INCIDENTAL = {
    "ost": 22.0, "soundtrack": 22.0, "bgm": 22.0, "theme": 12.0,
    "orchestral": 12.0, "opening": 14.0, "ending": 14.0,
}

# 'The Avengers (From "The Avengers")' — music for a film says so in the title.
_SOURCE_RE = re.compile(r"\bfrom\s+[\"“'(]", re.IGNORECASE)

# Words that describe an upload rather than whoever made the record.
_CHANNEL_NOISE = re.compile(r"\b(?:official|music|records|topic|channel|band)\b")
_VEVO_RE = re.compile(r"vevo\b")


def artist_key(title: str | None, channel: str | None = None) -> str:
    """A stable-ish name for whoever a track is by, for keeping a run varied.

    The channel is the better answer where there is one — "Guns N' Roses" — but
    a flat playlist listing frequently has none, and then the half of the title
    before the dash is where everybody writes it instead.
    """
    name = _VEVO_RE.sub(" ", _CHANNEL_NOISE.sub(" ", _fold(channel)))
    if not name.strip() and " - " in (title or ""):
        name = _fold((title or "").split(" - ")[0])
    return " ".join(name.split())


def song_score(candidate: SearchCandidate, max_duration_s: int = 0) -> float:
    """How much a pool entry looks like a record, with no query to go on.

    Relative, not absolute: it only ever decides between candidates drawn from
    the same pool, so the numbers matter against each other and nowhere else.
    """
    title_tokens = set(_tokens(candidate.title))
    folded_title = _fold(candidate.title)
    score = 0.0

    for vocabulary in (_NOT_MUSIC, _INCIDENTAL, _VARIANTS):
        for word, penalty in vocabulary.items():
            if word in title_tokens:
                score -= penalty
    for phrase, penalty in _VARIANT_PHRASES:
        if phrase in folded_title:
            score -= penalty
    for phrase in _BULK_PHRASES:
        if phrase in folded_title:
            score -= 30.0
    if _DATE_RE.search(candidate.title) or _SOURCE_RE.search(candidate.title):
        score -= 18.0

    # The catalogue's own upload is the record itself, which is also the one
    # without the music video's spoken intro and its long fade of credits.
    if candidate.catalog:
        score += 14.0
    if candidate.channel and candidate.channel.casefold().endswith(_TOPIC_SUFFIX):
        score += 14.0
    if candidate.verified:
        score += 4.0

    # A room cannot play what it will refuse to accept, and a 40-second upload
    # of a 4-minute song is a clip of it.
    if max_duration_s and candidate.duration_s > max_duration_s:
        score -= 60.0
    if 0 < candidate.duration_s < 60:
        score -= 30.0

    return score


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


# --- Pools -------------------------------------------------------------------
# A mix or a playlist is not a search: nobody typed a query, so there is nothing
# to rank *against*. What the flat listing carries is still exactly what a radio
# pick has to reason about — who it is by, how long it runs, whether it is a
# song at all — so a pool keeps whole candidates. Reducing it to bare ids threw
# that away and then paid one extraction per id to get some of it back.


async def _cached_candidates(
    redis: Redis, key: str, url: str, limit: int, ttl_s: int
) -> list[SearchCandidate]:
    cached = await redis.get(key)
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return [SearchCandidate(**row) for row in json.loads(cached)]
        except (json.JSONDecodeError, TypeError):
            pass  # poisoned entry; fall through and re-extract

    try:
        entries = await asyncio.to_thread(_flat_entries, url, limit)
    except Exception as exc:  # yt-dlp raises its own hierarchy
        raise YouTubeError(str(exc)[:200]) from exc

    candidates: list[SearchCandidate] = []
    seen: set[str] = set()
    for entry in entries:
        candidate = _candidate_from_entry(entry)
        if candidate is not None and candidate.youtube_id not in seen:
            seen.add(candidate.youtube_id)
            candidates.append(candidate)

    await redis.set(key, json.dumps([asdict(c) for c in candidates]), ex=ttl_s)
    return candidates


async def radio_candidates(
    redis: Redis,
    youtube_id: str,
    limit: int = 25,
    ttl_s: int = 6 * 3600,
) -> list[SearchCandidate]:
    """YouTube's radio mix for a track — songs that go with this one.

    Unbounded, unlike a host's mix: re-seeding from what it returns keeps
    turning up material long after a station has run out of it. This is the
    pool that lets a room play until somebody stops it.
    """
    if not _ID_RE.match(youtube_id):
        return []
    url = _MIX_URL.format(video=youtube_id, listing=f"RD{youtube_id}")
    pool = await _cached_candidates(redis, _radio_key(youtube_id), url, limit, ttl_s)
    # The mix always opens with the seed itself.
    return [candidate for candidate in pool if candidate.youtube_id != youtube_id]


# --- Mixes -------------------------------------------------------------------
# One fetch of a mix returns a *window* onto the station — the tracks around
# whichever video seeded it — and re-seeding from something that window gave
# back returns a different window of the same station. Walking it that way
# reaches around sixty tracks before the windows start repeating: a mix is
# bounded, which makes it a pool rather than a feed, and coherent, which is why
# it is worth having at all.
#
# The walk is not something one request can wait for — five hops is five
# extractions and the better part of a minute. So a refresh takes a single hop
# and merges it into what earlier refreshes found: the pool climbs to its
# ceiling over a room's evening rather than inside one autoplay pick, and no
# pick ever pays more than one extraction for it.
MIX_WINDOW = 50
MIX_POOL_CAP = 200
MIX_POOL_TTL_S = 24 * 3600
MIX_REFRESH_S = 900


def _mix_key(url: str) -> str:
    return f"streamchen:mix:v1:{url}"


def _mix_fresh_key(url: str) -> str:
    return f"streamchen:mixfresh:v1:{url}"


def _mix_parts(url: str) -> tuple[str, str] | None:
    """(seed video, list id) for a canonical mix URL, or None if it is not one."""
    found = _youtube_url(url)
    if found is None:
        return None
    query = parse_qs(found[1].query)
    seed, listing = query.get("v", [""])[0], query.get("list", [""])[0]
    if not _ID_RE.match(seed) or not _DYNAMIC_MIX_RE.match(listing):
        return None
    return seed, listing


def _next_hop(pool: list[SearchCandidate], walked: list[str], seed: str) -> tuple[str, list[str]]:
    """Where to seed the next window from, and the walk that follows.

    The frontier is whatever the pool holds that has not been a seed yet.
    Seeding twice from the same place asks the same question twice, and a lap
    that runs out of new vantage points starts again from the host's own seed —
    which is also how a station that has drifted since yesterday gets noticed.
    """
    walked_set = set(walked)
    for candidate in pool:
        if candidate.youtube_id not in walked_set:
            return candidate.youtube_id, [*walked, candidate.youtube_id]
    return seed, [seed]


async def mix_candidates(
    redis: Redis,
    url: str,
    limit: int = MIX_WINDOW,
    ttl_s: int = MIX_POOL_TTL_S,
    refresh_s: int = MIX_REFRESH_S,
) -> list[SearchCandidate]:
    """The station behind a mix URL, widened by one window per refresh."""
    parts = _mix_parts(url)
    if parts is None:
        raise YouTubeError("that mix link names no video to start from")
    seed, listing = parts

    key = _mix_key(url)
    pool: list[SearchCandidate] = []
    walked: list[str] = []
    cached = await redis.get(key)
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            stored = json.loads(cached)
            pool = [SearchCandidate(**row) for row in stored["pool"]]
            walked = list(stored["walked"])
        except (json.JSONDecodeError, TypeError, KeyError):
            pool, walked = [], []  # poisoned entry; walk again from the seed

    # Already widened recently. The next hop is the next refresh's business.
    if pool and await redis.get(_mix_fresh_key(url)):
        return pool

    hop, walked = _next_hop(pool, walked, seed)
    try:
        entries = await asyncio.to_thread(
            _flat_entries, _MIX_URL.format(video=hop, listing=listing), limit
        )
    except Exception as exc:  # yt-dlp raises its own hierarchy
        if pool:
            # A pool that already has songs in it outranks a hop that failed:
            # the room keeps playing and the walk resumes at the next refresh.
            logger.info("mix %s: hop from %s failed (%s); keeping %d", url, hop, exc, len(pool))
            await redis.set(_mix_fresh_key(url), "1", ex=refresh_s)
            return pool
        raise YouTubeError(str(exc)[:200]) from exc

    seen = {candidate.youtube_id for candidate in pool}
    for entry in entries:
        candidate = _candidate_from_entry(entry)
        if candidate is not None and candidate.youtube_id not in seen:
            seen.add(candidate.youtube_id)
            pool.append(candidate)
    pool = pool[:MIX_POOL_CAP]

    await redis.set(key, json.dumps({"pool": [asdict(c) for c in pool], "walked": walked}), ex=ttl_s)
    await redis.set(_mix_fresh_key(url), "1", ex=refresh_s)
    return pool


async def playlist_candidates(
    redis: Redis, url: str, limit: int = 100, ttl_s: int = 3600
) -> list[SearchCandidate]:
    """What is behind a playlist URL, for a host-supplied radio playlist.

    A mix is one of the things a host pastes here, and it is a pool like any
    other once it is fetched — only the fetching differs.
    """
    if is_dynamic_mix(url):
        return await mix_candidates(redis, url)
    return await _cached_candidates(redis, _playlist_key(url), url, limit, ttl_s)
