"""The realtime gateway (concept §13)."""

from __future__ import annotations

import asyncio
import json

from app import events


async def drain(pubsub, expected: int = 1, timeout: float = 2.0) -> list[dict]:
    """Collect up to ``expected`` published events, or give up."""
    received: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout

    while len(received) < expected and asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message and message.get("type") == "message":
            received.append(json.loads(message["data"]))
        else:
            await asyncio.sleep(0.01)

    return received


async def test_events_carry_a_type_and_a_payload(api):
    redis = api.state.redis
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.channel("room-1"))

    await events.publish(redis, "room-1", events.SONG_ADDED, {"title": "Something"})

    payload = (await drain(pubsub))[0]

    assert payload["type"] == "SONG_ADDED"
    assert payload["data"]["title"] == "Something"
    await pubsub.aclose()


async def test_adding_a_track_notifies_the_room(api, client, room):
    from tests.conftest import watch_url

    redis = api.state.redis
    pubsub = redis.pubsub(ignore_subscribe_messages=True)

    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    async with api.state.sessionmaker() as db:
        from app.service import get_room

        room_id = (await get_room(db, state["token"])).id

    await pubsub.subscribe(events.channel(room_id))
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})

    seen = {payload["type"] for payload in await drain(pubsub, expected=2)}

    assert events.SONG_ADDED in seen
    assert events.QUEUE_CHANGED in seen
    await pubsub.aclose()


async def test_presence_counts_a_listener_once(api):
    redis = api.state.redis

    assert await events.mark_present(redis, "room-2", "session-a") is True
    assert await events.mark_present(redis, "room-2", "session-a") is False
    await events.mark_present(redis, "room-2", "session-b")

    assert await events.count_present(redis, "room-2") == 2

    await events.mark_absent(redis, "room-2", "session-a")
    assert await events.count_present(redis, "room-2") == 1


async def test_now_playing_survives_a_reader_that_arrives_late(api):
    redis = api.state.redis
    await events.set_now_playing(redis, "room-3", {"track_id": "t1", "started_at": 1000.0})

    assert (await events.get_now_playing(redis, "room-3"))["track_id"] == "t1"

    await events.set_now_playing(redis, "room-3", None)
    assert await events.get_now_playing(redis, "room-3") is None
