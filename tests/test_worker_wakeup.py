"""Rooms asking for a worker instead of waiting to be noticed.

The supervisor sweeps on a timer, so a room always gets picked up eventually.
What these cover is that it does not have to *wait* for that: the sweep interval
between opening a brand new room and anything being connected to its mount is
the whole of "I pressed play and nothing happened".
"""

from __future__ import annotations

import asyncio
import time

from app import events
from tests.conftest import watch_url


async def wake_messages(pubsub, timeout: float = 1.0) -> list:
    """Everything published on the wake channel within ``timeout``."""
    received = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if message and message.get("type") == "message":
            received.append(message)
        await asyncio.sleep(0)
    return received


async def listening(api):
    pubsub = api.state.redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.WAKE_CHANNEL)
    return pubsub


async def test_creating_a_room_does_not_ask_for_a_worker(client, api):
    """A new room is created off the air, so there is nothing to connect yet.

    Waking a worker for it would be a nudge towards a room the sweep skips on
    purpose. The nudge that matters is the one on the host pressing start, and
    that is covered in test_stream_control.
    """
    pubsub = await listening(api)
    try:
        response = await client.post("/api/rooms", json={"name": "Kitchen Radio"})
        assert response.status_code == 201
        assert await wake_messages(pubsub, timeout=0.3) == []
    finally:
        await pubsub.aclose()


async def test_opening_the_room_page_asks_for_a_worker(client, room, api):
    pubsub = await listening(api)
    try:
        response = await client.get(f"/r/{room['token']}")
        assert response.status_code == 200
        assert await wake_messages(pubsub)
    finally:
        await pubsub.aclose()


async def test_a_second_page_view_does_not_ask_again(client, room, api):
    """Presence has a TTL, and that is what keeps a refresh from being a nudge:
    a room already being played has nothing to wake up for."""
    await client.get(f"/r/{room['token']}")

    pubsub = await listening(api)
    try:
        await client.get(f"/r/{room['token']}")
        assert await wake_messages(pubsub, timeout=0.3) == []
    finally:
        await pubsub.aclose()


async def test_adding_the_first_song_asks_for_a_worker(client, room, api):
    pubsub = await listening(api)
    try:
        response = await client.post(
            f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)}
        )
        assert response.status_code == 201
        assert await wake_messages(pubsub)
    finally:
        await pubsub.aclose()


async def test_the_wake_channel_is_not_a_room_channel(room, api):
    """Workers subscribe to one channel for every room, so it must not be
    confused with the per-room event bus."""
    assert events.WAKE_CHANNEL != events.channel(room["token"])
    assert room["token"] not in events.WAKE_CHANNEL
