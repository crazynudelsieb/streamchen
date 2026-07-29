"""Voting (concept §9): one vote per listener, changes overwrite."""

from __future__ import annotations

from tests.conftest import video_id, watch_url


async def add(client, token: str, seed: int):
    return await client.post(f"/api/rooms/{token}/tracks", json={"url": watch_url(seed)})


async def vote(client, token: str, track_id: str, value: int):
    return await client.post(
        f"/api/rooms/{token}/tracks/{track_id}/vote", json={"value": value}
    )


async def test_an_upvote_raises_the_score(client, room):
    track = (await add(client, room["token"], 1)).json()

    response = await vote(client, room["token"], track["id"], 1)

    assert response.status_code == 200
    assert response.json()["score"] == 1
    assert response.json()["my_vote"] == 1


async def test_a_downvote_lowers_it(client, room):
    track = (await add(client, room["token"], 1)).json()
    response = await vote(client, room["token"], track["id"], -1)
    assert response.json()["score"] == -1


async def test_voting_twice_does_not_count_twice(client, room):
    track = (await add(client, room["token"], 1)).json()

    await vote(client, room["token"], track["id"], 1)
    response = await vote(client, room["token"], track["id"], 1)

    assert response.json()["score"] == 1
    assert response.json()["upvotes"] == 1


async def test_changing_your_mind_overwrites_the_previous_vote(client, room):
    track = (await add(client, room["token"], 1)).json()

    await vote(client, room["token"], track["id"], 1)
    response = await vote(client, room["token"], track["id"], -1)

    assert response.json()["score"] == -1
    assert response.json()["upvotes"] == 0
    assert response.json()["downvotes"] == 1


async def test_a_zero_vote_withdraws_it(client, room):
    track = (await add(client, room["token"], 1)).json()

    await vote(client, room["token"], track["id"], 1)
    response = await vote(client, room["token"], track["id"], 0)

    assert response.json()["score"] == 0
    assert response.json()["my_vote"] == 0


async def test_votes_from_different_listeners_add_up(new_client, client, room):
    track = (await add(client, room["token"], 1)).json()
    guest = await new_client()

    await vote(client, room["token"], track["id"], 1)
    response = await vote(guest, room["token"], track["id"], 1)

    assert response.json()["score"] == 2


async def test_score_reorders_the_queue_within_a_round(new_client, client, room):
    guest = await new_client()
    first = (await add(client, room["token"], 1)).json()
    second = (await add(guest, room["token"], 2)).json()

    await vote(client, room["token"], second["id"], 1)

    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert [track["id"] for track in state["queue"]] == [second["id"], first["id"]]
    assert first["youtube_id"] == video_id(1)


async def test_voting_can_be_switched_off_by_the_host(client, room):
    track = (await add(client, room["token"], 1)).json()
    await client.patch(f"/api/rooms/{room['token']}", json={"voting_enabled": False})

    response = await vote(client, room["token"], track["id"], 1)
    assert response.status_code == 403


async def test_out_of_range_votes_are_rejected(client, room):
    track = (await add(client, room["token"], 1)).json()
    assert (await vote(client, room["token"], track["id"], 5)).status_code == 422
