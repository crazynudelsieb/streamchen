"""One room's playback loop.

Owns the room's Icecast source connection for as long as the room is alive,
and pushes exactly one thing into it at a time: a track, or silence. Playback
state lives here and nowhere else (concept §3) — the API only ever reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import autoplay, events
from app.config import Settings
from app.models import (
    STATE_FAILED,
    STATE_PLAYED,
    STATE_PLAYING,
    STATE_SKIPPED,
    Track,
    utcnow,
)
from app.service import playable_tracks
from app.worker.cache import AudioCache
from app.worker.download import download_audio
from app.worker.pipeline import (
    decoder_command,
    encoder_command,
    seconds_of,
    silence_chunk,
)
from app.youtube import YouTubeError

logger = logging.getLogger(__name__)

CHUNK_BYTES = 32 * 1024


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
        self._current_track_id: uuid.UUID | None = None
        self._prefetch: asyncio.Task | None = None

    @property
    def protected_keys(self) -> set[str]:
        return {key for key in (self.current_youtube_id, self.next_youtube_id) if key}

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
                    await self._stop_encoder()
                    await asyncio.sleep(2)
        finally:
            control.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control
            await self._cancel_prefetch()
            await self._stop_encoder()
            await events.set_now_playing(self.redis, self.room_id, None)

    async def _watch_control(self) -> None:
        """Listen for the host's skip. The API marks the row; this is only the
        nudge that saves us from polling the database every second."""
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
                if payload.get("type") == events.SONG_SKIPPED:
                    self._skip.set()
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
        track = await self._claim_next_track()
        if track is None:
            await self._autoplay()
            track = await self._claim_next_track()
        if track is None:
            await self._play_silence(self.settings.worker_poll_interval_s)
            return
        await self._play_track(track)

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

            upcoming = tracks[1].youtube_id if len(tracks) > 1 else None
            return {
                "id": track.id,
                "youtube_id": track.youtube_id,
                "title": track.title,
                "duration_s": track.duration_s,
                "thumbnail_url": track.thumbnail_url,
                "channel": track.channel,
                "next_youtube_id": upcoming,
            }

    async def _play_track(self, track: dict) -> None:
        self._skip.clear()
        self._current_track_id = track["id"]
        self.current_youtube_id = track["youtube_id"]
        self.next_youtube_id = track.get("next_youtube_id")

        try:
            path = await self._ensure_cached(track["youtube_id"])
        except YouTubeError as exc:
            logger.warning("room %s: %s is unplayable (%s)", self.token, track["title"], exc)
            await self._finish(track["id"], STATE_FAILED, str(exc))
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

        # Prepare the next track while this one plays: current + next, never
        # more (concept §11).
        self._start_prefetch(track.get("next_youtube_id"), keep=track["youtube_id"])

        skipped = await self._pump(path, track)

        await self._cancel_prefetch()
        self.cache.release(track["youtube_id"])
        await self._finish(track["id"], STATE_SKIPPED if skipped else STATE_PLAYED)
        await events.set_now_playing(self.redis, self.room_id, None)
        await events.publish(self.redis, self.room_id, events.QUEUE_CHANGED, {})
        self._current_track_id = None
        self.current_youtube_id = None

    async def _pump(self, path: Path, track: dict) -> bool:
        """Decode into the encoder. Returns True if the track was skipped."""
        decoder = await asyncio.create_subprocess_exec(
            *decoder_command(self.settings, str(path)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        written = 0
        last_report = time.monotonic()
        skipped = False

        try:
            assert decoder.stdout is not None
            while True:
                if self._skip.is_set():
                    skipped = True
                    break

                chunk = await decoder.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break

                await self._write(chunk)
                written += len(chunk)

                now = time.monotonic()
                if now - last_report >= 5:
                    last_report = now
                    await events.publish(
                        self.redis,
                        self.room_id,
                        events.PLAYBACK_POSITION,
                        {
                            "track_id": str(track["id"]),
                            "position_s": round(seconds_of(written, self.settings), 1),
                        },
                    )
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(decoder.wait(), timeout=5)

        return skipped

    async def _play_silence(self, seconds: float) -> None:
        """Keeps the mount connected between songs so listeners are not
        disconnected by an empty queue."""
        chunk = silence_chunk(self.settings, 0.25)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self._write(chunk)

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

    # --- Cache -----------------------------------------------------------
    async def _ensure_cached(self, youtube_id: str) -> Path:
        existing = self.cache.find(youtube_id)
        if existing is not None:
            return existing

        path = await asyncio.to_thread(download_audio, youtube_id, self.cache.directory)
        self.cache.enforce_budget(keep=(youtube_id,))
        return path

    def _start_prefetch(self, youtube_id: str | None, keep: str) -> None:
        if not youtube_id or self.cache.has(youtube_id):
            return

        async def _prefetch() -> None:
            try:
                await asyncio.to_thread(download_audio, youtube_id, self.cache.directory)
                self.cache.enforce_budget(keep=(keep, youtube_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Not fatal: the track is downloaded again when its turn comes.
                logger.info("room %s: prefetch of %s failed (%s)", self.token, youtube_id, exc)

        self._prefetch = asyncio.create_task(_prefetch())

    async def _cancel_prefetch(self) -> None:
        task, self._prefetch = self._prefetch, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
