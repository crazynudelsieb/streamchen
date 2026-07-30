"""FastAPI application factory.

One process serves everything: the rendered pages, the JSON API they act
through, the WebSocket, and the static assets. The API still owns all business
logic (concept §5) — the templates are just another client of it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Starlette's, not FastAPI's: FastAPI's subclasses it, so handling the base
# catches both -- including the 404 raised for a path that matches no route.
from starlette.exceptions import HTTPException

from app import __version__, web
from app.api.middleware import SessionMiddleware
from app.api.routers import chat, meta, moderation, queue, rooms, ws
from app.config import Settings, get_settings
from app.database import create_engine, create_schema, create_sessionmaker
from app.redis_client import make_redis

logger = logging.getLogger(__name__)

# What each status means to somebody who followed a link to a room.
_ERROR_PAGES = {
    403: ("bi-door-closed", "You can’t join this room", "Start your own"),
    404: ("bi-signpost-split", "Room not found", "Back to the start"),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        # No migration tool on purpose: the schema is small and additive, and
        # the only durable data is room state the app can afford to lose.
        await create_schema(engine)

        application.state.engine = engine
        application.state.sessionmaker = create_sessionmaker(engine)
        application.state.redis = make_redis(settings)

        logger.info("streamchen %s ready", __version__)
        try:
            yield
        finally:
            await application.state.redis.aclose()
            await engine.dispose()

    application = FastAPI(
        title="streamchen",
        version=__version__,
        description="Collaborative radio — one room, one stream, everybody's queue.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.templates = web.build_templates(settings)

    if settings.cors_origins:
        # Only needed when something else is served from another origin; the
        # bundled deployment is single-origin.
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    application.add_middleware(SessionMiddleware, settings=settings)

    application.include_router(meta.router, prefix="/api")
    application.include_router(rooms.router, prefix="/api")
    application.include_router(queue.router, prefix="/api")
    application.include_router(chat.router, prefix="/api")
    application.include_router(moderation.router, prefix="/api")
    application.include_router(ws.router, prefix="/api")
    application.include_router(web.router)

    application.mount(
        "/static", StaticFiles(directory=str(web.STATIC_DIR)), name="static"
    )

    @application.exception_handler(HTTPException)
    async def _errors(request: Request, exc: HTTPException):
        """JSON for the API, a rendered page for everything else — a broken
        room link should look like a page, not like a stack of braces."""
        if request.url.path.startswith("/api") or exc.status_code not in _ERROR_PAGES:
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )

        icon, heading, action = _ERROR_PAGES[exc.status_code]
        return web.error_page(
            request,
            status_code=exc.status_code,
            heading=heading,
            message=str(exc.detail),
            icon=icon,
            action=action,
        )

    return application


__all__ = ["create_app"]
