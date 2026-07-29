"""Redis connection helper.

One place to build the client so the API, the worker and the tests all get the
same decoding behaviour (``decode_responses=True`` — everything in this app is
JSON or a short string, never binary).
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.config import Settings


def make_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        health_check_interval=30,
    )
