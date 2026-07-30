"""The hourly news bulletin (app/news.py).

Three properties are worth holding on to, and they are what this file is about:
a bulletin is short, it is current, and it never interrupts a song. The first two
are decided when the feed is read; the third is decided by *where* the playback
loop is allowed to play one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from app import events, news
from app.config import Settings
from app.models import Room, utcnow
from app.security import hash_secret
from app.service import get_room
from app.worker import player as player_module
from app.worker.cache import AudioCache
from app.worker.download import download_clip
from app.worker.player import ReadyBulletin, RoomPlayer
from tests.conftest import video_id


def _item(guid: str, title: str, *, ago: timedelta, duration: str = "", audio: bool = True) -> str:
    """One feed item, dated relative to now.

    Relative on purpose: whether a bulletin is *current* is half of what this
    module decides, and a feed pinned to fixed dates would quietly stop testing
    that the day after it was written.
    """
    when = format_datetime(utcnow() - ago)
    parts = [f"<title>{title}</title>", f"<guid>{guid}</guid>", f"<pubDate>{when}</pubDate>"]
    if duration:
        parts.append(f"<itunes:duration>{duration}</itunes:duration>")
    if audio:
        parts.append(
            f'<enclosure url="https://podcast.example.at/{guid}.mp3" type="audio/mpeg"/>'
        )
    return "<item>" + "".join(parts) + "</item>"


def _feed(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        "<channel><title>Ö1 Journale</title>" + "".join(items) + "</channel></rss>"
    )


# What a real news feed looks like: a couple of short editions a day, several
# long programmes, and the occasional item that is not audio at all.
FEED = _feed(
    _item("journal-long", "Morgenjournal um 7", ago=timedelta(minutes=30), duration="1998"),
    _item("journal-6", "Frühjournal um 6", ago=timedelta(hours=1), duration="00:10:25"),
    _item("journal-unknown", "An edition that will not say how long it is",
          ago=timedelta(hours=2)),
    _item("article", "A written article, no audio at all", ago=timedelta(minutes=20),
          duration="300", audio=False),
    _item("journal-17", "Journal um 5", ago=timedelta(hours=3), duration="550"),
)

# The same station, days later, with nothing new published.
STALE_FEED = _feed(_item("journal-old", "Journal um 5", ago=timedelta(days=3), duration="550"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        news_feed_url="https://podcast.example.at/feed.xml",
        news_source_label="Test Radio",
        news_max_duration_s=700,
        add_rate_limit=100,
        vote_rate_limit=100,
    )


@pytest.fixture
def feed(monkeypatch) -> list[str]:
    """No network: the feed is a string, and every fetch is recorded."""
    calls: list[str] = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        return FEED

    monkeypatch.setattr(news, "_fetch", fake_fetch)
    return calls


async def make_room(api, **kwargs) -> Room:
    async with api.state.sessionmaker() as db:
        room = Room(
            token=f"news-room-{uuid.uuid4().hex}"[:64],
            name="News Room",
            host_secret_hash=hash_secret("secret"),
            **kwargs,
        )
        db.add(room)
        await db.commit()
        return room


# --- Reading the feed -------------------------------------------------------
def test_durations_come_in_every_shape_a_feed_uses():
    assert news.parse_duration("625") == 625
    assert news.parse_duration("10:25") == 625
    assert news.parse_duration("01:10:25") == 4225
    assert news.parse_duration("") == 0
    assert news.parse_duration("soon") == 0


def test_only_short_editions_with_audio_are_bulletins():
    found = news.parse_feed(FEED, "Test Radio", max_duration_s=700)

    # The half-hour Morgenjournal, the edition with no duration and the item
    # with no audio are all out; what is left is what a room can carry.
    assert [item.id for item in found] == ["journal-6", "journal-17"]
    assert found[0].duration_s == 625
    assert found[0].audio_url == "https://podcast.example.at/journal-6.mp3"
    assert found[0].source == "Test Radio"


def test_the_newest_edition_comes_first_whatever_order_the_feed_is_in():
    """Feeds are usually newest-first and are not required to be."""
    found = news.parse_feed(FEED, "Test Radio", max_duration_s=700)

    assert found[0].title == "Frühjournal um 6"


def test_how_old_a_bulletin_is_survives_the_cache():
    """Age is judged where there is a clock, so it travels as data — through
    JSON in Redis and out the other side."""
    found = news.parse_feed(FEED, "Test Radio", max_duration_s=700)

    assert found[0].is_current(max_age_h=24) is True
    assert news.parse_feed(STALE_FEED, "Test Radio", max_duration_s=700)[0].is_current(24) is False


def test_a_feed_that_is_not_xml_is_a_news_error():
    with pytest.raises(news.NewsError):
        news.parse_feed("<not xml", "Test Radio", max_duration_s=700)


def test_a_bulletin_has_a_cache_key_no_video_id_can_collide_with():
    bulletin = news.parse_feed(FEED, "Test Radio", max_duration_s=700)[0]

    assert bulletin.cache_key.startswith("news-")
    # A cache key is a filename stem, whatever the feed's guid looked like.
    assert "/" not in bulletin.cache_key and "." not in bulletin.cache_key


async def test_one_feed_read_serves_every_room(api, settings, feed):
    """A hundred rooms asking hourly is one answer, not a hundred requests to
    somebody else's server."""
    first = await news.episodes(api.state.redis, settings)
    second = await news.episodes(api.state.redis, settings)

    assert [item.id for item in first] == [item.id for item in second]
    assert len(feed) == 1


# --- Whether a room is owed one ---------------------------------------------
def test_news_is_off_until_a_host_turns_it_on():
    assert news.due(Room(news_enabled=False, news_interval_min=60)) is False


def test_a_room_that_has_never_had_news_is_owed_one_now():
    """Turning the switch on has to do something audible, or a host cannot tell
    whether it worked."""
    assert news.due(Room(news_enabled=True, news_interval_min=60)) is True


def test_the_next_bulletin_waits_out_the_interval():
    room = Room(news_enabled=True, news_interval_min=60, news_last_played_at=utcnow())

    assert news.due(room) is False
    assert news.due(room, now=utcnow() + timedelta(minutes=59)) is False
    assert news.due(room, now=utcnow() + timedelta(minutes=60)) is True


async def test_a_room_gets_the_newest_short_edition(api, settings, feed):
    """Newest of the ones it *can* play: the half-hour Morgenjournal above it in
    the feed is not a bulletin however recent it is."""
    room = await make_room(api, news_enabled=True, news_interval_min=60)

    async with api.state.sessionmaker() as db:
        bulletin = await news.pending(db, api.state.redis, settings, room.id)

    assert bulletin is not None
    assert bulletin.id == "journal-6"


async def test_the_current_bulletin_is_repeated_rather_than_reaching_into_yesterday(
    api, settings, feed
):
    """A station publishing a short edition twice a day is normal, so hearing
    the same one again on the next hour is right. Going further down the feed for
    something unheard would mean playing yesterday's news as though it were
    today's."""
    room = await make_room(
        api,
        news_enabled=True,
        news_interval_min=60,
        news_last_played_at=utcnow() - timedelta(hours=2),
        news_last_episode="journal-6",
    )

    async with api.state.sessionmaker() as db:
        bulletin = await news.pending(db, api.state.redis, settings, room.id)

    assert bulletin is not None and bulletin.id == "journal-6"


async def test_a_feed_that_has_gone_quiet_for_days_plays_nothing(api, settings, monkeypatch):
    """Silence is the right answer once the newest edition stops being news."""
    monkeypatch.setattr(news, "_fetch", lambda _url: STALE_FEED)
    room = await make_room(api, news_enabled=True)

    async with api.state.sessionmaker() as db:
        assert await news.pending(db, api.state.redis, settings, room.id) is None


async def test_nothing_is_pending_while_news_is_off(api, settings, feed):
    room = await make_room(api, news_enabled=False)

    async with api.state.sessionmaker() as db:
        assert await news.pending(db, api.state.redis, settings, room.id) is None
    # The feed is not even read for a room that does not want news.
    assert feed == []


async def test_a_feed_that_is_down_means_no_news_and_no_error(api, settings, monkeypatch):
    def explode(_url: str) -> str:
        raise news.NewsError("connection refused")

    monkeypatch.setattr(news, "_fetch", explode)
    room = await make_room(api, news_enabled=True)

    async with api.state.sessionmaker() as db:
        assert await news.pending(db, api.state.redis, settings, room.id) is None


async def test_an_instance_with_no_feed_configured_plays_no_news(api, feed):
    blank = Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        news_feed_url="",
    )
    room = await make_room(api, news_enabled=True)

    assert blank.news_available is False
    async with api.state.sessionmaker() as db:
        assert await news.pending(db, api.state.redis, blank, room.id) is None


async def test_playing_a_bulletin_records_which_one_and_when(api, settings, feed):
    room = await make_room(api, news_enabled=True)

    async with api.state.sessionmaker() as db:
        bulletin = await news.pending(db, api.state.redis, settings, room.id)
        assert await news.claim(db, room.id, bulletin) is True

    async with api.state.sessionmaker() as db:
        stored = await db.get(Room, room.id)
        assert stored.news_last_episode == "journal-6"
        assert stored.news_last_played_at is not None
        # And it is not owed another one for an hour.
        assert news.due(stored) is False


async def test_news_switched_off_after_the_clip_was_fetched_never_goes_on_air(
    api, settings, feed
):
    """A clip is fetched minutes before it plays. Those are minutes in which a
    host can change their mind, and the answer then is music."""
    room = await make_room(api, news_enabled=True)
    async with api.state.sessionmaker() as db:
        bulletin = await news.pending(db, api.state.redis, settings, room.id)

    async with api.state.sessionmaker() as db:
        stored = await db.get(Room, room.id)
        stored.news_enabled = False
        await db.commit()

    async with api.state.sessionmaker() as db:
        assert await news.claim(db, room.id, bulletin) is False


# --- Fetching the audio -----------------------------------------------------
def test_a_clip_lands_under_its_own_cache_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.worker.download.urllib.request.urlopen",
        lambda *args, **kwargs: _FakeResponse(b"audio bytes"),
    )

    path = download_clip("https://podcast.example.at/x.mp3", "news-abc", tmp_path, 1024)

    assert path == tmp_path / "news-abc.mp3"
    assert path.read_bytes() == b"audio bytes"
    # Nothing half-written left behind for a decoder to find.
    assert list(tmp_path.glob("*.part")) == []


def test_a_clip_over_the_cap_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.worker.download.urllib.request.urlopen",
        lambda *args, **kwargs: _FakeResponse(b"x" * 5000),
    )

    with pytest.raises(news.NewsError):
        download_clip("https://podcast.example.at/x.mp3", "news-abc", tmp_path, 1024)

    assert list(tmp_path.iterdir()) == []


class _FakeResponse:
    """Stands in for the HTTP response ``download_clip`` streams."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc) -> None:
        return None


# --- Where the player is allowed to play one --------------------------------
def make_player(settings: Settings, tmp_path, redis=None, sessionmaker=None) -> RoomPlayer:
    return RoomPlayer(
        settings=settings,
        sessionmaker=sessionmaker,
        redis=redis,
        room_id=uuid.uuid4(),
        token="newsroom",
        name="News Room",
        cache=AudioCache(directory=tmp_path / "audio", budget_bytes=10**9, ttl_s=600),
    )


def test_the_feed_is_not_asked_twice_in_a_row(settings, tmp_path):
    """``_request_news`` is called from the lookahead, which runs every couple of
    seconds. "Not due yet" is the answer nearly always, and it changes with the
    clock and nothing else."""
    player = make_player(settings, tmp_path)

    assert player._news_due() is True
    assert player._news_due() is False

    player._news_at -= player_module.NEWS_RETRY_S + 1
    assert player._news_due() is True


def test_the_first_ask_does_not_wait_out_the_backoff(settings, tmp_path, monkeypatch):
    """``time.monotonic`` counts from an arbitrary point — on Linux, boot — so a
    worker started on a machine that came up a minute ago sees a clock reading
    less than one retry window. "Never asked" has to be its own value, or that
    worker's first two minutes are spent believing it just asked."""
    monkeypatch.setattr(player_module.time, "monotonic", lambda: 12.0)
    player = make_player(settings, tmp_path)

    assert player._news_due() is True
    assert player._autoplay_due() is True


def test_an_instance_with_no_news_feed_never_looks(tmp_path):
    blank = Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        news_feed_url="",
    )
    assert make_player(blank, tmp_path)._news_due() is False


def test_a_prepared_bulletin_is_not_looked_for_again(settings, tmp_path):
    player = make_player(settings, tmp_path)
    player._news_ready = ReadyBulletin(
        bulletin=news.Bulletin(
            id="x", title="News", audio_url="https://x/y.mp3", duration_s=300, source="Test"
        ),
        path=tmp_path / "news.mp3",
    )

    assert player._news_due() is False


def test_a_bulletin_being_fetched_is_protected_from_every_cache_sweep(settings, tmp_path):
    """One worker plays many rooms out of one directory, so a sweep next door
    must not delete the clip this room is about to play."""
    player = make_player(settings, tmp_path)
    player.current_youtube_id = video_id(1)
    player._news_key = "news-abc"

    assert player.protected_keys == {video_id(1), "news-abc"}


async def test_a_bulletin_waits_for_the_song_that_is_playing(
    api, settings, tmp_path, feed, monkeypatch
):
    """The whole promise of the feature: news goes out *after* a song. Fetching
    it puts nothing on air — it only ever *plays* from the top of a tick, which
    is a place the loop can reach only once the previous track has finished."""
    room = await make_room(api, news_enabled=True)
    player = make_player(
        settings, tmp_path, redis=api.state.redis, sessionmaker=api.state.sessionmaker
    )
    player.room_id = room.id
    await events.mark_present(api.state.redis, room.id, "sess")

    def fake_clip(_url, key, directory, _max_bytes):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{key}.mp3"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(player_module, "download_clip", fake_clip)

    # What the lookahead does while a track is on air: fetch, and nothing else.
    player._request_news()
    await player._news_task

    assert player._news_ready is not None
    assert player._news_ready.bulletin.id == "journal-6"
    # And its audio is in the cache, under the key the sweeps protect.
    assert player.cache.has(player._news_ready.bulletin.cache_key)


async def test_no_bulletin_is_fetched_for_an_empty_room(api, settings, tmp_path, feed):
    """Otherwise a room with nobody in it spends its hourly slot on silence, and
    the next listener arrives to find it used."""
    room = await make_room(api, news_enabled=True)
    player = make_player(
        settings, tmp_path, redis=api.state.redis, sessionmaker=api.state.sessionmaker
    )
    player.room_id = room.id

    await player._prepare_news()

    assert player._news_ready is None
    assert feed == []


async def test_letting_go_of_a_room_leaves_no_news_audio_behind(api, settings, tmp_path):
    """Concept §12 covers a bulletin like anything else: no media survives."""
    player = make_player(
        settings, tmp_path, redis=api.state.redis, sessionmaker=api.state.sessionmaker
    )
    bulletin = news.Bulletin(
        id="x", title="News", audio_url="https://x/y.mp3", duration_s=300, source="Test"
    )
    path = player.cache.directory / f"{bulletin.cache_key}.mp3"
    path.write_bytes(b"audio")
    player._news_ready = ReadyBulletin(bulletin=bulletin, path=path)

    await player._discard_news()

    assert player._news_ready is None
    assert not path.exists()


# --- The host's switch ------------------------------------------------------
async def test_a_host_turns_news_on_and_off(client, room):
    response = await client.patch(
        f"/api/rooms/{room['token']}", json={"news_enabled": True, "news_interval_min": 90}
    )

    assert response.status_code == 200
    settings_out = response.json()["settings"]
    assert settings_out["news_enabled"] is True
    assert settings_out["news_interval_min"] == 90

    off = await client.patch(f"/api/rooms/{room['token']}", json={"news_enabled": False})
    assert off.json()["settings"]["news_enabled"] is False


async def test_news_is_off_in_a_new_room(client, room):
    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["settings"]["news_enabled"] is False
    assert state["settings"]["news_interval_min"] == 60


async def test_a_cadence_nobody_wants_is_refused(client, room):
    response = await client.patch(f"/api/rooms/{room['token']}", json={"news_interval_min": 2})

    assert response.status_code == 422


async def test_a_listener_cannot_turn_news_on(new_client, room):
    listener = await new_client()

    response = await listener.patch(
        f"/api/rooms/{room['token']}", json={"news_enabled": True}
    )

    assert response.status_code == 403


# --- What the room shows while a bulletin is on air -------------------------
async def _put_bulletin_on_air(api, token: str, **overrides) -> None:
    async with api.state.sessionmaker() as db:
        room = await get_room(db, token)

    payload = {
        "kind": news.KIND,
        "track_id": news.KIND,
        "title": "Journal um 5",
        "source": "Test Radio",
        "duration_s": 550,
        "started_at": datetime.now(UTC).timestamp() - 30,
    }
    payload.update(overrides)
    await events.set_now_playing(api.state.redis, room.id, payload)


async def test_the_room_says_the_news_is_on(api, client, room):
    await _put_bulletin_on_air(api, room["token"])

    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["now_playing"]["track"] is None
    assert state["now_playing"]["bulletin"]["title"] == "Journal um 5"
    assert state["now_playing"]["bulletin"]["source"] == "Test Radio"
    assert state["now_playing"]["position_s"] > 0


async def test_a_bulletin_left_behind_by_a_stopped_worker_is_not_still_playing(api, client, room):
    await _put_bulletin_on_air(
        api, room["token"], started_at=datetime.now(UTC).timestamp() - 5000
    )

    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["now_playing"]["bulletin"] is None


async def test_the_page_renders_the_bulletin_rather_than_nothing_playing(api, client, room):
    await _put_bulletin_on_air(api, room["token"])

    page = (await client.get(f"/r/{room['token']}")).text

    assert "Journal um 5" in page
    assert "Test Radio" in page
    assert "Nothing playing yet" not in page


async def test_the_host_panel_offers_news_when_the_instance_has_a_feed(client, room):
    page = (await client.get(f"/r/{room['token']}")).text

    assert 'data-host-action="news"' in page
    assert 'data-host-setting="news_interval_min"' in page


async def test_the_host_can_cut_a_bulletin_short(api, client, room):
    """There is no row to mark skipped, so the event is the whole message."""
    await _put_bulletin_on_air(api, room["token"])

    response = await client.post(f"/api/rooms/{room['token']}/skip")

    assert response.status_code == 202
    assert response.json() == {"skipped": news.KIND}


async def test_skipping_silence_is_still_a_conflict(client, room):
    response = await client.post(f"/api/rooms/{room['token']}/skip")

    assert response.status_code == 409
