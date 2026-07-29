"""Test fixtures.

The suite runs with no services: SQLite stands in for Postgres and fakeredis
for the bus, so ``pytest`` works on a laptop and in CI without docker-compose.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import create_app
from app.config import Settings
from app.youtube import TrackMetadata


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        icecast_public_url="http://stream.test",
        # Tests that care about limits set their own; the defaults here are
        # loose so unrelated tests are not tripped by the rate limiter.
        add_rate_limit=100,
        vote_rate_limit=100,
    )


@pytest.fixture(autouse=True)
def fake_youtube(monkeypatch) -> None:
    """No network in tests: every id resolves to a predictable track."""

    async def _fetch(_redis, youtube_id: str, _ttl: int = 0) -> TrackMetadata:
        return TrackMetadata(
            youtube_id=youtube_id,
            title=f"Track {youtube_id}",
            duration_s=180,
            thumbnail_url=f"https://i.ytimg.com/vi/{youtube_id}/hq.jpg",
            channel="Test Channel",
        )

    monkeypatch.setattr("app.api.routers.queue.fetch_metadata", _fetch)


@pytest_asyncio.fixture
async def api(settings: Settings, monkeypatch) -> AsyncIterator:
    monkeypatch.setattr(
        "app.api.make_redis",
        lambda _settings: fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def new_client(api) -> AsyncIterator[Callable]:
    """Factory for independent browsers — each gets its own cookie jar, which
    is what makes it a different anonymous listener."""
    opened: list[AsyncClient] = []

    async def factory() -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=api), base_url="http://test")
        await client.__aenter__()
        opened.append(client)
        # Bootstraps the session and CSRF cookies, then arms the header half
        # of the double-submit check for every later request.
        await client.get("/api/meta")
        client.headers["X-CSRF-Token"] = client.cookies["sc_csrf"]
        return client

    yield factory

    for client in opened:
        await client.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(new_client) -> AsyncClient:
    return await new_client()


@pytest_asyncio.fixture
async def room(client: AsyncClient) -> dict:
    """A room plus the host credentials, ready to use."""
    response = await client.post("/api/rooms", json={"name": "Test Room"})
    assert response.status_code == 201, response.text
    payload = response.json()
    client.headers["X-Host-Secret"] = payload["host_secret"]
    return payload


def video_id(seed: int) -> str:
    """A syntactically valid 11-character YouTube id."""
    return f"vid{seed:08d}"[:11]


def watch_url(seed: int) -> str:
    return f"https://www.youtube.com/watch?v={video_id(seed)}"


def unique_session() -> str:
    return str(uuid.uuid4())
