"""Room creation, joining, settings and host privilege."""

from __future__ import annotations

from httpx import AsyncClient

from app import events
from tests.conftest import watch_url
from tests.test_worker_wakeup import wake_messages


async def test_creating_a_room_returns_a_shareable_link_and_a_one_time_secret(client):
    response = await client.post("/api/rooms", json={"name": "Kitchen Radio"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "Kitchen Radio"
    assert payload["url"].endswith(f"/r/{payload['token']}")
    assert len(payload["host_secret"]) > 20


async def test_an_unnamed_room_still_gets_a_name(client):
    response = await client.post("/api/rooms", json={"name": "   "})
    assert response.json()["name"] == "streamchen radio"


async def test_a_new_room_starts_off_the_air(client):
    """Creating a room is not broadcasting one. Nothing goes out until the host
    presses start, which is also what keeps a half-configured room off the
    air while its link is being copied."""
    created = (await client.post("/api/rooms", json={"name": "Kitchen Radio"})).json()
    client.headers["X-Host-Secret"] = created["host_secret"]

    settings = (await client.get(f"/api/rooms/{created['token']}")).json()["settings"]

    assert settings["stream_stopped"] is True


async def test_a_new_room_has_the_news_off(client):
    """A room is music. The hourly bulletin is the one thing on the stream that
    nobody in the room asked for, so it takes a host asking for it."""
    created = (await client.post("/api/rooms", json={"name": "Kitchen Radio"})).json()
    client.headers["X-Host-Secret"] = created["host_secret"]

    settings = (await client.get(f"/api/rooms/{created['token']}")).json()["settings"]

    assert settings["news_enabled"] is False


async def test_room_state_is_everything_the_page_needs(client, room):
    response = await client.get(f"/api/rooms/{room['token']}")

    assert response.status_code == 200
    state = response.json()
    assert state["name"] == "Test Room"
    assert state["stream_url"] == f"http://stream.test/{room['token']}.mp3"
    assert state["queue"] == []
    assert state["now_playing"]["track"] is None
    assert state["is_host"] is True
    assert state["me"]["display_name"]


async def test_api_responses_are_never_cached(client, room):
    response = await client.get(f"/api/rooms/{room['token']}")
    assert response.headers["cache-control"] == "no-store"


async def test_a_stranger_joins_by_opening_the_link(new_client, room):
    guest = await new_client()
    response = await guest.get(f"/api/rooms/{room['token']}")

    assert response.status_code == 200
    assert response.json()["is_host"] is False


async def test_two_visitors_are_two_listeners(new_client, room, client):
    guest = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")

    listeners = await client.get(f"/api/rooms/{room['token']}/listeners")
    assert listeners.status_code == 200
    assert len(listeners.json()) == 2


async def test_returning_with_the_same_session_is_the_same_listener(client, room):
    first = (await client.get(f"/api/rooms/{room['token']}")).json()
    second = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert first["me"]["id"] == second["me"]["id"]


async def test_unknown_and_malformed_rooms_are_both_not_found(client):
    assert (await client.get("/api/rooms/doesnotexistdoesnotexist")).status_code == 404
    assert (await client.get("/api/rooms/../etc")).status_code == 404


async def test_the_host_can_change_the_settings(client, room):
    response = await client.patch(
        f"/api/rooms/{room['token']}",
        json={"name": "Renamed", "voting_enabled": False, "max_pending_per_listener": 5},
    )

    assert response.status_code == 200
    state = response.json()
    assert state["name"] == "Renamed"
    assert state["settings"]["voting_enabled"] is False
    assert state["settings"]["max_pending_per_listener"] == 5


async def test_the_host_can_set_a_mix_as_the_radio_playlist(client, room):
    """A mix pasted while a song is playing carries the seed it needs."""
    link = "https://music.youtube.com/watch?v=vDWWofpuHyc&list=RDEM7AbogW0cCnElSU0WYm1GqA"
    response = await client.patch(f"/api/rooms/{room['token']}", json={"fallback_playlist": link})

    assert response.status_code == 200
    assert response.json()["settings"]["fallback_playlist"] == link


async def test_saving_a_radio_playlist_asks_for_a_worker(client, room, api):
    """A host pasting a playlist into a quiet room means "play something".

    The radio only ever runs inside a worker, so a room with no source has
    nowhere for the list to be played. Waiting for the sweep to notice is the
    same "I pressed the button and nothing happened" the stream toggle avoids.
    """
    pubsub = api.state.redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.WAKE_CHANNEL)
    try:
        response = await client.patch(
            f"/api/rooms/{room['token']}",
            json={"fallback_playlist": "https://www.youtube.com/playlist?list=PLabc"},
        )
        assert response.status_code == 200
        assert await wake_messages(pubsub)
    finally:
        await pubsub.aclose()


async def test_clearing_the_radio_playlist_asks_for_nothing(client, room, api):
    """The opposite of "play something": there is nothing to wake up for."""
    pubsub = api.state.redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.WAKE_CHANNEL)
    try:
        response = await client.patch(f"/api/rooms/{room['token']}", json={"fallback_playlist": ""})
        assert response.status_code == 200
        assert await wake_messages(pubsub, timeout=0.3) == []
    finally:
        await pubsub.aclose()


async def test_a_mix_link_with_no_song_in_it_is_refused_with_a_reason(client, room):
    """Nothing can resolve it, so storing it would only be a silent dead end."""
    response = await client.patch(
        f"/api/rooms/{room['token']}",
        json={"fallback_playlist": "https://music.youtube.com/playlist?list=RDEM7Abog"},
    )

    assert response.status_code == 400
    assert "copy the link" in response.json()["detail"]


async def test_a_listener_cannot_change_the_settings(new_client, room):
    guest = await new_client()
    response = await guest.patch(f"/api/rooms/{room['token']}", json={"name": "Mine now"})
    assert response.status_code == 403


async def test_a_wrong_host_secret_is_not_a_host(new_client, room):
    guest = await new_client()
    guest.headers["X-Host-Secret"] = "wrong-secret-entirely"
    response = await guest.get(f"/api/rooms/{room['token']}")
    assert response.json()["is_host"] is False


async def test_locking_the_queue_stops_listeners_but_not_the_host(new_client, client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"queue_locked": True})

    guest = await new_client()
    blocked = await guest.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})
    assert blocked.status_code == 403

    allowed = await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(2)})
    assert allowed.status_code == 201


async def test_a_full_room_turns_newcomers_away(new_client, client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"max_listeners": 1})

    guest = await new_client()
    assert (await guest.get(f"/api/rooms/{room['token']}")).status_code == 403
    # The host is already inside and stays inside.
    assert (await client.get(f"/api/rooms/{room['token']}")).status_code == 200


async def test_the_host_can_delete_the_room(client: AsyncClient, room):
    assert (await client.delete(f"/api/rooms/{room['token']}")).status_code == 204
    assert (await client.get(f"/api/rooms/{room['token']}")).status_code == 404


async def test_state_changing_requests_need_the_csrf_header(new_client, room):
    guest = await new_client()
    del guest.headers["X-CSRF-Token"]

    response = await guest.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(3)})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
