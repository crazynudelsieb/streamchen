"""One room's playback loop.

Owns the room's Icecast source connection for as long as the room is alive,
and pushes exactly one thing into it at a time: a track, or silence. Playback
state lives here and nowhere else (concept §3) — the API only ever reads it.

Transitions are the hard part. The encoder consumes its input at real time, so
anything the loop does between two tracks — a database write, a yt-dlp
download, starting a decoder — is a hole in the broadcast of exactly that
length. So the work is moved off the boundary: while a track plays, a lookahead
decides what is next, downloads it, and starts its decoder shortly before the
handover, leaving the boundary itself with nothing to do but swap pipes.

Everything the lookahead does is a *guess* about what the queue will look like
when the current track ends. Votes reorder it, listeners arrive with a turn of
their own, the host promotes something. The guess is therefore checked against
what actually gets claimed (``_take_prepared``), so being wrong costs a cold
start and can never play the wrong song.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import autoplay, events, news
from app.config import Settings
from app.models import (
    STATE_FAILED,
    STATE_PLAYED,
    STATE_PLAYING,
    STATE_QUEUED,
    STATE_SKIPPED,
    Track,
    utcnow,
)
from app.service import playable_tracks
from app.worker.cache import AudioCache
from app.worker.download import download_audio, download_clip
from app.worker.loudness import Measurements
from app.worker.pipeline import (
    decoder_command,
    encoder_command,
    seconds_of,
    silence_chunk,
)
from app.youtube import YouTubeError

logger = logging.getLogger(__name__)

CHUNK_BYTES = 32 * 1024

# How often the lookahead re-asks the database what is coming next.
LOOKAHEAD_POLL_S = 2.0

# How close to the end of the current track the next one's decoder is started.
# Long enough that ffmpeg has opened the file, decoded its first frames and
# filled the pipe before we need a byte of it; short enough that a room is not
# holding a second decoder open for the length of a whole song.
PRESPAWN_LEAD_S = 20.0

# Silence goes out in small blocks so that a track becoming ready mid-gap waits
# for the block to drain and nothing longer.
SILENCE_BLOCK_S = 0.05

# How long the lookahead leaves the radio alone after asking it for a pick and
# not getting one. An empty queue is the *normal* state of a room the radio is
# carrying, so without this the lookahead would ask a hundred times a track.
AUTOPLAY_RETRY_S = 15.0

# How long the player leaves the news feed alone after a look that produced no
# bulletin. "Not due yet" is the answer nearly all of the time, and the answer
# only changes with the clock.
NEWS_RETRY_S = 120.0


@dataclass
class PreparedTrack:
    """A queue entry whose decoder is already running and blocked on a full
    pipe, waiting for the handover."""

    track_id: uuid.UUID
    youtube_id: str
    decoder: asyncio.subprocess.Process


@dataclass
class ReadyBulletin:
    """A news bulletin whose audio is already on disk, waiting for the current
    track to end. Never anything less than that: a bulletin still downloading
    is not something the next boundary is allowed to wait for."""

    bulletin: news.Bulletin
    path: Path


class Downloads:
    """One download per video id, shared by everyone who wants the file.

    Both the lookahead and the playback loop ask for the next track's audio —
    the lookahead early, the loop at the last moment if the lookahead did not
    get there first. Two yt-dlp processes writing one path would corrupt it, so
    the second caller waits on the first instead of starting over.
    """

    def __init__(self, cache: AudioCache) -> None:
        self._cache = cache
        self._tasks: dict[str, asyncio.Task[Path]] = {}

    @property
    def pending(self) -> set[str]:
        return {key for key, task in self._tasks.items() if not task.done()}

    async def fetch(self, youtube_id: str, keep: Iterable[str] = ()) -> Path:
        """The track's file, downloading it only if nobody else already is."""
        existing = self._cache.find(youtube_id)
        if existing is not None:
            return existing

        task = self._tasks.get(youtube_id)
        if task is None or task.done():
            task = asyncio.create_task(
                asyncio.to_thread(download_audio, youtube_id, self._cache.directory)
            )
            task.add_done_callback(_retrieve_failure)
            self._tasks = {key: t for key, t in self._tasks.items() if not t.done()}
            self._tasks[youtube_id] = task

        # Shielded on purpose: the caller waiting on this may be cancelled — a
        # skip, a lost room lock, the end of the track it was preparing for —
        # and abandoning a download that is nearly done is how a boundary ends
        # up paying for it twice.
        path = await asyncio.shield(task)
        self._cache.enforce_budget(keep=(youtube_id, *keep))
        return path

    def abandon(self) -> None:
        """Let go of every download still in flight.

        Cancelling the awaitable does not stop the thread yt-dlp runs in — that
        is not something ``to_thread`` can offer — so a file may still land
        after this returns. It is removed by the purge the next worker startup
        does, which is the same guarantee a killed worker has always relied on.
        """
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


def _retrieve_failure(task: asyncio.Task) -> None:
    """A download whose only caller went away still has to have its exception
    looked at, or asyncio complains when the task is collected."""
    if not task.cancelled():
        task.exception()


class RoomPlayer:
    """Plays a single room until cancelled."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        redis: Redis,
        room_id: uuid.UUID,
        token: str,
        name: str,
        cache: AudioCache,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.room_id = room_id
        self.token = token
        self.name = name
        self.cache = cache

        # What this player currently holds on disk. The supervisor reads these
        # so its cache sweep never deletes a file that is about to be needed.
        self.current_youtube_id: str | None = None
        self.next_youtube_id: str | None = None

        self._encoder: asyncio.subprocess.Process | None = None
        self._skip = asyncio.Event()
        # "The queue changed" — cuts a silence short so the first song of a
        # quiet room starts when it is added, not at the end of the poll.
        self._wake = asyncio.Event()
        self._current_track_id: uuid.UUID | None = None
        self._downloads = Downloads(cache)
        # How loud each cached file turned out to be. Filled in by the
        # lookahead while something else is on air, read when a decoder opens.
        self._loudness = Measurements(settings)
        self._lookahead: asyncio.Task | None = None
        self._prepared: PreparedTrack | None = None

        # Progress of the current track, in bytes handed to the encoder. The
        # encoder's pacing makes this a clock, which is how the lookahead knows
        # how long it has left.
        self._written = 0
        self._duration_s = 0
        self._idle_announced = False

        # When each of these was last asked, on the monotonic clock. None means
        # never — and it has to, because that clock counts from an arbitrary
        # point (on Linux, boot), so 0.0 does not mean "long ago", it means "at
        # boot", which on a machine that came up a minute ago is inside every
        # retry window.
        self._autoplay_at: float | None = None
        self._news_at: float | None = None

        # The hourly news bulletin: fetched while something else is on air, and
        # played at the next track boundary (app/news.py).
        self._news_ready: ReadyBulletin | None = None
        self._news_task: asyncio.Task | None = None
        self._news_key: str | None = None

    @property
    def protected_keys(self) -> set[str]:
        """Files no cache sweep may take: what is playing, what is prepared,
        and what is on its way down."""
        keys = {self.current_youtube_id, self.next_youtube_id, self._news_key}
        if self._prepared is not None:
            keys.add(self._prepared.youtube_id)
        return {key for key in keys if key} | self._downloads.pending

    # --- Lifecycle -------------------------------------------------------
    async def run(self) -> None:
        control = asyncio.create_task(self._watch_control())
        try:
            while True:
                try:
                    await self._ensure_encoder()
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("playback error in room %s", self.token)
                    await self._stop_lookahead()
                    await self._discard_prepared()
                    await self._stop_encoder()
                    await asyncio.sleep(2)
        finally:
            control.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control
            await self._stop_lookahead()
            await self._discard_prepared()
            await self._discard_news()
            self._downloads.abandon()
            self._loudness.abandon()
            await self._stop_encoder()
            await self._requeue_current()
            await events.set_now_playing(self.redis, self.room_id, None)

    async def _watch_control(self) -> None:
        """Listen for the host's skip and for queue changes. The API marks the
        rows; this is only the nudge that saves us from polling the database
        every second."""
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(events.channel(self.room_id))
        try:
            async for message in pubsub.listen():
                if not message or message.get("type") != "message":
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event = payload.get("type")
                if event == events.SONG_SKIPPED:
                    self._skip.set()
                    self._wake.set()
                elif event in (events.QUEUE_CHANGED, events.SONG_ADDED):
                    self._wake.set()
                elif event == events.ROOM_UPDATED:
                    # A setting changed, so what this player last decided about
                    # the room's news and about what the radio would pick may no
                    # longer be what its host wants. Ask both again at the next
                    # opportunity rather than in two minutes — a host who has
                    # just pasted a radio playlist is watching for it to start,
                    # and the cooldowns are there to stop *polling*, not to make
                    # somebody wait out an answer that has already changed.
                    self._news_at = None
                    self._autoplay_at = None
                    self._wake.set()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(events.channel(self.room_id))
                await pubsub.aclose()

    # --- Encoder ---------------------------------------------------------
    async def _ensure_encoder(self) -> None:
        if self._encoder is not None and self._encoder.returncode is None:
            return

        command = encoder_command(self.settings, f"/{self.token}.mp3", self.name)
        logger.info("room %s: connecting to icecast", self.token)
        self._encoder = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _stop_encoder(self) -> None:
        process, self._encoder = self._encoder, None
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=5)

    async def _write(self, data: bytes) -> None:
        """Blocks once the encoder's ``-re`` pacing fills the pipe, which is
        exactly the back-pressure that keeps us at real time."""
        if self._encoder is None or self._encoder.stdin is None:
            raise RuntimeError("encoder is not running")
        self._encoder.stdin.write(data)
        await self._encoder.stdin.drain()

    # --- Playback --------------------------------------------------------
    async def _tick(self) -> None:
        # Cleared before the decision, not after: a song added while we are
        # deciding must still cut the silence we are about to write.
        self._wake.clear()

        # A bulletin whose audio is already on disk goes out first. This is the
        # only place news is ever played, which is what makes "after a song,
        # never over one" true by construction: getting here means the previous
        # track has finished.
        if self._news_ready is not None:
            await self._play_bulletin()
            return

        self._request_news()

        track = await self._claim_next_track()
        if track is None:
            # Cold start only — during playback the lookahead has already done
            # this, early enough for the pick to be downloaded in time. The
            # radio's first pick reaches YouTube for a mix, so it is fed like
            # any other wait on this loop.
            await self._while_feeding(self._autoplay())
            track = await self._claim_next_track()
        if track is None:
            await self._idle(self.settings.worker_poll_interval_s)
            return
        await self._play_track(track)

    async def _idle(self, seconds: float) -> None:
        """Nothing to play. Hold the mount open with silence so listeners are
        not disconnected, and say so once rather than on every poll."""
        await self._stop_lookahead()
        await self._discard_prepared()
        self.next_youtube_id = None

        if not self._idle_announced:
            self._idle_announced = True
            await events.set_now_playing(self.redis, self.room_id, None)
            await events.publish(self.redis, self.room_id, events.QUEUE_CHANGED, {})

        await self._play_silence(seconds)

    async def _autoplay(self) -> None:
        """Let the radio pick something when nobody has requested anything.

        Failing here is never fatal: silence is the same outcome as before
        autoplay existed, and the next tick tries again.
        """
        try:
            async with self.sessionmaker() as db:
                await autoplay.top_up(db, self.redis, self.settings, self.room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("autoplay failed in room %s", self.token)

    # --- News ------------------------------------------------------------
    def _news_due(self) -> bool:
        """Whether it is worth asking about news again.

        Same shape as ``_autoplay_due``: the answer changes with the clock and
        with nothing else, so asking twice a track is already generous.
        """
        if not self.settings.news_available:
            return False
        if self._news_ready is not None:
            return False
        if self._news_task is not None and not self._news_task.done():
            return False

        now = time.monotonic()
        if self._news_at is not None and now - self._news_at < NEWS_RETRY_S:
            return False
        self._news_at = now
        return True

    def _request_news(self) -> None:
        """Start fetching the next bulletin, in the background.

        Deliberately not awaited. A download at a track boundary is a hole in
        the broadcast of exactly its own length, so the bulletin is fetched
        while something else is on air — a song, or the silence of an idle room
        — and is only ever played once its file is on disk.
        """
        if not self._news_due():
            return
        self._news_task = asyncio.create_task(self._prepare_news())
        self._news_task.add_done_callback(_retrieve_failure)

    async def _prepare_news(self) -> None:
        """Resolve the room's next bulletin and put its audio in the cache."""
        # Streaming a bulletin to nobody would spend the room's hourly slot on
        # an empty room, and the next listener would arrive to find it used.
        if not await events.anyone_present(self.redis, self.room_id):
            return

        async with self.sessionmaker() as db:
            bulletin = await news.pending(db, self.redis, self.settings, self.room_id)
        if bulletin is None:
            return

        self._news_key = bulletin.cache_key
        try:
            path = await asyncio.to_thread(
                download_clip,
                bulletin.audio_url,
                bulletin.cache_key,
                self.cache.directory,
                self.settings.news_max_bytes,
            )
        except asyncio.CancelledError:
            self._news_key = None
            raise
        except Exception as exc:
            # Never fatal: no news this hour is the room as it was before the
            # feature existed, and the next look tries again.
            self._news_key = None
            logger.info("room %s: could not fetch the news clip (%s)", self.token, exc)
            return

        self.cache.enforce_budget(keep=self.protected_keys)
        # Before the bulletin counts as ready, so the boundary never picks up a
        # clip that has not been levelled yet. A broadcaster's idea of how loud
        # a news bulletin is has nothing to do with the music around it, which
        # makes this the handover a listener would otherwise reach for the
        # volume at.
        await self._loudness.measure(bulletin.cache_key, path)
        self._news_ready = ReadyBulletin(bulletin=bulletin, path=path)
        logger.info("room %s: news ready (%s)", self.token, bulletin.title)

    async def _play_bulletin(self) -> None:
        """Put the prepared bulletin on air, between two songs."""
        ready, self._news_ready = self._news_ready, None
        bulletin = ready.bulletin

        # Asked once more, now, because the clip was fetched minutes ago: a host
        # who turned news off in the meantime gets their music.
        async with self.sessionmaker() as db:
            if not await news.claim(db, self.room_id, bulletin):
                self._release(bulletin.cache_key)
                self._news_key = None
                return

        self._skip.clear()
        self._idle_announced = False
        self._written = 0
        self._duration_s = bulletin.duration_s

        decoder = await self._open_decoder(ready.path, bulletin.cache_key)
        started = datetime.now(UTC).timestamp()

        await events.set_now_playing(
            self.redis,
            self.room_id,
            {
                "kind": news.KIND,
                # No track is playing, and no track id may ever match this.
                "track_id": news.KIND,
                "title": bulletin.title,
                "source": bulletin.source,
                "duration_s": bulletin.duration_s,
                "started_at": started,
            },
        )
        await events.publish(
            self.redis, self.room_id, events.NEWS_STARTED, {"title": bulletin.title}
        )

        # A bulletin is minutes of airtime in which the next song can be
        # downloaded and its decoder started, exactly like a track.
        self._start_lookahead()
        try:
            await self._pump(decoder, news.KIND)
        finally:
            await self._close_decoder(decoder)
            await self._stop_lookahead()
            # Concept §12 applies to a bulletin like anything else: the audio
            # does not survive playback.
            self._release(bulletin.cache_key)
            self._news_key = None
            self._duration_s = 0
            self._written = 0

    async def _discard_news(self) -> None:
        """Let go of the bulletin, prepared or still coming, and its audio."""
        task, self._news_task = self._news_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        ready, self._news_ready = self._news_ready, None
        key = self._news_key or (ready.bulletin.cache_key if ready is not None else None)
        self._news_key = None
        if key:
            self._release(key)

    async def _peek_next(self) -> dict | None:
        """What ``_claim_next_track`` would take, without taking it."""
        async with self.sessionmaker() as db:
            tracks = await playable_tracks(db, self.room_id)
            if not tracks:
                return None
            return {
                "id": tracks[0].id,
                "youtube_id": tracks[0].youtube_id,
                "title": tracks[0].title,
            }

    async def _claim_next_track(self) -> dict | None:
        """Take the top of the queue and mark it playing, atomically enough:
        one worker holds the room's lock, so there is no second claimant."""
        async with self.sessionmaker() as db:
            tracks = await playable_tracks(db, self.room_id)
            if not tracks:
                return None

            track = tracks[0]
            track.state = STATE_PLAYING
            track.started_at = utcnow()
            await db.commit()

            return {
                "id": track.id,
                "youtube_id": track.youtube_id,
                "title": track.title,
                "duration_s": track.duration_s,
                "thumbnail_url": track.thumbnail_url,
                "channel": track.channel,
            }

    async def _play_track(self, track: dict) -> None:
        self._skip.clear()
        self._idle_announced = False
        self._current_track_id = track["id"]
        self.current_youtube_id = track["youtube_id"]
        self._written = 0
        self._duration_s = track["duration_s"] or 0

        decoder = await self._decoder_for(track)
        if decoder is None:
            return

        started = datetime.now(UTC).timestamp()
        await events.set_now_playing(
            self.redis,
            self.room_id,
            {
                "track_id": str(track["id"]),
                "title": track["title"],
                "youtube_id": track["youtube_id"],
                "thumbnail_url": track["thumbnail_url"],
                "channel": track["channel"],
                "duration_s": track["duration_s"],
                "started_at": started,
            },
        )
        await events.publish(
            self.redis,
            self.room_id,
            events.SONG_STARTED,
            {"track_id": str(track["id"]), "title": track["title"]},
        )
        await events.publish(self.redis, self.room_id, events.QUEUE_CHANGED, {})

        self._start_lookahead()
        try:
            skipped = await self._pump(decoder, str(track["id"]))
        finally:
            await self._close_decoder(decoder)

        # Stops the loop, not the download it may have in flight: that is
        # shielded, and the next tick waits on the same task rather than
        # starting the fetch again from nothing.
        await self._stop_lookahead()
        self._release(track["youtube_id"])
        await self._finish(track["id"], STATE_SKIPPED if skipped else STATE_PLAYED)
        # Deliberately no event here. The next track's SONG_STARTED covers this
        # one ending, which means no client ever refetches the room during the
        # handover — and so never renders the moment where nothing is playing.
        self._current_track_id = None
        self.current_youtube_id = None

    async def _decoder_for(self, track: dict) -> asyncio.subprocess.Process | None:
        """The running decoder for a claimed track, or None if it is unplayable.

        Normally this is the process the lookahead already started, in which
        case there is nothing to wait for at all.
        """
        prepared = await self._take_prepared(track["id"])
        if prepared is not None:
            return prepared.decoder

        try:
            path = await self._while_feeding(
                self._downloads.fetch(track["youtube_id"], keep=self.protected_keys)
            )
        except YouTubeError as exc:
            logger.warning("room %s: %s is unplayable (%s)", self.token, track["title"], exc)
            await self._finish(track["id"], STATE_FAILED, str(exc))
            self._current_track_id = None
            self.current_youtube_id = None
            return None

        # Measured here rather than skipped. Getting this far means a cold
        # start — a hole in the broadcast that silence is already covering — and
        # the room's first song arriving at a different level than everything
        # after it is precisely what a listener notices. A track the lookahead
        # already measured costs nothing: the answer is waiting.
        await self._while_feeding(self._loudness.measure(track["youtube_id"], path))
        return await self._open_decoder(path, track["youtube_id"])

    async def _pump(self, decoder: asyncio.subprocess.Process, entry_id: str) -> bool:
        """Decode into the encoder. Returns True if it was skipped.

        ``entry_id`` is what the position reports are about: a track's id, or
        ``news.KIND`` for a bulletin, which is not a row in anything.
        """
        last_report = time.monotonic()
        skipped = False

        assert decoder.stdout is not None
        while True:
            if self._skip.is_set():
                skipped = True
                break

            chunk = await decoder.stdout.read(CHUNK_BYTES)
            if not chunk:
                break

            await self._write(chunk)
            self._written += len(chunk)

            now = time.monotonic()
            if now - last_report >= 5:
                last_report = now
                await events.publish(
                    self.redis,
                    self.room_id,
                    events.PLAYBACK_POSITION,
                    {
                        "track_id": entry_id,
                        "position_s": round(seconds_of(self._written, self.settings), 1),
                    },
                )

        return skipped

    async def _play_silence(self, seconds: float) -> None:
        """Keeps the mount connected between songs so listeners are not
        disconnected by an empty queue."""
        chunk = silence_chunk(self.settings, SILENCE_BLOCK_S)
        deadline = time.monotonic() + seconds
        # One block unconditionally: a wake arriving just before this would
        # otherwise let the idle loop go round without feeding the encoder at
        # all, and the encoder starving is the thing being avoided here.
        await self._write(chunk)
        while time.monotonic() < deadline and not self._wake.is_set():
            await self._write(chunk)

    async def _while_feeding[T](self, work: Awaitable[T]) -> T:
        """Await something slow without letting the encoder starve.

        Anything this loop waits on that touches the network — a cold-start
        download, the radio's first pick — is a hole in the broadcast of its
        own length, and Icecast disconnects a source that has gone quiet for
        ``source-timeout`` seconds, taking the mount and every listener on it
        with it. The lookahead keeps these waits off the boundary for as long
        as something is playing; this is what covers the cold start, where
        there is no current track to look ahead from.

        Silence is written at the encoder's pace, so waiting here costs real
        time rather than a busy loop.
        """
        task = asyncio.ensure_future(work)
        chunk = silence_chunk(self.settings, SILENCE_BLOCK_S)
        try:
            # One turn of the event loop first: work that never suspends — a
            # cache hit, a pick Redis already had — then costs no silence at
            # all, and a gapless boundary stays gapless.
            await asyncio.sleep(0)
            while not task.done():
                await self._write(chunk)
        except BaseException:
            # The encoder died under us. Let go of the work, but leave its
            # failure retrievable or asyncio complains when it is collected.
            # This does not abandon a download: ``Downloads.fetch`` shields the
            # real one, so the next attempt waits on it instead of starting
            # over.
            task.cancel()
            task.add_done_callback(_retrieve_failure)
            raise
        return await task

    async def _requeue_current(self) -> None:
        """Give the track that was playing back to the queue.

        A player is stopped mid-song whenever the room's source goes away: the
        last listener left, the host stopped the stream, this worker lost the
        lock. Its row is still marked playing and nothing will ever finish it,
        so it would sit in "now playing" forever and never be played. Handing
        it back is also what a listener expects — the stream returns and the
        song they queued is still waiting.
        """
        track_id, self._current_track_id = self._current_track_id, None
        self.current_youtube_id = None
        if track_id is None:
            return

        async with self.sessionmaker() as db:
            result = await db.execute(select(Track).where(Track.id == track_id))
            track = result.scalar_one_or_none()
            if track is None or track.state != STATE_PLAYING:
                return
            track.state = STATE_QUEUED
            track.started_at = None
            await db.commit()

        await events.publish(self.redis, self.room_id, events.QUEUE_CHANGED, {})

    async def _finish(self, track_id: uuid.UUID, state: str, error: str | None = None) -> None:
        async with self.sessionmaker() as db:
            result = await db.execute(select(Track).where(Track.id == track_id))
            track = result.scalar_one_or_none()
            if track is None:
                return
            # A host skip already wrote SKIPPED; do not overwrite their verdict.
            if track.state == STATE_PLAYING:
                track.state = state
            track.ended_at = utcnow()
            if error:
                track.error = error[:200]
            await db.commit()

    def _release(self, key: str) -> None:
        """Drop a finished entry's audio and what was measured about it. The
        two belong together: the numbers describe that file and nothing else."""
        self.cache.release(key)
        self._loudness.forget(key)

    # --- Decoders --------------------------------------------------------
    async def _open_decoder(self, path: Path, key: str) -> asyncio.subprocess.Process:
        """A decoder for one cached file, levelled with whatever was measured
        about it. Nothing measured is not a reason to wait for it here: the
        filter has a standing-start mode for exactly that."""
        return await asyncio.create_subprocess_exec(
            *decoder_command(self.settings, str(path), self._loudness.known(key)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _close_decoder(self, decoder: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            decoder.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(decoder.wait(), timeout=5)

    # --- Lookahead -------------------------------------------------------
    def _remaining_s(self) -> float:
        """How much of the current track is still to be written. A track whose
        duration we do not know counts as about to end, which costs nothing but
        an early decoder."""
        if self._duration_s <= 0:
            return 0.0
        return max(0.0, self._duration_s - seconds_of(self._written, self.settings))

    def _start_lookahead(self) -> None:
        if self._lookahead is not None and not self._lookahead.done():
            return
        self._lookahead = asyncio.create_task(self._lookahead_loop())

    async def _stop_lookahead(self) -> None:
        """Stop looking ahead. Anything already prepared is left alone — that is
        the whole point of having prepared it."""
        task, self._lookahead = self._lookahead, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _lookahead_loop(self) -> None:
        """Keep the next track ready for as long as this one plays."""
        while True:
            try:
                # News is due on the clock, so it usually comes due in the
                # middle of a song. This is the loop that is running then, and
                # fetching the bulletin here is what keeps it off the boundary.
                self._request_news()
                await self._lookahead_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never fatal: the boundary falls back to fetching the track
                # itself, which is what used to happen every time.
                logger.exception("lookahead failed in room %s", self.token)
            await asyncio.sleep(LOOKAHEAD_POLL_S)

    def _autoplay_due(self) -> bool:
        """Whether it is worth asking the radio again. Its answer only changes
        when the room's history does, so polling it is pure waste."""
        now = time.monotonic()
        if self._autoplay_at is not None and now - self._autoplay_at < AUTOPLAY_RETRY_S:
            return False
        self._autoplay_at = now
        return True

    async def _lookahead_step(self) -> None:
        upcoming = await self._peek_next()
        if upcoming is None and self._autoplay_due():
            # Top up while there is still audio playing, so the radio's mix
            # lookup and the download both happen off the critical path. This
            # is the difference between the radio carrying a room seamlessly
            # and it stalling for a few seconds at every handover.
            await self._autoplay()
            upcoming = await self._peek_next()

        if upcoming is None:
            self.next_youtube_id = None
            await self._discard_prepared()
            return

        # Reordered under us: what we warmed up is not what is next any more.
        if self._prepared is not None and self._prepared.track_id != upcoming["id"]:
            await self._discard_prepared()

        self.next_youtube_id = upcoming["youtube_id"]

        path = self.cache.find(upcoming["youtube_id"])
        if path is None:
            try:
                await self._downloads.fetch(upcoming["youtube_id"], keep=self.protected_keys)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Not fatal: the track is fetched again when its turn comes.
                logger.info(
                    "room %s: could not prefetch %s (%s)", self.token, upcoming["title"], exc
                )
            return

        # The whole reason the levelling can be a fixed gain rather than a
        # filter feeling its way to the target: measured here, minutes before
        # the track is due, while something else is on air. At the boundary
        # this would be a gap; here it is a few seconds of a CPU nobody is
        # waiting on.
        await self._loudness.measure(upcoming["youtube_id"], path)

        if self._prepared is None and self._remaining_s() <= PRESPAWN_LEAD_S:
            self._prepared = PreparedTrack(
                track_id=upcoming["id"],
                youtube_id=upcoming["youtube_id"],
                decoder=await self._open_decoder(path, upcoming["youtube_id"]),
            )

    async def _take_prepared(self, track_id: uuid.UUID) -> PreparedTrack | None:
        """The warmed-up decoder, but only for the track that was claimed.

        This is where the lookahead's guess is checked against reality, and the
        reason a reorder can only ever cost a cold start.
        """
        prepared, self._prepared = self._prepared, None
        if prepared is None:
            return None
        if prepared.track_id == track_id:
            return prepared
        await self._close_decoder(prepared.decoder)
        return None

    async def _discard_prepared(self) -> None:
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            await self._close_decoder(prepared.decoder)
