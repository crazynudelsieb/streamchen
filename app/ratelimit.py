"""Rate limiting and automatic shadow bans (concept §10).

Fixed windows in Redis. Counters are allowed to evaporate — losing them costs
one extra permitted request, which is cheaper than the machinery to make them
durable.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True)
class Limit:
    """``limit`` requests per ``window_s`` seconds."""

    limit: int
    window_s: int


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    remaining: int
    retry_after_s: int


def _key(scope: str, identity: str) -> str:
    return f"streamchen:rl:{scope}:{identity}"


async def check(redis: Redis, scope: str, identity: str, limit: Limit) -> Verdict:
    """Count this request and say whether it may proceed."""
    key = _key(scope, identity)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, limit.window_s)

    if count <= limit.limit:
        return Verdict(True, limit.limit - count, 0)

    ttl = await redis.ttl(key)
    return Verdict(False, 0, max(int(ttl), 1) if ttl and ttl > 0 else limit.window_s)


async def record_strike(redis: Redis, identity: str, window_s: int) -> int:
    """Count a rejected request. Enough of them in a row is not impatience,
    it is flooding — the caller turns that into a shadow ban."""
    key = _key("strike", identity)
    strikes = await redis.incr(key)
    if strikes == 1:
        await redis.expire(key, window_s)
    return strikes


async def clear_strikes(redis: Redis, identity: str) -> None:
    await redis.delete(_key("strike", identity))
