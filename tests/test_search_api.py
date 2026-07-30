"""Searching for songs by name, rather than pasting links."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.youtube import TrackMetadata, normalize_query
from tests.conftest import video_id


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        add_rate_limit=100,
        vote_rate_limit=100,
        search_rate_limit=100,
        max_track_duration_s=900,
    )


@pytest.fixture
def fake_search(monkeypatch):
    """Records the queries the endpoint makes and returns canned hits."""
    calls: list[str] = []
    hits = [
        TrackMetadata(video_id(1), "Short song", 180, channel="Test Channel"),
        TrackMetadata(video_id(2), "Epic song", 1800, channel="Test Channel"),
    ]

    async def _search(_redis, query: str, limit: int = 8, **_kwargs):
        calls.append(query)
        return hits

    monkeypatch.setattr("app.api.routers.queue.search_music", _search)
    return calls


async def test_search_returns_playable_hits(client, room, fake_search):
    response = await client.get(f"/api/rooms/{room['token']}/search", params={"q": "short song"})

    assert response.status_code == 200
    results = response.json()
    assert [hit["title"] for hit in results] == ["Short song", "Epic song"]
    assert results[0]["youtube_id"] == video_id(1)
    assert results[0]["channel"] == "Test Channel"


async def test_search_flags_what_cannot_be_added(client, room, fake_search):
    """A listener should not have to submit a track to learn it is too long."""
    results = (
        await client.get(f"/api/rooms/{room['token']}/search", params={"q": "songs"})
    ).json()

    assert results[0]["too_long"] is False
    assert results[1]["too_long"] is True


async def test_search_flags_tracks_already_in_the_queue(client, room, fake_search):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": video_id(1)})

    results = (
        await client.get(f"/api/rooms/{room['token']}/search", params={"q": "songs"})
    ).json()

    assert results[0]["queued"] is True
    assert results[1]["queued"] is False


async def test_a_search_result_can_be_queued_by_id(client, room, fake_search):
    results = (
        await client.get(f"/api/rooms/{room['token']}/search", params={"q": "songs"})
    ).json()

    response = await client.post(
        f"/api/rooms/{room['token']}/tracks", json={"url": results[0]["youtube_id"]}
    )

    assert response.status_code == 201
    assert response.json()["youtube_id"] == video_id(1)


async def test_blank_and_one_character_queries_cost_nothing(client, room, fake_search):
    for query in ("", " ", "a"):
        response = await client.get(
            f"/api/rooms/{room['token']}/search", params={"q": query}
        )
        assert response.status_code == 200
        assert response.json() == []

    assert fake_search == []


async def test_queries_are_normalized_so_equivalent_searches_share_a_cache(client, room, fake_search):
    await client.get(f"/api/rooms/{room['token']}/search", params={"q": "  Daft   PUNK "})

    assert fake_search == ["daft punk"]


def test_normalize_query_collapses_case_and_whitespace():
    assert normalize_query("  One   More  TIME ") == "one more time"
    assert normalize_query(None) == ""


async def test_search_is_rate_limited(api, new_client, fake_search):
    """Every search costs an upstream extraction, so it is capped."""
    api.state.settings = Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        search_rate_limit=2,
        search_rate_window_s=60,
    )
    client = await new_client()
    token = (await client.post("/api/rooms", json={"name": "Test Room"})).json()["token"]

    codes = [
        (await client.get(f"/api/rooms/{token}/search", params={"q": f"song {n}"})).status_code
        for n in range(4)
    ]

    assert codes[:2] == [200, 200]
    assert codes[2:] == [429, 429]


async def test_search_needs_a_room_that_exists(client, fake_search):
    response = await client.get("/api/rooms/nope/search", params={"q": "songs"})

    assert response.status_code == 404
