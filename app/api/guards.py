"""Request guards shared by the routers (concept §10).

Rate limiting lives in ``app.ratelimit``; this is the part that turns a verdict
into an HTTP answer and decides when repeated refusals stop being impatience.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Listener
from app.ratelimit import Limit, check, record_strike


async def spam_guard(
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
    listener: Listener,
    scope: str,
    limit: Limit,
) -> None:
    """Rate limit, and escalate persistent flooding into a shadow ban.

    A listener who keeps hammering after being told no is not impatient, they
    are a bot; the ban is silent so they get no feedback to tune against.
    """
    identity = f"{scope}:{listener.id}"
    verdict = await check(redis, scope, str(listener.id), limit)
    if verdict.allowed:
        return

    strikes = await record_strike(redis, identity, settings.strike_window_s)
    if strikes >= settings.shadow_ban_strikes and not listener.is_shadow_banned():
        listener.shadow_ban(settings.shadow_ban_minutes)
        # Committed here on purpose: the exception below unwinds the request
        # transaction, and the ban is the one thing that has to survive it.
        await db.commit()

    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Slow down a moment.",
        headers={"Retry-After": str(verdict.retry_after_s)},
    )


__all__ = ["spam_guard"]
