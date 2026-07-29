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
import uuid
from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import events
from app.config import Settings
from app.models import STATE_PLAYING, STATE_QUEUED, Room, Track, utcnow
from app.service import prune_idle_rooms
from app.worker.cache import AudioCache
from app.worker.player import RoomPlayer

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 5
IDLE_GRACE = timedelta(minutes=10)
PRUNE_INTERVAL_S = 3600
PRUNE_LOCK_KEY = "streamchen:prune-lock"


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
        )
        self.active: dict[uuid.UUID, ActiveRoom] = {}

    async def run(self) -> None:
        # A previous process may have died mid-track. Nothing on disk is worth
        # keeping, and keeping it would break the retention promise.
        purged = self.cache.purge()
        if purged:
            logger.info("cleared %s leftover audio file(s) at startup", purged)

        logger.info("worker %s started", self.worker_id)
        try:
            while True:
                try:
                    await self._sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("supervisor sweep failed")
                await asyncio.sleep(SWEEP_INTERVAL_S)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for room_id in list(self.active):
            await self._release(room_id)
        self.cache.purge()
        logger.info("worker %s stopped", self.worker_id)

    # --- Sweep -----------------------------------------------------------
    async def _sweep(self) -> None:
        for room_id, entry in list(self.active.items()):
            if entry.task.done():
                logger.warning("player for room %s exited; releasing", room_id)
                await self._release(room_id)

        self.cache.sweep(keep=self._protected_keys())
        await self._prune_idle_rooms()

        for room in await self._rooms_needing_playback():
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
        """A room needs a worker while it has anything queued or playing, and
        for a grace period after, so a quiet moment does not drop the stream."""
        cutoff = utcnow() - IDLE_GRACE
        async with self.sessionmaker() as db:
            result = await db.execute(
                select(Room)
                .where(
                    or_(
                        Room.id.in_(
                            select(Track.room_id).where(
                                Track.state.in_((STATE_QUEUED, STATE_PLAYING))
                            )
                        ),
                        Room.last_active_at >= cutoff,
                    )
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
