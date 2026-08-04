"""Host controls (concept §7) and spam protection (concept §10)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from tests.conftest import watch_url


async def add(client, token: str, seed: int):
    return await client.post(f"/api/rooms/{token}/tracks", json={"url": watch_url(seed)})


async def test_promoting_a_track_puts_it_at_the_front(new_client, client, room):
    guest = await new_client()
    await add(client, room["token"], 1)
    behind = (await add(guest, room["token"], 2)).json()

    response = await client.post(f"/api/rooms/{room['token']}/tracks/{behind['id']}/promote")

    assert response.status_code == 202
    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert state["queue"][0]["id"] == behind["id"]


async def test_only_the_host_can_promote(new_client, client, room):
    track = (await add(client, room["token"], 1)).json()
    guest = await new_client()

    response = await guest.post(f"/api/rooms/{room['token']}/tracks/{track['id']}/promote")
    assert response.status_code == 403


async def test_skipping_with_nothing_playing_is_a_conflict(client, room):
    response = await client.post(f"/api/rooms/{room['token']}/skip")
    assert response.status_code == 409


async def test_banning_a_listener_removes_them_and_their_queue(new_client, client, room):
    guest = await new_client()
    await add(guest, room["token"], 20)

    listeners = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    victim = next(row for row in listeners if not row["is_host"])

    response = await client.post(
        f"/api/rooms/{room['token']}/bans", json={"listener_id": victim["id"], "reason": "spam"}
    )

    assert response.status_code == 201
    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert state["queue"] == []
    assert (await guest.get(f"/api/rooms/{room['token']}")).status_code == 403


async def test_banning_a_listener_who_already_has_history(api, new_client, client, room):
    """Their played tracks go with them — nothing is left pointing at a
    listener row that no longer exists."""
    from app.models import STATE_PLAYED, Track, utcnow
    from app.service import get_room

    guest = await new_client()
    track = (await add(guest, room["token"], 21)).json()

    async with api.state.sessionmaker() as db:
        db_track = await db.get(Track, uuid.UUID(track["id"]))
        db_track.state = STATE_PLAYED
        db_track.ended_at = utcnow()
        await db.commit()

    listeners = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    victim = next(row for row in listeners if not row["is_host"])

    response = await client.post(
        f"/api/rooms/{room['token']}/bans", json={"listener_id": victim["id"]}
    )
    assert response.status_code == 201

    async with api.state.sessionmaker() as db:
        db_room = await get_room(db, room["token"])
        assert db_room is not None
        assert await db.get(Track, uuid.UUID(track["id"])) is None


async def test_the_host_cannot_ban_themselves(client, room):
    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    response = await client.post(
        f"/api/rooms/{room['token']}/bans", json={"listener_id": state["me"]["id"]}
    )
    assert response.status_code == 409


async def test_unbanning_lets_them_back_in(new_client, client, room):
    guest = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")
    session_id = guest.cookies["sc_session"]

    listeners = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    victim = next(row for row in listeners if not row["is_host"])
    await client.post(f"/api/rooms/{room['token']}/bans", json={"listener_id": victim["id"]})

    await client.delete(f"/api/rooms/{room['token']}/bans/{session_id}")

    assert (await guest.get(f"/api/rooms/{room['token']}")).status_code == 200


# --- Rate limiting ---------------------------------------------------------
@pytest.fixture
def settings() -> Settings:
    """Tight limits so the guard trips deterministically."""
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        add_rate_limit=1,
        add_rate_window_s=15,
        vote_rate_limit=2,
        vote_rate_window_s=10,
        shadow_ban_strikes=2,
        shadow_ban_minutes=15,
    )


async def test_adding_too_fast_is_refused_with_a_retry_hint(new_client, room):
    guest = await new_client()

    assert (await add(guest, room["token"], 30)).status_code == 201
    blocked = await add(guest, room["token"], 31)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_the_host_is_not_rate_limited(client, room):
    assert (await add(client, room["token"], 40)).status_code == 201
    assert (await add(client, room["token"], 41)).status_code == 201


async def test_repeated_flooding_earns_a_silent_shadow_ban(new_client, client, room):
    """The flooder keeps seeing their own submissions; nobody else does."""
    guest = await new_client()
    await add(guest, room["token"], 50)

    for seed in range(51, 55):
        await add(guest, room["token"], seed)  # each refused, each a strike

    listeners = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    flooder = next(row for row in listeners if not row["is_host"])
    assert flooder["shadow_banned"] is True


async def test_a_shadow_banned_listener_sees_their_own_tracks_and_nobody_else_does(
    api, new_client, client, room
):
    guest = await new_client()
    await add(guest, room["token"], 60)
    for seed in range(61, 65):
        await add(guest, room["token"], seed)

    # Wait out the window by starting a fresh limiter identity: a new request
    # after the ban lands is what we care about, so clear the counters.
    redis = api.state.redis
    await redis.flushall()

    shadowed = await add(guest, room["token"], 70)
    assert shadowed.status_code == 201
    assert shadowed.json()["shadowed"] is True

    own_view = (await guest.get(f"/api/rooms/{room['token']}")).json()
    assert any(track["youtube_id"].startswith("vid") for track in own_view["queue"])

    everyone_else = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert all(not track["shadowed"] for track in everyone_else["queue"])
    assert shadowed.json()["id"] not in [track["id"] for track in everyone_else["queue"]]


async def test_a_shadowed_track_is_never_played(api, new_client, room):
    """The worker asks for playable tracks; a shadowed one is not among them."""
    from app.service import get_room, playable_tracks

    guest = await new_client()
    await add(guest, room["token"], 80)
    for seed in range(81, 85):
        await add(guest, room["token"], seed)
    await api.state.redis.flushall()
    await add(guest, room["token"], 90)

    async with api.state.sessionmaker() as db:
        db_room = await get_room(db, room["token"])
        playable = await playable_tracks(db, db_room.id)

    assert all(not track.shadow for track in playable)


async def test_clients_do_not_share_a_rate_limit_bucket(api, room):
    """Two listeners are two buckets — one flooder must not mute the room."""
    first = AsyncClient(transport=ASGITransport(app=api), base_url="http://test")
    second = AsyncClient(transport=ASGITransport(app=api), base_url="http://test")

    async with first, second:
        for client in (first, second):
            await client.get("/api/healthz")
            client.headers["X-CSRF-Token"] = client.cookies["sc_csrf"]

        assert (await add(first, room["token"], 100)).status_code == 201
        assert (await add(second, room["token"], 101)).status_code == 201
