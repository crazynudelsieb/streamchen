"""Submitting tracks: parsing, limits, duplicates, ordering, removal."""

from __future__ import annotations

from tests.conftest import video_id, watch_url


async def add(client, token: str, seed: int):
    return await client.post(f"/api/rooms/{token}/tracks", json={"url": watch_url(seed)})


async def queue_titles(client, token: str) -> list[str]:
    state = (await client.get(f"/api/rooms/{token}")).json()
    return [track["title"] for track in state["queue"]]


async def test_a_track_is_queued_with_its_metadata(client, room):
    response = await add(client, room["token"], 1)

    assert response.status_code == 201
    track = response.json()
    assert track["youtube_id"] == video_id(1)
    assert track["title"] == f"Track {video_id(1)}"
    assert track["duration_s"] == 180
    assert track["state"] == "queued"
    assert track["mine"] is True


async def test_a_non_youtube_link_is_rejected(client, room):
    response = await client.post(
        f"/api/rooms/{room['token']}/tracks", json={"url": "https://vimeo.com/1"}
    )
    assert response.status_code == 400


async def test_a_playlist_is_sent_to_the_radio_settings_instead(client, room):
    """Refusing is right; refusing without saying where it belongs is not."""
    response = await client.post(
        f"/api/rooms/{room['token']}/tracks",
        json={"url": "https://music.youtube.com/playlist?list=OLAK5uy_abc"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "playlist" in detail
    assert "radio" in detail


async def test_the_same_song_cannot_be_queued_twice(client, room):
    await add(client, room["token"], 1)
    response = await add(client, room["token"], 1)

    assert response.status_code == 409
    assert "already" in response.json()["detail"].lower()


async def test_a_listener_may_only_have_so_many_songs_waiting(new_client, client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"max_pending_per_listener": 2})
    guest = await new_client()

    assert (await add(guest, room["token"], 10)).status_code == 201
    assert (await add(guest, room["token"], 11)).status_code == 201

    blocked = await add(guest, room["token"], 12)
    assert blocked.status_code == 409


async def test_overlong_tracks_are_refused(client, room, monkeypatch):
    from app.youtube import TrackMetadata

    async def _long(_redis, youtube_id, _ttl=0):
        return TrackMetadata(youtube_id=youtube_id, title="Ten hours of rain", duration_s=36000)

    monkeypatch.setattr("app.api.routers.queue.fetch_metadata", _long)

    response = await add(client, room["token"], 1)
    assert response.status_code == 422
    assert "minutes" in response.json()["detail"]


async def test_the_queue_is_served_in_fair_order(new_client, client, room):
    guest = await new_client()

    await add(client, room["token"], 1)  # host, first
    await add(client, room["token"], 2)  # host, second
    await add(guest, room["token"], 3)  # guest, first

    titles = await queue_titles(client, room["token"])
    assert titles == [
        f"Track {video_id(1)}",
        f"Track {video_id(3)}",
        f"Track {video_id(2)}",
    ]


async def test_a_listener_can_withdraw_their_own_track(new_client, room):
    guest = await new_client()
    track = (await add(guest, room["token"], 5)).json()

    response = await guest.delete(f"/api/rooms/{room['token']}/tracks/{track['id']}")
    assert response.status_code == 204
    assert await queue_titles(guest, room["token"]) == []


async def test_a_listener_cannot_remove_someone_elses_track(new_client, client, room):
    track = (await add(client, room["token"], 6)).json()
    guest = await new_client()

    response = await guest.delete(f"/api/rooms/{room['token']}/tracks/{track['id']}")
    assert response.status_code == 403


async def test_the_host_can_remove_anything(new_client, client, room):
    guest = await new_client()
    track = (await add(guest, room["token"], 7)).json()

    response = await client.delete(f"/api/rooms/{room['token']}/tracks/{track['id']}")
    assert response.status_code == 204


async def test_removing_a_track_that_is_not_there(client, room):
    missing = "00000000-0000-0000-0000-000000000000"
    response = await client.delete(f"/api/rooms/{room['token']}/tracks/{missing}")
    assert response.status_code == 404


async def test_tracks_from_one_room_are_invisible_in_another(new_client, client, room):
    other = (await client.post("/api/rooms", json={"name": "Other"})).json()
    await add(client, room["token"], 1)

    assert await queue_titles(client, other["token"]) == []
