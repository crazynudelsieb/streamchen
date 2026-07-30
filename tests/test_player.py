"""Gapless handover (concept §11).

The playback loop is not testable end to end without ffmpeg and yt-dlp, but the
parts that decide whether a transition stalls are: what the lookahead is allowed
to do with what it prepared, and whether two callers can end up downloading the
same track twice.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.worker import player as player_module
from app.worker.cache import AudioCache
from app.worker.pipeline import bytes_per_second
from app.worker.player import Downloads, PreparedTrack, RoomPlayer


class FakeDecoder:
    """Stands in for the ffmpeg process the lookahead starts early."""

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return 0


def make_cache(tmp_path) -> AudioCache:
    return AudioCache(directory=tmp_path / "audio", budget_bytes=10**9, ttl_s=600)


def make_player(settings: Settings, tmp_path) -> RoomPlayer:
    """A player with no database and no Redis: nothing exercised here reaches
    either of them."""
    return RoomPlayer(
        settings=settings,
        sessionmaker=None,
        redis=None,
        room_id=uuid.uuid4(),
        token="testroom",
        name="Test Room",
        cache=make_cache(tmp_path),
    )


def stub_downloads(monkeypatch, delay_s: float = 0.0) -> list[str]:
    """Replace the yt-dlp call with a file appearing after ``delay_s``, and
    return the list every call is recorded in."""
    calls: list[str] = []

    def fake_download(youtube_id: str, directory: Path) -> Path:
        calls.append(youtube_id)
        if delay_s:
            time.sleep(delay_s)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{youtube_id}.webm"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(player_module, "download_audio", fake_download)
    return calls


# --- What the lookahead prepared --------------------------------------------
async def test_a_prepared_decoder_is_used_for_the_track_that_was_claimed(settings, tmp_path):
    player = make_player(settings, tmp_path)
    track_id = uuid.uuid4()
    decoder = FakeDecoder()
    player._prepared = PreparedTrack(track_id=track_id, youtube_id="abc", decoder=decoder)

    taken = await player._take_prepared(track_id)

    assert taken is not None
    assert taken.decoder is decoder
    assert not decoder.terminated


async def test_a_prepared_decoder_is_thrown_away_when_the_queue_reordered(settings, tmp_path):
    """The lookahead only ever guesses what will be next: votes, a host
    promotion or another listener's turn can change the answer. The guess is
    checked against what actually got claimed, so being wrong costs a cold
    start and can never play the wrong song."""
    player = make_player(settings, tmp_path)
    decoder = FakeDecoder()
    player._prepared = PreparedTrack(track_id=uuid.uuid4(), youtube_id="abc", decoder=decoder)

    assert await player._take_prepared(uuid.uuid4()) is None
    assert decoder.terminated
    assert player._prepared is None


async def test_nothing_prepared_is_not_an_error(settings, tmp_path):
    player = make_player(settings, tmp_path)
    assert await player._take_prepared(uuid.uuid4()) is None


async def test_protected_keys_cover_playing_prepared_and_in_flight(settings, tmp_path, monkeypatch):
    """What the supervisor must not delete under this player's feet."""
    stub_downloads(monkeypatch, delay_s=0.2)
    player = make_player(settings, tmp_path)
    player.current_youtube_id = "playing"
    player.next_youtube_id = "chosen"
    player._prepared = PreparedTrack(
        track_id=uuid.uuid4(), youtube_id="warmed", decoder=FakeDecoder()
    )

    fetching = asyncio.create_task(player._downloads.fetch("arriving"))
    await asyncio.sleep(0.02)
    try:
        assert player.protected_keys == {"playing", "chosen", "warmed", "arriving"}
    finally:
        await fetching


# --- Timing the handover ----------------------------------------------------
def test_remaining_time_counts_down_as_the_encoder_is_fed(settings, tmp_path):
    player = make_player(settings, tmp_path)
    player._duration_s = 180
    player._written = bytes_per_second(settings) * 60

    assert player._remaining_s() == pytest.approx(120.0)


def test_a_track_of_unknown_length_counts_as_about_to_end(settings, tmp_path):
    """Which costs an early decoder and never a late one -- the failure that
    matters is starting the next track too late."""
    player = make_player(settings, tmp_path)
    player._duration_s = 0
    player._written = 0

    assert player._remaining_s() == 0.0


def test_a_track_played_past_its_stated_length_does_not_go_negative(settings, tmp_path):
    player = make_player(settings, tmp_path)
    player._duration_s = 10
    player._written = bytes_per_second(settings) * 30

    assert player._remaining_s() == 0.0


# --- Not pestering the radio ------------------------------------------------
def test_the_radio_is_asked_straight_away_the_first_time(settings, tmp_path):
    assert make_player(settings, tmp_path)._autoplay_due() is True


def test_the_radio_is_not_asked_again_on_the_next_poll(settings, tmp_path):
    """An empty queue is the normal state of a room the radio is carrying, and
    the lookahead polls every couple of seconds — asking every time would be a
    hundred mix lookups a track for an answer that has not changed."""
    player = make_player(settings, tmp_path)

    assert player._autoplay_due() is True
    assert player._autoplay_due() is False


def test_the_radio_is_asked_again_once_the_cooldown_has_passed(settings, tmp_path):
    player = make_player(settings, tmp_path)
    assert player._autoplay_due() is True

    # Pretend the whole cooldown went by rather than sleeping through it.
    player._autoplay_at -= player_module.AUTOPLAY_RETRY_S + 1
    assert player._autoplay_due() is True


# --- One download per track -------------------------------------------------
async def test_a_cached_track_is_never_downloaded_again(tmp_path, monkeypatch):
    calls = stub_downloads(monkeypatch)
    cache = make_cache(tmp_path)
    downloads = Downloads(cache)

    first = await downloads.fetch("abc")
    second = await downloads.fetch("abc")

    assert calls == ["abc"]
    assert first == second


async def test_a_second_caller_waits_for_the_download_already_running(tmp_path, monkeypatch):
    """The lookahead and the boundary both want the next track. Two yt-dlp
    processes writing one path would corrupt it, and the second one would be
    pure added delay at exactly the wrong moment."""
    calls = stub_downloads(monkeypatch, delay_s=0.1)
    downloads = Downloads(make_cache(tmp_path))

    first, second = await asyncio.gather(downloads.fetch("abc"), downloads.fetch("abc"))

    assert calls == ["abc"]
    assert first == second


async def test_a_cancelled_caller_does_not_take_the_download_with_it(tmp_path, monkeypatch):
    """A track ending cancels the lookahead. Abandoning a download that is
    nearly finished is how a handover used to pay for it twice."""
    calls = stub_downloads(monkeypatch, delay_s=0.1)
    cache = make_cache(tmp_path)
    downloads = Downloads(cache)

    waiting = asyncio.create_task(downloads.fetch("abc"))
    await asyncio.sleep(0.02)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert await downloads.fetch("abc") == cache.find("abc")
    assert calls == ["abc"]


async def test_a_failed_download_is_reported_to_whoever_asked(tmp_path, monkeypatch):
    def explode(youtube_id: str, directory: Path) -> Path:
        raise RuntimeError("gone")

    monkeypatch.setattr(player_module, "download_audio", explode)
    downloads = Downloads(make_cache(tmp_path))

    with pytest.raises(RuntimeError):
        await downloads.fetch("abc")
    assert downloads.pending == set()


async def test_a_failed_download_can_be_retried(tmp_path, monkeypatch):
    """A dead task must not be mistaken for one still in flight, or the track
    would never be fetched again."""
    attempts: list[str] = []

    def flaky(youtube_id: str, directory: Path) -> Path:
        attempts.append(youtube_id)
        if len(attempts) == 1:
            raise RuntimeError("first try")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{youtube_id}.webm"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(player_module, "download_audio", flaky)
    downloads = Downloads(make_cache(tmp_path))

    with pytest.raises(RuntimeError):
        await downloads.fetch("abc")
    assert (await downloads.fetch("abc")).name == "abc.webm"
    assert attempts == ["abc", "abc"]
