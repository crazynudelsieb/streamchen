"""Worker entry point: ``python -m app.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app import __version__
from app.config import get_settings
from app.database import create_engine, create_sessionmaker
from app.redis_client import make_redis
from app.worker.supervisor import Supervisor

logger = logging.getLogger("app.worker")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("streamchen worker %s starting", __version__)

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = make_redis(settings)

    supervisor = Supervisor(settings, sessionmaker, redis)
    task = asyncio.create_task(supervisor.run())

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, signame), task.cancel)

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
