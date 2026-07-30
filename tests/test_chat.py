"""Room chat.

Chat is the one thing that travels over the socket whole rather than as a hint
to refetch, and the one thing kept only in Redis. Both of those are choices
about cost, so most of what is pinned here is what chat is *not*: not in the
database, not in the room snapshot, not unbounded, and not louder than the
person typing it is allowed to be.
"""

from __future__ import annotations

import asyncio

from app import chat, events
from app.config import Settings


async def send(client, token: str, text: str):
    return await client.post(f"/api/rooms/{token}/chat", json={"text": text})


async def history(client, token: str) -> list[dict]:
    return (await client.get(f"/api/rooms/{token}/chat")).json()


# --- Saying something -------------------------------------------------------
async def test_a_message_comes_back_and_stays(client, room):
    response = await send(client, room["token"], "hello room")

    assert response.status_code == 201
    assert response.json()["text"] == "hello room"
    assert [m["text"] for m in await history(client, room["token"])] == ["hello room"]


async def test_everybody_in_the_room_sees_it(client, new_client, room):
    other = await new_client()
    await other.get(f"/api/rooms/{room['token']}")

    await send(client, room["token"], "anybody there")

    assert [m["text"] for m in await history(other, room["token"])] == ["anybody there"]


async def test_history_is_oldest_first(client, room):
    for text in ("one", "two", "three"):
        await send(client, room["token"], text)

    assert [m["text"] for m in await history(client, room["token"])] == ["one", "two", "three"]


async def test_a_message_carries_a_name_and_an_avatar(client, room):
    message = (await send(client, room["token"], "hi")).json()

    assert message["name"]
    assert message["avatar"]
    assert message["id"]
    assert message["at"]


async def test_an_empty_message_is_not_a_message(client, room):
    for text in (" ", "\n\n", "​"):
        assert (await send(client, room["token"], text)).status_code == 400


async def test_a_message_is_folded_onto_one_line(client, room):
    """It is rendered in a fixed row; a wall of newlines is a way to take the
    room's chat over."""
    message = (await send(client, room["token"], "top\n\n\n\n\nbottom")).json()

    assert message["text"] == "top bottom"


async def test_a_very_long_message_is_refused_rather_than_silently_cut(client, room):
    assert (await send(client, room["token"], "x" * 5000)).status_code == 422


def test_cleaning_caps_what_gets_through():
    assert len(chat.clean_message("x" * chat.MESSAGE_MAX_LENGTH * 2)) == chat.MESSAGE_MAX_LENGTH


# --- What it costs ----------------------------------------------------------
async def _room_id(api, token: str):
    from sqlalchemy import select

    from app.models import Room

    async with api.state.sessionmaker() as db:
        return (await db.execute(select(Room.id).where(Room.token == token))).scalar_one()


async def test_a_room_only_remembers_the_last_of_it(api, room, client):
    """Capped, so a long night in a busy room is still a few kilobytes."""
    for index in range(chat.HISTORY_LIMIT + 15):
        await send(client, room["token"], f"message {index}")

    room_id = await _room_id(api, room["token"])
    kept = await history(client, room["token"])

    assert await api.state.redis.llen(chat.key(room_id)) == chat.HISTORY_LIMIT
    assert len(kept) == chat.HISTORY_LIMIT
    assert kept[-1]["text"] == f"message {chat.HISTORY_LIMIT + 14}"


async def test_chat_is_not_in_the_room_snapshot(client, room):
    """The snapshot is refetched on every playback event; chat is not part of
    that and must not make it heavier."""
    await send(client, room["token"], "hello")

    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert "chat" not in state
    assert "hello" not in str(state)


async def test_chat_history_dies_with_redis_and_that_is_fine(api, client, room):
    await send(client, room["token"], "ephemeral")
    await api.state.redis.flushall()

    assert await history(client, room["token"]) == []
    assert (await client.get(f"/r/{room['token']}")).status_code == 200


# --- Live delivery ----------------------------------------------------------
async def test_a_message_is_published_whole_rather_than_as_a_hint(api, client, room):
    """The exception to "the socket carries notifications": refetching the room
    because somebody typed "lol" would cost a database round trip per word."""
    pubsub = api.state.redis.pubsub(ignore_subscribe_messages=True)
    room_id = await _room_id(api, room["token"])
    await pubsub.subscribe(events.channel(room_id))
    try:
        await send(client, room["token"], "carried whole")

        received = None
        for _ in range(40):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
            if message and message.get("type") == "message":
                received = message
                break
            await asyncio.sleep(0)

        assert received is not None
        assert "carried whole" in str(received["data"])
        assert events.CHAT_MESSAGE in str(received["data"])
    finally:
        await pubsub.aclose()


# --- Who may talk -----------------------------------------------------------
async def test_a_host_can_turn_chat_off(client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"chat_enabled": False})

    assert (await send(client, room["token"], "hello")).status_code == 403
    assert await history(client, room["token"]) == []


async def test_chat_is_on_by_default(client, room):
    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["settings"]["chat_enabled"] is True


async def test_flooding_is_rate_limited(api, new_client):
    api.state.settings = Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
        chat_rate_limit=3,
        chat_rate_window_s=60,
    )
    client = await new_client()
    token = (await client.post("/api/rooms", json={"name": "Test Room"})).json()["token"]

    codes = [(await send(client, token, f"spam {n}")).status_code for n in range(5)]

    assert codes[:3] == [201, 201, 201]
    assert codes[3:] == [429, 429]


async def test_a_shadow_banned_listener_talks_to_nobody(api, new_client, client, room):
    """Their message lands for them and reaches no one else — the same silence
    their queued songs get."""
    talker = await new_client()
    await talker.get(f"/api/rooms/{room['token']}")

    from sqlalchemy import select

    from app.models import Listener

    async with api.state.sessionmaker() as db:
        listener = (
            await db.execute(
                select(Listener).where(Listener.session_id == talker.cookies["sc_session"])
            )
        ).scalar_one()
        listener.shadow_ban(15)
        await db.commit()

    response = await send(talker, room["token"], "does anyone hear me")

    assert response.status_code == 201
    assert await history(client, room["token"]) == []


async def test_chat_needs_a_room_that_exists(client):
    assert (await client.get("/api/rooms/nope/chat")).status_code == 404
    assert (await send(client, "nope", "hello")).status_code == 404
