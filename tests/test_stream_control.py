"""Stopping and starting a room's stream.

Two rules, and the interesting part is where they meet: a stream follows its
listeners, and a host's stop overrules that. Everything here is about a room
outliving its stream — the queue, the listeners, the name and above all the
link are the room, and none of them depend on anything being on the air.
"""

from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import select

from app import events
from app.config import Settings
from app.database import create_engine, create_schema, create_sessionmaker
from app.models import STATE_QUEUED, Listener, Room, Track, utcnow
from app.security import hash_secret, new_room_token
from app.worker.supervisor import STARTUP_GRACE, Supervisor
from tests.conftest import video_id, watch_url
from tests.test_worker_wakeup import wake_messages


# --- The host's stop, over the API -------------------------------------------
async def test_a_host_can_stop_the_stream(client, room):
    response = await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": True})

    assert response.status_code == 200
    assert response.json()["settings"]["stream_stopped"] is True


async def test_stopping_the_stream_keeps_the_room_and_its_link(client, room):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})
    await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": True})

    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["token"] == room["token"]
    assert state["name"] == room["name"]
    assert len(state["queue"]) == 1


async def test_the_room_page_still_works_with_the_stream_stopped(client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": True})

    response = await client.get(f"/r/{room['token']}")

    assert response.status_code == 200
    assert room["name"] in response.text


async def test_songs_can_still_be_queued_while_the_stream_is_stopped(client, room):
    """The queue is what the room comes back to."""
    await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": True})

    response = await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(2)})

    assert response.status_code == 201


async def test_starting_it_again_asks_for_a_worker_rather_than_waiting(client, room, api):
    await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": True})

    pubsub = api.state.redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.WAKE_CHANNEL)
    try:
        await client.patch(f"/api/rooms/{room['token']}", json={"stream_stopped": False})
        assert await wake_messages(pubsub)
    finally:
        await pubsub.aclose()


async def test_only_the_host_may_stop_the_stream(new_client, room):
    stranger = await new_client()

    response = await stranger.patch(
        f"/api/rooms/{room['token']}", json={"stream_stopped": True}
    )

    assert response.status_code == 403


async def test_renaming_a_room_never_changes_its_link(client, room):
    """The token is the link and is not derived from the name, so this is a
    property of the schema rather than of the handler — worth pinning anyway,
    because it is the reason renaming can be offered at all."""
    response = await client.patch(f"/api/rooms/{room['token']}", json={"name": "Something Else"})

    assert response.status_code == 200
    assert response.json()["name"] == "Something Else"
    assert response.json()["token"] == room["token"]
    assert (await client.get(f"/r/{room['token']}")).status_code == 200


# --- Which rooms the supervisor gives a source to ----------------------------
@pytest.fixture
def worker_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def supervisor(worker_settings):
    engine = create_engine(worker_settings.database_url)
    await create_schema(engine)
    sessionmaker = create_sessionmaker(engine)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    yield Supervisor(worker_settings, sessionmaker, redis, worker_id="test-worker")

    await redis.aclose()
    await engine.dispose()


async def make_room(supervisor: Supervisor, **kwargs) -> Room:
    async with supervisor.sessionmaker() as db:
        room = Room(
            token=new_room_token(),
            name="Test Room",
            host_secret_hash=hash_secret("secret"),
            **kwargs,
        )
        db.add(room)
        await db.commit()
        return room


async def wanted(supervisor: Supervisor) -> set[uuid.UUID]:
    return {room.id for room in await supervisor._rooms_needing_playback()}


async def test_a_room_with_a_listener_gets_a_source(supervisor):
    room = await make_room(supervisor, last_active_at=utcnow() - STARTUP_GRACE * 10)
    await events.mark_present(supervisor.redis, room.id, "session-1")

    assert room.id in await wanted(supervisor)


async def test_a_room_nobody_is_listening_to_does_not(supervisor):
    """The automatic stop: an encoder, a decoder and a download serving nobody
    is pure cost."""
    room = await make_room(supervisor, last_active_at=utcnow() - STARTUP_GRACE * 10)

    assert room.id not in await wanted(supervisor)


async def test_a_queue_is_not_a_reason_to_stream_to_an_empty_room(supervisor):
    room = await make_room(supervisor, last_active_at=utcnow() - STARTUP_GRACE * 10)
    async with supervisor.sessionmaker() as db:
        listener = Listener(room_id=room.id, session_id="gone", display_name="gone")
        db.add(listener)
        await db.flush()
        db.add(
            Track(
                room_id=room.id,
                youtube_id=video_id(1),
                title="Queued",
                added_by_id=listener.id,
                state=STATE_QUEUED,
            )
        )
        # The listener row exists; nobody is present. Queueing a song and
        # closing the tab must not leave a stream running for nobody.
        await db.commit()

    assert room.id not in await wanted(supervisor)


async def test_a_brand_new_room_gets_a_source_before_anybody_is_present(supervisor):
    """Otherwise the host presses play into a mount that does not exist yet."""
    room = await make_room(supervisor)

    assert room.id in await wanted(supervisor)


async def test_a_stopped_room_gets_no_source_however_busy_it_is(supervisor):
    """The manual stop overrules the automatic start."""
    room = await make_room(supervisor, stream_stopped=True)
    await events.mark_present(supervisor.redis, room.id, "session-1")

    assert room.id not in await wanted(supervisor)


async def test_starting_the_stream_again_makes_it_wanted(supervisor):
    room = await make_room(supervisor, stream_stopped=True)
    await events.mark_present(supervisor.redis, room.id, "session-1")

    async with supervisor.sessionmaker() as db:
        stored = (await db.execute(select(Room).where(Room.id == room.id))).scalar_one()
        stored.stream_stopped = False
        await db.commit()

    assert room.id in await wanted(supervisor)


# --- Presence, cheaply -------------------------------------------------------
async def test_presence_marks_the_room_as_live(supervisor):
    room = await make_room(supervisor)

    assert await events.anyone_present(supervisor.redis, room.id) is False
    await events.mark_present(supervisor.redis, room.id, "session-1")
    assert await events.anyone_present(supervisor.redis, room.id) is True


async def test_one_listener_leaving_does_not_empty_the_room(supervisor):
    room = await make_room(supervisor)
    await events.mark_present(supervisor.redis, room.id, "session-1")
    await events.mark_present(supervisor.redis, room.id, "session-2")

    await events.mark_absent(supervisor.redis, room.id, "session-1")

    assert await events.anyone_present(supervisor.redis, room.id) is True
    assert room.id in await wanted(supervisor)


async def test_live_room_ids_lists_rooms_rather_than_sessions(supervisor):
    first = await make_room(supervisor)
    second = await make_room(supervisor)
    await events.mark_present(supervisor.redis, first.id, "session-1")
    await events.mark_present(supervisor.redis, first.id, "session-2")
    await events.mark_present(supervisor.redis, second.id, "session-3")

    assert await events.live_room_ids(supervisor.redis) == {str(first.id), str(second.id)}
