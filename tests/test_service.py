"""Service-layer behaviour that is easier to state directly than over HTTP."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.models import Room, utcnow
from app.security import hash_secret
from app.service import generate_display_name, get_room, prune_idle_rooms
from tests.conftest import watch_url


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        room_idle_days=7,
        add_rate_limit=100,
        vote_rate_limit=100,
    )


def test_generated_names_are_readable_and_varied():
    names = {generate_display_name() for _ in range(200)}

    assert len(names) > 50
    for name in names:
        assert "-" in name
        assert name.islower()
        assert len(name) <= 40


async def test_idle_rooms_are_pruned_and_active_ones_are_not(api, settings, client):
    fresh = (await client.post("/api/rooms", json={"name": "Still used"})).json()

    async with api.state.sessionmaker() as db:
        stale = Room(
            token="stale-room-token-aaaaaaaaaaaa",
            name="Abandoned",
            host_secret_hash=hash_secret("whatever"),
            last_active_at=utcnow() - timedelta(days=30),
        )
        db.add(stale)
        await db.commit()

        removed = await prune_idle_rooms(db, settings)
        await db.commit()

        assert removed == 1
        assert await get_room(db, "stale-room-token-aaaaaaaaaaaa") is None
        assert await get_room(db, fresh["token"]) is not None


async def test_queueing_a_song_keeps_the_room_alive(api, client, room):
    """`last_active_at` is what the pruner reads, so it has to move."""
    async with api.state.sessionmaker() as db:
        before = (await get_room(db, room["token"])).last_active_at

    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})

    async with api.state.sessionmaker() as db:
        after = (await get_room(db, room["token"])).last_active_at

    assert after >= before
