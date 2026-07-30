"""Hourly news — the one thing on the stream nobody in the room queued.

A radio has news on the hour, so a room can too: once an hour (or whatever its
host set) the worker plays one short bulletin from a podcast feed and then goes
back to the queue. Three rules make that bearable rather than annoying:

* it never interrupts. A bulletin is played *between* tracks, at the boundary
  the playback loop was going to have anyway (see ``app/worker/player.py``);
* it is short. The audio comes from a feed whose editions run from nine minutes
  to an hour, and only the short ones qualify (``NEWS_MAX_DURATION_S``);
* it is current. Always the newest edition, and never one older than
  ``NEWS_MAX_AGE_H``. A station that publishes a short bulletin twice a day is
  normal, so the same one being heard again on the next hour is the expected
  case — going *backwards* through the feed to find something unheard is not,
  because yesterday's news is worse than the same news again.

Where the audio comes from is the *operator's* choice — one feed URL for the
instance — and whether a room plays it is the *host's*. That split is
deliberate: a per-room feed field would be a listener-supplied URL the server
fetches, which is a hole nobody needs. The default is the ORF Ö1 Journale feed:
Austrian, and its short editions — "Frühjournal um 6" and "Journal um 5" — run
about nine or ten minutes, against twenty to sixty for the rest of that feed.

Nothing here is ever fatal. A feed that is down, malformed or slow means no news
this hour, which is exactly the room the feature was added to.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings
from app.models import Room, as_utc, utcnow

logger = logging.getLogger(__name__)

# What the worker calls a bulletin in the now-playing payload. A track id can
# never collide with it: those are UUIDs.
KIND = "news"

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

# A podcast feed is a few hundred kilobytes of XML. Anything past this is not
# one, and the point of reading it in a thread is not to read it forever.
MAX_FEED_BYTES = 4 * 1024 * 1024
FEED_TIMEOUT_S = 15

# What is cached is the feed's answer, not the decision taken from it — only the
# newest edition is ever played. Keeping a couple behind it costs nothing and
# makes the cached value worth reading when a room played something surprising.
KEEP_EPISODES = 3

USER_AGENT = f"streamchen/{__version__} (+podcast news)"


class NewsError(Exception):
    """The feed could not be turned into a bulletin."""


@dataclass(frozen=True)
class Bulletin:
    """One episode of a news feed, ready to be played."""

    # The feed's guid (or, failing that, the audio URL): which *edition* this is.
    id: str
    title: str
    audio_url: str
    duration_s: int
    # The station, for the player card. Not from the feed: an operator naming
    # their source is more use than a channel title nobody set.
    source: str
    # ISO 8601, because this travels through the Redis cache as JSON.
    published: str = ""

    @property
    def published_at(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.published)
        except (TypeError, ValueError):
            return None

    def is_current(self, max_age_h: int, now: datetime | None = None) -> bool:
        """Whether this is still news rather than history."""
        when = self.published_at
        if when is None:
            return False
        return (now or utcnow()) - when <= timedelta(hours=max(1, max_age_h))

    @property
    def cache_key(self) -> str:
        """Stem for the audio cache.

        The cache addresses files by stem and protects them by stem, so a
        bulletin needs one that no video id can collide with and that is safe as
        a filename whatever the feed's guid looks like.
        """
        return "news-" + hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:24]


# --- Feed reading -----------------------------------------------------------
def _feed_key(url: str) -> str:
    return "streamchen:news:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _fetch(url: str) -> str:
    """Blocking. Call through ``asyncio.to_thread``."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_S) as response:
            raw = response.read(MAX_FEED_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NewsError(f"could not fetch the news feed: {exc}"[:200]) from exc

    if len(raw) > MAX_FEED_BYTES:
        raise NewsError("news feed is larger than the cap")
    return raw.decode("utf-8", errors="replace")


def parse_duration(value: str | None) -> int:
    """iTunes durations come as ``625``, ``10:25`` or ``01:10:25``."""
    if not value:
        return 0
    try:
        parts = [int(float(part)) for part in value.strip().split(":")]
    except ValueError:
        return 0

    total = 0
    for part in parts:
        total = total * 60 + part
    return max(0, total)


def _published(item: ElementTree.Element) -> datetime:
    """The item's date, or the beginning of time if it has none — an item
    without one sorts last rather than being dropped."""
    raw = (item.findtext("pubDate") or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=UTC)
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _audio_url(item: ElementTree.Element) -> str | None:
    for enclosure in item.findall("enclosure"):
        url = (enclosure.get("url") or "").strip()
        kind = (enclosure.get("type") or "").lower()
        if url.startswith(("http://", "https://")) and (not kind or kind.startswith("audio")):
            return url
    return None


def parse_feed(xml: str, source: str, *, max_duration_s: int) -> list[Bulletin]:
    """Every episode that could be a bulletin, newest first.

    An episode with no duration in the feed is skipped rather than guessed at:
    the whole promise of this feature is a short interruption, and an item that
    will not say how long it is could be a half-hour programme.

    The feed is XML from a URL the operator configured, read with the standard
    library parser and capped in size on the way in. It is not user input.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise NewsError(f"news feed is not valid XML: {exc}"[:200]) from exc

    items = sorted(root.iter("item"), key=_published, reverse=True)

    bulletins: list[Bulletin] = []
    for item in items:
        audio_url = _audio_url(item)
        if audio_url is None:
            continue

        duration = parse_duration(item.findtext(f"{ITUNES}duration"))
        if duration <= 0 or duration > max_duration_s:
            continue

        title = " ".join((item.findtext("title") or "News").split())[:200]
        guid = (item.findtext("guid") or "").strip() or audio_url
        bulletins.append(
            Bulletin(
                id=guid[:200],
                title=title,
                audio_url=audio_url,
                duration_s=duration,
                source=source,
                # Kept rather than judged here: how old is too old is a
                # decision with a clock in it, and this function has none.
                published=_published(item).isoformat(),
            )
        )

    return bulletins


async def episodes(redis: Redis, settings: Settings) -> list[Bulletin]:
    """The feed's short editions, newest first, cached for every room.

    One fetch serves every room on the instance for ``NEWS_FEED_TTL_S``: a news
    feed changes a few times an hour at most, and a hundred rooms asking it
    hourly is a hundred requests to somebody else's server for one answer.
    """
    key = _feed_key(settings.news_feed_url)

    cached = await redis.get(key)
    if cached:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            return [Bulletin(**row) for row in json.loads(cached)]
        except (json.JSONDecodeError, TypeError):
            pass  # poisoned entry; fall through and re-read the feed

    xml = await asyncio.to_thread(_fetch, settings.news_feed_url)
    found = parse_feed(
        xml, settings.news_source_label, max_duration_s=settings.news_max_duration_s
    )[:KEEP_EPISODES]

    # Cached even when empty: a feed that currently has nothing short enough
    # should be asked again on the next TTL, not on the next track.
    await redis.set(key, json.dumps([asdict(item) for item in found]), ex=settings.news_feed_ttl_s)
    return found


# --- What a room is owed ----------------------------------------------------
def due(room: Room, now: datetime | None = None) -> bool:
    """Whether this room is owed a bulletin.

    A room that has never had one is owed one immediately: a host turning news
    on means "let me hear it", and waiting an hour to find out whether the
    switch did anything is not an answer.
    """
    if not room.news_enabled:
        return False

    last = as_utc(room.news_last_played_at)
    if last is None:
        return True

    interval = timedelta(minutes=max(1, room.news_interval_min or 60))
    return (now or utcnow()) - last >= interval


async def pending(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    room_id: uuid.UUID,
) -> Bulletin | None:
    """The bulletin this room should hear next, or None if it should hear none.

    Always the feed's newest short edition — the same one again if the station
    has not published since, which through an afternoon is what "the news" means
    on any radio. What is never done is reaching further down the feed for
    something this room has not heard: that is yesterday.

    None is the normal answer: news is off, or not due yet, or the newest
    edition is too old to be news at all.
    """
    if not settings.news_available:
        return None

    room = (await db.execute(select(Room).where(Room.id == room_id))).scalar_one_or_none()
    if room is None or not due(room):
        return None

    try:
        found = await episodes(redis, settings)
    except NewsError as exc:
        logger.info("room %s: news feed unavailable (%s)", room.token, exc)
        return None

    if not found:
        return None

    newest = found[0]
    if not newest.is_current(settings.news_max_age_h):
        logger.info(
            "room %s: newest bulletin (%s) is too old to play", room.token, newest.published
        )
        return None

    if newest.id == room.news_last_episode:
        logger.info("room %s: repeating the current bulletin (%s)", room.token, newest.title)
    return newest


async def claim(db: AsyncSession, room_id: uuid.UUID, bulletin: Bulletin) -> bool:
    """Take this room's news slot for the bulletin. Commits. False means don't.

    The last gate before a bulletin goes on air, and the reason there is one: a
    clip is fetched minutes before it plays, and a host who switched news off in
    those minutes should get their music, not one last bulletin.

    The timestamp written here is the cadence — it is what ``due`` measures the
    next hour from — and it is written when the bulletin *starts* rather than
    when it ends, so a clip that turns out to be undecodable costs one slot
    instead of being retried all hour. The episode id decides nothing; it is
    here so that "why did it play that one twice?" has an answer in the room's
    own row.
    """
    room = (await db.execute(select(Room).where(Room.id == room_id))).scalar_one_or_none()
    if room is None or not room.news_enabled:
        return False

    room.news_last_played_at = utcnow()
    room.news_last_episode = bulletin.id[:200]
    await db.commit()
    return True
