"""Worker supervisor: one player per room, exactly one worker per room.

Rooms come and go, and there may be several worker containers. A short-lived
Redis lock per room decides who plays it; whoever holds it renews it, and if
that process dies the lock simply expires and another worker picks the room up
on its next sweep. That is the whole failover story (concept §3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import events
from app.config import Settings
from app.models import Room, utcnow
from app.service import prune_idle_rooms
from app.worker.cache import AudioCache
from app.worker.player import RoomPlayer

logger = logging.getLogger(__name__)


def _is_uuid(value: str) -> bool:
    """Redis is shared and its keys are strings; a room id that is not one is
    somebody else's key, not a room to play."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True

SWEEP_INTERVAL_S = 5

# The shortest gap between two sweeps. A wake is published every time a room is
# opened, so a busy instance can ask for one many times a second; without a
# floor the supervisor would answer each of them with a database round trip.
# Well under the delay anybody can perceive, so it costs a start nothing.
MIN_SWEEP_GAP_S = 0.4

# How long after a room was last touched it may still get a source without
# anybody being present. This covers the seconds between "a room was created"
# or "a song was added" and the browser that did it registering as a listener,
# so the mount is connected by the time somebody presses play. It is not a
# grace period for an empty room: presence is what keeps a stream running.
#
# Every request touching a room refreshes what this is measured against, so the
# window is also what an emptied room coasts on after its last listener has
# gone — and for as long as it is set to, something is encoding for nobody.
# Only the way *in* needs covering here: a browser that has loaded a room page
# opens its socket a moment later, and the way *out* has an exact signal of its
# own now (``events.LAST_LISTENER_GRACE_S``), so this no longer has to be
# generous enough to stand in for one.
STARTUP_GRACE = timedelta(seconds=30)

PRUNE_INTERVAL_S = 3600
PRUNE_LOCK_KEY = "streamchen:prune-lock"


async def _warm_extractor() -> None:
    """Import yt-dlp now rather than at the first download.

    It is a large package with a few thousand extractors in it, and the import
    lands on whichever room happens to be the first this worker plays — as a
    second or two of silence, at the one moment a room is being listened to
    hardest. Done here it lands on nobody.
    """
    def _import() -> None:
        import yt_dlp  # noqa: F401

    try:
        await asyncio.to_thread(_import)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - a broken install fails later too
        logger.warning("could not preload yt-dlp: %s", exc)


@dataclass
class ActiveRoom:
    player: RoomPlayer
    task: asyncio.Task
    renewer: asyncio.Task


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        redis: Redis,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.worker_id = worker_id or str(uuid.uuid4())
        self.cache = AudioCache(
            directory=settings.audio_cache_dir,
            budget_bytes=settings.audio_cache_budget_bytes,
            ttl_s=settings.audio_prefetch_ttl_s,
            # Every room in this worker shares the directory, so eviction has
            # to know about all of them, not just whoever triggered it.
            protected=self._protected_keys,
        )
        self.active: dict[uuid.UUID, ActiveRoom] = {}
        self._wake = asyncio.Event()

    async def run(self) -> None:
        # A previous process may have died mid-track. Nothing on disk is worth
        # keeping, and keeping it would break the retention promise.
        purged = self.cache.purge()
        if purged:
            logger.info("cleared %s leftover audio file(s) at startup", purged)

        logger.info("worker %s started", self.worker_id)
        wakeups = asyncio.create_task(self._watch_wakeups())
        warmup = asyncio.create_task(_warm_extractor())
        try:
            while True:
                # Cleared before the sweep, so a room that goes live *during*
                # one is picked up by the next instead of being missed.
                self._wake.clear()
                swept_at = time.monotonic()
                try:
                    await self._sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("supervisor sweep failed")

                # A brand new room should not wait out a whole sweep interval
                # before anything is connected to its mount: that wait is the
                # "I pressed play and nothing happened" at the start of a
                # room's life.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=SWEEP_INTERVAL_S)

                gap = MIN_SWEEP_GAP_S - (time.monotonic() - swept_at)
                if gap > 0:
                    await asyncio.sleep(gap)
        finally:
            warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warmup
            wakeups.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wakeups
            await self.shutdown()

    async def _watch_wakeups(self) -> None:
        """Rooms that just became live ask for a worker rather than waiting to
        be noticed. Losing one of these costs a sweep interval, nothing more,
        which is why it can be fire-and-forget on the publishing side."""
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(events.WAKE_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    self._wake.set()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(events.WAKE_CHANNEL)
                await pubsub.aclose()

    async def shutdown(self) -> None:
        for room_id in list(self.active):
            await self._release(room_id)
        self.cache.purge()
        logger.info("worker %s stopped", self.worker_id)

    # --- Sweep -----------------------------------------------------------
    async def _sweep(self) -> None:
        crashed = [room_id for room_id, entry in self.active.items() if entry.task.done()]
        for room_id in crashed:
            logger.warning("player for room %s exited; releasing", room_id)
        await self._release_all(crashed)

        self.cache.sweep(keep=self._protected_keys())
        await self._prune_idle_rooms()

        wanted = await self._rooms_needing_playback()
        wanted_ids = {room.id for room in wanted}

        # A room that no longer wants a source loses it here: the last listener
        # left, or the host stopped the stream. Encoder down, mount gone, room
        # untouched — and the queue it had is still there when it comes back.
        stale = [room_id for room_id in self.active if room_id not in wanted_ids]
        for room_id in stale:
            logger.info("room %s no longer needs a source; releasing", room_id)
        await self._release_all(stale)

        for room in wanted:
            if room.id in self.active:
                continue
            if not await self._acquire(room.id):
                continue
            await self._start(room)

    def _protected_keys(self) -> set[str]:
        """Current and next track of every room this worker is playing."""
        keys: set[str] = set()
        for entry in self.active.values():
            keys |= entry.player.protected_keys
        return keys

    async def _prune_idle_rooms(self) -> None:
        """Delete rooms nobody has touched in ROOM_IDLE_DAYS.

        Rooms are disposable, so this is the only housekeeping the system
        needs. The lock both rate-limits it to hourly and keeps several worker
        replicas from doing it at once.
        """
        if not await self.redis.set(PRUNE_LOCK_KEY, self.worker_id, nx=True, ex=PRUNE_INTERVAL_S):
            return

        async with self.sessionmaker() as db:
            removed = await prune_idle_rooms(db, self.settings)
            await db.commit()

        if removed:
            logger.info("pruned %s idle room(s)", removed)

    async def _rooms_needing_playback(self) -> list[Room]:
        """The rooms that should have a source right now.

        A stream follows its listeners: it runs while somebody is in the room
        and stops when the last of them leaves, because an encoder, a decoder
        and a download serving nobody is pure cost. The exceptions in both
        directions are the interesting part — a room that was just created or
        just had a song added gets a source before anybody has registered as
        present, so that pressing play works immediately; and a room whose host
        stopped the stream gets none however busy it is.
        """
        live = await events.live_room_ids(self.redis)
        cutoff = utcnow() - STARTUP_GRACE

        async with self.sessionmaker() as db:
            result = await db.execute(
                select(Room)
                .where(
                    Room.stream_stopped.is_(False),
                    or_(
                        Room.id.in_([uuid.UUID(value) for value in live if _is_uuid(value)]),
                        Room.last_active_at >= cutoff,
                    ),
                )
                .limit(200)
            )
            return list(result.scalars().all())

    # --- Locking ---------------------------------------------------------
    async def _acquire(self, room_id: uuid.UUID) -> bool:
        return bool(
            await self.redis.set(
                events.worker_lock_key(room_id),
                self.worker_id,
                nx=True,
                ex=self.settings.worker_lock_ttl_s,
            )
        )

    async def _renew(self, room_id: uuid.UUID) -> None:
        interval = max(1, self.settings.worker_lock_ttl_s // 3)
        key = events.worker_lock_key(room_id)
        while True:
            await asyncio.sleep(interval)
            holder = await self.redis.get(key)
            if holder != self.worker_id:
                logger.warning("lost lock for room %s", room_id)
                return
            await self.redis.expire(key, self.settings.worker_lock_ttl_s)

    async def _start(self, room: Room) -> None:
        logger.info("worker %s taking room %s", self.worker_id, room.token)
        player = RoomPlayer(
            settings=self.settings,
            sessionmaker=self.sessionmaker,
            redis=self.redis,
            room_id=room.id,
            token=room.token,
            name=room.name,
            cache=self.cache,
        )
        self.active[room.id] = ActiveRoom(
            player=player,
            task=asyncio.create_task(player.run()),
            renewer=asyncio.create_task(self._renew(room.id)),
        )

    async def _release_all(self, room_ids: Iterable[uuid.UUID]) -> None:
        """Let go of several rooms at once.

        One at a time is the obvious way to write this and puts every room's
        teardown in front of the next room's start: a worker holding a dozen
        rooms when a deploy lands, or one room whose ffmpeg needs a second to
        die, and the host who has just pressed "Start stream" somewhere else
        waits for all of it.
        """
        await asyncio.gather(*(self._release(room_id) for room_id in room_ids))

    async def _release(self, room_id: uuid.UUID) -> None:
        entry = self.active.pop(room_id, None)
        if entry is None:
            return

        for task in (entry.task, entry.renewer):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        # Only drop the lock if it is still ours.
        holder = await self.redis.get(events.worker_lock_key(room_id))
        if holder == self.worker_id:
            await self.redis.delete(events.worker_lock_key(room_id))
